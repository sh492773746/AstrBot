"""Whole-yuan packet plans and opt-in full-capacity publication."""

import json

import httpx
import pytest

from astrbot.builtin_stars.wangshangliao_moderation.activity_store import database
from astrbot.builtin_stars.wangshangliao_moderation.lottery_rules import packet_plan
from tests.test_wangshangliao_activities import configure, event, latest, member
from tests.test_wangshangliao_activities import setup as setup
from tests.test_wangshangliao_dashboard import PREFIX
from tests.test_wangshangliao_dashboard import headers as headers
from tests.test_wangshangliao_dashboard import page_app as page_app


@pytest.mark.parametrize("mode", ["ordinary", "lucky", "tiered"])
@pytest.mark.parametrize("count", [1, 11, 100])
def test_integer_plan_is_exact_positive_and_never_rounded(mode, count):
    tiers = [{"amount": 200, "count": 1}] if mode == "tiered" else []
    if mode == "tiered" and count == 1:
        tiers = [{"amount": 111100, "count": 1}]
    for _ in range(20):
        plan = packet_plan(111100, count, mode, tiers, integer_packets=True)
        assert len(plan) == count and sum(plan) == 111100
        assert all(
            type(amount) is int and amount >= 100 and amount % 100 == 0
            for amount in plan
        )


@pytest.mark.parametrize(
    "total,count,tiers",
    [
        (111101, 11, []),
        (1000, 11, []),
        (111100, 11, [{"amount": 199, "count": 1}]),
        (1100, 11, [{"amount": 200, "count": 1}]),
    ],
)
def test_impossible_integer_plans_are_rejected(total, count, tiers):
    with pytest.raises(ValueError):
        packet_plan(
            total, count, "tiered" if tiers else "lucky", tiers, integer_packets=True
        )


@pytest.mark.asyncio
async def test_eleven_entries_publish_exact_frozen_integer_pool_and_extra_bonus(setup):
    service, adapter, roster, _, now, _ = setup
    caller = await configure(setup, capacity=11)
    for uid in range(10, 21):
        roster[str(uid)] = member(uid)
    with database(adapter) as db:
        db.execute(
            "UPDATE lottery_settings SET total_amount=111100,capacity=11,winners=11,"
            "packet_mode='lucky',integer_packets=1,draw_on_full=1,luckiest_bonus=18800"
        )
    assert "报满11人" in await service.command(caller, "5", "开启抽奖", "")
    frozen = latest(adapter)
    notice = adapter.send_reply_text.await_args.args[2]
    assert "每份整数元" in notice and "报满11人后公布" in notice
    assert "倒计时" not in notice and "预计开奖" not in notice
    assert len(notice.splitlines()) <= 9
    with database(adapter) as db:
        db.execute("UPDATE lottery_settings SET integer_packets=0,draw_on_full=0")
    now[0] += 10000
    for uid in range(10, 20):
        assert "参加抽奖成功" in await service.command(
            event(adapter, str(uid), private=False), "5", "参加抽奖", ""
        )
    await service.tick(adapter)
    assert latest(adapter)["status"] == "open"
    assert "报满" in await service.command(caller, "5", "立即开奖", "")
    assert adapter.send_reply_text.await_count == 1
    assert "参加抽奖成功" in await service.command(
        event(adapter, "20", private=False), "5", "参加抽奖", ""
    )
    await service.tick(adapter)
    result = latest(adapter)
    assert result["status"] == "finished"
    allocations = json.loads(result["results"])
    assert len(allocations) == 11
    assert sum(winner["amount"] for winner in allocations) == 111100
    assert all(
        winner["amount"] >= 100 and winner["amount"] % 100 == 0
        for winner in allocations
    )
    bonus = [winner for winner in allocations if winner.get("bonus_amount")]
    assert len(bonus) == 1 and bonus[0]["bonus_amount"] == 18800
    assert bonus[0]["amount"] == max(winner["amount"] for winner in allocations)
    assert result["unclaimed_amount"] == 0
    assert result["packet_amounts"] == frozen["packet_amounts"]
    await service.tick(adapter)
    assert latest(adapter)["results"] == result["results"]
    assert adapter.send_reply_text.await_count == 2


@pytest.mark.asyncio
async def test_full_round_blocks_on_departure_without_spending_or_rerolling(setup):
    service, adapter, roster, _, _, _ = setup
    caller = await configure(setup, capacity=3)
    with database(adapter) as db:
        db.execute(
            "UPDATE lottery_settings SET packet_mode='lucky',total_amount=111100,"
            "integer_packets=1,draw_on_full=1"
        )
    await service.command(caller, "5", "开启抽奖", "")
    for uid in ("2", "3", "4"):
        await service.command(event(adapter, uid, private=False), "5", "参加抽奖", "")
    del roster["4"]
    await service.tick(adapter)
    assert latest(adapter)["status"] == "blocked"
    assert latest(adapter)["results"] == "[]"
    assert adapter.send_reply_text.await_count == 1


@pytest.mark.asyncio
async def test_new_flags_roundtrip_and_old_clients_preserve_them(page_app, headers):
    app, page, _, _, _ = page_app
    adapter = page.context.platform_manager.platform_insts[0]
    params = {"bot_id": adapter.config["id"], "group_id": "5"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        snapshot = (
            await client.get(PREFIX + "/activities", headers=headers, params=params)
        ).json()["data"]
        settings = {
            **snapshot["lottery"],
            "prize": "Integer test",
            "contact": "Admin",
            "packet_mode": "lucky",
            "total_amount": "1111.00",
            "capacity": 11,
            "winners": 11,
            "integer_packets": True,
            "draw_on_full": True,
            "luckiest_bonus": "188.00",
        }
        response = await client.post(
            PREFIX + "/activities",
            headers=headers,
            json={**params, "revision": snapshot["revision"], "lottery": settings},
        )
        assert response.status_code == 200, response.text
        saved = response.json()["data"]
        assert saved["lottery"]["integer_packets"] is True
        assert saved["lottery"]["draw_on_full"] is True
        settings.pop("integer_packets")
        settings.pop("draw_on_full")
        response = await client.post(
            PREFIX + "/activities",
            headers=headers,
            json={**params, "revision": saved["revision"], "lottery": settings},
        )
        assert response.status_code == 200
        saved = response.json()["data"]
        assert saved["lottery"]["integer_packets"] is True
        assert saved["lottery"]["draw_on_full"] is True
        for updates in (
            {"total_amount": "1111.01"},
            {"luckiest_bonus": "188.01"},
            {"draw_on_full": 1},
            {"integer_packets": "true"},
        ):
            response = await client.post(
                PREFIX + "/activities",
                headers=headers,
                json={
                    **params,
                    "revision": saved["revision"],
                    "lottery": {**settings, **updates},
                },
            )
            assert response.status_code == 400
