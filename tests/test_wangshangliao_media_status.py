"""Offline media status snapshots never admit work or imply unchecked clearance."""

import asyncio
import copy
import json
from collections import OrderedDict, deque
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import media_moderation
from astrbot.builtin_stars.wangshangliao_moderation.open_rpc import OpenCall, OpenRPC
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError


@pytest.fixture
def media_status(monkeypatch):
    """Provide the real idle runtime with synthetic read-only status dependencies.

    Args:
        monkeypatch: Restore blocked media execution boundaries after each test.

    Returns:
        RPC dispatcher, real runtime and synthetic service.
    """
    media = media_moderation.MediaModeration()
    for method in ("enqueue", "_consume", "inspect", "_download", "_observe_queue"):
        monkeypatch.setattr(
            media,
            method,
            Mock(side_effect=AssertionError("Status must not execute media work")),
        )
    monkeypatch.setattr(
        media_moderation.aiohttp,
        "ClientSession",
        Mock(side_effect=AssertionError("Real HTTP is forbidden in status tests")),
    )
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        Mock(side_effect=AssertionError("Status must not launch OCR")),
    )
    service = SimpleNamespace(
        integrated=True,
        plugin=SimpleNamespace(media_moderation=media),
        budget={"version": 1, "confirmed": False, "provider": "", "checked": 0},
        settings={"paused": False},
        tenants={},
        suspended_groups=set(),
        capacity_paused=False,
        disk_free_bytes=4 * 1024**3,
        adapters=lambda: [],
        store=SimpleNamespace(
            usage=Mock(return_value={}),
            manager_quotas=Mock(return_value=[]),
            audit=Mock(return_value=[]),
        ),
        plans=SimpleNamespace(status=Mock(return_value={"prices": []})),
        group_ai=SimpleNamespace(configs={}),
        model={},
        context=SimpleNamespace(get_all_providers=lambda: []),
        profiles=SimpleNamespace(rows=Mock(return_value=[]), inventory=lambda: {}),
        quota_admins=[],
    )
    return OpenRPC(service), media, service


@pytest.mark.asyncio
@pytest.mark.parametrize("absent", ["missing", "none"])
async def test_missing_media_runtime_is_unavailable_not_clear(media_status, absent):
    rpc, _, service = media_status
    if absent == "missing":
        del service.plugin.media_moderation
    else:
        service.plugin.media_moderation = None
    result = await rpc.dispatch(OpenCall(action="status", actor="dashboard/offline"))
    assert result["media_inspection"] is None


@pytest.mark.asyncio
async def test_idle_media_status_does_not_create_workers_or_mutate_runtime(
    media_status, monkeypatch
):
    rpc, media, _ = media_status
    counts = copy.deepcopy(media.counts)
    before = set(asyncio.all_tasks())
    with monkeypatch.context() as scope:
        scope.setattr(
            asyncio,
            "create_task",
            Mock(side_effect=AssertionError("Status must not create workers")),
        )
        first = await rpc.dispatch(OpenCall(action="status", actor="dashboard/offline"))
        second = await rpc.dispatch(
            OpenCall(action="status", actor="dashboard/offline")
        )
    assert first["media_inspection"] == second["media_inspection"]
    assert first["media_inspection"] == {
        **counts,
        "pending": 0,
        "running": 0,
        "capacity": media_moderation.MAX_PENDING_IMAGES,
        "account_capacity": media_moderation.MAX_ACCOUNT_PENDING_IMAGES,
        "group_capacity": media_moderation.MAX_GROUP_PENDING_IMAGES,
        "workers": media_moderation.MAX_MEDIA_WORKERS,
    }
    assert media.counts == counts
    assert media.active
    assert not media.tasks and not media.running and not media.pending
    assert not media.cache and not media.seen and not media.downloads
    assert set(asyncio.all_tasks()) == before
    for method in ("enqueue", "_consume", "inspect", "_download", "_observe_queue"):
        getattr(media, method).assert_not_called()


@pytest.mark.asyncio
async def test_media_status_uses_metadata_queue_not_worker_task_count(media_status):
    rpc, media, _ = media_status
    media.queued = 12
    media.tasks = {object(), object()}
    media.running = set(media.tasks)
    media.pending = OrderedDict(
        {
            ("offline", "99"): OrderedDict(
                {"5": deque([{"asset": "never-expose-image-metadata"}] * 12)}
            )
        }
    )
    media.counts["admitted"] = 14
    pending = copy.deepcopy(media.pending)
    result = await rpc.dispatch(OpenCall(action="status", actor="dashboard/offline"))
    state = result["media_inspection"]
    assert state["pending"] == 12
    assert state["running"] == 2
    assert state["workers"] == 2
    assert state["capacity"] == 64
    assert state["account_capacity"] == 32
    assert state["group_capacity"] == 16
    assert state["clear"] == 0
    assert media.pending == pending
    assert len(media.tasks) == len(media.running) == 2
    assert "never-expose-image-metadata" not in json.dumps(state)
    state["clear"] = 999
    assert media.counts["clear"] == 0


@pytest.mark.asyncio
async def test_expired_overloaded_and_cancelled_images_never_become_clear(
    media_status,
):
    rpc, media, _ = media_status
    media.active = False
    media.counts.update(
        admitted=10,
        completed=10,
        busy=9,
        queue_full=9,
        expired=7,
        cancelled=3,
        unchecked=19,
    )
    counts = dict(media.counts)
    result = await rpc.dispatch(OpenCall(action="status", actor="dashboard/offline"))
    state = result["media_inspection"]
    assert {key: state[key] for key in counts} == counts
    assert state["pending"] == state["running"] == state["clear"] == 0
    assert state["queue_full"] == state["busy"] == 9
    assert state["expired"] == 7
    assert state["unchecked"] == 19
    assert media.counts == counts
    assert not media.active


@pytest.mark.asyncio
async def test_matched_unknown_enforcement_is_not_reported_as_clear(media_status):
    rpc, media, _ = media_status
    media.counts.update(
        admitted=3,
        completed=3,
        matched=3,
        failed=3,
        enforcement_unknown=3,
    )
    result = await rpc.dispatch(OpenCall(action="status", actor="dashboard/offline"))
    state = result["media_inspection"]
    assert state["matched"] == state["enforcement_unknown"] == 3
    assert state["clear"] == 0
    assert not media.tasks


@pytest.mark.asyncio
async def test_status_capacity_comes_from_actual_scheduler_limits(
    media_status, monkeypatch
):
    rpc, _, _ = media_status
    for field, value in {
        "MAX_PENDING_IMAGES": 20,
        "MAX_ACCOUNT_PENDING_IMAGES": 10,
        "MAX_GROUP_PENDING_IMAGES": 5,
        "MAX_MEDIA_WORKERS": 1,
    }.items():
        monkeypatch.setattr(media_moderation, field, value)
    result = await rpc.dispatch(OpenCall(action="status", actor="dashboard/offline"))
    state = result["media_inspection"]
    assert (
        state["capacity"],
        state["account_capacity"],
        state["group_capacity"],
        state["workers"],
    ) == (20, 10, 5, 1)


@pytest.mark.asyncio
async def test_media_status_preserves_argument_rejection(media_status):
    rpc, media, service = media_status
    with pytest.raises(OpenError, match="status_arguments"):
        await rpc.dispatch(
            OpenCall(
                action="status",
                actor="dashboard/offline",
                payload={"inspect": True},
            )
        )
    assert not media.tasks and not media.pending
    assert not any(media.counts.values())
    service.store.usage.assert_not_called()
