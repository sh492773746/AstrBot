"""Offline image admission preserves bounded coverage and revocation fences."""

import asyncio
import copy
import io
import json
import math
import os
import shutil
import socket
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from PIL import Image, ImageDraw, ImageFont

from astrbot.builtin_stars.wangshangliao_moderation import (
    content_rules,
    media_moderation,
    member_exemptions,
)
from astrbot.core.platform.sources.wangshangliao.group_scope import scoped
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from astrbot.core.star.component_runtime import ExecutionContext, current_execution
from tests.test_wangshangliao_content_fastlane import runtime_for
from tests.test_wangshangliao_flood import fresh_bot, ingest
from tests.test_wangshangliao_media_moderation import METADATA, configure

bot = fresh_bot


@pytest_asyncio.fixture
async def media_queue(bot, monkeypatch):
    """Run the real scheduler with blocked fake downloads and fake OCR.

    Args:
        bot: Isolated native adapter with a temporary ingress ledger.
        monkeypatch: Restore every fake boundary after this test.

    Yields:
        Runtime, release controls and recorded offline worker activity.
    """
    configure(bot)
    bot.config["_open_groups"]["6"].update(
        image_qr_recall=True, image_digits_recall=True
    )
    bot.config["enabled_groups"].append("6")
    bot.config["moderation"]["permissions"]["6"] = ["recall"]
    bot.groups["6"] = "66"
    bot.diagnostics.gauges = {}
    runtime = media_moderation.MediaModeration()
    payload = await ingest(bot, "100", "", kind="image")
    payload["image"] = copy.deepcopy(METADATA)
    env = SimpleNamespace(
        adapter=bot,
        runtime=runtime,
        payload=payload,
        release=asyncio.Event(),
        started=asyncio.Queue(),
        downloads=[],
        processes=[],
        process_contexts=[],
        output=b'{"qr":false,"digits":false}',
        returncode=0,
        offset=0,
    )
    wall, monotonic = time.time, time.monotonic
    monkeypatch.setattr(
        media_moderation,
        "time",
        SimpleNamespace(
            time=lambda: wall() + env.offset,
            monotonic=lambda: monotonic() + env.offset,
        ),
    )

    async def download(key, endpoints, asset):
        env.downloads.append((key[1], key[2], asset))
        env.started.put_nowait(asset)
        await env.release.wait()
        return b"offline-bitmap-" + asset.encode()

    async def spawn(*args, **kwargs):
        process = SimpleNamespace(
            returncode=env.returncode,
            communicate=AsyncMock(return_value=(env.output, b"")),
            wait=AsyncMock(),
        )
        env.processes.append(process)
        env.process_contexts.append(current_execution.get())
        return process

    monkeypatch.setattr(runtime, "_download", download)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(side_effect=spawn))
    monkeypatch.setattr(
        media_moderation.aiohttp,
        "ClientSession",
        Mock(side_effect=AssertionError("Real HTTP is forbidden in queue tests")),
    )
    try:
        yield env
    finally:
        await runtime.close()


def enqueue(env, mid, *, adapter=None, group="5", execution_check=None):
    """Submit a distinct native image without waiting for any worker.

    Args:
        env: Offline media fixture.
        mid: Unique native message identifier.
        adapter: Optional other isolated account.
        group: Exact group queue.
        execution_check: Optional upstream authority fence.

    Returns:
        Whether this image was admitted.
    """
    payload = copy.deepcopy(env.payload)
    payload["image"]["asset"] = str(mid)
    return env.runtime.enqueue(
        scoped(adapter or env.adapter, group),
        group,
        str(mid),
        payload,
        execution_check,
    )


@pytest.mark.asyncio
async def test_burst_beyond_four_is_queued_without_waiting_or_false_clear(media_queue):
    env = media_queue
    for mid in range(100, 112):
        assert enqueue(env, mid)
    assert not env.downloads
    assert len(env.runtime.tasks) == 2
    assert env.runtime.queued == 12
    assert env.runtime.counts["clear"] == 0
    await asyncio.wait_for(env.started.get(), 1)
    await asyncio.wait_for(env.started.get(), 1)
    assert len(env.downloads) == 2
    assert env.runtime.queued == 10
    env.release.set()
    await asyncio.wait_for(asyncio.gather(*env.runtime.tasks), 3)
    assert len(env.downloads) == 12
    assert len(env.processes) == 12
    assert env.runtime.counts["completed"] == 12
    assert env.runtime.counts["clear"] == 12
    assert env.runtime.counts["unchecked"] == 0
    assert not env.runtime.pending
    assert not env.runtime.tasks
    assert env.adapter.diagnostics.gauges["media_pending"] == 0
    assert env.adapter.diagnostics.gauges["media_workers"] == 0
    assert (
        len(
            [
                call
                for call in env.adapter.diagnostics.observe.call_args_list
                if call.args[0] == "media_queue"
            ]
        )
        == 12
    )


@pytest.mark.asyncio
async def test_group_account_and_global_overload_are_bounded_and_explicit(media_queue):
    env = media_queue
    for mid in (100, 101):
        assert enqueue(env, mid)
    await asyncio.wait_for(env.started.get(), 1)
    await asyncio.wait_for(env.started.get(), 1)
    for mid in range(200, 216):
        assert enqueue(env, mid)
    assert not enqueue(env, 299)
    for mid in range(300, 316):
        assert enqueue(env, mid, group="6")
    assert not enqueue(env, 399, group="6")

    other = copy.copy(env.adapter)
    other.account = "9"
    other.config = copy.deepcopy(env.adapter.config)
    for group, first in (("5", 400), ("6", 500)):
        for mid in range(first, first + 16):
            assert enqueue(env, mid, adapter=other, group=group)
    third = copy.copy(other)
    third.account = "10"
    for mid in range(600, 700):
        assert not enqueue(env, mid, adapter=third)
    assert len(env.runtime.tasks) == 2
    assert env.runtime.queued == 64
    assert len(env.downloads) == 2
    assert env.runtime.counts["queue_full"] == 102
    assert env.runtime.counts["busy"] == 102
    assert env.runtime.counts["unchecked"] == 102
    outcomes = {call.args[1] for call in env.adapter.diagnostics.emit.call_args_list}
    assert {
        "unchecked_group_full",
        "unchecked_account_full",
        "unchecked_queue_full",
    } <= outcomes
    assert env.adapter.diagnostics.gauges["media_pending"] == 64
    assert env.adapter.diagnostics.gauges["media_queue_full"] == 102
    await env.runtime.close()
    assert env.runtime.counts["cancelled"] == 66
    assert env.runtime.counts["completed"] == 66
    assert env.runtime.counts["clear"] == 0
    assert env.runtime.counts["unchecked"] == 168
    assert not env.runtime.pending
    assert not env.runtime.tasks
    assert env.adapter.diagnostics.gauges["media_workers"] == 0
    assert env.adapter.diagnostics.gauges["media_pending"] == 0


@pytest.mark.asyncio
async def test_waiting_accounts_and_their_groups_are_round_robin(media_queue):
    env = media_queue
    other = copy.copy(env.adapter)
    other.account = "9"
    other.config = copy.deepcopy(env.adapter.config)
    for mid in (100, 101):
        assert enqueue(env, mid)
    await asyncio.wait_for(env.started.get(), 1)
    await asyncio.wait_for(env.started.get(), 1)
    for adapter, group, first in (
        (env.adapter, "5", 200),
        (env.adapter, "6", 300),
        (other, "5", 400),
        (other, "6", 500),
    ):
        for mid in range(first, first + 2):
            assert enqueue(env, mid, adapter=adapter, group=group)
    env.release.set()
    await asyncio.wait_for(asyncio.gather(*env.runtime.tasks), 3)
    assert [(account, group) for account, group, _ in env.downloads[2:]] == [
        ("1", "5"),
        ("9", "5"),
        ("1", "6"),
        ("9", "6"),
        ("1", "5"),
        ("9", "5"),
        ("1", "6"),
        ("9", "6"),
    ]
    assert env.runtime.counts["clear"] == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("age,wait", [(0, 31), (29, 2)])
async def test_queue_deadline_uses_source_stamp_not_a_fresh_thirty_seconds(
    media_queue, age, wait
):
    env = media_queue
    env.runtime.started_at -= 60
    for mid in (100, 101):
        assert enqueue(env, mid)
    await asyncio.wait_for(env.started.get(), 1)
    await asyncio.wait_for(env.started.get(), 1)
    env.payload["recall_route"]["time"] = str(int((time.time() - age) * 1000))
    assert enqueue(env, 102)
    env.offset = wait
    env.release.set()
    await asyncio.wait_for(asyncio.gather(*env.runtime.tasks), 3)
    assert len(env.downloads) == 2
    assert len(env.processes) == (0 if age == 0 else 2)
    assert env.runtime.counts["expired"] == (3 if age == 0 else 1)
    assert env.runtime.counts["unchecked"] == (3 if age == 0 else 1)
    assert env.runtime.counts["clear"] == (0 if age == 0 else 2)
    assert any(
        call.args[1] == "unchecked_expired" and call.args[2] == "102"
        for call in env.adapter.diagnostics.emit.call_args_list
    )
    assert not env.adapter.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "account",
        "epoch",
        "revision",
        "pause",
        "disabled",
        "permission",
        "policy",
        "native_role",
        "origin",
        "business",
        "upstream",
    ],
)
async def test_waiting_images_recheck_authority_before_any_download(
    media_queue, change
):
    env = media_queue
    check = Mock()
    for mid in (100, 101, 102):
        assert enqueue(env, mid, execution_check=check)
    await asyncio.wait_for(env.started.get(), 1)
    await asyncio.wait_for(env.started.get(), 1)
    if change == "account":
        env.adapter.account = "9"
    elif change == "epoch":
        env.adapter.config["_business_epoch"] = "new"
    elif change == "revision":
        env.adapter.config["_open_revisions"]["5"] = "new"
    elif change == "pause":
        env.adapter.config["_business_active"] = False
    elif change == "disabled":
        env.adapter.config["enable"] = False
    elif change == "permission":
        env.adapter.config["moderation"]["permissions"]["5"] = []
    elif change == "policy":
        env.adapter.config["_open_groups"]["5"]["image_qr_recall"] = False
    elif change == "native_role":
        env.adapter.config["_open_native_roles"]["5"] = "GROUP_ROLE_MEMBER"
    elif change == "origin":
        env.adapter.business.deployment.origin = "https://unreviewed.invalid"
    elif change == "business":
        env.adapter.business = SimpleNamespace(
            deployment=SimpleNamespace(origin="https://47.86.91.23:5015")
        )
    else:
        check.side_effect = ProtocolError("content_policy_changed")
    env.release.set()
    await asyncio.wait_for(asyncio.gather(*env.runtime.tasks), 3)
    assert len(env.downloads) == 2
    assert not env.processes
    assert not env.adapter.calls
    assert env.runtime.counts["revoked"] == 3
    assert env.runtime.counts["unchecked"] == 3
    assert env.runtime.counts["clear"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("protection", ["admin", "whitelist", "identity"])
async def test_queued_matching_images_preserve_protected_identity(
    media_queue, monkeypatch, tmp_path, protection
):
    env = media_queue
    env.output = b'{"qr":true,"digits":false}'
    for mid in (100, 101, 102):
        assert enqueue(env, mid)
    await asyncio.wait_for(env.started.get(), 1)
    await asyncio.wait_for(env.started.get(), 1)
    if protection == "admin":
        env.adapter.get_moderation_members.return_value["groupMemberInfo"][1][
            "groupRole"
        ] = "GROUP_ROLE_ADMIN"
    elif protection == "identity":
        env.adapter.members["5"]["2"] = "different-peer"
    else:
        monkeypatch.setattr(member_exemptions, "instance_dir", lambda _: tmp_path)
        member_exemptions.set_exemption(
            "fixture",
            "1",
            "5",
            "2",
            "22",
            enabled=True,
            actor="server/test",
            reason="Fixture",
            guard=lambda: None,
        )
    env.release.set()
    await asyncio.wait_for(asyncio.gather(*env.runtime.tasks), 3)
    assert env.runtime.counts["matched"] == 3
    assert env.runtime.counts["enforcement_not_applied"] == 3
    assert env.runtime.counts["enforcement_unknown"] == 0
    assert env.runtime.counts["clear"] == 0
    assert not env.adapter.calls
    env.adapter.execute_moderation.assert_not_awaited()


@pytest.mark.asyncio
async def test_waiting_match_keeps_opaque_proof_and_unknown_enforcement_is_not_retried(
    media_queue, monkeypatch
):
    env = media_queue
    env.output = b'{"qr":true,"digits":false}'
    checked = []

    async def enforce(adapter, group, mid, payload, *, assessment, execution_check):
        execution_check()
        assert media_moderation.verified_assessment(
            adapter, group, mid, payload, assessment
        )
        assert not media_moderation.verified_assessment(
            adapter, group, "different", payload, assessment
        )
        checked.append(mid)
        raise ProtocolError("result_unknown")

    monkeypatch.setattr(content_rules, "enforce", enforce)
    for mid in range(100, 108):
        assert enqueue(env, mid)
    env.release.set()
    await asyncio.wait_for(asyncio.gather(*env.runtime.tasks), 3)
    assert set(checked) == {str(mid) for mid in range(100, 108)}
    assert len(checked) == 8
    assert env.runtime.counts["clear"] == 0
    assert env.runtime.counts["failed"] == 8
    assert env.runtime.counts["enforcement_unknown"] == 8
    for mid in range(100, 108):
        assert enqueue(env, mid)
    assert not env.runtime.tasks
    assert env.runtime.counts["admitted"] == 8
    assert len(checked) == 8


@pytest.mark.asyncio
async def test_reused_workers_bind_each_images_own_execution_context(media_queue):
    env = media_queue
    first = ExecutionContext(("first",), lambda: None)
    second = ExecutionContext(("second",), lambda: None)
    for execution, mids in ((first, (100, 101)), (second, (102, 103, 104))):
        token = current_execution.set(execution)
        try:
            for mid in mids:
                assert enqueue(env, mid)
        finally:
            current_execution.reset(token)
    await asyncio.wait_for(env.started.get(), 1)
    await asyncio.wait_for(env.started.get(), 1)
    first.revoked = True
    env.release.set()
    await asyncio.wait_for(asyncio.gather(*env.runtime.tasks), 3)
    assert env.runtime.counts["revoked"] == 2
    assert env.runtime.counts["clear"] == 3
    assert env.process_contexts == [second] * 3
    assert current_execution.get() is None


@pytest.mark.asyncio
async def test_each_queued_image_keeps_its_own_upstream_check(media_queue):
    env = media_queue
    allowed, revoked = Mock(), Mock()
    for mid in (100, 101):
        assert enqueue(env, mid, execution_check=allowed)
    await asyncio.wait_for(env.started.get(), 1)
    await asyncio.wait_for(env.started.get(), 1)
    assert enqueue(env, 102, execution_check=revoked)
    assert enqueue(env, 103, execution_check=allowed)
    revoked.side_effect = ProtocolError("content_policy_changed")
    env.release.set()
    await asyncio.wait_for(asyncio.gather(*env.runtime.tasks), 3)
    assert env.runtime.counts["revoked"] == 1
    assert env.runtime.counts["clear"] == 3
    assert [asset for _, _, asset in env.downloads] == ["100", "101", "103"]
    revoked.assert_called_once()
    assert allowed.call_count == 9


@pytest.mark.asyncio
async def test_blocked_ocr_does_not_block_flood_mute_first(
    media_queue, monkeypatch, tmp_path
):
    from astrbot.core.platform.sources.wangshangliao.progression_store import (
        ProgressionStore,
    )

    env = media_queue
    env.adapter.config["_open_groups"]["5"]["flood"] = True
    env.adapter.config["moderation"]["permissions"]["5"] = ["mute", "recall"]
    store = ProgressionStore(tmp_path / "moderation.sqlite3")
    store.initialize()
    with store.connect() as db:
        db.executemany(
            "INSERT INTO progressive_warnings VALUES('1','5','2',?,'accepted')",
            [(f"prior-warning-{n}",) for n in range(3)],
        )
    for mid in range(100, 112):
        assert enqueue(env, mid)
    await asyncio.wait_for(env.started.get(), 1)
    await asyncio.wait_for(env.started.get(), 1)
    runtime = runtime_for(env.adapter, media_runtime=env.runtime)
    mute_entered, release_mute = asyncio.Event(), asyncio.Event()

    async def mute(operation, action, group, member, text="", **kwargs):
        if kwargs.get("action_check"):
            await kwargs["action_check"]()
        env.adapter.calls.append((action, str(member), kwargs["minutes"]))
        mute_entered.set()
        await release_mute.wait()
        return {"status": "accepted"}

    monkeypatch.setattr(env.adapter, "execute_moderation", AsyncMock(side_effect=mute))
    try:
        for mid in range(200, 205):
            await ingest(env.adapter, str(mid), "ordinary", kind="text")
        await asyncio.wait_for(runtime.step(), 1)
        await asyncio.wait_for(mute_entered.wait(), 2)
        assert env.adapter.calls[0] == ("mute", "2", 1)
        assert not any(call[0] == "recall" for call in env.adapter.calls)
        assert not env.release.is_set()
        assert len(env.downloads) == 2
    finally:
        release_mute.set()
        await runtime.close()


@pytest.mark.asyncio
async def test_private_images_never_enter_the_group_queue(media_queue):
    env = media_queue
    assert not enqueue(env, 100, group="private/2/MjI")
    assert not env.runtime.tasks
    assert env.runtime.counts["admitted"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output,returncode,counter",
    [
        (b'{"error":"media_image_unsupported_format"}', 1, "skipped"),
        (b'{"error":"media_ocr_failed"}', 1, "failed"),
        (b'{"qr":"unknown","digits":false}', 0, "failed"),
        (b"not-json", 0, "failed"),
    ],
)
async def test_failed_or_unsupported_recognition_is_unchecked_not_clear(
    media_queue, output, returncode, counter
):
    env = media_queue
    env.output, env.returncode = output, returncode
    assert enqueue(env, 100)
    env.release.set()
    await asyncio.wait_for(asyncio.gather(*env.runtime.tasks), 3)
    assert env.runtime.counts[counter] == 1
    assert env.runtime.counts["completed"] == 1
    assert env.runtime.counts["unchecked"] == 1
    assert env.runtime.counts["clear"] == 0
    assert not env.adapter.calls


@pytest.mark.asyncio
async def test_one_cancelled_worker_does_not_drop_the_rest_of_the_queue(media_queue):
    env = media_queue
    for mid in range(100, 112):
        assert enqueue(env, mid)
    await asyncio.wait_for(env.started.get(), 1)
    await asyncio.wait_for(env.started.get(), 1)
    task = next(iter(env.runtime.tasks))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    env.release.set()
    await asyncio.wait_for(asyncio.gather(*env.runtime.tasks), 3)
    assert env.runtime.counts["completed"] == 12
    assert env.runtime.counts["cancelled"] == 1
    assert env.runtime.counts["unchecked"] == 1
    assert env.runtime.counts["clear"] == 11
    assert not env.runtime.pending
    assert not env.runtime.tasks


@pytest.mark.asyncio
async def test_close_kills_active_recognizers_and_terminates_waiting_metadata(
    media_queue, monkeypatch
):
    env = media_queue
    entered = asyncio.Queue()
    processes = []

    async def spawn(*args, **kwargs):
        process = SimpleNamespace(returncode=None, pid=101 + len(processes))

        async def communicate(_):
            entered.put_nowait(process.pid)
            await asyncio.Event().wait()

        async def reap():
            process.returncode = -9

        process.communicate = communicate
        process.wait = AsyncMock(side_effect=reap)
        processes.append(process)
        return process

    kill = Mock()
    monkeypatch.setattr(media_moderation.os, "killpg", kill)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(side_effect=spawn))
    for mid in range(100, 105):
        assert enqueue(env, mid)
    env.release.set()
    await asyncio.wait_for(entered.get(), 1)
    await asyncio.wait_for(entered.get(), 1)
    await asyncio.wait_for(env.runtime.close(), 2)
    assert kill.call_count == 2
    for process in processes:
        process.wait.assert_awaited_once()
    assert env.runtime.counts["cancelled"] == 5
    assert env.runtime.counts["completed"] == 5
    assert env.runtime.counts["unchecked"] == 5
    assert env.runtime.counts["clear"] == 0
    assert not env.runtime.pending
    assert not env.runtime.tasks
    assert not env.runtime.downloads
    assert not env.adapter.calls


@pytest.mark.asyncio
async def test_media_total_retains_time_spent_waiting_in_the_queue(media_queue):
    env = media_queue
    for mid in (100, 101, 102):
        assert enqueue(env, mid)
    await asyncio.wait_for(env.started.get(), 1)
    await asyncio.wait_for(env.started.get(), 1)
    env.offset = 2
    env.release.set()
    await asyncio.wait_for(asyncio.gather(*env.runtime.tasks), 3)
    waits = [
        call.args[1]
        for call in env.adapter.diagnostics.observe.call_args_list
        if call.args[0] == "media_queue"
    ]
    totals = [
        call.args[1]
        for call in env.adapter.diagnostics.observe.call_args_list
        if call.args[0] == "media_total"
    ]
    assert max(waits) >= 2
    assert len(totals) == 3
    assert all(total >= 2 for total in totals)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change,outcome",
    [
        ("expired", "unchecked_expired"),
        ("history", "unchecked_historical"),
        ("metadata", "unchecked_metadata_invalid"),
        ("missing_time", "unchecked_time_invalid"),
        ("origin", "unchecked_origin_unreviewed"),
    ],
)
async def test_ineligible_images_report_unchecked_reason_before_admission(
    media_queue, change, outcome
):
    env = media_queue
    if change == "expired":
        env.payload["recall_route"]["time"] = str(int((time.time() - 31) * 1000))
    elif change == "history":
        env.payload["recall_route"]["time"] = str(int((time.time() - 5) * 1000))
    elif change == "metadata":
        env.payload["image"]["width"] = -1
    elif change == "missing_time":
        del env.payload["recall_route"]["time"]
    else:
        env.adapter.business.deployment.origin = "https://unreviewed.invalid"
    assert not enqueue(env, 100)
    assert env.runtime.counts["admitted"] == 0
    assert env.runtime.counts["unchecked"] == 1
    assert env.runtime.counts["clear"] == 0
    assert env.adapter.diagnostics.emit.call_args.args[1] == outcome
    assert not env.runtime.tasks
    assert not env.downloads


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,counter",
    [
        ("accepted", "recall_accepted"),
        ("verified", "recall_accepted"),
        ("rejected", "enforcement_rejected"),
        ("unknown", "enforcement_unknown"),
        ("preflight_failed", "enforcement_unknown"),
    ],
)
async def test_enforcement_consumed_flag_is_not_an_accepted_recall(
    media_queue, monkeypatch, status, counter
):
    env = media_queue
    env.output = b'{"qr":true,"digits":false}'

    async def execute(request, *, authorize):
        authorize()
        return {"status": status}

    action = AsyncMock(side_effect=execute)
    monkeypatch.setattr(env.adapter, "execute_action", action)
    if status == "preflight_failed":
        env.adapter.get_moderation_members.side_effect = ProtocolError(
            "moderation_identity"
        )
    assert enqueue(env, 100)
    env.release.set()
    await asyncio.wait_for(asyncio.gather(*env.runtime.tasks), 3)
    assert env.runtime.counts["matched"] == 1
    assert env.runtime.counts["clear"] == 0
    assert env.runtime.counts[counter] == 1
    if counter != "recall_accepted":
        assert env.runtime.counts["recall_accepted"] == 0
    calls = 0 if status == "preflight_failed" else 1
    assert action.await_count == calls
    assert enqueue(env, 100)
    assert not env.runtime.tasks
    assert action.await_count == calls


@pytest.mark.asyncio
async def test_cancelled_enforcement_is_unknown_not_an_unchecked_image(
    media_queue, monkeypatch
):
    env = media_queue
    env.output = b'{"qr":true,"digits":false}'
    entered = asyncio.Event()

    async def enforce(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(content_rules, "enforce", enforce)
    assert enqueue(env, 100)
    env.release.set()
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.wait_for(env.runtime.close(), 2)
    assert env.runtime.counts["matched"] == 1
    assert env.runtime.counts["cancelled"] == 1
    assert env.runtime.counts["enforcement_unknown"] == 1
    assert env.runtime.counts["unchecked"] == 0
    assert env.runtime.counts["clear"] == 0
    assert env.runtime.counts["recall_accepted"] == 0


@pytest.mark.asyncio
async def test_offline_1000_mixed_group_enqueue_benchmark(media_queue):
    env = media_queue
    env.release.set()
    other = copy.copy(env.adapter)
    other.account = "9"
    other.config = copy.deepcopy(env.adapter.config)
    timings, pending_peak = [], 0
    kinds = ("image", "text", "image", "video", "forward")
    for batch in range(25):
        for number in range(batch * 40, (batch + 1) * 40):
            adapter = other if number % 2 else env.adapter
            group = "6" if number // 2 % 2 else "5"
            payload = copy.deepcopy(env.payload)
            payload["kind"] = kinds[number % len(kinds)]
            mid = str(1000 + number)
            payload["image"]["asset"] = mid
            view = scoped(adapter, group)
            started = time.perf_counter_ns()
            admitted = env.runtime.enqueue(view, group, mid, payload)
            timings.append((time.perf_counter_ns() - started) / 1_000_000)
            assert admitted == (payload["kind"] == "image")
            pending_peak = max(pending_peak, env.runtime.queued)
            assert len(env.runtime.tasks) <= 2
        await asyncio.wait_for(asyncio.gather(*env.runtime.tasks), 3)
    assert env.runtime.counts["admitted"] == 400
    assert env.runtime.counts["completed"] == 400
    assert env.runtime.counts["clear"] == 400
    assert env.runtime.counts["unchecked"] == 0
    assert not env.runtime.pending
    timings.sort()
    print(
        json.dumps(
            {
                "mode": "offline_mixed_group_enqueue",
                "messages": 1000,
                "image_jobs": 400,
                "ineligible_nonimages": 600,
                "pending_peak": pending_peak,
                "worker_limit": 2,
                "counts": env.runtime.counts,
                "enqueue_ms": {
                    key: round(timings[math.ceil(len(timings) * rank) - 1], 6)
                    for key, rank in (("p95", 0.95), ("p99", 0.99))
                },
                "boundaries": "enqueue call only; envelope preparation and drain excluded",
                "http_ocr": "fake blocked/released download and fake OCR process; no real HTTP/OCR",
            },
            sort_keys=True,
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("offered", [500, 1000])
async def test_offline_saturated_burst_terminal_accounting_and_enqueue_benchmark(
    media_queue, offered
):
    env = media_queue
    other = copy.copy(env.adapter)
    other.account = "9"
    other.config = copy.deepcopy(env.adapter.config)
    timings = []
    for number in range(offered):
        adapter = env.adapter if number < 2 or (number - 2) % 2 == 0 else other
        group = "5" if number < 2 or (number - 2) // 2 % 2 == 0 else "6"
        payload = copy.deepcopy(env.payload)
        mid = str(1000 + number)
        payload["image"]["asset"] = mid
        view = scoped(adapter, group)
        started = time.perf_counter_ns()
        admitted = env.runtime.enqueue(view, group, mid, payload)
        timings.append((time.perf_counter_ns() - started) / 1_000_000)
        assert admitted == (number < 66)
        if number == 1:
            await asyncio.wait_for(env.started.get(), 1)
            await asyncio.wait_for(env.started.get(), 1)
    assert env.runtime.queued == 64
    assert len(env.runtime.tasks) == 2
    assert len(env.downloads) == 2
    assert not env.processes
    assert env.runtime.counts["clear"] == 0
    assert env.runtime.counts["completed"] == 0
    env.release.set()
    await asyncio.wait_for(asyncio.gather(*env.runtime.tasks), 3)
    counts = env.runtime.counts
    assert counts["admitted"] == counts["completed"] == counts["clear"] == 66
    assert counts["queue_full"] == counts["unchecked"] == offered - 66
    assert counts["completed"] + counts["queue_full"] == offered
    assert counts["failed"] == counts["cancelled"] == counts["expired"] == 0
    assert not env.runtime.pending
    assert not env.runtime.tasks
    timings.sort()
    print(
        json.dumps(
            {
                "mode": "offline_saturated_group_enqueue",
                "messages": offered,
                "pending_peak": 64,
                "workers_peak": 2,
                "counts": counts,
                "terminal_accounted": counts["completed"] + counts["queue_full"],
                "enqueue_ms": {
                    key: round(timings[math.ceil(len(timings) * rank) - 1], 6)
                    for key, rank in (("p95", 0.95), ("p99", 0.99))
                },
                "boundaries": "enqueue call only; fake downloads stay blocked throughout burst",
                "http_ocr": "fake download and fake OCR process; no real HTTP/OCR latency measured",
            },
            sort_keys=True,
        )
    )


@pytest.mark.asyncio
async def test_opt_in_real_local_ocr_twelve_image_burst(bot, monkeypatch):
    """Measure real local recognition with memory-only HTTP and no native effects.

    Args:
        bot: Isolated adapter with a temporary ingress ledger and fake native IO.
        monkeypatch: Restore HTTP, process observation and enforcement boundaries.
    """
    if os.environ.get("WSL_OFFLINE_REAL_OCR_BURST") != "1":
        pytest.skip("Set WSL_OFFLINE_REAL_OCR_BURST=1 for twelve local OCR workers")
    pytest.importorskip("cv2")
    if not shutil.which("tesseract"):
        pytest.skip("Optional local Tesseract runtime is not installed")
    configure(bot)
    font = ImageFont.truetype("DejaVuSans.ttf", 56)
    endpoints = media_moderation.REVIEWED_MEDIA_ENDPOINTS[
        bot.business.deployment.origin
    ]
    bitmaps = {}
    for number in range(12):
        bitmap = Image.new("RGB", (600, 180), "white")
        draw = ImageDraw.Draw(bitmap)
        draw.text((30, 40), str(123450 + number), font=font, fill="black")
        buffer = io.BytesIO()
        bitmap.save(buffer, "PNG")
        bitmaps[endpoints[1] + str(900 + number)] = buffer.getvalue()
    assert len(set(bitmaps.values())) == 12
    downloads, processes, completed = [], [], []
    peak_processes = 0
    worker_path = str(Path(media_moderation.__file__).with_name("media_worker.py"))
    spawn = asyncio.create_subprocess_exec

    def forbidden(*args, **kwargs):
        raise AssertionError("Real network and native effects are forbidden")

    class Response:
        status = 200

        def __init__(self, data):
            self.data = data
            self.content = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def iter_chunked(self, size):
            for offset in range(0, len(self.data), size):
                yield self.data[offset : offset + size]

    class Session:
        def __init__(self, **options):
            assert options["trust_env"] is False
            assert options["timeout"].total == 5

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        def get(self, url, **options):
            assert options == {"allow_redirects": False, "ssl": True}
            downloads.append(url)
            return Response(bitmaps[url])

    async def observe_spawn(*args, **kwargs):
        nonlocal peak_processes
        assert args[:2] == (sys.executable, worker_path)
        process = await spawn(*args, **kwargs)
        processes.append(process)
        peak_processes = max(
            peak_processes,
            sum(process.returncode is None for process in processes),
        )
        return process

    async def assess_only(adapter, group, mid, payload, *, assessment, execution_check):
        execution_check()
        assert assessment["category"] == "image_digits"
        assert media_moderation.verified_assessment(
            adapter, group, mid, payload, assessment
        )
        completed.append((mid, time.time() - source_stamp))
        return False

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(media_moderation.aiohttp, "ClientSession", Session)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", observe_spawn)
    monkeypatch.setattr(content_rules, "enforce", assess_only)
    monkeypatch.setattr(bot, "execute_action", Mock(side_effect=forbidden))
    monkeypatch.setattr(bot, "execute_moderation", Mock(side_effect=forbidden))
    monkeypatch.setattr(bot.nim, "request", Mock(side_effect=forbidden))
    runtime = media_moderation.MediaModeration()
    try:
        payload = await ingest(bot, "900", "", kind="image")
        source_stamp = float(payload["recall_route"]["time"]) / 1000
        started = time.monotonic()
        for number in range(12):
            mid = str(900 + number)
            envelope = copy.deepcopy(payload)
            envelope["image"] = {
                "asset": mid,
                "width": 600,
                "height": 180,
                "size": len(bitmaps[endpoints[1] + mid]),
            }
            assert runtime.enqueue(scoped(bot, "5"), "5", mid, envelope)
        assert runtime.queued == 12
        assert len(runtime.tasks) == 2
        await asyncio.wait_for(asyncio.gather(*runtime.tasks), 30)
        elapsed = time.monotonic() - started
        assert elapsed < 30
        assert peak_processes == 2
        assert len(downloads) == len(processes) == len(completed) == 12
        assert all(process.returncode == 0 for process in processes)
        assert max(age for _, age in completed) < 30
        assert runtime.counts["admitted"] == runtime.counts["completed"] == 12
        assert runtime.counts["matched"] == 12
        assert runtime.counts["enforcement_not_applied"] == 12
        for key in (
            "clear",
            "unchecked",
            "failed",
            "expired",
            "cancelled",
            "enforcement_unknown",
            "recall_accepted",
        ):
            assert runtime.counts[key] == 0
        assert not runtime.pending and not runtime.tasks and not runtime.downloads
        bot.execute_action.assert_not_called()
        bot.execute_moderation.assert_not_called()
        bot.nim.request.assert_not_called()
        bot.get_moderation_members.assert_not_awaited()
        timings = {
            name: sorted(
                call.args[1] * 1000
                for call in bot.diagnostics.observe.call_args_list
                if call.args[0] == name
            )
            for name in ("media_queue", "media_recognizer", "media_total")
        }
        timings["source_to_assessment"] = sorted(age * 1000 for _, age in completed)
        print(
            json.dumps(
                {
                    "mode": "opt_in_real_local_ocr_burst",
                    "images": 12,
                    "real_recognizer_processes": len(processes),
                    "peak_real_processes": peak_processes,
                    "elapsed_ms": round(elapsed * 1000, 3),
                    "counts": runtime.counts,
                    "timings_ms": {
                        metric: {
                            key: round(values[math.ceil(len(values) * rank) - 1], 3)
                            for key, rank in (("p95", 0.95), ("p99", 0.99))
                        }
                        for metric, values in timings.items()
                    },
                    "boundaries": "real OpenCV/Tesseract; in-memory fake HTTP; enforcement proof spy only",
                    "native_actions": 0,
                    "real_http_requests": 0,
                    "source_deadline_seconds": 30,
                },
                sort_keys=True,
            )
        )
    finally:
        await runtime.close()
