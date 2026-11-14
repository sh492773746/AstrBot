"""The highest actual random packet wins one separate, frozen extra award."""

import json
import os
from contextlib import closing
from pathlib import Path

import httpx
import pytest

from astrbot.builtin_stars.wangshangliao_moderation import activity_store
from astrbot.builtin_stars.wangshangliao_moderation.activities import GroupActivities
from astrbot.builtin_stars.wangshangliao_moderation.lottery_rules import (
    validate_luckiest_bonus,
)
from tests.test_wangshangliao_activities import configure, event, latest
from tests.test_wangshangliao_activities import setup as setup
from tests.test_wangshangliao_dashboard import PREFIX
from tests.test_wangshangliao_dashboard import headers as headers
from tests.test_wangshangliao_dashboard import page_app as page_app


@pytest.mark.parametrize("mode", ["prize", "ordinary", "tiered"])
def test_other_modes_require_bonus_disabled(mode):
    assert validate_luckiest_bonus(0, mode) == 0
    with pytest.raises(ValueError, match="仅支持拼手气"):
        validate_luckiest_bonus(1, mode)


@pytest.mark.parametrize("value", [-1, 100000000, True, 1.5, "1", None])
def test_bonus_uses_bounded_integer_cents(value):
    with pytest.raises(ValueError):
        validate_luckiest_bonus(value, "lucky")


@pytest.mark.asyncio
@pytest.mark.parametrize("entrants", [0, 1, 2, 3])
@pytest.mark.parametrize("packets", [[100, 100, 100], [1, 199, 100]])
async def test_only_highest_claimed_packet_gets_extra_without_reducing_pool(
    setup, entrants, packets
):
    service, adapter, _, _, now, _ = setup
    caller = await configure(setup, capacity=3)
    with activity_store.database(adapter) as db:
        db.execute(
            "UPDATE lottery_settings SET total_amount=300,packet_mode='lucky',"
            "luckiest_bonus=28800,winners=3"
        )
    assert "抽奖已开启" in await service.command(caller, "5", "开启抽奖", "")
    notice = adapter.send_reply_text.await_args.args[2]
    assert "人气王额外奖励：288.00" in notice and "总预算：291.00" in notice
    assert "最高金额并列时随机选1人" in notice
    with activity_store.database(adapter) as db:
        db.execute("UPDATE lotteries SET packet_amounts=?", (json.dumps(packets),))
        db.execute("UPDATE lottery_settings SET luckiest_bonus=999")
    for uid in ("2", "3", "4")[:entrants]:
        assert "参加抽奖成功" in await service.command(
            event(adapter, uid, private=False), "5", "参加抽奖", ""
        )
    now[0] += 61
    await service.tick(adapter)
    row = latest(adapter)
    assert row["status"] == "finished" and row["luckiest_bonus"] == 28800
    winners = json.loads(row["results"])
    assert [winner["amount"] for winner in winners] == packets[:entrants]
    assert row["unclaimed_amount"] == sum(packets[entrants:])
    extra = [winner for winner in winners if winner.get("bonus_amount")]
    assert len(extra) == int(bool(entrants))
    if entrants:
        assert extra[0]["amount"] == max(packets[:entrants])
        assert extra[0]["bonus_amount"] == 28800
    text = await service.command(caller, "5", "中奖名单", "")
    assert "本期待发放合计（红包＋额外奖励）" in text
    if not entrants:
        assert "额外奖励未支出：288.00" in text
    else:
        mentions = adapter.send_reply_text.await_args.kwargs["mentions"]
        encoded = text.encode("utf-16-le")
        for mention in mentions:
            assert (
                encoded[mention["start"] * 2 : mention["end"] * 2].decode("utf-16-le")
                == "@" + mention["nick"] + " "
            )
        assert "（红包最高者；并列随机选1人，不占原奖池）" in (
            await service.command(caller, "5", "抽奖状态", "")
        )
    await GroupActivities(service.context).tick(adapter)
    assert latest(adapter) == row
    assert adapter.send_reply_text.await_count == 2
    with closing(activity_store.database(adapter)) as db:
        assert db.execute("SELECT COUNT(*) FROM invite_credits").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_scheduled_bonus_freezes_at_reservation_not_publication(setup):
    service, adapter, roster, _, now, _ = setup
    caller = await configure(setup, capacity=3)
    roster["9"]["groupRole"] = "GROUP_ROLE_ADMIN"
    with activity_store.database(adapter) as db:
        db.execute(
            "UPDATE lottery_settings SET total_amount=300,packet_mode='lucky',"
            "luckiest_bonus=100,start_at='1970-01-01T08:18',winners=3"
        )
    assert "已预约" in await service.command(caller, "5", "开启抽奖", "")
    frozen = latest(adapter)
    with activity_store.database(adapter) as db:
        db.execute("UPDATE lottery_settings SET luckiest_bonus=200")
    now[0] = 1080
    await GroupActivities(service.context).tick(adapter)
    assert latest(adapter)["luckiest_bonus"] == frozen["luckiest_bonus"] == 100
    assert "人气王额外奖励：1.00" in adapter.send_reply_text.await_args.args[2]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["unknown", "exception", "drawing_recovery"])
async def test_unknown_result_never_picks_another_bonus_recipient(setup, outcome):
    service, adapter, _, _, now, _ = setup
    caller = await configure(setup, capacity=3)
    with activity_store.database(adapter) as db:
        db.execute(
            "UPDATE lottery_settings SET total_amount=3,packet_mode='lucky',"
            "luckiest_bonus=1,winners=3"
        )
    await service.command(caller, "5", "开启抽奖", "")
    for uid in ("2", "3", "4"):
        await service.command(event(adapter, uid, private=False), "5", "参加抽奖", "")
    now[0] += 61
    if outcome != "drawing_recovery":
        adapter.send_reply_text.return_value = "unknown"
        if outcome == "exception":
            adapter.send_reply_text.side_effect = RuntimeError("transport")
    await service.tick(adapter)
    row = latest(adapter)
    if outcome == "drawing_recovery":
        with activity_store.database(adapter) as db:
            db.execute("UPDATE lotteries SET status='drawing'")
    calls = adapter.send_reply_text.await_count
    await GroupActivities(service.context).tick(adapter)
    after = latest(adapter)
    assert after["results"] == row["results"]
    assert after["status"] == "result_unknown"
    assert sum(w.get("bonus_amount", 0) for w in json.loads(after["results"])) == 1
    assert adapter.send_reply_text.await_count == calls


@pytest.mark.asyncio
async def test_old_schema_migrates_without_rewriting_allocations(setup):
    service, adapter, _, _, now, _ = setup
    caller = await configure(setup, capacity=3)
    with activity_store.database(adapter) as db:
        db.execute(
            "UPDATE lottery_settings SET total_amount=300,packet_mode='lucky',winners=3"
        )
    await service.command(caller, "5", "开启抽奖", "")
    before = latest(adapter)
    with activity_store.database(adapter) as db:
        for table in ("lottery_settings", "lotteries"):
            db.execute(f"ALTER TABLE {table} DROP COLUMN luckiest_bonus")
    await service.command(event(adapter, "2", private=False), "5", "参加抽奖", "")
    now[0] += 61
    await service.tick(adapter)
    row = latest(adapter)
    assert row["luckiest_bonus"] == 0
    assert row["packet_amounts"] == before["packet_amounts"]
    assert all("bonus_amount" not in w for w in json.loads(row["results"]))


@pytest.mark.asyncio
async def test_cancelled_bonus_is_never_awarded(setup):
    service, adapter, _, _, now, _ = setup
    caller = await configure(setup, capacity=3)
    with activity_store.database(adapter) as db:
        db.execute(
            "UPDATE lottery_settings SET total_amount=300,packet_mode='lucky',"
            "luckiest_bonus=28800,winners=3"
        )
    await service.command(caller, "5", "开启抽奖", "")
    await service.command(event(adapter, "2", private=False), "5", "参加抽奖", "")
    assert "已取消" in await service.command(caller, "5", "取消抽奖", "")
    assert "人气王额外奖励未支出：288.00" in adapter.send_reply_text.await_args.args[2]
    now[0] += 61
    await service.tick(adapter)
    row = latest(adapter)
    assert row["status"] == "cancelled" and row["results"] == "[]"
    assert row["unclaimed_amount"] == 300 and row["luckiest_bonus"] == 28800


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ordinary", "prize", "tiered"])
async def test_incompatible_stored_bonus_cannot_start(setup, mode):
    service, adapter, _, _, _, _ = setup
    caller = await configure(setup, capacity=3)
    with activity_store.database(adapter) as db:
        db.execute(
            "UPDATE lottery_settings SET total_amount=300,packet_mode=?,luckiest_bonus=1",
            (mode,),
        )
    assert "仅支持拼手气" in await service.command(caller, "5", "开启抽奖", "")
    adapter.send_reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_api_amount_validation_omission_revision_and_frozen_state(
    page_app, headers
):
    app, page, _, _, root = page_app
    adapter = page.context.platform_manager.platform_insts[0]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fixture",
        headers=headers,
    ) as client:
        params = {"bot_id": "fixture", "group_id": "5"}
        data = (await client.get(PREFIX + "/activities", params=params)).json()["data"]
        lottery = {
            **data["lottery"],
            "prize": "Neutral",
            "contact": "Admin",
            "packet_mode": "lucky",
            "total_amount": "3.00",
            "capacity": 3,
            "winners": 3,
            "luckiest_bonus": "288.00",
        }
        saved = await client.post(
            PREFIX + "/activities",
            json={**params, "revision": data["revision"], "lottery": lottery},
        )
        assert saved.status_code == 200, saved.text
        data = saved.json()["data"]
        assert data["lottery"]["luckiest_bonus"] == "288.00"
        assert (
            data["revision"]
            != (
                await client.get(
                    PREFIX + "/activities", params={**params, "group_id": "6"}
                )
            ).json()["data"]["revision"]
        )
        lottery.pop("luckiest_bonus")
        for patch in ({}, {"luckiest_bonus": "0.01"}, {"luckiest_bonus": "999999.99"}):
            saved = await client.post(
                PREFIX + "/activities",
                json={
                    **params,
                    "revision": data["revision"],
                    "lottery": {**lottery, **patch},
                },
            )
            assert saved.status_code == 200, saved.text
            expected = patch.get("luckiest_bonus", data["lottery"]["luckiest_bonus"])
            data = saved.json()["data"]
            assert data["lottery"]["luckiest_bonus"] == expected
        for bad in (True, None, {}, [], "NaN", "Infinity", "-0.01", "1.001", "1000000"):
            rejected = await client.post(
                PREFIX + "/activities",
                json={
                    **params,
                    "revision": data["revision"],
                    "lottery": {**lottery, "luckiest_bonus": bad},
                },
            )
            assert rejected.status_code == 400, (bad, rejected.text)
        rejected = await client.post(
            PREFIX + "/activities",
            json={
                **params,
                "revision": data["revision"],
                "lottery": {**lottery, "packet_mode": "ordinary"},
            },
        )
        assert rejected.status_code == 400
        assert (await client.get(PREFIX + "/activities", params=params)).json()[
            "data"
        ] == data
        with activity_store.database(adapter) as db:
            db.execute(
                "INSERT INTO lotteries(id,account,group_id,prize,status,created,results,luckiest_bonus) "
                "VALUES('bonus-fixture','1','5','Neutral','open',1,'[]',99999999)"
            )
        rejected = await client.post(
            PREFIX + "/activities",
            json={
                **params,
                "revision": data["revision"],
                "lottery": {**lottery, "luckiest_bonus": "1.00"},
            },
        )
        assert rejected.status_code == 409
        current = (await client.get(PREFIX + "/activities", params=params)).json()[
            "data"
        ]["current_lottery"]
        assert current["luckiest_bonus"] == "999999.99"
        with activity_store.database(adapter) as db:
            db.execute(
                "UPDATE lotteries SET status='finished',packet_mode='lucky',"
                "capacity=3,total_amount=300,packet_amounts='[100,100,100]',"
                "unclaimed_amount=200,"
                'results=\'[{"uid":"2","peer":"902","name":"Neutral",'
                '"amount":100,"bonus_amount":99999999}]\''
            )
        current = (await client.get(PREFIX + "/activities", params=params)).json()[
            "data"
        ]["current_lottery"]
        assert current["allocated_amount"] == "1.00"
        assert current["unclaimed_amount"] == "2.00"
        assert current["bonus_allocated_amount"] == "999999.99"
        assert current["total_payable_amount"] == "1000000.99"
        with closing(activity_store.database(adapter)) as db:
            assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert (root / "activities.sqlite3").exists()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("WSL_ACTIVITY_PAGE"), reason="Opt-in compiled UI"
)
async def test_bonus_form_saves_roundtrip_and_clears_incompatible_mode(
    page_app, headers, tmp_path
):
    from playwright.async_api import async_playwright, expect

    output = Path(os.environ.get("WSL_BONUS_OUTPUT", str(tmp_path)))
    output.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=page_app[0]),
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
                        viewport={"width": width, "height": 1000}
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
                    await section.get_by_label("活动名称", exact=True).fill(
                        "中性加奖测试"
                    )
                    await section.get_by_label("领奖联系人", exact=True).fill(
                        "测试管理员"
                    )
                    await section.get_by_label("红包类型", exact=True).select_option(
                        "lucky"
                    )
                    await section.get_by_label("总金额（元）", exact=True).fill("3000")
                    await section.get_by_label("参加人数", exact=True).fill("30")
                    bonus = section.get_by_label(
                        "人气王额外奖励（元，0为不加奖）", exact=True
                    )
                    await bonus.fill("288")
                    await expect(
                        section.get_by_text("3288.00 元", exact=True)
                    ).to_be_visible()
                    await section.get_by_role(
                        "button", name="保存抽奖参数", exact=True
                    ).click()
                    await expect(page.get_by_role("status")).to_contain_text(
                        "活动参数已保存"
                    )
                    await expect(bonus).to_have_value("288.00")
                    readback = (
                        await client.get(
                            PREFIX + "/activities",
                            params={"bot_id": "fixture", "group_id": "5"},
                        )
                    ).json()["data"]
                    assert readback["lottery"]["total_amount"] == "3000.00"
                    assert readback["lottery"]["luckiest_bonus"] == "288.00"
                    await page.screenshot(
                        path=str(output / f"luckiest-bonus-{width}.png"), full_page=True
                    )
                    assert await page.evaluate(
                        "document.documentElement.scrollWidth <= innerWidth + 1"
                    )
                    assert not errors
                    await section.get_by_label("红包类型", exact=True).select_option(
                        "ordinary"
                    )
                    await expect(bonus).not_to_be_visible()
                    await section.get_by_role(
                        "button", name="保存抽奖参数", exact=True
                    ).click()
                    await expect(page.get_by_role("status")).to_contain_text(
                        "活动参数已保存"
                    )
                    readback = (
                        await client.get(
                            PREFIX + "/activities",
                            params={"bot_id": "fixture", "group_id": "5"},
                        )
                    ).json()["data"]
                    assert readback["lottery"]["luckiest_bonus"] == "0.00"
                    await page.close()
            finally:
                await browser.close()
