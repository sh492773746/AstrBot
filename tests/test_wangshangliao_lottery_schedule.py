"""Explicit Beijing publication timing preserves frozen, once-only lotteries."""

import asyncio
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

from astrbot.builtin_stars.wangshangliao_moderation import activity_store, lottery_time
from astrbot.builtin_stars.wangshangliao_moderation.activities import GroupActivities
from astrbot.builtin_stars.wangshangliao_moderation.syntax import recognize_command
from tests.test_wangshangliao_activities import configure, event, latest
from tests.test_wangshangliao_activities import setup as setup
from tests.test_wangshangliao_dashboard import PREFIX
from tests.test_wangshangliao_dashboard import headers as headers
from tests.test_wangshangliao_dashboard import page_app as page_app


def test_time_is_beijing_not_browser_or_host_timezone(monkeypatch):
    monkeypatch.setattr(lottery_time.time, "time", lambda: 1000)
    assert lottery_time.parse_start_at("1970-01-01T08:18", future=True) == 1080
    assert lottery_time.parse_start_at("1970-01-01 08:18") == 1080
    assert lottery_time.parse_start_at("", future=True) == 0
    assert "08:18:00（北京时间）" in lottery_time.beijing_time(1080)
    assert recognize_command("抽奖开始时间 1970-01-01 08:18") == (
        "抽奖开始时间 1970-01-01 08:18"
    )


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        1080,
        "明天8点",
        "1970-01-01T08:18Z",
        "1970-01-01T08:18:00",
        "1970-02-30T08:18",
        "1970-01-01T24:00",
        "1970-01-01T08:16",
        "1970-02-02T08:18",
        " 1970-01-01T08:18",
    ],
)
def test_invalid_or_past_calendar_minute_is_rejected(monkeypatch, value):
    monkeypatch.setattr(lottery_time.time, "time", lambda: 1000)
    with pytest.raises(ValueError):
        lottery_time.parse_start_at(value, future=True)


async def scheduled(setup):
    service, adapter, roster, _, _, _ = setup
    caller = await configure(setup)
    roster["9"]["groupRole"] = "GROUP_ROLE_ADMIN"
    assert "已设置" in await service.command(
        caller, "5", "抽奖开始时间", "1970-01-01 08:18"
    )
    with activity_store.database(adapter) as db:
        db.execute(
            "UPDATE lottery_settings SET total_amount=300,packet_mode='ordinary',"
            "capacity=3,winners=3"
        )
    assert "已预约抽奖" in await service.command(caller, "5", "开启抽奖", "")
    return caller


@pytest.mark.asyncio
async def test_scheduled_freeze_prevents_early_enrollment_draw_or_duplicate_start(
    setup,
):
    service, adapter, _, _, _, _ = setup
    caller = await scheduled(setup)
    before = latest(adapter)
    assert before["status"] == "scheduled" and before["starts"] == 1080
    assert before["ends"] == 1140 and len(json.loads(before["packet_amounts"])) == 3
    adapter.send_reply_text.assert_not_awaited()
    assert "没有开放报名" in await service.command(
        event(adapter, "2", private=False), "5", "参加抽奖", ""
    )
    assert "不能提前开奖" in await service.command(caller, "5", "立即开奖", "")
    assert "已处理" in await service.command(caller, "5", "开启抽奖", "")
    assert "已有进行中" in await service.command(
        event(adapter, mid="different"), "5", "开启抽奖", ""
    )
    assert "活动已开启" in await service.command(caller, "5", "抽奖倒计时", "2")
    await service.tick(adapter)
    assert latest(adapter) == before


@pytest.mark.asyncio
async def test_restart_and_concurrent_ticks_publish_once_then_draw_after_countdown(
    setup,
):
    service, adapter, _, _, now, _ = setup
    await scheduled(setup)
    frozen = latest(adapter)
    restarted = GroupActivities(service.context)
    now[0] = 1085
    await asyncio.gather(restarted.tick(adapter), service.tick(adapter))
    opened = latest(adapter)
    assert opened["status"] == "open" and opened["ends"] == 1145
    assert opened["packet_amounts"] == frozen["packet_amounts"]
    adapter.send_reply_text.assert_awaited_once()
    assert "北京时间" in adapter.send_reply_text.await_args.args[2]
    assert "参加抽奖成功" in await restarted.command(
        event(adapter, "2", private=False), "5", "参加抽奖", ""
    )
    now[0] = 1144
    await restarted.tick(adapter)
    assert latest(adapter)["status"] == "open"
    now[0] = 1146
    await restarted.tick(adapter)
    assert latest(adapter)["status"] == "finished"
    assert adapter.send_reply_text.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason", ["late", "owner", "posting", "roster", "slow_roster", "native"]
)
async def test_due_work_fails_closed_without_automatic_retry(setup, reason):
    service, adapter, roster, _, now, config = setup
    await scheduled(setup)
    now[0] = 1080
    if reason == "late":
        now[0] = 1141
    elif reason == "owner":
        config["admins_id"] = []
    elif reason == "posting":
        adapter.config["proactive_send"]["targets"] = []
    elif reason == "roster":
        roster["9"]["groupRole"] = "GROUP_ROLE_MEMBER"
    elif reason == "native":
        adapter.config["_open_groups"] = {"5": {}}
        adapter.config["_open_native_roles"] = {"5": "GROUP_ROLE_MEMBER"}
    elif reason == "slow_roster":

        async def slow(_):
            now[0] = 1141
            return {"complete": True, "groupMemberInfo": list(roster.values())}

        adapter.get_moderation_members.side_effect = slow
    await service.tick(adapter)
    row = latest(adapter)
    assert row["status"] == "schedule_blocked" and row["error"]
    config["admins_id"] = ["9"]
    roster["9"]["groupRole"] = "GROUP_ROLE_ADMIN"
    adapter.config["proactive_send"]["targets"] = ["5"]
    adapter.config["_open_native_roles"] = {"5": "GROUP_ROLE_ADMIN"}
    await service.tick(adapter)
    assert latest(adapter) == row
    assert "不能提前开奖" in await service.command(event(adapter), "5", "立即开奖", "")
    adapter.send_reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_scheduled_work_is_silent_and_survives_restart(setup):
    service, adapter, _, _, now, _ = setup
    caller = await scheduled(setup)
    assert "未向群内发送" in await service.command(caller, "5", "取消抽奖", "")
    now[0] = 1080
    await GroupActivities(service.context).tick(adapter)
    assert latest(adapter)["status"] == "cancelled"
    adapter.send_reply_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", ["unknown", "exception", "crash_accepted", "crash_unknown"]
)
async def test_scheduled_unknown_or_crash_boundary_never_republishes(setup, outcome):
    service, adapter, _, _, now, _ = setup
    await scheduled(setup)
    now[0] = 1080
    if outcome.startswith("crash"):
        with activity_store.database(adapter) as db:
            db.execute("UPDATE lotteries SET status='announcing',ends=1140")
        if outcome == "crash_accepted":
            key = f"lottery-start/{adapter.account}/{latest(adapter)['id']}"
            await adapter.ledger.reserve(key, "Neutral test")
            await adapter.ledger.finish(key, "accepted")
    else:
        adapter.send_reply_text.return_value = outcome
        if outcome == "exception":
            adapter.send_reply_text.side_effect = RuntimeError("transport")
    await service.tick(adapter)
    first = latest(adapter)
    assert first["status"] == (
        "open" if outcome == "crash_accepted" else "notice_unknown"
    )
    before = adapter.send_reply_text.await_count
    await GroupActivities(service.context).tick(adapter)
    assert latest(adapter) == first
    assert adapter.send_reply_text.await_count == before


@pytest.mark.asyncio
async def test_new_index_keeps_schedule_exclusive(setup):
    _, adapter, _, _, _, _ = setup
    await scheduled(setup)
    with closing(activity_store.database(adapter)) as db:
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO lotteries(id,command,account,group_id,status) "
                "VALUES('duplicate','other','1','5','open')"
            )


@pytest.mark.asyncio
async def test_dashboard_calendar_time_and_legacy_client_preservation(
    page_app, headers
):
    app, _, _, _, _ = page_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fixture",
        headers=headers,
    ) as client:
        data = (
            await client.get(
                PREFIX + "/activities", params={"bot_id": "fixture", "group_id": "5"}
            )
        ).json()["data"]
        assert data["lottery"]["start_at"] == ""
        value = (datetime.now(lottery_time.BEIJING) + timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M"
        )
        lottery = {
            **data["lottery"],
            "prize": "Neutral test",
            "contact": "Admin",
            "start_at": value,
        }
        saved = await client.post(
            PREFIX + "/activities",
            json={
                "bot_id": "fixture",
                "group_id": "5",
                "revision": data["revision"],
                "lottery": lottery,
            },
        )
        assert saved.status_code == 200, saved.text
        result = saved.json()["data"]
        assert result["lottery"]["start_at"] == value
        lottery.pop("start_at")
        saved = await client.post(
            PREFIX + "/activities",
            json={
                "bot_id": "fixture",
                "group_id": "5",
                "revision": result["revision"],
                "lottery": lottery,
            },
        )
        assert saved.json()["data"]["lottery"]["start_at"] == value
        for bad in (None, 1080, "2020-01-01T08:00", "2030-01-01T08:00", "today"):
            result = saved.json()["data"]
            rejected = await client.post(
                PREFIX + "/activities",
                json={
                    "bot_id": "fixture",
                    "group_id": "5",
                    "revision": result["revision"],
                    "lottery": {**lottery, "start_at": bad},
                },
            )
            assert rejected.status_code == 400


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("WSL_ACTIVITY_PAGE"), reason="Opt-in compiled UI"
)
async def test_compiled_schedule_form_uses_beijing_without_browser_offset(
    page_app, headers, tmp_path
):
    from playwright.async_api import async_playwright, expect

    app = page_app[0]
    output = Path(os.environ.get("WSL_LOTTERY_OUTPUT", str(tmp_path)))
    output.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fixture",
        headers=headers,
    ) as client:

        async def api(_source, method, endpoint, body=None):
            response = await client.request(
                method,
                PREFIX + "/" + endpoint,
                **({"params": body or {}} if method == "GET" else {"json": body}),
            )
            assert response.status_code == 200, response.text
            return response.json()["data"]

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(args=["--no-sandbox"])
            try:
                for width in (320, 390, 1440):
                    page = await browser.new_page(
                        viewport={"width": width, "height": 1000},
                        timezone_id="America/Los_Angeles",
                    )
                    errors = []
                    page.on("pageerror", lambda exc: errors.append(str(exc)))
                    await page.expose_binding("fixtureAPI", api)
                    await page.add_init_script("""
                        Object.defineProperty(window, "AstrBotPluginPage", {value:{
                            ready:async()=>({}),getLocale:()=> "zh-CN",onContext:()=>()=>{},
                            apiGet:(endpoint,body)=>window.fixtureAPI("GET",endpoint,body),
                            apiPost:(endpoint,body)=>window.fixtureAPI("POST",endpoint,body),
                        }});
                    """)
                    await page.goto(
                        Path(os.environ["WSL_ACTIVITY_PAGE"]).resolve().as_uri()
                    )
                    await page.get_by_role("tab", name="活动与财务", exact=True).click()
                    await page.get_by_role(
                        "tab", name="玩家福利抽奖", exact=True
                    ).click()
                    section = page.locator("#panel-activities")
                    start = section.get_by_label(
                        "开始时间（北京时间，留空立即发送）", exact=True
                    )
                    await expect(start).to_be_visible()
                    await section.get_by_label("活动名称", exact=True).fill(
                        "中性定时测试"
                    )
                    await section.get_by_label("领奖联系人", exact=True).fill(
                        "测试管理员"
                    )
                    await section.get_by_label("总金额（元）", exact=True).fill("3")
                    await section.get_by_label("参加人数", exact=True).fill("3")
                    value = (
                        datetime.now(lottery_time.BEIJING)
                        + timedelta(hours=width / 320)
                    ).strftime("%Y-%m-%dT%H:%M")
                    await start.fill(value)
                    await section.get_by_role(
                        "button", name="保存抽奖参数", exact=True
                    ).click()
                    await expect(page.get_by_role("status")).to_contain_text(
                        "活动参数已保存"
                    )
                    await expect(start).to_have_value(value)
                    readback = (
                        await client.get(
                            PREFIX + "/activities",
                            params={"bot_id": "fixture", "group_id": "5"},
                        )
                    ).json()["data"]
                    assert readback["lottery"]["start_at"] == value
                    await page.screenshot(
                        path=str(output / f"lottery-schedule-{width}.png"),
                        full_page=True,
                    )
                    assert await page.evaluate(
                        "document.documentElement.scrollWidth <= innerWidth + 1"
                    )
                    assert not errors
                    await page.close()
            finally:
                await browser.close()
