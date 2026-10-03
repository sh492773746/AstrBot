"""Activation replies use the quoted, durable, ten_second feedback path."""

# ruff: noqa: F811

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
from telegram.error import RetryAfter, TimedOut
from test_superbot_community import community  # noqa: F401
from test_superbot_moderation import setup  # noqa: F401
from test_superbot_text_game import request, text_service, update  # noqa: F401

from data.plugins.astrbot_plugin_superbot.text_game import TextGame


@pytest.mark.asyncio
@pytest.mark.parametrize("word", ["jnd", "加拿大", " Canada ", "JND"])
async def test_activation_quoted_status_and_ten_second_deletion(text_service, word):
    s = text_service
    event = update(s, word, 1)
    await asyncio.gather(s.text.message(event, word), s.text.message(event, word))
    assert request(s, 1)["result"] == "activated"
    assert s.store.db.execute("SELECT COUNT(*) FROM gt_requests").fetchone()[0] == 1
    assert s.store.balance("2") == 1000
    assert not s.store.db.execute("SELECT 1 FROM bets").fetchone()
    await s.text.deliver("-1001")
    args = s.bot.send_message.await_args.kwargs
    assert args["reply_parameters"].message_id == 1
    assert args["reply_parameters"].allow_sending_without_reply is False
    assert "第 101 期" in args["text"] and "可下注" in args["text"]
    assert "距封盘 03:00" in args["text"]
    assert "30:00" in args["text"]
    await s.text.cleanup()
    s.bot.delete_message.assert_not_awaited()
    s.clock[0] += 9
    await s.text.cleanup()
    s.bot.delete_message.assert_not_awaited()
    s.clock[0] += 1
    await s.text.cleanup()
    assert s.bot.delete_message.await_args.kwargs["message_id"] == 900
    await s.text.message(event, word)
    await s.text.deliver("-1001")
    assert s.bot.send_message.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change,expected,known",
    [
        ("closed", "已封盘", True),
        ("stale", "数据核查", False),
        ("no_draw", "数据核查", False),
        ("conflict", "数据核查", False),
        ("missing_round", "数据核查", False),
        ("module", "游戏模块未开放", True),
        ("paused", "休息中", True),
        ("text_disabled", "文字下注暂停", True),
        ("room", "倍率未开放", True),
    ],
)
async def test_activation_reports_current_state_not_guessed_issue(
    text_service, change, expected, known
):
    s = text_service
    with s.store.tx() as db:
        if change == "closed":
            db.execute("UPDATE draws SET at=?", (s.clock[0] - 195,))
        elif change == "stale":
            db.execute("UPDATE draws SET received=?", (s.clock[0] - 61,))
        elif change == "no_draw":
            db.execute("DELETE FROM draws")
        elif change == "conflict":
            db.execute("UPDATE draws SET conflict=1")
        elif change == "missing_round":
            db.execute("UPDATE draws SET at=?", (s.clock[0] - 211,))
        elif change == "module":
            s.store.put(db, "modules", {"game": False})
        elif change == "paused":
            s.store.put(
                db, "game_hours", {"mode": "paused", "start": "20:00", "end": "02:00"}
            )
        elif change == "text_disabled":
            s.store.put(db, "text_betting_enabled", False)
        else:
            rooms = s.runtime.game.rooms()
            rooms["room28"]["enabled"] = False
            s.store.put(db, "rooms", rooms)
    await s.text.message(update(s, "jnd", 1), "jnd")
    await s.text.deliver("-1001")
    text = s.bot.send_message.await_args.kwargs["text"]
    assert expected in text
    assert ("当前期号：第 101 期" in text) is known
    if not known:
        assert "当前期号：待核实" in text


@pytest.mark.asyncio
async def test_delayed_activation_refreshes_period_and_survives_restart(text_service):
    s = text_service
    await s.text.message(update(s, "jnd", 1), "jnd")
    expiry = s.store.db.execute("SELECT expires FROM gt_sessions").fetchone()[0]
    s.clock[0] += 210
    s.store.db.execute(
        "UPDATE draws SET issue=101,at=?,received=?", (s.clock[0], s.clock[0])
    )
    s.text = TextGame(s.group)
    await s.text.deliver("-1001")
    assert "第 102 期" in s.bot.send_message.await_args.kwargs["text"]
    assert s.store.db.execute("SELECT expires FROM gt_sessions").fetchone()[0] == expiry
    assert (
        s.store.db.execute("SELECT due FROM gt_delete").fetchone()[0] == s.clock[0] + 10
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["exit", "expired", "disabled", "new_activation"])
async def test_obsolete_activation_never_sends_success(text_service, change):
    s = text_service
    await s.text.message(update(s, "jnd", 1), "jnd")
    if change == "exit":
        await s.text.message(update(s, "退出加拿大", 2), "退出加拿大")
    elif change == "expired":
        s.clock[0] += 1800
    elif change == "disabled":
        s.store.db.execute("UPDATE mod_groups SET enabled=0")
    else:
        await s.text.message(update(s, "Canada", 2), "Canada")
    await s.text.deliver("-1001")
    s.bot.send_message.assert_not_awaited()
    assert request(s, 1)["status"] in {"superseded", "blocked"}


@pytest.mark.asyncio
async def test_activation_feedback_atomic_failure_and_unknown_send(text_service):
    s = text_service
    s.store.db.execute(
        "CREATE TRIGGER fail_activation BEFORE INSERT ON gt_requests "
        "BEGIN SELECT RAISE(ABORT,'fixture'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        await s.text.message(update(s, "jnd", 1), "jnd")
    assert not s.store.db.execute("SELECT 1 FROM gt_sessions").fetchone()
    s.store.db.execute("DROP TRIGGER fail_activation")
    await s.text.message(update(s, "jnd", 1), "jnd")
    s.bot.send_message.side_effect = TimedOut()
    await s.text.deliver("-1001")
    assert request(s, 1)["status"] == "review"
    await TextGame(s.group).deliver("-1001")
    assert s.bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_activation_rate_limit_refresh_and_following_bet_order(text_service):
    s = text_service
    await s.text.message(update(s, "jnd", 1), "jnd")
    await s.text.message(update(s, "大10", 2), "大10")
    s.bot.send_message.side_effect = RetryAfter(5)
    await s.text.deliver("-1001")
    assert s.store.balance("2") == 990
    s.clock[0] += 5
    s.bot.send_message.side_effect = None
    await s.text.deliver("-1001")
    assert s.bot.send_message.await_args.kwargs["reply_parameters"].message_id == 1
    assert "02:55" in s.bot.send_message.await_args.kwargs["text"]
    s.clock[0] += 1
    s.bot.send_message.return_value = SimpleNamespace(message_id=901)
    await s.text.deliver("-1001")
    assert s.bot.send_message.await_args.kwargs["reply_parameters"].message_id == 2
    assert "已受理" in s.bot.send_message.await_args.kwargs["text"]
