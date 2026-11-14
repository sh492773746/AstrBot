"""Cleanup backpressure never occupies the member's sanction worker."""

import asyncio
import json
import math
import os
import socket
import sqlite3
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.review_store import ReviewStore
from tests import test_wangshangliao_flood as fixtures
from tests.test_wangshangliao_content_fastlane import runtime_for
from tests.test_wangshangliao_flood import drain_recalls, ingest

bot = fixtures.bot
fresh_bot = fixtures.fresh_bot


@pytest.mark.asyncio
async def test_blocked_cleanup_cannot_delay_the_same_members_next_rule_check(bot):
    runtime = runtime_for(bot)
    entered, release = asyncio.Event(), asyncio.Event()
    original = bot.execute_action

    async def delayed(request, **kwargs):
        if request.action == "recall":
            entered.set()
            await release.wait()
        return await original(request, **kwargs)

    bot.execute_action = AsyncMock(side_effect=delayed)
    try:
        for n in range(5):
            await ingest(bot, str(9000 + n), "", kind="image")
        await runtime.step()
        await asyncio.wait_for(asyncio.gather(*runtime.workers.values()), 1)
        assert bot.calls == [("mute", "2", 1)]
        await runtime.step()
        await asyncio.wait_for(entered.wait(), 1)
        await ingest(bot, "9010", "", kind="image")
        await runtime.step()
        await asyncio.wait_for(asyncio.gather(*runtime.workers.values()), 1)
        assert not runtime.pending
        assert len(bot.calls) == 1
        assert runtime.recall_counts[("fixture", "1")] >= 5
        release.set()
        await drain_recalls(runtime)
        assert len([r for r in bot.calls if r[0] == "recall"]) == 6
        assert len([r for r in bot.calls if r[0] == "mute"]) == 1
    finally:
        release.set()
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["paused", "revision", "administrator", "stopping"])
async def test_deferred_cleanup_rechecks_native_and_business_authority(bot, change):
    runtime = runtime_for(bot)
    for n in range(5):
        await ingest(bot, str(9100 + n), "", kind="image")
    try:
        await runtime.step()
        await asyncio.gather(*runtime.workers.values())
        assert bot.calls == [("mute", "2", 1)]
        if change == "paused":
            bot.config["_business_active"] = False
        elif change == "revision":
            bot.config["_business_revision"] = "revoked"
        elif change == "stopping":
            bot.stopping.set()
        else:
            bot.get_moderation_members.return_value["groupMemberInfo"][1][
                "groupRole"
            ] = "GROUP_ROLE_ADMIN"
        if change in {"paused", "stopping"}:
            await runtime.close()
        else:
            await drain_recalls(runtime)
        assert bot.calls == [("mute", "2", 1)]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_cleanup_admission_is_bounded_and_close_never_replays(bot):
    runtime = runtime_for(bot, backlog=2)
    work = AsyncMock()
    assert runtime.defer_recalls(bot, 2, work)
    assert not runtime.defer_recalls(bot, 1, work)
    await runtime.close()
    work.assert_not_awaited()
    assert not runtime.recalls
    assert not runtime.recall_workers
    assert not runtime.recall_counts


@pytest.mark.asyncio
async def test_duplicate_recall_waits_for_original_receipt_without_resubmission(
    fresh_bot,
):
    from astrbot.builtin_stars.wangshangliao_moderation.action_receipts import (
        ModerationActions,
    )
    from astrbot.core.platform.sources.wangshangliao.actions import ActionRequest

    bot = fresh_bot
    await ingest(bot, "9190", "", kind="image")
    entered, release = asyncio.Event(), asyncio.Event()
    original = bot.nim.request.side_effect

    async def slow(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    bot.nim.request.side_effect = slow
    request = ActionRequest.bind(
        bot,
        "recall/1/5/9190",
        "recall",
        5,
        member=2,
        expected_nim="22",
        message="9190",
    )
    owner = asyncio.create_task(bot.execute_action(request))
    follower = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        follower = asyncio.create_task(
            ModerationActions(bot, "5").execute_action(request)
        )
        await asyncio.sleep(0.02)
        assert not follower.done()
        release.set()
        assert (await owner)["status"] == (await follower)["status"] == "accepted"
        assert bot.nim.request.await_count == 1
        with bot._review_store.connect() as db:
            assert db.execute(
                "SELECT status,submitted FROM moderation_action_audit"
            ).fetchone() == ("accepted", 1)
    finally:
        release.set()
        await asyncio.gather(
            *(t for t in (owner, follower) if t), return_exceptions=True
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["accepted", "rejected", "sending", "unknown"])
@pytest.mark.parametrize("match", [True, False])
async def test_only_matching_terminal_receipts_refine_unknown_audit(
    fresh_bot, state, match
):
    bot = fresh_bot
    payload = await ingest(bot, "9200", "", kind="image")
    root = bot.ledger.path.parent
    store = ReviewStore(root / "moderation.sqlite3")
    operation = "recall/1/5/9200"
    route = payload["recall_route"]
    await bot.ledger.reserve(
        operation, json.dumps(route, sort_keys=True) if match else "{}"
    )
    await bot.ledger.finish(operation, state)
    store.save_action(
        operation,
        "1",
        "5",
        "2",
        "9200",
        "recall",
        "unknown",
        None,
        "result_unknown",
        None,
    )
    with store.connect() as db:
        created = db.execute("SELECT created FROM moderation_action_audit").fetchone()[
            0
        ]
    expected = match and state in {"accepted", "rejected"}
    assert store.reconcile_recalls("1", bot.ledger.path) == int(expected)
    with store.connect() as db:
        row = db.execute(
            "SELECT status,created FROM moderation_action_audit"
        ).fetchone()
    assert row == (state if expected else "unknown", created)
    assert not bot.calls


@pytest.mark.asyncio
async def test_unknown_cleanup_is_never_retried_after_replacement(bot):
    runtime = runtime_for(bot)
    bot.nim.request.side_effect = TimeoutError()
    for n in range(5):
        await ingest(bot, str(9300 + n), "", kind="image")
    try:
        await runtime.step()
        await asyncio.gather(*runtime.workers.values())
        await drain_recalls(runtime)
        assert bot.nim.request.await_count == 5
    finally:
        await runtime.close()
    replacement = runtime_for(bot)
    try:
        await replacement.step()
        await asyncio.gather(*replacement.workers.values())
        await drain_recalls(replacement)
        assert bot.nim.request.await_count == 5
    finally:
        await replacement.close()


@pytest.mark.asyncio
async def test_busy_receipt_maintenance_does_not_hold_rule_dispatch(fresh_bot):
    bot = fresh_bot
    payload = await ingest(bot, "9390", "", kind="image")
    store = ReviewStore(bot.ledger.path.parent / "moderation.sqlite3")
    operation = "recall/1/5/9390"
    await bot.ledger.reserve(
        operation, json.dumps(payload["recall_route"], sort_keys=True)
    )
    await bot.ledger.finish(operation, "accepted")
    store.save_action(
        operation, "1", "5", "2", "9390", "recall", "unknown", None, "", None
    )
    bot._review_store = store
    runtime = runtime_for(bot)
    db = sqlite3.connect(store.path)
    try:
        db.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        await asyncio.wait_for(runtime.step(), 0.5)
        assert time.monotonic() - started < 0.5
        assert store.content_outcome("1", "5", "9390") == "not_applied"
        await asyncio.wait_for(
            runtime.receipt_workers[(bot.config["id"], bot.account)], 0.5
        )
        bot.diagnostics.emit.assert_any_call(
            "recall_reconcile", "busy", failed=False, error=""
        )
    finally:
        db.rollback()
        db.close()
        await runtime.close()


@pytest.mark.asyncio
async def test_group_role_resolution_has_a_total_budget_without_guessing_roles(
    monkeypatch,
):
    from astrbot.core.platform.sources.wangshangliao import directory

    clock = iter([0, 1, 9])
    monkeypatch.setattr(
        directory,
        "time",
        SimpleNamespace(monotonic=lambda: next(clock), time=lambda: 100),
    )
    read = AsyncMock(side_effect=TimeoutError())
    monkeypatch.setattr(directory, "member_directory", read)
    client = SimpleNamespace(
        request=AsyncMock(
            return_value={
                "owner": [],
                "member": [{"groupId": 5}, {"groupId": 6}],
            }
        )
    )
    result = await directory.group_directory(client, "1")
    assert [g["role"] for g in result["groups"]] == ["unknown", "unknown"]
    assert [g["role_reason"] for g in result["groups"]] == [
        "member_request_timeout",
        "role_resolution_deferred",
    ]
    assert read.await_count == 1
    assert result["complete"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["baseline", "candidate"])
@pytest.mark.parametrize("scenario", ["mixed_flood", "content_burst"])
async def test_opt_in_1000_message_burst_replay(
    fresh_bot, monkeypatch, variant, scenario, record_property
):
    """Measure local intake and rule work separately from simulated native IO.

    Args:
        fresh_bot: Fully isolated inbox and fake native action port.
        monkeypatch: Restore benchmark-only fake network timing.
        variant: Archived rule scheduler or candidate scheduler.
        scenario: Existing mixed flood or independently moderated content burst.
        record_property: Store measurements in the JUnit evidence.
    """
    if os.environ.get("WSL_BURST_BENCHMARK") != "1":
        pytest.skip("Opt-in bounded offline benchmark")
    from astrbot.builtin_stars.wangshangliao_moderation.flood import FloodRuntime

    def forbidden(*args, **kwargs):
        raise AssertionError("Offline benchmark must not access the network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    bot = fresh_bot
    bot.config["moderation"]["mute_keywords"] = ["fixture-ad"]
    if scenario == "content_burst":
        bot.config["moderation"]["flood_enabled"] = False
    bot.members["5"]["3"] = "33"
    bot.get_moderation_members.return_value["groupMemberInfo"].append(
        {"userId": "3", "nimId": "33", "groupRole": "GROUP_ROLE_MEMBER"}
    )
    original_roster = bot.get_moderation_members.return_value
    last = 0

    async def roster(group, *, max_age=0):
        nonlocal last
        if not max_age or time.monotonic() - last > max_age:
            await asyncio.sleep(0.005)
            last = time.monotonic()
        return original_roster

    original_recall = bot.nim.request.side_effect

    async def recall(*args):
        await asyncio.sleep(0.005)
        return await original_recall(*args)

    bot.get_moderation_members.side_effect = roster
    bot.nim.request.side_effect = recall
    cls = FloodRuntime
    if variant == "baseline":
        from astrbot.core.platform.sources.wangshangliao import progressive

        baseline = os.environ.get("WSL_BURST_BASELINE", "")
        if not baseline or not Path(baseline).is_file():
            pytest.skip("Provide WSL_BURST_BASELINE source archive for comparison")
        with tarfile.open(baseline) as archive:
            source = archive.extractfile(
                "astrbot/builtin_stars/wangshangliao_moderation/flood.py"
            ).read()
            progression = archive.extractfile(
                "astrbot/core/platform/sources/wangshangliao/progressive.py"
            ).read()
        namespace = {"__package__": "astrbot.builtin_stars.wangshangliao_moderation"}
        exec(compile(source, "<offline-baseline>", "exec"), namespace)
        cls = namespace["FloodRuntime"]
        namespace = {"__package__": progressive.__package__}
        exec(compile(progression, "<baseline-progression>", "exec"), namespace)
        original_enforce = namespace["enforce"]

        async def baseline_enforce(*args, **kwargs):
            kwargs.pop("recall_queue", None)
            kwargs.pop("recall_complete", None)
            return await original_enforce(*args, **kwargs)

        monkeypatch.setattr(progressive, "enforce", baseline_enforce)
    runtime = cls(
        SimpleNamespace(
            platform_manager=SimpleNamespace(platform_insts=[bot]),
            get_config=lambda _: {"admins_id": []},
        )
    )
    samples = {}
    bot.diagnostics.observe = lambda name, value: samples.setdefault(name, []).append(
        value * 1000
    )
    intake = []
    try:
        for n in range(1000):
            before = time.perf_counter()
            await ingest(
                bot,
                str(10000 + n),
                "" if n < 80 else "fixture-ad" if n < 100 else f"normal-{n}",
                sender="2" if n < 80 else "3" if n < 100 else str(1000 + n),
                kind="image" if n < 80 else None,
            )
            intake.append((time.perf_counter() - before) * 1000)
        for _ in range(120):
            await runtime.step()
            await asyncio.gather(*runtime.workers.values())
            if not runtime.pending and not any(
                not t.done() for t in runtime.workers.values()
            ):
                break
        assert not runtime.pending
        await drain_recalls(runtime)
        assert len({c[1] for c in bot.calls if c[0] == "recall"}) == (
            20 if scenario == "content_burst" else 100
        )
        values = {"intake": intake, **samples}
        report = {}
        for metric, observations in values.items():
            observations = sorted(observations)
            report[metric] = {
                "samples": len(observations),
                "p95_ms": round(
                    observations[math.ceil(len(observations) * 0.95) - 1], 3
                ),
                "p99_ms": round(
                    observations[math.ceil(len(observations) * 0.99) - 1], 3
                ),
            }
        report = {
            "variant": variant,
            "scenario": scenario,
            "messages": 1000,
            "timings": report,
            "native_io": "simulated 5ms; no network",
        }
        record_property("burst_benchmark", json.dumps(report))
        print(json.dumps(report))
    finally:
        await runtime.close()
