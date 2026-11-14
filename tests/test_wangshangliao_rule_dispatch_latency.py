"""Local audit maintenance must not hold up newly committed rule decisions."""

import asyncio
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from tests.test_wangshangliao_content_fastlane import runtime_for
from tests.test_wangshangliao_flood import fresh_bot, ingest

bot = fresh_bot


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "busy", "locked", "other"])
async def test_receipt_maintenance_does_not_block_rule_dispatch(bot, failure):
    bot.config["moderation"].update(mute_keywords=["fixture-ad"], flood_enabled=False)
    runtime = runtime_for(bot, slots=1)
    entered, release = threading.Event(), threading.Event()
    dispatched = asyncio.Event()
    calls = []

    def reconcile(account, path):
        calls.append((account, path))
        entered.set()
        assert release.wait(3)
        if failure == "other":
            raise ValueError("offline receipt failure")
        if failure:
            exc = sqlite3.OperationalError("database is locked")
            exc.sqlite_errorcode = (
                sqlite3.SQLITE_BUSY if failure == "busy" else sqlite3.SQLITE_LOCKED
            )
            raise exc

    async def execute(*args):
        dispatched.set()

    bot._review_store = SimpleNamespace(reconcile_recalls=reconcile)
    runtime.execute = execute
    await ingest(bot, "100", "fixture-ad")
    step = asyncio.create_task(runtime.step())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        await asyncio.wait_for(dispatched.wait(), 0.25)
        await step
        identity = (bot.config["id"], bot.account)
        # Even after its interval expires, an in-flight scan is not duplicated.
        runtime.receipt_checks[identity] = time.monotonic() - 6
        await runtime.step()
        assert len(calls) == 1
        assert calls[0] == (bot.account, bot.ledger.path)
        assert not bot.calls
        release.set()
        await asyncio.gather(runtime.receipt_workers[identity], return_exceptions=True)
        if failure == "other":
            bot.diagnostics.emit.assert_any_call(
                "recall_reconcile", "failed", failed=True, error="operation_failed"
            )
        runtime.receipt_checks[identity] = time.monotonic()
        await runtime.step()
    finally:
        release.set()
        await asyncio.gather(step, return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
async def test_close_drains_receipt_thread_without_cancelling_its_wrapper(bot):
    runtime = runtime_for(bot)
    entered, release = threading.Event(), threading.Event()

    def reconcile(*args):
        entered.set()
        assert release.wait(3)

    bot._review_store = SimpleNamespace(reconcile_recalls=reconcile)
    step = asyncio.create_task(runtime.step())
    closing = None
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        await asyncio.wait_for(asyncio.shield(step), 0.25)
        task = runtime.receipt_workers[(bot.config["id"], bot.account)]
        closing = asyncio.create_task(runtime.close())
        await asyncio.sleep(0.02)
        assert not closing.done()
        assert not task.cancelled()
        release.set()
        await asyncio.wait_for(closing, 1)
        assert task.done() and not task.cancelled()
        assert not runtime.receipt_workers
    finally:
        release.set()
        await asyncio.gather(
            step, *([closing] if closing is not None else []), return_exceptions=True
        )
        await runtime.close()
