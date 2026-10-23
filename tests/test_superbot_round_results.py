"""Nonempty settled rounds and private history expire without replay or leakage."""

# ruff: noqa: F811

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, RetryAfter, TimedOut
from test_superbot import draw
from test_superbot_community import community  # noqa: F401
from test_superbot_moderation import setup  # noqa: F401
from test_superbot_text_game import accept, request, text_service, update  # noqa: F401

from data.plugins.astrbot_plugin_superbot.round_results import RoundResults
from data.plugins.astrbot_plugin_superbot.text_game import TextGame


async def settled(s):
    """Seed a real atomic group order and settle against a mock canonical draw."""
    event = update(s, "大10大5", 10)
    event.effective_user.full_name = "玩家甲"
    await accept(s, "jnd", 1)
    await s.text.message(event, event.message.text)
    s.store.db.execute("UPDATE gt_requests SET status='sent'")
    s.clock[0] += 210
    s.runtime.game.ingest([draw(101, s.clock[0])])


@pytest.mark.asyncio
async def test_nonempty_round_once_public_summary_and_thirty_second_delete(
    text_service,
):
    s = text_service
    await settled(s)
    balance = s.store.balance("2")
    s.text.results.tick()
    s.text.results.tick()
    row = s.store.db.execute(
        "SELECT * FROM gt_requests WHERE result='settlement'"
    ).fetchone()
    assert row["source"] < 0
    assert "玩家甲" in row["text"] and "参与 1 人" in row["text"]
    assert "投注15" in row["text"] and "第 101 期结算名单" in row["text"]
    await s.text.deliver("-1001")
    args = s.bot.send_message.await_args.kwargs
    assert "api_kwargs" not in args and "reply_parameters" not in args
    s.clock[0] += 29
    await s.text.cleanup()
    s.bot.delete_message.assert_not_awaited()
    s.clock[0] += 1
    await s.text.cleanup()
    assert s.bot.delete_message.await_args.kwargs["message_id"] == 900
    s.text = TextGame(s.group)
    s.text.results.tick()
    await s.text.deliver("-1001")
    assert s.bot.send_message.await_count == 1
    assert s.store.balance("2") == balance


@pytest.mark.asyncio
async def test_no_players_or_only_activation_never_publishes(text_service):
    s = text_service
    await accept(s, "jnd", 1)
    s.clock[0] += 210
    s.runtime.game.ingest([draw(101, s.clock[0])])
    s.text.results.tick()
    assert not s.store.db.execute("SELECT 1 FROM gt_round_results").fetchone()
    assert not s.store.db.execute(
        "SELECT 1 FROM gt_requests WHERE result='settlement'"
    ).fetchone()


@pytest.mark.asyncio
async def test_waits_for_every_bet_and_has_group_isolation(text_service):
    s = text_service
    await settled(s)
    s.store.db.execute("UPDATE bets SET status='pending' WHERE amount=5")
    s.text.results.tick()
    assert not s.store.db.execute(
        "SELECT 1 FROM gt_requests WHERE result='settlement'"
    ).fetchone()
    s.store.db.execute("UPDATE bets SET status='lose' WHERE amount=5")
    s.store.db.execute(
        "INSERT INTO mod_groups(chat,title,enabled) VALUES('-1002','Empty group',1)"
    )
    s.text.results.tick()
    rows = s.store.db.execute(
        "SELECT chat FROM gt_requests WHERE result='settlement'"
    ).fetchall()
    assert [r["chat"] for r in rows] == ["-1001"]


@pytest.mark.asyncio
async def test_upgrade_does_not_replay_completed_history(text_service):
    s = text_service
    await settled(s)
    with s.store.tx() as db:
        s.store.put(db, "gt_results_initialized", False)
    service = RoundResults(s.group)
    service.tick()
    assert (
        s.store.db.execute("SELECT status FROM gt_round_results").fetchone()[0]
        == "historical"
    )
    assert not s.store.db.execute(
        "SELECT 1 FROM gt_requests WHERE result='settlement'"
    ).fetchone()


@pytest.mark.asyncio
async def test_upgrade_keeps_inflight_round_eligible(text_service):
    s = text_service
    await accept(s, "jnd", 1)
    await accept(s, "大10", 10)
    with s.store.tx() as db:
        s.store.put(db, "gt_results_initialized", False)
    service = RoundResults(s.group)
    assert not s.store.db.execute("SELECT 1 FROM gt_round_results").fetchone()
    s.clock[0] += 210
    s.runtime.game.ingest([draw(101, s.clock[0])])
    service.tick()
    assert (
        s.store.db.execute(
            "SELECT COUNT(*) FROM gt_requests WHERE result='settlement'"
        ).fetchone()[0]
        == 1
    )


@pytest.mark.asyncio
async def test_disabled_group_not_replayed_after_reenable(text_service):
    s = text_service
    await settled(s)
    s.store.db.execute("UPDATE mod_groups SET enabled=0")
    s.text.results.tick()
    s.store.db.execute("UPDATE mod_groups SET enabled=1")
    s.text.results.tick()
    assert (
        s.store.db.execute("SELECT status FROM gt_round_results").fetchone()[0]
        == "suppressed"
    )
    assert not s.store.db.execute(
        "SELECT 1 FROM gt_requests WHERE result='settlement'"
    ).fetchone()


@pytest.mark.asyncio
async def test_summary_unknown_send_never_retries(text_service):
    s = text_service
    await settled(s)
    s.text.results.tick()
    s.bot.send_message.side_effect = TimedOut()
    await s.text.deliver("-1001")
    s.text = TextGame(s.group)
    s.text.results.tick()
    await s.text.deliver("-1001")
    assert s.bot.send_message.await_count == 1
    assert (
        s.store.db.execute(
            "SELECT status FROM gt_requests WHERE result='settlement'"
        ).fetchone()[0]
        == "review"
    )


@pytest.mark.asyncio
async def test_private_bets_quote_receiver_and_dedicated_delete(text_service):
    s = text_service
    s.bot._post = AsyncMock(return_value=True)
    s.bot.send_message.return_value = SimpleNamespace(
        message_id=0, api_kwargs={"ephemeral_message_id": 55}
    )
    event = update(s, "/bets")
    await s.group.message(event, "/bets", "/bets")
    await s.group.message(event, "/bets", "/bets")
    await s.text.deliver("-1001")
    args = s.bot.send_message.await_args.kwargs
    assert args["api_kwargs"] == {
        "ephemeral_message_parameters": {"receiver_user_id": 2}
    }
    assert args["reply_parameters"].message_id == 10
    assert "尚未进入下注房间" in args["text"]
    assert s.bot.send_message.await_count == 1
    job = s.store.db.execute("SELECT * FROM gt_delete").fetchone()
    assert job["message"] < 0 and job["ephemeral"] == 55 and job["receiver"] == "2"
    s.clock[0] += 9
    await s.text.cleanup()
    s.bot._post.assert_not_awaited()
    s.clock[0] += 1
    await TextGame(s.group).cleanup()
    assert s.bot._post.await_args.args == ("deleteEphemeralMessage",)
    assert s.bot._post.await_args.kwargs["data"] == {
        "chat_id": "-1001",
        "receiver_user_id": 2,
        "ephemeral_message_id": 55,
    }
    s.bot.delete_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,status", [(TimedOut(), "review"), (BadRequest("unsupported"), "blocked")]
)
async def test_private_send_never_falls_back_to_public(text_service, error, status):
    s = text_service
    await s.group.message(update(s, "/bets"), "/bets", "/bets")
    s.bot.send_message.side_effect = error
    await s.text.deliver("-1001")
    await s.text.deliver("-1001")
    assert s.bot.send_message.await_count == 1
    assert request(s)["status"] == status
    assert not s.store.db.execute("SELECT 1 FROM gt_delete").fetchone()


@pytest.mark.asyncio
async def test_ephemeral_and_public_ids_do_not_collide_and_retry_scoped(text_service):
    s = text_service
    s.bot._post = AsyncMock(side_effect=RetryAfter(5))
    s.bot.send_message.return_value = SimpleNamespace(
        message_id=0, ephemeral_message_id=55
    )
    await s.group.message(update(s, "/bets"), "/bets", "/bets")
    await s.text.deliver("-1001")
    s.store.db.execute(
        "INSERT INTO gt_delete(chat,message,source,due,next) VALUES('-1001',55,11,?,?)",
        (s.clock[0] + 10, s.clock[0] + 10),
    )
    s.clock[0] += 10
    await s.text.cleanup()
    rows = s.store.db.execute("SELECT * FROM gt_delete ORDER BY message").fetchall()
    assert rows[0]["status"] == "pending" and rows[0]["attempts"] == 0
    assert rows[1]["status"] == "deleted"
    s.clock[0] += 5
    s.bot._post.side_effect = BadRequest("MESSAGE_ID_INVALID")
    await s.text.cleanup()
    assert (
        s.store.db.execute(
            "SELECT COUNT(*) FROM gt_delete WHERE status='deleted'"
        ).fetchone()[0]
        == 2
    )


@pytest.mark.asyncio
async def test_large_roster_pages_are_complete_and_idempotent(text_service):
    s = text_service
    await settled(s)
    s.group.broadcast.roster = lambda chat, issue: [
        {"uid": str(i), "name": "名字" * 30, "n": 1, "stake": 10, "payout": 20}
        for i in range(45)
    ]
    s.text.results.tick()
    s.text.results.tick()
    rows = s.store.db.execute(
        "SELECT * FROM gt_requests WHERE result='settlement' ORDER BY rowid"
    ).fetchall()
    assert len(rows) == 3
    assert [r["text"].count("净变动") for r in rows] == [20, 20, 5]
    assert all(len(r["text"].encode("utf-16-le")) // 2 < 4096 for r in rows)


@pytest.mark.asyncio
async def test_bets_history_and_ephemeral_cleanup_are_user_scoped(text_service):
    s = text_service
    await settled(s)
    s.bot._post = AsyncMock(return_value=True)
    s.bot.send_message.return_value = SimpleNamespace(
        message_id=0, ephemeral_message_id=55
    )
    await s.group.message(update(s, "/bets", 20), "/bets", "/bets")
    await s.group.message(update(s, "/bets", 21, uid=3), "/bets", "/bets")
    assert "加拿大28" in request(s, 20)["text"]
    assert "第 101 期 · 投注15" in request(s, 20)["text"]
    assert "尚未进入下注房间" in request(s, 21)["text"]
    await s.text.deliver("-1001")
    s.clock[0] += 1
    await s.text.deliver("-1001")
    s.clock[0] += 10
    await s.text.cleanup()
    assert {
        call.kwargs["data"]["receiver_user_id"] for call in s.bot._post.await_args_list
    } == {2, 3}
    assert len(s.bot._post.await_args_list) == 2
    s.bot.delete_message.assert_not_awaited()
