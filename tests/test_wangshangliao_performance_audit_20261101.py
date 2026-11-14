"""Offline regressions for bounded group moderation scheduling and storage."""

import asyncio
import hashlib
import json
import math
import os
import socket
import statistics
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import (
    flood,
    media_moderation,
    raid_defense,
)
from astrbot.core.platform.sources.wangshangliao.storage import Ledger
from tests.test_wangshangliao_content_fastlane import runtime_for
from tests.test_wangshangliao_flood import fresh_bot, ingest
from tests.test_wangshangliao_raid_defense import defense
from tests.test_wangshangliao_raid_defense import store as store

bot = fresh_bot
raid_runtime = defense


@pytest.mark.asyncio
async def test_rule_workers_rotate_pending_members_without_reordering_each_lane(bot):
    bot.config["moderation"].update(mute_keywords=["fixture-ad"], flood_enabled=False)
    runtime = runtime_for(bot, slots=1, backlog=4)
    runtime.execute = AsyncMock()
    try:
        for mid, sender in (
            ("100", "2"),
            ("101", "2"),
            ("102", "2"),
            ("103", "3"),
        ):
            await ingest(bot, mid, "fixture-ad", sender=sender)
        for _ in range(4):
            await runtime.step()
            assert len(runtime.workers) <= 1
            assert sum(map(len, runtime.pending.values())) <= 4
            await asyncio.gather(*runtime.workers.values())
        assert [call.args[2] for call in runtime.execute.await_args_list] == [
            "100",
            "103",
            "101",
            "102",
        ]
        assert not bot.calls
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_idle_rule_scan_seeks_past_the_committed_cursor(bot):
    runtime = runtime_for(bot)
    db = bot.ledger.db
    now = time.time()
    await db.executemany(
        "INSERT INTO inbox(account,team,message,payload,state,received_at) "
        "VALUES('1','5',?,?,'processed',?)",
        [(str(number), json.dumps({"text": "hello"}), now) for number in range(1500)],
    )
    await db.commit()
    async with db.execute("SELECT MAX(rowid) FROM inbox") as cursor:
        high_water = (await cursor.fetchone())[0]
    runtime.cursors[("fixture", "1")] = high_water
    operations = []
    await db.set_progress_handler(lambda: operations.append(1) or 0, 100)
    try:
        await runtime.step()
        assert len(operations) <= 2
        assert runtime.cursors[("fixture", "1")] == high_water
        assert not runtime.workers
        assert not bot.calls
    finally:
        await db.set_progress_handler(None, 100)
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("completed", [True, False])
async def test_old_join_page_does_not_hide_a_fresh_admission(raid_runtime, completed):
    runtime, adapter, _ = raid_runtime
    now = time.time() - 0.1
    notice = {"event_at": int(now * 1000), "member_peers": ["30"]}
    for number in range(64):
        mid = f"completed-{number}"
        await adapter.ledger.ingest(
            "1",
            "5",
            mid,
            {**notice, "member_peers": ["40"]},
            state="invite_notification",
        )
        if completed:
            runtime.seen[("1", "5", mid)] = True
    await adapter.ledger.ingest(
        "1", "5", "fresh-join", notice, state="invite_notification"
    )
    runtime.mute = AsyncMock()
    events = {
        "list": [
            {
                "groupId": "5",
                "applyId": "30",
                "applyTime": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                "applicant": {"userId": "3", "nimId": "30"},
                "state": "MEMBER_STATE_GOOD",
                "groupApplyType": "APPLY",
            }
        ]
    }
    if not completed:
        adapter.business.request.return_value = {"list": []}
        await runtime.process_joins(adapter, "5")
        assert "3" not in runtime.members[("1", "5")]
        runtime.mute.assert_not_awaited()
    adapter.business.request.return_value = events
    await runtime.process_joins(adapter, "5")
    assert runtime.members[("1", "5")]["3"]["join_id"] == "30"
    assert ("1", "5", "fresh-join") in runtime.seen
    runtime.mute.assert_awaited_once()
    adapter.execute_action.assert_not_awaited()
    await runtime.process_joins(adapter, "5")
    runtime.mute.assert_awaited_once()


@pytest.mark.asyncio
async def test_seen_notice_exclusion_stays_within_its_account_and_group(raid_runtime):
    runtime, adapter, _ = raid_runtime
    runtime.seen[("other-account", "5", "join")] = True
    runtime.seen[("1", "other-group", "join")] = True
    now = time.time() - 0.1
    await adapter.ledger.ingest(
        "1",
        "5",
        "join",
        {"event_at": int(now * 1000), "member_peers": ["30"]},
        state="invite_notification",
    )
    runtime.mute = AsyncMock()
    adapter.business.request.return_value = {
        "list": [
            {
                "groupId": "5",
                "applyId": "30",
                "applyTime": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                "applicant": {"userId": "3", "nimId": "30"},
                "state": "MEMBER_STATE_GOOD",
                "groupApplyType": "APPLY",
            }
        ]
    }
    await runtime.process_joins(adapter, "5")
    assert runtime.members[("1", "5")]["3"]["join_id"] == "30"
    runtime.mute.assert_awaited_once()
    adapter.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_join_notices_do_not_overflow_the_seen_bound(raid_runtime):
    runtime, adapter, _ = raid_runtime
    for number in range(4096):
        runtime.seen[("other-account", "other-group", str(number))] = True
    await adapter.ledger.ingest(
        "1",
        "5",
        "stale-join",
        {"event_at": 0, "member_peers": ["30"]},
        state="invite_notification",
    )
    adapter.business.request.return_value = {"list": []}
    await runtime.process_joins(adapter, "5")
    assert len(runtime.seen) == 4096
    assert ("1", "5", "stale-join") in runtime.seen
    adapter.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_media_download_survives_one_cancelled_consumer(monkeypatch):
    runtime = media_moderation.MediaModeration()
    entered, release = asyncio.Event(), asyncio.Event()
    response = MagicMock(status=200)
    request = MagicMock()
    request.__aenter__.return_value = response
    session = MagicMock()
    session.__aenter__.return_value = session
    session.get.return_value = request
    factory = MagicMock(return_value=session)
    monkeypatch.setattr(media_moderation.aiohttp, "ClientSession", factory)

    async def read(*_):
        entered.set()
        await release.wait()
        return b"bounded-fixture-bitmap"

    monkeypatch.setattr(media_moderation, "_read_bounded", read)
    endpoints = ("https://upload.invalid/", "https://download.invalid/")
    key = ("fixture", "1", "5", endpoints[1], "123", 400, 160)
    first = asyncio.create_task(runtime._download(key, endpoints, "123"))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        fetch = runtime.downloads[key][0]
        second = asyncio.create_task(runtime._download(key, endpoints, "123"))
        await asyncio.sleep(0)
        assert runtime.downloads[key][1] == 2
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not fetch.cancelled()
        assert runtime.downloads[key][1] == 1
        release.set()
        assert await asyncio.wait_for(second, 1) == b"bounded-fixture-bitmap"
        assert not runtime.downloads
        factory.assert_called_once()
        options = factory.call_args.kwargs
        assert options["trust_env"] is False
        assert options["timeout"].total == 5
        assert isinstance(
            options["cookie_jar"], media_moderation.aiohttp.DummyCookieJar
        )
        session.get.assert_called_once_with(
            endpoints[1] + "123", allow_redirects=False, ssl=True
        )
    finally:
        for task in (first, second):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(task for task in (first, second) if task is not None),
            return_exceptions=True,
        )
        await runtime.close()


def _audit_percentiles(values):
    """Compute nearest-rank percentiles without making latency assertions.

    Args:
        values: Bounded numeric observations in the caller's reported units.

    Returns:
        Median, p95 and p99, or None when no work completed.
    """
    if not values:
        return None
    ordered = sorted(values)
    return {
        f"p{int(percentile * 100)}": round(
            ordered[max(0, math.ceil(len(ordered) * percentile) - 1)], 4
        )
        for percentile in (0.5, 0.95, 0.99)
    }


async def _audit_flood_replay(runtime_type, path):
    """Replay only scheduling against a temporary inbox and no sanction port.

    Args:
        runtime_type: Archived or candidate FloodRuntime class.
        path: Pytest-owned temporary ledger path.

    Returns:
        Local replay percentiles and bounded queue observations.
    """
    ledger = Ledger(path)
    await ledger.open()
    diagnostics = SimpleNamespace(emit=Mock(), observe=Mock(), gauges={})
    adapter = SimpleNamespace(
        account="1",
        config={
            "id": "offline-audit",
            "type": "wangshangliao",
            "enabled_groups": ["5", "6", "7", "8"],
            "_business_active": True,
            "_business_epoch": 1,
        },
        connection_state="online",
        stopping=asyncio.Event(),
        ledger=ledger,
        diagnostics=diagnostics,
        execute_action=AsyncMock(
            side_effect=AssertionError("offline_action_forbidden")
        ),
        business=SimpleNamespace(
            request=AsyncMock(side_effect=AssertionError("offline_http_forbidden"))
        ),
    )
    runtime = runtime_type(
        SimpleNamespace(
            platform_manager=SimpleNamespace(platform_insts=[adapter]),
            get_config=lambda _: {"admins_id": []},
        ),
        slots=4,
        backlog=256,
    )
    now = time.time()
    records, expected, cold = [], set(), set()
    kinds = (
        "text",
        "text",
        "text",
        "text",
        "command",
        "text",
        "text",
        "image",
        "video",
        "forward",
    )
    categories = {
        5: "mute_keyword",
        6: "message_flood",
        7: "image_inspection",
        8: "restricted_video",
        9: "restricted_forward",
    }
    for number in range(1200):
        lane = number // 200 if number < 800 else 4 + (number - 800) // 10
        group, sender, mid = str(5 + lane % 4), str(100 + lane), str(number + 1)
        kind = kinds[number % 10]
        payload = {
            "sender": sender,
            "text": "fixture",
            "kind": "text" if kind == "command" else kind,
            "recall_route": {"time": str(int(now * 1000)), "peer": sender},
        }
        if kind == "command":
            payload["_dispatch_class"] = "command"
        category = categories.get(number % 10)
        if category:
            field = (
                "_ingress_flood" if category == "message_flood" else "_ingress_content"
            )
            payload[field] = {
                "category": category,
                "received_at": now,
                "account": "1",
                "group": group,
                "sender": sender,
                "message": mid,
            }
            expected.add(mid)
            if number >= 800:
                cold.add(mid)
        records.append(
            (
                "1",
                group,
                mid,
                json.dumps(payload),
                ("pending", "processing", "processed")[number % 3],
                now,
            )
        )
    await ledger.db.executemany(
        "INSERT INTO inbox(account,team,message,payload,state,received_at) VALUES(?,?,?,?,?,?)",
        records,
    )
    await ledger.db.commit()
    admitted, completed, lane_order, active = {}, {}, {}, set()
    dispatch_ms, queue_ms, queue_turns = [], [], []
    turn = peak_workers = peak_pending = 0
    started = time.perf_counter()
    runtime.started_at = now - 1

    def execute(key, original, mid, payload, epoch):
        admitted.setdefault(mid, (turn, time.perf_counter()))

        async def consume():
            nonlocal peak_workers
            assert original is adapter and key[2] in adapter.config["enabled_groups"]
            assert key not in active and mid not in completed
            active.add(key)
            peak_workers = max(peak_workers, len(active))
            current = time.perf_counter()
            dispatch_ms.append((current - started) * 1000)
            queue_ms.append((current - admitted[mid][1]) * 1000)
            queue_turns.append(turn - admitted[mid][0])
            completed[mid] = turn
            lane_order.setdefault(key, []).append(int(mid))
            try:
                await asyncio.sleep(0)
            finally:
                active.remove(key)

        return consume()

    runtime.execute = execute
    try:
        for turn in range(700):
            await runtime.step()
            pending = sum(map(len, runtime.pending.values()))
            peak_pending = max(peak_pending, pending)
            assert pending <= 256 and len(runtime.workers) <= 4
            # Sample admission after the scan, separately from ingress/ACK latency.
            for items in runtime.pending.values():
                for _, mid, _, _ in items:
                    admitted.setdefault(mid, (turn, time.perf_counter()))
            await asyncio.gather(*runtime.workers.values())
            if completed.keys() == expected:
                break
        assert completed.keys() == expected and len(expected) == 600
        assert all(order == sorted(order) for order in lane_order.values())
        polls_to_drain = turn + 1
        idle_ms, vm_steps, queries = [], [], []
        operations = 0

        def progress():
            nonlocal operations
            operations += 100
            return 0

        await ledger.db.set_progress_handler(progress, 100)
        await ledger.db.set_trace_callback(
            lambda sql: (
                queries.append(sql)
                if sql.startswith("SELECT rowid,team,message,payload")
                else None
            )
        )
        try:
            for _ in range(100):
                before, before_operations = time.perf_counter(), operations
                await runtime.step()
                idle_ms.append((time.perf_counter() - before) * 1000)
                vm_steps.append(operations - before_operations)
        finally:
            await ledger.db.set_progress_handler(None, 100)
            await ledger.db.set_trace_callback(None)
        async with ledger.db.execute("EXPLAIN QUERY PLAN " + queries[-1]) as cursor:
            plan = [row[3] for row in await cursor.fetchall()]
        adapter.execute_action.assert_not_awaited()
        adapter.business.request.assert_not_awaited()
        return {
            "messages": len(records),
            "mix": {
                "benign_text": 480,
                "group_command": 120,
                "keyword": 120,
                "flood": 120,
                "image_inspection": 120,
                "restricted_video": 120,
                "restricted_forward": 120,
            },
            "completed": len(completed),
            "replay_to_dispatch_ms": _audit_percentiles(dispatch_ms),
            "pending_wait_ms": _audit_percentiles(queue_ms),
            "pending_wait_turns": _audit_percentiles(queue_turns),
            "first_cold_member_turn": min(completed[mid] for mid in cold),
            "polls_to_drain": polls_to_drain,
            "peak_workers": peak_workers,
            "peak_pending_after_dispatch": peak_pending,
            "backpressure_polls": diagnostics.gauges.get("rule_queue_full", 0),
            "idle_poll_ms": _audit_percentiles(idle_ms),
            "idle_vm_steps_per_poll": {"min": min(vm_steps), "max": max(vm_steps)},
            "idle_query_plan": plan,
            "real_http_calls": 0,
            "ocr_jobs": 0,
            "sanctions": 0,
        }
    finally:
        await runtime.close()
        await ledger.close()


async def _audit_raid_replay(runtime_type, path, monkeypatch):
    """Replay bounded admission pages without native HTTP, OCR or sanctions.

    Args:
        runtime_type: Archived or candidate RaidDefense class.
        path: Temporary group-only inbox database.
        monkeypatch: Scoped replacement of the admission metadata store.

    Returns:
        Page progress and local metadata-callback percentiles.
    """
    ledger = Ledger(path)
    await ledger.open()
    now = time.time()
    adapter = SimpleNamespace(
        account="1",
        config={
            "id": "offline-raid-audit",
            "enabled_groups": ["5"],
            "_business_active": True,
            "_business_epoch": 1,
        },
        ledger=ledger,
        diagnostics=SimpleNamespace(emit=Mock()),
        business=SimpleNamespace(request=AsyncMock()),
        execute_action=AsyncMock(
            side_effect=AssertionError("offline_action_forbidden")
        ),
    )
    service = SimpleNamespace(roster=AsyncMock(return_value={}))
    runtime = runtime_type(service)
    runtime.enabled = Mock(return_value=True)
    runtime.protected = AsyncMock(return_value=False)
    runtime.mute = AsyncMock()
    runtime.states[("1", "5")] = {
        "enabled": True,
        "enabled_at": now - 1,
        "generation": "offline",
    }
    rows, events, accepted, latencies = [], [], set(), []
    for number in range(1024):
        mid = str(number + 1)
        peer = str(8000 + number)
        if number < 64:
            runtime.seen[("1", "5", mid)] = True
        if number >= 832:
            uid, peer = str(9000 + number), str(19000 + number)
            service.roster.return_value[uid] = {
                "nimId": peer,
                "groupRole": "GROUP_ROLE_MEMBER",
            }
            events.append(
                {
                    "groupId": "5",
                    "applyId": mid,
                    "applyTime": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                    "applicant": {"userId": uid, "nimId": peer},
                    "state": "MEMBER_STATE_GOOD",
                    "groupApplyType": "APPLY",
                }
            )
        rows.append(
            (
                "1",
                "5",
                mid,
                json.dumps({"event_at": int(now * 1000), "member_peers": [peer]}),
                now + number / 1_000_000,
            )
        )
    await ledger.db.executemany(
        "INSERT INTO inbox(account,team,message,payload,received_at,state) "
        "VALUES(?,?,?,?,?,'invite_notification')",
        rows,
    )
    await ledger.db.commit()
    adapter.business.request.return_value = {"list": events}
    started = time.perf_counter()

    def join(account, group, uid, peer, join_id, joined_at, *, guard):
        guard()
        assert account == "1" and group == "5"
        if uid in accepted:
            return None
        accepted.add(uid)
        latencies.append((time.perf_counter() - started) * 1000)
        return {
            "member": uid,
            "peer": peer,
            "join_id": join_id,
            "joined_at": joined_at,
            "observed_until": now + 600,
            "mute_until": 0,
        }

    monkeypatch.setitem(
        runtime_type.process_joins.__globals__,
        "RaidStore",
        lambda _: SimpleNamespace(join=join),
    )
    poll_ms, progress = [], []
    try:
        for _ in range(20):
            before = time.perf_counter()
            await runtime.process_joins(adapter, "5")
            poll_ms.append((time.perf_counter() - before) * 1000)
            progress.append(len(accepted))
        adapter.execute_action.assert_not_awaited()
        assert runtime.mute.await_count == len(accepted)
        assert not [
            call
            for call in adapter.diagnostics.emit.call_args_list
            if call.args[:2] == ("raid_membership", "unavailable")
        ]
        return {
            "group_notices": len(rows),
            "already_seen": 64,
            "provider_unresolved": 768,
            "corroborated_ready": 192,
            "metadata_callbacks": len(accepted),
            "replay_to_metadata_ms": _audit_percentiles(latencies),
            "poll_ms": _audit_percentiles(poll_ms),
            "progress_by_poll": progress,
            "fake_roster_calls": service.roster.await_count,
            "fake_apply_log_calls": adapter.business.request.await_count,
            "real_http_calls": 0,
            "ocr_jobs": 0,
            "sanctions": 0,
        }
    finally:
        await runtime.close()
        await ledger.close()


@pytest.mark.asyncio
async def test_opt_in_backed_up_group_scheduling_benchmark(tmp_path, monkeypatch):
    """Compare archived scheduling only, never native business execution.

    Args:
        tmp_path: Pytest-owned directory for every replay database.
        monkeypatch: Restore all benchmark-only metadata replacements.
    """
    if os.environ.get("WSL_OFFLINE_AUDIT_BENCHMARK") != "1":
        pytest.skip("Set WSL_OFFLINE_AUDIT_BENCHMARK=1 for the bounded offline replay")

    def forbidden(*args, **kwargs):
        raise AssertionError("offline_benchmark_network_forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=AssertionError("offline_benchmark_ocr_forbidden")),
    )
    archive_path = Path(
        "/opt/backups/wsl-raid-defense-20261031T1403Z/source-pending.tar.gz"
    )
    if not archive_path.is_file():
        pytest.skip("The immutable local pre-audit archive is unavailable")
    package = "astrbot.builtin_stars.wangshangliao_moderation"
    baseline, hashes = {}, {}
    with tarfile.open(archive_path, "r:gz") as archive:
        for name, candidate in (("flood", flood), ("raid_defense", raid_defense)):
            member = archive.getmember(
                f"astrbot/builtin_stars/wangshangliao_moderation/{name}.py"
            )
            assert member.isfile() and member.size < 128 * 1024
            with archive.extractfile(member) as source:
                raw = source.read()
            namespace = {
                "__name__": package + "._offline_baseline_" + name,
                "__package__": package,
            }
            exec(compile(raw, str(archive_path) + ":" + member.name, "exec"), namespace)
            baseline[name] = namespace
            hashes[name] = {
                "baseline": hashlib.sha256(raw).hexdigest(),
                "candidate": hashlib.sha256(
                    Path(candidate.__file__).read_bytes()
                ).hexdigest(),
            }
    runs = {"baseline": [], "candidate": []}
    for repeat in range(3):
        order = (
            ("baseline", "candidate") if repeat % 2 == 0 else ("candidate", "baseline")
        )
        for label in order:
            flood_type = (
                baseline["flood"]["FloodRuntime"]
                if label == "baseline"
                else flood.FloodRuntime
            )
            raid_type = (
                baseline["raid_defense"]["RaidDefense"]
                if label == "baseline"
                else raid_defense.RaidDefense
            )
            result = {
                "flood": await _audit_flood_replay(
                    flood_type, tmp_path / f"{label}-{repeat}-flood.sqlite3"
                ),
                "raid": await _audit_raid_replay(
                    raid_type, tmp_path / f"{label}-{repeat}-raid.sqlite3", monkeypatch
                ),
            }
            assert result["raid"]["metadata_callbacks"] == (
                0 if label == "baseline" else 192
            )
            runs[label].append(result)
    for baseline_run, candidate_run in zip(runs["baseline"], runs["candidate"]):
        assert (
            candidate_run["flood"]["first_cold_member_turn"]
            < (baseline_run["flood"]["first_cold_member_turn"])
        )
    summary = {}
    for label, observations in runs.items():
        summary[label] = {
            "median_run_percentiles": {
                metric: {
                    percentile: round(
                        statistics.median(
                            observation["flood"][metric][percentile]
                            for observation in observations
                        ),
                        4,
                    )
                    for percentile in ("p50", "p95", "p99")
                }
                for metric in (
                    "replay_to_dispatch_ms",
                    "pending_wait_ms",
                    "idle_poll_ms",
                )
            },
            "raid_metadata_median_run_percentiles": (
                {
                    percentile: round(
                        statistics.median(
                            observation["raid"]["replay_to_metadata_ms"][percentile]
                            for observation in observations
                        ),
                        4,
                    )
                    for percentile in ("p50", "p95", "p99")
                }
                if observations[0]["raid"]["metadata_callbacks"]
                else None
            ),
        }
    print(
        json.dumps(
            {
                "mode": "offline_group_scheduler_only",
                "baseline_archive": str(archive_path),
                "source_sha256": hashes,
                "paired_runs": 3,
                "polling": "drain loop; no production 100ms/3s sleeps",
                "execution": "rule bodies and sanctions stubbed; raid metadata store stubbed",
                "timing_boundaries": {
                    "replay_to_dispatch": "bulk commit excluded; replay start to stub worker entry",
                    "pending_wait": "first end-of-scan admission observation to stub worker entry",
                    "raid_metadata": "bulk commit excluded; replay start to fake metadata-store callback",
                    "idle_poll": "complete idle FloodRuntime.step; no pending workers",
                },
                "http_ocr": "not measured; no real HTTP calls or OCR workers",
                "percentiles": "nearest rank per run; summary is median of three run percentiles",
                "summary": summary,
                "runs": runs,
            },
            sort_keys=True,
        )
    )
