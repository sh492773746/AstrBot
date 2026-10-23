"""Packet allocation is exact, durable and independent of invitation rewards."""

import json
import sqlite3
from contextlib import closing

import httpx
import pytest

from astrbot.builtin_stars.wangshangliao_moderation import activity_store
from astrbot.builtin_stars.wangshangliao_moderation.syntax import recognize_command
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from tests.test_wangshangliao_activities import configure, event, latest
from tests.test_wangshangliao_activities import setup as setup
from tests.test_wangshangliao_dashboard import PREFIX
from tests.test_wangshangliao_dashboard import headers as headers
from tests.test_wangshangliao_dashboard import page_app as page_app


def test_new_entry_command_is_exact_and_keeps_legacy_alias():
    assert recognize_command("参与玩家福利抽奖") == "参加抽奖"
    assert recognize_command("/参与玩家福利抽奖") == "参加抽奖"
    assert recognize_command("参加抽奖") == "参加抽奖"
    for text in ("参与玩家福利抽奖吗", "请参与玩家福利抽奖", "参与玩家福利抽奖 2"):
        assert not recognize_command(text)


@pytest.mark.asyncio
async def test_migration_keeps_existing_invite_gate_and_prize_draws(setup):
    adapter = setup[1]
    await configure(setup)
    with activity_store.database(adapter) as db:
        db.execute("UPDATE lottery_settings SET invite_gate=5")
        for column in ("by_invites", "packet_mode", "total_amount"):
            db.execute(f"ALTER TABLE lottery_settings DROP COLUMN {column}")
    with closing(activity_store.database(adapter)) as db:
        row = db.execute("SELECT * FROM lottery_settings").fetchone()
        assert row["by_invites"] == 1 and row["invite_gate"] == 5
        assert row["packet_mode"] == "prize" and row["total_amount"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ordinary", "lucky"])
@pytest.mark.parametrize("count,total", [(1, 1), (3, 3), (3, 100), (3, 99999999)])
async def test_amounts_are_positive_exact_frozen_and_not_redrawn(
    setup, mode, count, total
):
    service, adapter, _, _, now, _ = setup
    caller = await configure(setup, capacity=max(count, 3))
    with activity_store.database(adapter) as db:
        db.execute(
            "UPDATE lottery_settings SET total_amount=?,packet_mode=?,"
            "by_invites=0,invite_gate=99,capacity=?,winners=?",
            (total, mode, count, count),
        )
    assert "抽奖已开启" in await service.command(caller, "5", "开启抽奖", "")
    assert "参与玩家福利抽奖" in adapter.send_reply_text.await_args.args[2]
    for uid in ("2", "3", "4")[:count]:
        assert "参加抽奖成功" in await service.command(
            event(adapter, uid, private=False), "5", "参加抽奖", ""
        )
    with activity_store.database(adapter) as db:
        db.execute("UPDATE lottery_settings SET total_amount=999,packet_mode='prize'")
    now[0] += 61
    await service.tick(adapter)
    row = latest(adapter)
    winners = json.loads(row["results"])
    assert row["status"] == "finished"
    assert len(winners) == count
    amounts = [winner["amount"] for winner in winners]
    assert sum(amounts) == total and min(amounts) >= 1
    if mode == "ordinary":
        assert max(amounts) - min(amounts) <= 1
    assert "待发放" in adapter.send_reply_text.await_args.args[2]
    assert all(
        "amount" not in mention
        for mention in adapter.send_reply_text.await_args.kwargs["mentions"]
    )
    await service.tick(adapter)
    assert latest(adapter)["results"] == row["results"]
    assert adapter.send_reply_text.await_count == 2
    with closing(activity_store.database(adapter)) as db:
        assert db.execute("SELECT COUNT(*) FROM invite_credits").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_packet_invitation_gate_applies_only_when_checked(setup):
    service, adapter, _, _, _, _ = setup
    caller = await configure(setup, capacity=3)
    with activity_store.database(adapter) as db:
        db.execute(
            "UPDATE lottery_settings SET total_amount=100,packet_mode='ordinary',"
            "by_invites=1,invite_gate=2"
        )
    await service.command(caller, "5", "开启抽奖", "")
    reply = await service.command(
        event(adapter, "2", private=False), "5", "参加抽奖", ""
    )
    assert "当前为0" in reply


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ordinary", "lucky"])
@pytest.mark.parametrize("entrants", [0, 1, 2, 3])
@pytest.mark.parametrize("total", [3, 10001])
async def test_unclaimed_packets_are_not_redistributed(setup, mode, entrants, total):
    service, adapter, _, _, now, _ = setup
    caller = await configure(setup, capacity=3)
    note = "须使用活动头像\n昵称包含江哥；领奖时核验"
    with activity_store.database(adapter) as db:
        db.execute(
            "UPDATE lottery_settings SET total_amount=?,packet_mode=?,"
            "capacity=3,winners=3,remarks=?",
            (total, mode, note),
        )
    await service.command(caller, "5", "开启抽奖", "")
    opened = latest(adapter)
    packets = json.loads(opened["packet_amounts"])
    assert len(packets) == 3 and sum(packets) == total and min(packets) >= 1
    assert note in adapter.send_reply_text.await_args.args[2]
    assert "不重新分摊" in adapter.send_reply_text.await_args.args[2]
    for uid in ("2", "3", "4")[:entrants]:
        assert "参加抽奖成功" in await service.command(
            event(adapter, uid, private=False), "5", "参加抽奖", ""
        )
    with activity_store.database(adapter) as db:
        db.execute("UPDATE lottery_settings SET total_amount=999,remarks='下一期'")
    now[0] += 61
    await service.tick(adapter)
    result = latest(adapter)
    winners = json.loads(result["results"])
    assert result["status"] == "finished" and len(winners) == entrants
    assert [winner["amount"] for winner in winners] == packets[:entrants]
    assert result["unclaimed_amount"] == sum(packets[entrants:])
    assert (
        sum(winner["amount"] for winner in winners) + result["unclaimed_amount"]
        == total
    )
    assert result["packet_amounts"] == opened["packet_amounts"]
    assert result["remarks"] == note
    text = await service.command(caller, "5", "中奖名单", "")
    assert note in text and "退回（未支出）" in text
    status = await service.command(caller, "5", "抽奖状态", "")
    assert note in status and "未领取退回" in status
    await service.tick(adapter)
    assert latest(adapter) == result
    assert adapter.send_reply_text.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ordinary", "lucky"])
async def test_cancel_returns_entire_unallocated_pool_and_keeps_packet_plan(
    setup, mode
):
    service, adapter, _, _, _, _ = setup
    caller = await configure(setup, capacity=3)
    with activity_store.database(adapter) as db:
        db.execute(
            "UPDATE lottery_settings SET total_amount=10000,packet_mode=?", (mode,)
        )
    await service.command(caller, "5", "开启抽奖", "")
    before = latest(adapter)
    await service.command(event(adapter, "2", private=False), "5", "参加抽奖", "")
    assert "已取消" in await service.command(caller, "5", "取消抽奖", "")
    row = latest(adapter)
    assert row["results"] == "[]" and row["unclaimed_amount"] == 10000
    assert row["packet_amounts"] == before["packet_amounts"]
    assert "100.00" in adapter.send_reply_text.await_args.args[2]
    assert "退回（未支出）" in adapter.send_reply_text.await_args.args[2]


@pytest.mark.asyncio
async def test_one_of_ten_claims_ten_yuan_instead_of_all_one_hundred(setup):
    service, adapter, _, _, now, _ = setup
    caller = await configure(setup, capacity=10)
    with activity_store.database(adapter) as db:
        db.execute(
            "UPDATE lottery_settings SET total_amount=10000,packet_mode='ordinary',"
            "winners=10"
        )
    await service.command(caller, "5", "开启抽奖", "")
    await service.command(event(adapter, "2", private=False), "5", "参加抽奖", "")
    now[0] += 61
    await service.tick(adapter)
    row = latest(adapter)
    assert json.loads(row["results"])[0]["amount"] == 1000
    assert row["unclaimed_amount"] == 9000


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ordinary", "lucky"])
async def test_legacy_open_packet_keeps_promised_actual_entrant_allocation(setup, mode):
    service, adapter, _, _, now, _ = setup
    caller = await configure(setup, capacity=3)
    with activity_store.database(adapter) as db:
        db.execute(
            "UPDATE lottery_settings SET total_amount=10000,packet_mode=?", (mode,)
        )
    await service.command(caller, "5", "开启抽奖", "")
    await service.command(event(adapter, "2", private=False), "5", "参加抽奖", "")
    with activity_store.database(adapter) as db:
        db.execute("UPDATE lotteries SET packet_amounts='[]'")
        for column in ("remarks", "packet_amounts", "unclaimed_amount"):
            db.execute(f"ALTER TABLE lotteries DROP COLUMN {column}")
    now[0] += 61
    await service.tick(adapter)
    row = latest(adapter)
    assert row["packet_amounts"] == "[]" and row["unclaimed_amount"] == 0
    assert json.loads(row["results"])[0]["amount"] == 10000


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["notice", "result", "recovered_notice"])
async def test_unknown_receipts_preserve_allocations_and_unspent_without_replay(
    setup, failure
):
    service, adapter, _, _, now, _ = setup
    caller = await configure(setup, capacity=3)
    with activity_store.database(adapter) as db:
        db.execute(
            "UPDATE lottery_settings SET total_amount=300,packet_mode='ordinary'"
        )
    if failure == "notice":
        adapter.send_reply_text.return_value = "unknown"
    await service.command(caller, "5", "开启抽奖", "")
    if failure == "result":
        await service.command(event(adapter, "2", private=False), "5", "参加抽奖", "")
        adapter.send_reply_text.return_value = "unknown"
    elif failure == "recovered_notice":
        with activity_store.database(adapter) as db:
            db.execute("UPDATE lotteries SET status='announcing'")
    now[0] += 61
    await service.tick(adapter)
    row = latest(adapter)
    assert row["status"] == (
        "result_unknown" if failure == "result" else "notice_unknown"
    )
    assert row["unclaimed_amount"] == (200 if failure == "result" else 300)
    calls = adapter.send_reply_text.await_count
    await service.tick(adapter)
    assert latest(adapter) == row
    assert adapter.send_reply_text.await_count == calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "amounts", ["[0,1,2]", "[true,1,1]", "[100]", "null", "{}", "["]
)
async def test_invalid_fixed_packet_state_blocks_draw_instead_of_inflating_claims(
    setup, amounts
):
    service, adapter, _, _, now, _ = setup
    caller = await configure(setup, capacity=3)
    with activity_store.database(adapter) as db:
        db.execute(
            "UPDATE lottery_settings SET total_amount=300,packet_mode='ordinary'"
        )
    await service.command(caller, "5", "开启抽奖", "")
    with activity_store.database(adapter) as db:
        db.execute("UPDATE lotteries SET packet_amounts=?", (amounts,))
    now[0] += 61
    with pytest.raises(ProtocolError, match="lottery_amount_invalid"):
        await service.draw(adapter, latest(adapter)["id"])
    assert latest(adapter)["results"] == "[]"
    assert adapter.send_reply_text.await_count == 1


async def save_packet(client, headers, *, fields=None, invitation=False):
    current = (
        await client.get(
            f"{PREFIX}/activities",
            headers=headers,
            params={"bot_id": "fixture", "group_id": "5"},
        )
    ).json()["data"]
    payload = {
        "bot_id": "fixture",
        "group_id": "5",
        "revision": current["revision"],
    }
    if invitation:
        payload["invite_rate"] = "6.20"
    else:
        payload["lottery"] = {
            **current["lottery"],
            "prize": "玩家福利",
            "contact": "管理员",
            "capacity": 3,
            "total_amount": "10.01",
            "packet_mode": "ordinary",
            "by_invites": False,
            "invite_gate": 8,
            **(fields or {}),
        }
    return await client.post(f"{PREFIX}/activities", headers=headers, json=payload)


@pytest.mark.asyncio
async def test_split_pages_save_only_their_own_settings(page_app, headers):
    app, _, _, _, _ = page_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        lottery = await save_packet(client, headers)
        assert lottery.status_code == 200, lottery.text
        before = lottery.json()["data"]
        assert before["lottery"]["invite_gate"] == 0
        assert before["lottery"]["winners"] == 3
        invitation = await save_packet(client, headers, invitation=True)
        assert invitation.status_code == 200, invitation.text
        assert invitation.json()["data"]["lottery"] == before["lottery"]
        again = await save_packet(client, headers, fields={"packet_mode": "lucky"})
        assert again.status_code == 200, again.text
        assert again.json()["data"]["invite_rate"] == "6.20"


@pytest.mark.asyncio
async def test_remarks_save_preserve_for_legacy_clients_and_freeze_with_active_draw(
    page_app, headers
):
    app, _, _, _, root = page_app
    note = "必须带活动头像\n昵称：江哥福利 <文字仅展示>"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        saved = await save_packet(client, headers, fields={"remarks": note})
        assert saved.status_code == 200, saved.text
        current = saved.json()["data"]
        assert current["lottery"]["remarks"] == note
        old = {
            key: value for key, value in current["lottery"].items() if key != "remarks"
        }
        result = await client.post(
            f"{PREFIX}/activities",
            headers=headers,
            json={
                "bot_id": "fixture",
                "group_id": "5",
                "revision": current["revision"],
                "lottery": old,
            },
        )
        assert result.status_code == 200, result.text
        assert result.json()["data"]["lottery"]["remarks"] == note
        with sqlite3.connect(root / "activities.sqlite3") as db:
            db.execute(
                "INSERT INTO lotteries(id,account,group_id,status,created) "
                "VALUES('active','1','5','open',1000)"
            )
        rejected = await save_packet(client, headers, fields={"remarks": "临时改要求"})
        assert rejected.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "note", [None, True, [], {}, "x" * 501, "要求\u202e隐藏", "要\x00求"]
)
async def test_invalid_remarks_are_rejected_without_changing_settings(
    page_app, headers, note
):
    app, _, _, _, _ = page_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        rejected = await save_packet(client, headers, fields={"remarks": note})
        assert rejected.status_code == 400, rejected.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields",
    [
        {"total_amount": "0.02"},
        {"total_amount": "10.001"},
        {"total_amount": "NaN"},
        {"total_amount": "Infinity"},
        {"total_amount": "-1"},
        {"total_amount": True},
        {"total_amount": "1000000"},
        {"capacity": 0},
        {"capacity": 101},
        {"by_invites": True, "invite_gate": 0},
        {"by_invites": "true"},
        {"packet_mode": []},
        {"packet_mode": {}},
        {"packet_mode": "other"},
    ],
)
async def test_invalid_packet_settings_are_rejected(page_app, headers, fields):
    app, _, _, _, _ = page_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        result = await save_packet(client, headers, fields=fields)
        assert result.status_code == 400, result.text
