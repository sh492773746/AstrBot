"""Offline collection timing and shared source cooldown regressions."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from test_superbot import draw, env  # noqa: F401

from data.plugins.astrbot_plugin_superbot.game import Game
from data.plugins.astrbot_plugin_superbot.keno import Keno, KenoDeferred, poll_interval
from data.plugins.astrbot_plugin_superbot.main import Main


def test_first_seen_survives_repeat_and_reload(env):  # noqa: F811
    store, game, clock = env
    first = clock[0]
    clock[0] += 113
    game.ingest([draw(100, first)])
    Game(store)
    row = store.db.execute("SELECT * FROM draw_timing WHERE issue=100").fetchone()
    assert row["first_seen"] == first
    assert row["last_verified"] == first + 113
    assert store.db.execute("SELECT received FROM draws").fetchone()[0] == first + 113


def test_legacy_timing_does_not_invent_first_seen(env):  # noqa: F811
    store, _, _ = env
    store.db.execute("DELETE FROM draw_timing")
    Game(store)
    row = store.db.execute("SELECT * FROM draw_timing").fetchone()
    assert row["first_seen"] is None
    assert row["origin"] == "legacy"


def test_backfill_timing_is_identified(env):  # noqa: F811
    store, game, clock = env
    game.ingest([draw(99, clock[0] - 210)], historical=True)
    assert (
        store.db.execute("SELECT origin FROM draw_timing WHERE issue=99").fetchone()[0]
        == "backfill"
    )


@pytest.mark.parametrize(
    "offset,expected", [(199, 10), (200, 3), (210, 3), (300, 3), (301, 10)]
)
def test_fast_window_is_bounded(offset, expected):
    assert poll_interval(1000, 1000 + offset) == expected
    assert poll_interval(None, 1000) == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("status,header,minimum", [(429, "60", 60), (503, "", 30)])
async def test_live_and_history_share_retry_after(status, header, minimum):
    keno = Keno()
    await keno.client.aclose()
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status, headers={"Retry-After": header})

    keno.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        before = time.time()
        with pytest.raises(KenoDeferred):
            await keno.fetch()
        assert keno.retry_at >= before + minimum
        with pytest.raises(KenoDeferred):
            await keno.fetch(offset=100)
        assert len(calls) == 1
    finally:
        await keno.close()


@pytest.mark.asyncio
async def test_network_failure_cools_down_without_immediate_retry():
    keno = Keno()
    await keno.client.aclose()

    def fail(request):
        raise httpx.ConnectError("offline", request=request)

    keno.client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
    try:
        with pytest.raises(httpx.ConnectError):
            await keno.fetch()
        with pytest.raises(KenoDeferred):
            await keno.fetch()
        assert keno.failures == 1
    finally:
        await keno.close()


@pytest.mark.asyncio
async def test_slow_collection_does_not_block_settlement(env):  # noqa: F811
    store, _, _ = env
    entered, settled = asyncio.Event(), asyncio.Event()

    async def blocked_fetch():
        entered.set()
        await asyncio.Event().wait()

    runtime = SimpleNamespace(
        store=store,
        keno=SimpleNamespace(fetch=blocked_fetch, retry_at=0),
        game=SimpleNamespace(settle=settled.set, tick_chases=lambda: None),
        failure={},
        report=MagicMock(),
    )
    collector = asyncio.create_task(Main.keno_loop(runtime))
    await asyncio.wait_for(entered.wait(), 1)
    worker = asyncio.create_task(
        Main.worker(runtime, SimpleNamespace(key="game", interval=10))
    )
    try:
        await asyncio.wait_for(settled.wait(), 1)
        assert not collector.done()
    finally:
        collector.cancel()
        worker.cancel()
        await asyncio.gather(collector, worker, return_exceptions=True)
