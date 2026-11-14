"""Authenticated WebUI lifecycle controls never impersonate a native message."""

import asyncio
import copy
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import httpx
import pytest

from astrbot.builtin_stars.wangshangliao_moderation import activities as activity_module
from astrbot.builtin_stars.wangshangliao_moderation.activities import GroupActivities
from astrbot.builtin_stars.wangshangliao_moderation.activity_store import database
from astrbot.builtin_stars.wangshangliao_moderation.lottery_rules import (
    validate_entry_phrase,
)
from astrbot.builtin_stars.wangshangliao_moderation.syntax import recognize_command
from astrbot.dashboard.responses import ApiError
from tests.test_wangshangliao_activities import event, member
from tests.test_wangshangliao_dashboard import PREFIX
from tests.test_wangshangliao_dashboard import headers as headers
from tests.test_wangshangliao_dashboard import page_app as page_app


@pytest.fixture
def controlled_page(page_app, monkeypatch):
    app, page, _, configs, root = page_app
    monkeypatch.setattr(activity_module, "instance_dir", lambda _: root)
    adapter = page.context.platform_manager.platform_insts[0]
    configs["fixture"]["proactive_send"] = {"enabled": True, "targets": ["5"]}
    adapter.config = copy.deepcopy(configs["fixture"])
    adapter.config.update(
        _open_integrated=True,
        _open_groups={"5": {}},
        _open_managers={"5": ["9"]},
        _open_native_roles={"5": "GROUP_ROLE_ADMIN"},
    )
    adapter.stopping = asyncio.Event()
    adapter.connection_state = "online"
    adapter.groups = {"5": "905", "6": "906"}
    adapter.send_reply_text = AsyncMock(return_value="accepted")
    roster = {str(uid): member(uid) for uid in (1, 2, 3, 4, 9)}
    roster["1"]["groupRole"] = "GROUP_ROLE_ADMIN"
    roster["9"]["groupRole"] = "GROUP_ROLE_OWNER"
    adapter.get_moderation_members = AsyncMock(
        side_effect=lambda _: {
            "complete": True,
            "groupMemberInfo": list(roster.values()),
        }
    )
    activities = GroupActivities(SimpleNamespace())
    tenant = {
        "account": "1",
        "group_id": "5",
        "active": True,
        "paused": False,
        "managers": ["9"],
    }
    authority = MagicMock()
    integrated = SimpleNamespace(
        active=True,
        integrated=True,
        tenants={("1", "5"): tenant},
        member_evidence={("1", "5"): roster},
        roster=AsyncMock(return_value=roster),
        rpc=SimpleNamespace(control_guard=MagicMock(return_value=authority)),
    )
    page.plugin = SimpleNamespace(open_service=integrated, activities=activities)
    with database(adapter) as db:
        db.execute(
            "INSERT INTO lottery_settings(account,group_id,prize,contact,winners,"
            "duration,capacity,total_amount,packet_mode,entry_phrase,integer_packets,"
            "draw_on_full,luckiest_bonus) VALUES('1','5','Integer test','Admin',3,"
            "600,3,111100,'lucky','参与抽奖',1,1,18800)"
        )
    return app, page, adapter, roster, authority


async def control_body(client, *, action="start", operation="webui-operation-0001"):
    state = (
        await client.get(
            PREFIX + "/activities", params={"bot_id": "fixture", "group_id": "5"}
        )
    ).json()["data"]
    return {
        "bot_id": "fixture",
        "group_id": "5",
        "revision": state["revision"],
        "operation_id": operation,
        "action": action,
        "lottery_id": state["current_lottery"]["id"] if action != "start" else "",
    }


def test_short_public_entry_phrase_is_exact_and_valid():
    assert validate_entry_phrase("参与抽奖") == "参与抽奖"
    assert recognize_command("参与抽奖") == "参加抽奖"
    assert not recognize_command("请参与抽奖")
    assert not recognize_command("参与抽奖 2")


@pytest.mark.asyncio
async def test_dashboard_opens_once_and_members_enter_without_any_admin_message(
    controlled_page, headers
):
    app, page, adapter, _, authority = controlled_page
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fixture",
        headers=headers,
    ) as client:
        body = await control_body(client)
        replies = await asyncio.gather(
            client.post(PREFIX + "/lottery-control", json=body),
            client.post(PREFIX + "/lottery-control", json=body),
        )
        assert all(reply.status_code == 200 for reply in replies)
        state = replies[0].json()["data"]["state"]
        assert state["current_lottery"]["status"] == "open"
        with database(adapter) as db:
            rows = db.execute("SELECT * FROM lotteries").fetchall()
            assert len(rows) == 1
            row = dict(rows[0])
        assert row["control_actor"] == row["session"] == "dashboard/fixture-admin"
        assert row["owner"] == "9" and row["draw_on_full"] == 1
        assert sum(json.loads(row["packet_amounts"])) == 111100
        adapter.send_reply_text.assert_awaited_once()
        authority.assert_called()
        caller = event(adapter, "2", private=False)
        caller.message_str = "参与抽奖"
        assert await page.plugin.activities.entry_command(caller) == "参加抽奖"
        assert "参加抽奖成功" in await page.plugin.activities.command(
            caller, "5", "参加抽奖", ""
        )
        assert "已参加" in await page.plugin.activities.command(
            caller, "5", "参加抽奖", ""
        )
        cancelled = await client.post(
            PREFIX + "/lottery-control",
            json=await control_body(client, action="cancel"),
        )
        assert cancelled.status_code == 200, cancelled.text
        assert (
            cancelled.json()["data"]["state"]["current_lottery"]["status"]
            == "cancelled"
        )
        assert adapter.send_reply_text.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["auth", "stale", "target", "manager", "revoked"])
async def test_control_refuses_invalid_authority_or_scope(
    controlled_page, headers, failure
):
    app, page, adapter, roster, authority = controlled_page
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fixture",
        headers=headers,
    ) as client:
        body = await control_body(client)
        request_headers = {}
        if failure == "auth":
            request_headers = {"Authorization": ""}
        elif failure == "stale":
            body["revision"] = "old"
        elif failure == "target":
            body["group_id"] = "unknown"
        elif failure == "manager":
            roster["9"]["groupRole"] = "GROUP_ROLE_MEMBER"
        else:
            authority.side_effect = ApiError("Permission revoked", status_code=409)
        reply = await client.post(
            PREFIX + "/lottery-control", headers=request_headers, json=body
        )
        assert reply.status_code in {401, 409}, reply.text
    with database(adapter) as db:
        assert db.execute("SELECT COUNT(*) FROM lotteries").fetchone()[0] == 0
    adapter.send_reply_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", ["notice_unknown", "result_unknown"])
async def test_unknown_round_cannot_be_cancelled_or_bypassed_with_a_new_request(
    controlled_page, headers, unknown
):
    app, _, adapter, _, _ = controlled_page
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fixture",
        headers=headers,
    ) as client:
        body = await control_body(client)
        assert (
            await client.post(PREFIX + "/lottery-control", json=body)
        ).status_code == 200
        with database(adapter) as db:
            db.execute("UPDATE lotteries SET status=?", (unknown,))
        reply = await client.post(
            PREFIX + "/lottery-control",
            json=await control_body(client, operation="different-request-0002"),
        )
        assert reply.status_code == 200
        assert "已有进行中" in reply.json()["data"]["result"]
        reply = await client.post(
            PREFIX + "/lottery-control",
            json=await control_body(client, action="cancel"),
        )
        assert reply.status_code == 409
        with database(adapter) as db:
            assert db.execute("SELECT COUNT(*) FROM lotteries").fetchone()[0] == 1
        adapter.send_reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_midnight_deadline_preserves_entries_packets_and_waits_when_full(
    controlled_page, headers, monkeypatch
):
    app, page, adapter, _, _ = controlled_page
    now = [datetime.now(ZoneInfo("Asia/Shanghai")).timestamp()]
    midnight = datetime.fromtimestamp(now[0], ZoneInfo("Asia/Shanghai")).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) + timedelta(days=1)
    monkeypatch.setattr(activity_module.time, "time", lambda: now[0])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fixture",
        headers=headers,
    ) as client:
        reply = await client.post(
            PREFIX + "/lottery-control", json=await control_body(client)
        )
        assert reply.status_code == 200
        for uid in ("2", "3", "4"):
            caller = event(adapter, uid, private=False)
            caller.message_str = "参与抽奖"
            assert "成功" in await page.plugin.activities.command(
                caller, "5", "参加抽奖", ""
            )
        with database(adapter) as db:
            before = dict(db.execute("SELECT * FROM lotteries").fetchone())
            entries = db.execute(
                "SELECT * FROM lottery_entries ORDER BY member"
            ).fetchall()
        body = {
            **await control_body(client, action="reschedule"),
            "draw_at": midnight.strftime("%Y-%m-%dT%H:%M"),
        }
        replies = await asyncio.gather(
            client.post(PREFIX + "/lottery-control", json=body),
            client.post(PREFIX + "/lottery-control", json=body),
        )
        assert all(reply.status_code == 200 for reply in replies)
        assert adapter.send_reply_text.await_count == 2
        with database(adapter) as db:
            after = dict(db.execute("SELECT * FROM lotteries").fetchone())
            assert {
                key: value
                for key, value in before.items()
                if key not in {"ends", "draw_on_full"}
            } == {
                key: value
                for key, value in after.items()
                if key not in {"ends", "draw_on_full"}
            }
            assert (
                db.execute("SELECT * FROM lottery_entries ORDER BY member").fetchall()
                == entries
            )
            assert after["ends"] == midnight.timestamp() and not after["draw_on_full"]
            assert (
                db.execute("SELECT COUNT(*) FROM lottery_controls").fetchone()[0] == 1
            )
        page.plugin.activities = GroupActivities(SimpleNamespace())
        await page.plugin.activities.tick(adapter)
        with database(adapter) as db:
            assert db.execute("SELECT status FROM lotteries").fetchone()[0] == "open"
        now[0] = midnight.timestamp()
        await page.plugin.activities.tick(adapter)
        with database(adapter) as db:
            result = dict(db.execute("SELECT * FROM lotteries").fetchone())
        assert result["status"] == "finished"
        winners = json.loads(result["results"])
        assert len(winners) == 3
        assert sum(item["amount"] for item in winners) == 111100
        assert sum(item.get("bonus_amount", 0) for item in winners) == 18800
        await page.plugin.activities.tick(adapter)
        assert adapter.send_reply_text.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["past", "blank", "malformed", "unknown", "finished", "wrong_round", "revoked"],
)
async def test_deadline_change_rejects_invalid_or_unsafe_targets(
    controlled_page, headers, failure
):
    app, _, adapter, _, authority = controlled_page
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fixture",
        headers=headers,
    ) as client:
        assert (
            await client.post(
                PREFIX + "/lottery-control", json=await control_body(client)
            )
        ).status_code == 200
        body = {
            **await control_body(client, action="reschedule"),
            "draw_at": (
                datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(days=1)
            ).strftime("%Y-%m-%dT%H:%M"),
        }
        if failure == "past":
            body["draw_at"] = "2020-01-01T00:00"
        elif failure == "blank":
            body["draw_at"] = ""
        elif failure == "malformed":
            body["draw_at"] = "00:00"
        elif failure in {"unknown", "finished"}:
            with database(adapter) as db:
                db.execute(
                    "UPDATE lotteries SET status=?",
                    ("notice_unknown" if failure == "unknown" else "finished",),
                )
        elif failure == "wrong_round":
            body["lottery_id"] = "another-round"
        else:
            authority.side_effect = ApiError("Permission revoked", status_code=409)
        reply = await client.post(PREFIX + "/lottery-control", json=body)
        assert reply.status_code in {400, 409}, reply.text
        with database(adapter) as db:
            assert (
                db.execute("SELECT COUNT(*) FROM lottery_controls").fetchone()[0] == 0
            )
            assert db.execute("SELECT draw_on_full FROM lotteries").fetchone()[0] == 1
        adapter.send_reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_unknown_deadline_notice_is_not_replayed_or_used_to_reroll(
    controlled_page, headers
):
    app, _, adapter, _, _ = controlled_page
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fixture",
        headers=headers,
    ) as client:
        assert (
            await client.post(
                PREFIX + "/lottery-control", json=await control_body(client)
            )
        ).status_code == 200
        body = {
            **await control_body(client, action="reschedule"),
            "draw_at": (
                datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(days=1)
            ).strftime("%Y-%m-%dT%H:%M"),
        }
        adapter.send_reply_text.side_effect = TimeoutError
        first = await client.post(PREFIX + "/lottery-control", json=body)
        assert first.status_code == 200
        assert "未确认" in first.json()["data"]["result"]
        assert (
            await client.post(PREFIX + "/lottery-control", json=body)
        ).status_code == 200
        body["draw_at"] = (
            datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(days=2)
        ).strftime("%Y-%m-%dT%H:%M")
        assert (
            await client.post(PREFIX + "/lottery-control", json=body)
        ).status_code == 409
        assert adapter.send_reply_text.await_count == 2
        with database(adapter) as db:
            assert db.execute("SELECT COUNT(*) FROM lotteries").fetchone()[0] == 1
            assert (
                db.execute("SELECT status FROM lottery_controls").fetchone()[0]
                == "notice_unknown"
            )


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("WSL_ACTIVITY_PAGE"), reason="Opt-in compiled UI"
)
async def test_compiled_page_controls_immediate_and_scheduled_draws(
    controlled_page, headers, tmp_path
):
    from playwright.async_api import async_playwright, expect

    app, _, adapter, _, _ = controlled_page
    output = Path(os.environ.get("WSL_LOTTERY_OUTPUT", str(tmp_path)))
    output.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fixture",
        headers=headers,
    ) as client:
        lose_first_response = [True]

        async def api(_source, method, endpoint, body=None):
            response = await client.request(
                method,
                PREFIX + "/" + endpoint,
                **({"params": body or {}} if method == "GET" else {"json": body}),
            )
            assert response.status_code == 200, response.text
            if (
                endpoint == "lottery-control"
                and body["action"] == "start"
                and lose_first_response[0]
            ):
                lose_first_response[0] = False
                raise RuntimeError("Fixture response lost after accepted operation")
            return response.json()["data"]

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(args=["--no-sandbox"])
            try:
                for width in (320, 390, 1440):
                    browser_page = await browser.new_page(
                        viewport={"width": width, "height": 1000},
                        timezone_id="America/Los_Angeles",
                    )
                    errors = []
                    browser_page.on("pageerror", lambda exc: errors.append(str(exc)))
                    browser_page.on("dialog", lambda dialog: dialog.accept())
                    await browser_page.expose_binding("fixtureAPI", api)
                    await browser_page.add_init_script("""
                        Object.defineProperty(window, "AstrBotPluginPage", {value:{
                            ready:async()=>({}),getLocale:()=> "zh-CN",onContext:()=>()=>{},
                            apiGet:(endpoint,body)=>window.fixtureAPI("GET",endpoint,body),
                            apiPost:(endpoint,body)=>window.fixtureAPI("POST",endpoint,body),
                        }});
                    """)
                    await browser_page.goto(
                        Path(os.environ["WSL_ACTIVITY_PAGE"]).resolve().as_uri()
                    )
                    await browser_page.get_by_role(
                        "tab", name="活动与财务", exact=True
                    ).click()
                    await browser_page.get_by_role(
                        "tab", name="玩家福利抽奖", exact=True
                    ).click()
                    section = browser_page.locator("#panel-activities")
                    await section.get_by_label("活动名称", exact=True).fill(
                        f"中性网页控制测试{width}"
                    )
                    await section.get_by_label("红包类型", exact=True).select_option(
                        "lucky"
                    )
                    await section.get_by_label("总金额（元）", exact=True).fill("1111")
                    await section.get_by_label("参加人数", exact=True).fill("11")
                    await section.get_by_label(
                        "仅整数元（每份至少1元）", exact=True
                    ).check()
                    await section.get_by_label("开奖方式", exact=True).select_option(
                        label="报满人数"
                    )
                    await section.get_by_label(
                        "人气王额外奖励（元，0为不加奖）", exact=True
                    ).fill("188")
                    await section.get_by_label("报名口令（整句）", exact=True).fill(
                        "参与抽奖"
                    )
                    start_time = section.get_by_label(
                        "开始时间（北京时间，留空立即发送）", exact=True
                    )
                    await start_time.fill("")
                    await section.get_by_role(
                        "button", name="保存抽奖参数", exact=True
                    ).click()
                    await expect(browser_page.get_by_role("status")).to_contain_text(
                        "活动参数已保存"
                    )
                    await section.get_by_role(
                        "button", name="开启活动", exact=True
                    ).click()
                    if width == 320:
                        await expect(section.get_by_role("alert")).to_contain_text(
                            "请刷新核对当前活动"
                        )
                        await section.get_by_role(
                            "button", name="刷新活动状态", exact=True
                        ).click()
                        await expect(
                            section.locator(".control-overview")
                        ).to_contain_text("报名中")
                        assert adapter.send_reply_text.await_count == 1
                    else:
                        await expect(
                            browser_page.get_by_role("status")
                        ).to_contain_text("抽奖已开启")
                    await browser_page.screenshot(
                        path=str(output / f"lottery-control-{width}.png"),
                        full_page=True,
                    )
                    assert await browser_page.evaluate(
                        "document.documentElement.scrollWidth <= innerWidth + 1"
                    )
                    await section.get_by_role(
                        "button", name="取消活动", exact=True
                    ).click()
                    await expect(browser_page.get_by_role("status")).to_contain_text(
                        "活动已取消"
                    )
                    value = (
                        datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(hours=1)
                    ).strftime("%Y-%m-%dT%H:%M")
                    await start_time.fill(value)
                    await section.get_by_role(
                        "button", name="保存抽奖参数", exact=True
                    ).click()
                    await expect(browser_page.get_by_role("status")).to_contain_text(
                        "活动参数已保存"
                    )
                    await section.get_by_role(
                        "button", name="预约发布", exact=True
                    ).click()
                    await expect(browser_page.get_by_role("status")).to_contain_text(
                        "已预约抽奖"
                    )
                    await expect(start_time).to_have_value(value)
                    await section.get_by_role(
                        "button", name="取消活动", exact=True
                    ).click()
                    await expect(browser_page.get_by_role("status")).to_contain_text(
                        "活动已取消"
                    )
                    assert not errors
                    await browser_page.close()
                assert adapter.send_reply_text.await_count == 6
            finally:
                await browser.close()
