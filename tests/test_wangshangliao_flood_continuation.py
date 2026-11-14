"""Overlapping muted bursts avoid redundant preflight, not native authorization."""

import asyncio
import json
import math
import os
import time

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.review_store import ReviewStore
from tests import test_wangshangliao_flood as fixtures
from tests.test_wangshangliao_content_fastlane import runtime_for
from tests.test_wangshangliao_flood import drain_recalls, ingest

bot = fixtures.bot
fresh_bot = fixtures.fresh_bot


@pytest.mark.parametrize("status", ["accepted", "verified", "rejected", "unknown"])
@pytest.mark.parametrize(
    "content_status", ["muted", "execution_rejected", "unknown", "observed", "recalled"]
)
def test_only_completed_mute_batches_skip_preliminary_reads(
    tmp_path, status, content_status
):
    store = ReviewStore(tmp_path / "moderation.sqlite3")
    scope = ("1", "5", "2", "22", "revision")
    store.observe("1", "5", "104", "2", "message_flood", 1000, 900)
    store.reserve_flood("batch", *scope, "104", 1000, ["104"])
    store.finish_flood("batch", status)
    store.finish("1", "5", "104", content_status, "none")
    assert store.flood_continuation(*scope, 1001) is (
        content_status in {"muted", "execution_rejected", "unknown"}
    )
    store.finish_flood("batch", "none")
    assert not store.flood_continuation(*scope, 1001)


@pytest.mark.parametrize(
    "change",
    ["account", "group", "sender", "peer", "revision", "expired", "test_scope"],
)
def test_continuation_cannot_cross_identity_or_policy_scope(tmp_path, change):
    store = ReviewStore(tmp_path / "moderation.sqlite3")
    scope = ["1", "5", "2", "22", "revision"]
    store.observe("1", "5", "104", "2", "message_flood", 1000, 900)
    store.reserve_flood("batch", *scope, "104", 1000, ["104"], scope="fixture-a")
    store.finish_flood("batch", "unknown")
    store.finish("1", "5", "104", "unknown", "pending")
    assert store.flood_continuation(*scope, 1001, scope="fixture-a")
    stamp, test_scope = 1001, "fixture-a"
    if change in {"account", "group", "sender", "peer", "revision"}:
        scope[["account", "group", "sender", "peer", "revision"].index(change)] = "9"
    elif change == "expired":
        stamp = 1030.001
    else:
        test_scope = "fixture-b"
    assert not store.flood_continuation(*scope, stamp, scope=test_scope)


def test_latest_warning_batch_never_inherits_an_older_mute(tmp_path):
    store = ReviewStore(tmp_path / "moderation.sqlite3")
    scope = ("1", "5", "2", "22", "revision")
    for operation, stamp, status, content_status in (
        ("older", 1000, "accepted", "muted"),
        ("latest", 1031, "none", "recalled"),
    ):
        store.observe("1", "5", operation, "2", "message_flood", stamp, 900)
        store.reserve_flood(operation, *scope, operation, stamp, [operation])
        store.finish_flood(operation, status)
        store.finish("1", "5", operation, content_status, "accepted")
    assert not store.flood_continuation(*scope, 1032)


@pytest.mark.asyncio
@pytest.mark.parametrize("mute_status", ["accepted", "unknown"])
async def test_eighty_five_messages_keep_every_recall_without_repeated_preflight(
    bot, mute_status
):
    runtime = runtime_for(bot)
    bot.diagnostics.gauges = {}
    original = bot.get_moderation_members.return_value
    preflight, native = [], []

    async def directory(group, **kwargs):
        (preflight if kwargs else native).append(group)
        if kwargs:
            await asyncio.sleep(0.002)
        return original

    bot.get_moderation_members.side_effect = directory
    if mute_status == "unknown":
        bot.execute_moderation.side_effect = None
        bot.execute_moderation.return_value = {"status": "unknown"}
    try:
        for number in range(85):
            mid = str(100 + number)
            payload = await ingest(bot, mid, "", kind="image")
            if number >= 4:
                await runtime.execute(
                    ("fixture", "1", "5", "2"), bot, mid, payload, None
                )
        await drain_recalls(runtime)
        assert preflight == ["5"]
        assert len(native) == 85
        assert bot.execute_moderation.await_count == 1
        assert sorted(c[1] for c in bot.calls if c[0] == "recall") == [
            str(100 + number) for number in range(85)
        ]
        assert bot.diagnostics.gauges["flood_preflight_reused"] == 80
        with bot._review_store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM flood_batches").fetchone() == (1,)
            assert db.execute(
                "SELECT COUNT(*) FROM flood_recalls WHERE status='accepted'"
            ).fetchone() == (85,)
            assert db.execute("SELECT mute_status FROM flood_batches").fetchone() == (
                mute_status,
            )
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("baseline", [True, False])
async def test_offline_1000_messages_with_slow_preflight(bot, monkeypatch, baseline):
    if os.environ.get("WSL_OFFLINE_LATENCY_BENCHMARK") != "1":
        pytest.skip("Set WSL_OFFLINE_LATENCY_BENCHMARK=1 for the offline benchmark")
    runtime = runtime_for(bot)
    bot.diagnostics.gauges = {}
    roster = bot.get_moderation_members.return_value
    preflight, native = [], []
    completed, waits = [], []
    started = asyncio.Event()
    done = asyncio.Event()
    if baseline:
        monkeypatch.setattr(ReviewStore, "flood_continuation", lambda *a, **k: False)

    async def directory(group, **kwargs):
        (preflight if kwargs else native).append(group)
        if kwargs:
            await asyncio.sleep(0.01)
        return roster

    bot.get_moderation_members.side_effect = directory
    original = runtime.execute

    async def measured(key, adapter, mid, payload, epoch):
        waits.append(time.time() - payload["_ingress_flood"]["received_at"])
        await original(key, adapter, mid, payload, epoch)
        completed.append(mid)
        started.set()
        if len(completed) == 96:
            done.set()

    runtime.execute = measured
    runner = None
    try:
        for number in range(1000):
            await ingest(
                bot,
                str(1000 + number),
                "" if number >= 900 else f"ordinary-{number}",
                sender="2" if number >= 900 else str(10000 + number),
                kind="image" if number >= 900 else None,
            )
        begin = time.perf_counter()
        runner = asyncio.create_task(runtime.run())
        await asyncio.wait_for(started.wait(), 10)
        await asyncio.wait_for(done.wait(), 20)
        elapsed = (time.perf_counter() - begin) * 1000
        await drain_recalls(runtime)
        assert len(preflight) == (96 if baseline else 1)
        assert len(native) == 100
        assert bot.execute_moderation.await_count == 1
        assert len(completed) == len(set(completed)) == 96
        assert len([c for c in bot.calls if c[0] == "recall"]) == 100
        ordered = sorted(waits)
        print(
            "OFFLINE_FLOOD_CONTINUATION "
            + json.dumps(
                {
                    "baseline": baseline,
                    "messages": 1000,
                    "benign": 900,
                    "images": 100,
                    "preflight_delay_ms": 10,
                    "preflight_reads": len(preflight),
                    "native_recall_checks": len(native),
                    "mute_submissions": bot.execute_moderation.await_count,
                    "rule_completion_ms": round(elapsed, 3),
                    "rule_wait_p95_ms": round(
                        ordered[math.ceil(len(ordered) * 0.95) - 1] * 1000, 3
                    ),
                    "rule_wait_p99_ms": round(
                        ordered[math.ceil(len(ordered) * 0.99) - 1] * 1000, 3
                    ),
                }
            )
        )
    finally:
        if runner is not None:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["promoted", "peer", "permission", "stopping"])
async def test_continuation_still_blocks_new_effects_after_revocation(bot, change):
    runtime = runtime_for(bot)
    try:
        for number in range(5):
            payload = await ingest(bot, str(100 + number), "repeat")
        await runtime.execute(("fixture", "1", "5", "2"), bot, "104", payload, None)
        await drain_recalls(runtime)
        original_calls = list(bot.calls)
        payload = await ingest(bot, "105", "repeat")
        if change == "promoted":
            bot.get_moderation_members.return_value["groupMemberInfo"][1][
                "groupRole"
            ] = "GROUP_ROLE_ADMIN"
        elif change == "peer":
            bot.members["5"]["2"] = "99"
        elif change == "permission":
            bot.config["moderation"]["permissions"] = {}
        else:
            bot.stopping.set()
        await runtime.execute(("fixture", "1", "5", "2"), bot, "105", payload, None)
        await drain_recalls(runtime)
        assert bot.calls == original_calls
        assert bot.execute_moderation.await_count == 1
    finally:
        await runtime.close()
