"""Packet deadlines and display ordering never change random allocations."""

import asyncio
import copy
import json
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio

from astrbot.builtin_stars.wangshangliao_moderation import activities, activity_store
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from tests.test_wangshangliao_activities import configure, event, latest
from tests.test_wangshangliao_activities import setup as setup
from tests.test_wangshangliao_dashboard import PREFIX
from tests.test_wangshangliao_dashboard import headers as headers
from tests.test_wangshangliao_dashboard import page_app as page_app


@pytest_asyncio.fixture(params=["ordinary", "lucky"])
async def packet(setup, request):
    """Open one isolated packet activity with a frozen deadline.

    Args:
        setup: Existing isolated activity service and controllable clock.
        request: Parameter selecting the packet allocation mode.

    Returns:
        Scoped activity handles without any native network calls.
    """
    service, adapter, _, _, now, _ = setup
    caller = await configure(setup, capacity=3)
    with closing(activity_store.database(adapter)) as db, db:
        db.execute(
            "UPDATE lottery_settings SET total_amount=600,packet_mode=?",
            (request.param,),
        )
    assert "抽奖已开启" in await service.command(caller, "5", "开启抽奖", "")
    return SimpleNamespace(
        service=service, adapter=adapter, caller=caller, now=now, mode=request.param
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["open", "blocked"])
@pytest.mark.parametrize("full", [False, True])
async def test_early_admin_draw_keeps_packet_deadline_and_allocations(
    packet, status, full
):
    service, adapter = packet.service, packet.adapter
    if full:
        for uid in ("2", "3", "4"):
            assert "参加抽奖成功" in await service.command(
                event(adapter, uid, private=False), "5", "参加抽奖", ""
            )
    with closing(activity_store.database(adapter)) as db, db:
        db.execute("UPDATE lotteries SET status=?", (status,))
    before = latest(adapter)
    packet.now[0] = before["ends"] - 0.001
    adapter.get_moderation_members.reset_mock()
    reply = await service.command(packet.caller, "5", "立即开奖", "")
    assert "到设定时间" in reply and "不提前开奖" in reply
    assert latest(adapter) == before
    adapter.get_moderation_members.assert_not_awaited()
    assert adapter.send_reply_text.await_count == 1
    assert "尚未开奖" in await service.command(packet.caller, "5", "中奖名单", "")
    if status == "open":
        await service.tick(adapter)
        assert latest(adapter) == before
        assert adapter.send_reply_text.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("late", [0, 10])
async def test_admin_draw_at_or_after_deadline_preserves_original_time(packet, late):
    service, adapter = packet.service, packet.adapter
    await service.command(event(adapter, "2", private=False), "5", "参加抽奖", "")
    before = latest(adapter)
    packet.now[0] = before["ends"] + late
    assert "已开奖" in await service.command(packet.caller, "5", "立即开奖", "")
    row = latest(adapter)
    assert row["status"] == "finished" and row["ends"] == before["ends"]
    assert row["packet_amounts"] == before["packet_amounts"]
    assert len(json.loads(row["results"])) == 1
    assert adapter.send_reply_text.await_count == 2
    assert "没有进行中" in await service.command(packet.caller, "5", "立即开奖", "")
    await service.tick(adapter)
    assert latest(adapter) == row
    assert adapter.send_reply_text.await_count == 2


@pytest.mark.asyncio
async def test_direct_draw_before_deadline_does_not_generate_or_publish(packet):
    before = latest(packet.adapter)
    packet.now[0] = before["ends"] - 0.001
    reply = await packet.service.draw(packet.adapter, before["id"])
    assert "尚未到开奖时间" in reply
    assert latest(packet.adapter) == before
    assert packet.adapter.send_reply_text.await_count == 1
    assert "满员也不提前开奖" in packet.adapter.send_reply_text.await_args.args[2]


@pytest.mark.asyncio
async def test_deadline_is_rechecked_after_roster_wait(packet, monkeypatch):
    before = latest(packet.adapter)
    packet.now[0] = before["ends"]
    roster = packet.service.roster

    async def delayed_roster(*args, **kwargs):
        result = await roster(*args, **kwargs)
        packet.now[0] = before["ends"] - 1
        return result

    monkeypatch.setattr(packet.service, "roster", delayed_roster)
    reply = await packet.service.draw(packet.adapter, before["id"])
    assert "尚未到开奖时间" in reply
    assert latest(packet.adapter) == before
    assert packet.adapter.send_reply_text.await_count == 1


@pytest.mark.asyncio
async def test_packet_order_only_changes_display_not_identity_amount_or_receipts(
    packet, monkeypatch
):
    service, adapter = packet.service, packet.adapter
    for uid in ("2", "3", "4"):
        await service.command(event(adapter, uid, private=False), "5", "参加抽奖", "")
    amounts = [200, 200, 200] if packet.mode == "ordinary" else [101, 479, 20]
    with closing(activity_store.database(adapter)) as db, db:
        db.execute("UPDATE lotteries SET packet_amounts=?", (json.dumps(amounts),))
    monkeypatch.setattr(
        activities.secrets.SystemRandom,
        "sample",
        lambda self, population, count: list(reversed(population))[:count],
    )
    packet.now[0] = latest(adapter)["ends"]
    adapter.send_reply_text.return_value = "unknown"
    await service.tick(adapter)
    row = latest(adapter)
    winners = json.loads(row["results"])
    assert row["status"] == "result_unknown"
    assert [winner["uid"] for winner in winners] == ["4", "3", "2"]
    assert [winner["amount"] for winner in winners] == amounts
    assert row["unclaimed_amount"] == 0
    expected = (
        sorted(winners, key=lambda winner: winner["amount"], reverse=True)
        if packet.mode == "lucky"
        else winners
    )
    sent = adapter.send_reply_text.await_args
    assert [mention["uid"] for mention in sent.kwargs["mentions"]] == [
        int(winner["peer"]) for winner in expected
    ]
    for mention, winner in zip(sent.kwargs["mentions"], expected, strict=True):
        mentioned = (
            sent.args[2]
            .encode("utf-16-le")[mention["start"] * 2 : mention["end"] * 2]
            .decode("utf-16-le")
        )
        assert mentioned == "@" + winner["name"] + " "
    replay = await service.command(packet.caller, "5", "中奖名单", "")
    assert replay == sent.args[2]
    await service.tick(adapter)
    assert latest(adapter) == row
    assert adapter.send_reply_text.await_count == 2
    with closing(activity_store.database(adapter)) as db:
        assert db.execute("SELECT COUNT(*) FROM invite_credits").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ordinary", "lucky"])
async def test_display_order_is_stable_preserves_ties_and_utf16_mentions(setup, mode):
    service = setup[0]
    row = {
        "packet_mode": mode,
        "packet_amounts": "[]",
        "total": 4,
        "prize": "Packets",
        "owner_name": "Admin",
        "owner": "9",
        "contact": "Admin",
    }
    winners = [
        {"uid": "2", "peer": "902", "name": "A\U0001f600", "amount": 1},
        {"uid": "3", "peer": "903", "name": "B", "amount": 300},
        {"uid": "4", "peer": "904", "name": "C", "amount": 300},
        {"uid": "9", "peer": "909", "name": "D", "amount": 100},
    ]
    before = copy.deepcopy(winners)
    text, mentions = service.result_text(row, winners)
    assert [mention["uid"] for mention in mentions] == (
        [903, 904, 909, 902] if mode == "lucky" else [902, 903, 904, 909]
    )
    for mention in mentions:
        nick = text.encode("utf-16-le")[
            mention["start"] * 2 : mention["end"] * 2
        ].decode("utf-16-le")
        assert nick == "@" + mention["nick"] + " "
    assert winners == before
    assert service.result_text(row, winners) == (text, mentions)


@pytest.mark.asyncio
async def test_dashboard_explains_not_due_without_creating_an_unknown_receipt(
    page_app, headers
):
    app, page, _, _, _ = page_app
    rpc = SimpleNamespace(
        lock=asyncio.Lock(),
        dispatch=AsyncMock(side_effect=OpenError("test_lottery_not_due")),
    )
    page.plugin = SimpleNamespace(
        open_service=SimpleNamespace(integrated=True, active=True, rpc=rpc)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        response = await client.post(
            PREFIX + "/open-service",
            headers=headers,
            json={"action": "test_lottery", "payload": {}},
        )
    assert response.status_code == 409
    assert "尚未到设定开奖时间" in response.json()["message"]
    assert "不会提前公布或占用开奖回执" in response.json()["message"]
    rpc.dispatch.assert_awaited_once()
