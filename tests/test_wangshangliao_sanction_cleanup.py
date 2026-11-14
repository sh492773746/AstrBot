"""Earned content mutes do not wait for earlier deferred recalls."""

import asyncio
import json
import threading
from unittest.mock import AsyncMock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import policy
from astrbot.builtin_stars.wangshangliao_moderation.review_store import ReviewStore
from tests import test_wangshangliao_flood as fixtures
from tests.test_wangshangliao_content_fastlane import runtime_for
from tests.test_wangshangliao_flood import drain_recalls, ingest

bot = fixtures.bot
fresh_bot = fixtures.fresh_bot


async def process(runtime):
    """Run one scheduler pass without waiting for deferred native cleanup.

    Args:
        runtime: Isolated rule runtime with fake native actions.
    """
    await runtime.step()
    await asyncio.wait_for(asyncio.gather(*runtime.workers.values()), 2)


@pytest.mark.asyncio
async def test_blocked_content_recall_does_not_delay_next_earned_mute(bot):
    bot.config["moderation"].update(mute_keywords=["fixture-ad"], flood_enabled=False)
    runtime = runtime_for(bot)
    entered, release = asyncio.Event(), asyncio.Event()
    original = bot.nim.request.side_effect

    async def slow(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    bot.nim.request.side_effect = slow
    try:
        await ingest(bot, "100", "fixture-ad")
        await process(runtime)
        assert bot.calls == [("mute", "2", 1)]
        with bot._progression_store.connect() as db:
            assert (
                json.loads(
                    db.execute("SELECT result FROM progressive_actions").fetchone()[0]
                )["recall_status"]
                == "pending"
            )
        await runtime.step()
        await asyncio.wait_for(entered.wait(), 1)
        await ingest(bot, "101", "fixture-ad")
        await process(runtime)
        assert not release.is_set()
        assert bot.calls == [("mute", "2", 1), ("mute", "2", 3)]
        release.set()
        await drain_recalls(runtime)
        assert len([c for c in bot.calls if c[0] == "recall"]) == 2
        with bot._progression_store.connect() as db:
            assert {
                json.loads(r[0])["recall_status"]
                for r in db.execute("SELECT result FROM progressive_actions")
            } == {"accepted"}
        with bot._review_store.connect() as db:
            assert db.execute(
                "SELECT status,warning FROM content_violations ORDER BY message"
            ).fetchall() == [("muted", "accepted"), ("muted", "accepted")]
    finally:
        release.set()
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt", ["accepted", "unknown", "rejected"])
async def test_only_successful_warning_recalls_advance_grace(fresh_bot, receipt):
    bot = fresh_bot
    bot.config["moderation"].update(mute_keywords=["fixture-ad"], flood_enabled=False)
    bot.nim.request.return_value = (200 if receipt == "accepted" else 403, b"")
    bot.nim.request.side_effect = TimeoutError() if receipt == "unknown" else None
    runtime = runtime_for(bot)
    try:
        for n in range(4):
            await ingest(bot, str(100 + n), "fixture-ad")
            await process(runtime)
        mutes = [c for c in bot.calls if c[0] == "mute"]
        assert mutes == ([("mute", "2", 1)] if receipt == "accepted" else [])
        with bot._progression_store.connect() as db:
            assert db.execute("SELECT warnings FROM moderation_cycles").fetchone()[
                0
            ] == (3 if receipt == "accepted" else 0)
        await drain_recalls(runtime)
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revision", "administrator", "paused", "stopping"])
async def test_content_cleanup_rechecks_authority_and_records_cancel(bot, change):
    bot.config["moderation"].update(mute_keywords=["fixture-ad"], flood_enabled=False)
    runtime = runtime_for(bot)
    try:
        await ingest(bot, "100", "fixture-ad")
        await process(runtime)
        assert bot.calls == [("mute", "2", 1)]
        if change == "revision":
            bot.config["_business_revision"] = "revoked"
        elif change == "administrator":
            bot.get_moderation_members.return_value["groupMemberInfo"][1][
                "groupRole"
            ] = "GROUP_ROLE_ADMIN"
        elif change == "paused":
            bot.config["_business_active"] = False
        else:
            bot.stopping.set()
        if change in {"paused", "stopping"}:
            await runtime.close()
        else:
            await drain_recalls(runtime)
        assert bot.calls == [("mute", "2", 1)]
        with bot._progression_store.connect() as db:
            result = json.loads(
                db.execute("SELECT result FROM progressive_actions").fetchone()[0]
            )
        assert result["status"] == "accepted"
        assert result["recall_status"] == "rejected"
        with bot._review_store.connect() as db:
            assert db.execute(
                "SELECT status,warning FROM content_violations"
            ).fetchone() == ("muted", "rejected")
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_content_cleanup_unknown_is_not_retried(bot):
    bot.config["moderation"].update(mute_keywords=["fixture-ad"], flood_enabled=False)
    runtime = runtime_for(bot)
    bot.nim.request.side_effect = TimeoutError()
    payload = await ingest(bot, "100", "fixture-ad")
    try:
        await process(runtime)
        await drain_recalls(runtime)
        assert bot.nim.request.await_count == 1
        assert len([c for c in bot.calls if c[0] == "mute"]) == 1
        with bot._review_store.connect() as db:
            assert db.execute(
                "SELECT status,warning FROM content_violations"
            ).fetchone() == ("muted", "unknown")
        await policy.handle(bot, "5", "100", payload)
        await process(runtime)
        await drain_recalls(runtime)
        assert bot.nim.request.await_count == 1
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_final_mute_cycle_continuations_only_queue_recalls(bot):
    bot.config["moderation"].update(mute_keywords=["fixture-ad"], flood_enabled=False)
    runtime = runtime_for(bot)
    try:
        for mid in ("100", "101", "102"):
            await ingest(bot, mid, "fixture-ad")
            await process(runtime)
        assert [c for c in bot.calls if c[0] == "mute"] == [
            ("mute", "2", 1),
            ("mute", "2", 3),
            ("mute", "2", 10),
        ]
        await drain_recalls(runtime)
        before = len(bot.calls)
        await ingest(bot, "103", "fixture-ad")
        await process(runtime)
        assert len(bot.calls) == before
        with bot._review_store.connect() as db:
            assert db.execute(
                "SELECT status,warning FROM content_violations WHERE message='103'"
            ).fetchone() == ("recall_pending", "pending")
        await drain_recalls(runtime)
        assert bot.calls[-1] == ("recall", "103", "22")
        with bot._progression_store.connect() as db:
            result = json.loads(
                db.execute(
                    "SELECT result FROM progressive_actions WHERE message='103'"
                ).fetchone()[0]
            )
        assert result["cycle_wait"] is True
        assert result["status"] == result["recall_status"] == "accepted"
        assert len([c for c in bot.calls if c[0] == "mute"]) == 3
        with bot._review_store.connect() as db:
            assert db.execute(
                "SELECT status,warning FROM content_violations WHERE message='103'"
            ).fetchone() == ("recalled", "accepted")
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_content_cleanup_full_queue_preserves_inline_fallback(bot):
    bot.config["moderation"].update(mute_keywords=["fixture-ad"], flood_enabled=False)
    runtime = runtime_for(bot, backlog=1)
    release = asyncio.Event()
    unrelated = AsyncMock(side_effect=release.wait)
    assert runtime.defer_recalls(bot, 1, unrelated)
    try:
        await ingest(bot, "100", "fixture-ad")
        await process(runtime)
        assert bot.calls == [("mute", "2", 1), ("recall", "100", "22")]
        with bot._progression_store.connect() as db:
            assert (
                json.loads(
                    db.execute("SELECT result FROM progressive_actions").fetchone()[0]
                )["recall_status"]
                == "accepted"
            )
    finally:
        release.set()
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["queued", "waiting"])
@pytest.mark.parametrize("repeat_cancel", [False, True])
async def test_cancel_drains_initial_audit_before_closing_cleanup(
    bot, phase, repeat_cancel
):
    bot.config["moderation"].update(mute_keywords=["fixture-ad"], flood_enabled=False)
    store = bot._review_store = ReviewStore(
        bot.ledger.path.parent / "moderation.sqlite3"
    )
    finish = store.finish
    entered, release = threading.Event(), threading.Event()

    def slow_finish(*args, **kwargs):
        if args[3:5] == ("muted", "pending"):
            entered.set()
            assert release.wait(5)
        return finish(*args, **kwargs)

    store.finish = slow_finish
    runtime = runtime_for(bot)
    closing = None
    try:
        await ingest(bot, "100", "fixture-ad")
        await runtime.step()
        assert await asyncio.to_thread(entered.wait, 2)
        worker = next(iter(runtime.workers.values()))
        if phase == "waiting":
            await runtime.step()
            await asyncio.sleep(0)
            assert runtime.recall_workers
            assert not runtime.recalls
        closing = asyncio.create_task(runtime.close())
        await asyncio.sleep(0.02)
        if repeat_cancel:
            worker.cancel()
            await asyncio.sleep(0.02)
        assert not closing.done()
        bot.nim.request.assert_not_awaited()
        release.set()
        await asyncio.wait_for(closing, 3)
        with bot._progression_store.connect() as db:
            result = json.loads(
                db.execute("SELECT result FROM progressive_actions").fetchone()[0]
            )
        assert result["status"] == "accepted"
        assert result["recall_status"] == "rejected"
        with store.connect() as db:
            assert db.execute(
                "SELECT status,warning FROM content_violations"
            ).fetchone() == ("muted", "rejected")
        bot.nim.request.assert_not_awaited()
    finally:
        release.set()
        if closing is not None:
            await closing
        await runtime.close()


@pytest.mark.asyncio
async def test_cancel_submitted_cleanup_keeps_unknown_without_retry(bot):
    bot.config["moderation"].update(mute_keywords=["fixture-ad"], flood_enabled=False)
    runtime = runtime_for(bot)
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(*args):
        entered.set()
        await release.wait()
        return 200, b""

    bot.nim.request.side_effect = slow
    payload = await ingest(bot, "100", "fixture-ad")
    try:
        await process(runtime)
        await runtime.step()
        await asyncio.wait_for(entered.wait(), 1)
        await runtime.close()
        assert bot.nim.request.await_count == 1
        with bot._progression_store.connect() as db:
            result = json.loads(
                db.execute("SELECT result FROM progressive_actions").fetchone()[0]
            )
        assert result["status"] == "accepted"
        assert result["recall_status"] == "unknown"
        with bot._review_store.connect() as db:
            assert db.execute(
                "SELECT status,warning FROM content_violations"
            ).fetchone() == ("muted", "unknown")
        await policy.handle(bot, "5", "100", payload)
        assert bot.nim.request.await_count == 1
    finally:
        release.set()
        await runtime.close()
