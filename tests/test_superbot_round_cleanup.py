"""Announcement-only deletion, replacement ordering and failure isolation."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, Forbidden, RetryAfter, TimedOut
from test_superbot_community import community  # noqa: F401
from test_superbot_game_hardening import flow  # noqa: F401
from test_superbot_moderation import setup  # noqa: F401

from data.plugins.astrbot_plugin_superbot.group_game import GroupGame


@pytest.mark.asyncio
async def test_replacement_only_same_group_and_kind(flow):  # noqa: F811
    s = flow
    s.bot.delete_message = AsyncMock()
    for key, chat, kind, issue, message in [
        ("old", "-1001", "round_open", 100, 10),
        ("new", "-1001", "round_open", 101, 11),
        ("result", "-1001", "round_result", 100, 12),
        ("other", "-1002", "round_open", 100, 13),
    ]:
        s.store.db.execute(
            "INSERT INTO gg_dispatch(id,chat,op,kind,issue,message,text,status) VALUES(?,?,?,?,?,?,'text','sent')",
            (key, chat, key, kind, issue, message),
        )
    s.store.db.execute("INSERT INTO gb_cleanup(job) VALUES('old')")
    s.store.db.execute("INSERT INTO gb_cleanup(job) VALUES('result')")
    s.store.db.execute("INSERT INTO gb_cleanup(job) VALUES('other')")
    await s.flow.broadcast.cleanup()
    assert s.bot.delete_message.await_count == 2
    assert {c.kwargs["message_id"] for c in s.bot.delete_message.await_args_list} == {
        10,
        12,
    }
    await s.flow.broadcast.cleanup()
    assert s.bot.delete_message.await_count == 2
    assert (
        s.store.db.execute("SELECT status FROM gb_cleanup WHERE job='old'").fetchone()[
            0
        ]
        == "deleted"
    )


@pytest.mark.asyncio
async def test_delete_bounded_retry_restart_and_rate_limit(flow):  # noqa: F811
    s = flow
    s.bot.delete_message = AsyncMock(side_effect=RetryAfter(10))
    for issue in (100, 101):
        s.store.db.execute(
            "INSERT INTO gg_dispatch(id,chat,op,kind,issue,message,text,status) VALUES(?,'-1001',?,'round_result',?,?,'text','sent')",
            (str(issue), str(issue), issue, issue),
        )
    s.store.db.execute("INSERT INTO gb_cleanup(job) VALUES('100')")
    await s.flow.broadcast.cleanup()
    assert s.store.db.execute("SELECT attempts FROM gb_cleanup").fetchone()[0] == 0
    assert (
        s.store.db.execute("SELECT status FROM gb_cleanup").fetchone()[0] == "pending"
    )
    await s.flow.broadcast.cleanup()
    assert s.bot.delete_message.await_count == 1
    s.clock[0] += 11
    s.bot.delete_message.side_effect = TimeoutError
    for attempt, delay in enumerate((5, 15, 45, 120), 1):
        await s.flow.broadcast.cleanup()
        row = s.store.db.execute("SELECT * FROM gb_cleanup").fetchone()
        assert row["status"] == "pending" and row["attempts"] == attempt
        assert row["next"] == s.clock[0] + delay
        await GroupGame(s.runtime).broadcast.cleanup()
        assert s.bot.delete_message.await_count == attempt + 1
        s.clock[0] += delay
    await s.flow.broadcast.cleanup()
    assert s.store.db.execute("SELECT status FROM gb_cleanup").fetchone()[0] == "review"
    await GroupGame(s.runtime).broadcast.cleanup()
    assert s.bot.delete_message.await_count == 6


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (BadRequest("Message to delete not found"), "deleted"),
        (BadRequest("Message can't be deleted"), "blocked"),
        (Forbidden("No permission"), "blocked"),
        (TimedOut(), "pending"),
        (ConnectionError(), "pending"),
        (ValueError("unexpected"), "review"),
    ],
)
async def test_delete_error_classification(flow, error, expected):  # noqa: F811
    s = flow
    s.bot.delete_message = AsyncMock(side_effect=error)
    for issue in (100, 101):
        s.store.db.execute(
            "INSERT INTO gg_dispatch(id,chat,op,kind,issue,message,text,status) VALUES(?,'-1001',?,'round_result',?,?,'text','sent')",
            (str(issue), str(issue), issue, issue),
        )
    s.store.db.execute("INSERT INTO gb_cleanup(job) VALUES('100')")
    await s.flow.broadcast.cleanup()
    row = s.store.db.execute("SELECT * FROM gb_cleanup").fetchone()
    assert row["status"] == expected and row["attempts"] == 1


def test_interrupted_cleanup_preserves_budget_and_historical_review(flow):  # noqa: F811
    s = flow
    for job, status, attempts in [
        ("retry", "sending", 4),
        ("exhausted", "sending", 5),
        ("old", "review", 0),
    ]:
        s.store.db.execute(
            "INSERT INTO gb_cleanup(job,status,attempts) VALUES(?,?,?)",
            (job, status, attempts),
        )
    GroupGame(s.runtime)
    rows = {r["job"]: r for r in s.store.db.execute("SELECT * FROM gb_cleanup")}
    assert rows["retry"]["status"] == "pending"
    assert rows["retry"]["attempts"] == 4
    assert rows["retry"]["next"] == s.clock[0] + 120
    assert rows["exhausted"]["status"] == rows["old"]["status"] == "review"


@pytest.mark.asyncio
async def test_cancelled_delete_resumes_after_restart(flow):  # noqa: F811
    s = flow
    s.bot.delete_message = AsyncMock(side_effect=asyncio.CancelledError())
    for issue in (100, 101):
        s.store.db.execute(
            "INSERT INTO gg_dispatch(id,chat,op,kind,issue,message,text,status) VALUES(?,'-1001',?,'round_result',?,?,'text','sent')",
            (str(issue), str(issue), issue, issue),
        )
    s.store.db.execute("INSERT INTO gb_cleanup(job) VALUES('100')")
    with pytest.raises(asyncio.CancelledError):
        await s.flow.broadcast.cleanup()
    row = s.store.db.execute("SELECT * FROM gb_cleanup").fetchone()
    assert row["status"] == "sending" and row["attempts"] == 1
    resumed = GroupGame(s.runtime)
    await resumed.broadcast.cleanup()
    assert s.bot.delete_message.await_count == 1
    s.clock[0] += 5
    s.bot.delete_message.side_effect = None
    await resumed.broadcast.cleanup()
    row = s.store.db.execute("SELECT * FROM gb_cleanup").fetchone()
    assert row["status"] == "deleted" and row["attempts"] == 2
