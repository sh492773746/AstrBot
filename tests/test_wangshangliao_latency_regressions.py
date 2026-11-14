"""Offline latency probes; no production clients, configuration or actions."""

import asyncio
import hashlib
import json
import marshal
import math
import os
import socket
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from astrbot.builtin_stars.wangshangliao_moderation import flood
from astrbot.core.platform.sources.wangshangliao.storage import Ledger
from tests.test_wangshangliao_content_fastlane import runtime_for
from tests.test_wangshangliao_flood import drain_recalls, fresh_bot, ingest

bot = fresh_bot


@pytest.mark.asyncio
async def test_completed_rule_workers_wake_the_next_member_item_immediately(bot):
    bot.config["moderation"].update(mute_keywords=["fixture-ad"], flood_enabled=False)
    runtime = runtime_for(bot, slots=1)
    finished = asyncio.Event()
    completed = []

    async def execute(key, original, mid, payload, epoch):
        completed.append(mid)
        if len(completed) == 40:
            finished.set()

    runtime.execute = execute
    for number in range(40):
        await ingest(bot, str(100 + number), "fixture-ad")
    runner = asyncio.create_task(runtime.run())
    try:
        # The former fixed 100ms sleep needed at least 3.9s for these items.
        await asyncio.wait_for(finished.wait(), 1)
        assert completed == [str(100 + number) for number in range(40)]
        assert not bot.calls
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        await runtime.close()


@pytest_asyncio.fixture(autouse=True)
async def offline_only(monkeypatch):
    """Forbid all network and subprocess execution after loop initialization.

    Args:
        monkeypatch: Test-scoped replacement owner.
    """
    assert asyncio.get_running_loop().is_running()

    def forbidden(*args, **kwargs):
        raise AssertionError("offline_latency_probe_external_io_forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(side_effect=forbidden)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "192.0.2.1"])
async def test_offline_guard_blocks_loopback_and_remote_connections(host):
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family) as connection:
        for connect in (
            connection.connect,
            connection.connect_ex,
            socket.create_connection,
        ):
            with pytest.raises(
                AssertionError, match="^offline_latency_probe_external_io_forbidden$"
            ):
                connect((host, 443))
    with pytest.raises(
        AssertionError, match="^offline_latency_probe_external_io_forbidden$"
    ):
        await asyncio.create_subprocess_exec("offline-probe-must-not-run")


@pytest.mark.asyncio
@pytest.mark.parametrize("poll_seconds", [0, 0.1])
async def test_offline_1000_mixed_rule_scheduling(tmp_path, poll_seconds):
    """Measure real scheduler admission separately from transport execution.

    Args:
        tmp_path: Temporary inbox owner.
        poll_seconds: Zero for a CPU floor, or 0.1 to use the production run loop.
    """
    if os.environ.get("WSL_OFFLINE_LATENCY_BENCHMARK") != "1":
        pytest.skip("Set WSL_OFFLINE_LATENCY_BENCHMARK=1 for the offline benchmark")
    ledger = Ledger(tmp_path / "messages.sqlite3")
    await ledger.open()
    adapter = SimpleNamespace(
        account="1",
        config={
            "id": "offline-latency",
            "type": "wangshangliao",
            "enabled_groups": ["5", "6", "7", "8"],
            "_business_active": True,
            "_business_epoch": 1,
        },
        ledger=ledger,
        stopping=asyncio.Event(),
        connection_state="online",
        diagnostics=SimpleNamespace(emit=Mock(), gauges={}),
        execute_action=AsyncMock(side_effect=AssertionError("native_action_forbidden")),
        business=SimpleNamespace(
            request=AsyncMock(side_effect=AssertionError("http_forbidden"))
        ),
    )
    runtime = flood.FloodRuntime(
        SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[adapter]))
    )
    now = time.time()
    runtime.started_at = now - 1
    categories = {
        5: "mute_keyword",
        6: "message_flood",
        7: "image_inspection",
        8: "restricted_video",
        9: "restricted_forward",
    }
    kinds = {7: "image", 8: "video", 9: "forward"}
    rows, expected, cold = [], set(), set()
    for number in range(1000):
        lane = number // 200 if number < 800 else 4 + (number - 800) // 10
        group, sender, mid = str(5 + lane % 4), str(100 + lane), str(number + 1)
        payload = {
            "sender": sender,
            "name": "Offline Fixture",
            "text": "fixture",
            "mentions": [],
            "created_at": int(now * 1000),
            "kind": kinds.get(number % 10, "text"),
            "recall_route": {"time": str(int(now * 1000)), "peer": sender},
        }
        if number % 10 == 4:
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
        rows.append(
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
        "INSERT INTO inbox(account,team,message,payload,state,received_at) "
        "VALUES(?,?,?,?,?,?)",
        rows,
    )
    await ledger.db.commit()
    started = time.perf_counter()
    completed, admission, orders, dispatch_ms, pending_ms = {}, {}, {}, [], []
    peak_workers = peak_pending = 0
    turn = -1
    finished = asyncio.Event()
    runner = None

    async def execute(key, original, mid, payload, epoch):
        assert original is adapter and epoch == 1 and mid not in completed
        completed[mid] = turn
        orders.setdefault(key, []).append(int(mid))
        dispatch_ms.append((time.perf_counter() - started) * 1000)
        pending_ms.append((time.perf_counter() - admission.get(mid, started)) * 1000)
        if completed.keys() == expected:
            finished.set()
        await asyncio.sleep(0)

    runtime.execute = execute
    original_step = runtime.step

    async def measured_step():
        nonlocal turn, peak_workers, peak_pending
        turn += 1
        await original_step()
        peak_workers = max(peak_workers, len(runtime.workers))
        queued = sum(map(len, runtime.pending.values()))
        peak_pending = max(peak_pending, queued)
        assert queued <= 256 and len(runtime.workers) <= 4
        for items in runtime.pending.values():
            for _, mid, _, _ in items:
                admission.setdefault(mid, time.perf_counter())

    runtime.step = measured_step
    try:
        if poll_seconds:
            runner = asyncio.create_task(runtime.run())
            await asyncio.wait_for(finished.wait(), 60)
        else:
            for _ in range(600):
                await runtime.step()
                await asyncio.gather(*runtime.workers.values())
                if completed.keys() == expected:
                    break
                await asyncio.sleep(0)
        assert completed.keys() == expected and len(expected) == 500
        assert all(order == sorted(order) for order in orders.values())
        adapter.execute_action.assert_not_awaited()
        adapter.business.request.assert_not_awaited()

        def percentiles(values):
            ordered = sorted(values)
            return {
                f"p{int(p * 100)}": round(ordered[math.ceil(len(ordered) * p) - 1], 3)
                for p in (0.5, 0.95, 0.99, 1)
            }

        print(
            "OFFLINE_MIXED_LATENCY "
            + json.dumps(
                {
                    "messages": 1000,
                    "mix": {
                        "benign": 400,
                        "commands": 100,
                        "keyword": 100,
                        "flood": 100,
                        "image": 100,
                        "video": 100,
                        "forward": 100,
                    },
                    "rule_candidates_dispatched": len(completed),
                    "poll_seconds": poll_seconds,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                    "dispatch_ms": percentiles(dispatch_ms),
                    "admitted_pending_ms": percentiles(pending_ms),
                    "polls": turn + 1,
                    "first_cold_member_turn": min(completed[mid] for mid in cold),
                    "peak_workers": peak_workers,
                    "peak_pending": peak_pending,
                    "backpressure_polls": adapter.diagnostics.gauges.get(
                        "rule_queue_full", 0
                    ),
                    "loaded_step_code_sha256": hashlib.sha256(
                        marshal.dumps(type(runtime).step.__code__)
                    ).hexdigest(),
                    "real_http": 0,
                    "real_actions": 0,
                    "ocr": 0,
                    "boundary": "committed inbox to mocked rule execution start",
                },
                sort_keys=True,
            )
        )
    finally:
        if runner is not None:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        await runtime.close()
        await ledger.close()


@pytest.mark.asyncio
async def test_slow_recall_keeps_rules_ordered_without_holding_the_member_lane(
    bot, monkeypatch
):
    """Keep rule order while deferred recalls leave both member lanes available.

    Args:
        bot: Existing isolated production-policy fixture.
        monkeypatch: Scoped replacement of the progressive and recall transports.
    """
    from astrbot.core.platform.sources.wangshangliao import progressive

    runtime = runtime_for(bot)
    entered, release = asyncio.Event(), asyncio.Event()
    sanctions, executions = [], []
    original_execute = runtime.execute

    async def execute(key, adapter, mid, payload, epoch):
        executions.append(mid)
        return await original_execute(key, adapter, mid, payload, epoch)

    async def progress(adapter, group, sender, mid, **kwargs):
        sanctions.append(mid)
        return {"status": "accepted", "action": "mute", "recall_status": "none"}

    async def recall(*args):
        entered.set()
        await release.wait()
        return 200, b""

    monkeypatch.setattr(progressive, "enforce", progress)
    bot.nim.request.side_effect = recall
    runtime.execute = execute
    bot.members["5"]["3"] = "33"
    bot.get_moderation_members.return_value["groupMemberInfo"].append(
        {"userId": "3", "nimId": "33", "groupRole": "GROUP_ROLE_MEMBER"}
    )
    try:
        for number in range(6):
            await ingest(bot, str(100 + number), f"fixture-{number}")
        bot.config["moderation"]["mute_keywords"] = ["fixture-ad"]
        await ingest(bot, "200", "fixture-ad", sender="3")
        await runtime.step()
        await asyncio.gather(*runtime.workers.values())
        assert sorted(sanctions) == ["104", "200"]
        bot.nim.request.assert_not_awaited()
        # Deferred cleanup starts on the next poll, beside the next rule.
        await runtime.step()
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.gather(*runtime.workers.values())
        for _ in range(3):
            await runtime.step()
            await asyncio.gather(*runtime.workers.values())
        advanced = "105" in executions
        print(
            "OFFLINE_RECALL_BLOCK "
            + json.dumps(
                {
                    "sanctions_before_release": sanctions,
                    "rule_starts_before_release": executions,
                    "rule_workers": len(runtime.workers),
                    "pending": sum(map(len, runtime.pending.values())),
                    "next_same_member_rule_started": advanced,
                    "real_actions": 0,
                }
            )
        )
        assert advanced
        assert [mid for mid in executions if mid in {"104", "105"}] == ["104", "105"]
        assert "200" in executions
        assert sorted(sanctions) == ["104", "200"]
        assert bot.nim.request.await_count == 1
    finally:
        release.set()
        try:
            await drain_recalls(runtime)
        finally:
            await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["accepted", "unknown"])
async def test_reserved_recall_does_not_repeat_remote_roster_read(bot, status):
    """Expose directory IO before a duplicate recall's no-resubmission result.

    Args:
        bot: Existing isolated production-policy fixture.
        status: Accepted or uncertain existing recall receipt.
    """
    bot.config["moderation"].update(mute_keywords=["fixture-ad"], flood_enabled=False)
    runtime = runtime_for(bot)
    if status == "unknown":
        bot.nim.request.side_effect = TimeoutError()
    try:
        await ingest(bot, "100", "fixture-ad")
        await runtime.step()
        await asyncio.gather(*runtime.workers.values())
        receipt = await bot.ledger.receipt("recall/1/5/100")
        assert receipt["status"] == status
        submissions = bot.nim.request.await_count
        bot.get_moderation_members.reset_mock()
        assert await bot.recall_violation("5", "100", "2") == status
        reads = bot.get_moderation_members.await_count
        assert bot.nim.request.await_count == submissions
        print(
            "OFFLINE_DUPLICATE_RECALL "
            + json.dumps({"status": status, "duplicate_roster_reads": reads})
        )
        assert reads == 0, (
            "an existing exact recall receipt still requires remote roster IO"
        )
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_open_event_reads_existing_result_after_fastlane_completion(
    bot, tmp_path, monkeypatch
):
    """Completed routing falls back without another roster or native submission.

    Args:
        bot: Isolated adapter whose exact recall receipt is committed.
        tmp_path: Owner of the isolated embedded service.
        monkeypatch: Scoped spy owner.
    """
    from astrbot.builtin_stars.wangshangliao_moderation import open_service, policy
    from tests.test_wangshangliao_integrated_service import service
    from tests.test_wangshangliao_plain_commands import native_event

    bot.config["moderation"].update(mute_keywords=["fixture-ad"], flood_enabled=False)
    runtime = runtime_for(bot)
    poller = None
    try:
        payload = await ingest(bot, "100", "fixture-ad")
        await runtime.step()
        await asyncio.gather(*runtime.workers.values())
        assert (await bot.ledger.receipt("recall/1/5/100"))["status"] == "accepted"
        value = service(tmp_path)
        value.plugin.flood_runtime = runtime
        poller = asyncio.create_task(runtime.run())
        value.plugin.flood_poller = poller
        event = native_event("fixture-ad")
        event.platform = bot
        event.message_obj.message_id = "100"
        event.set_extra("wangshangliao_payload", payload)
        handler = AsyncMock(wraps=policy.handle)
        monkeypatch.setattr(open_service, "handle", handler)
        submissions = bot.nim.request.await_count
        roster_reads = bot.get_moderation_members.await_count
        await value._on_event(event)
        assert bot.nim.request.await_count == submissions
        assert bot.get_moderation_members.await_count == roster_reads
        print(
            "OFFLINE_DUPLICATE_HANDLE "
            + json.dumps({"awaited_handle_after_fastlane": handler.await_count})
        )
        handler.assert_awaited_once()
        assert not runtime.pending
    finally:
        if poller is not None:
            poller.cancel()
            await asyncio.gather(poller, return_exceptions=True)
        await runtime.close()
