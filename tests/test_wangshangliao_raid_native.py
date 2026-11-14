"""Exercise short-mute defense through real action, adapter and native fences."""

import asyncio
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import raid_defense
from astrbot.core.platform.sources.wangshangliao import raid
from astrbot.core.platform.sources.wangshangliao.actions import ActionRequest
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from tests import test_wangshangliao_blacklist_final_fence as native_fence

native_case = native_fence.fence_case


@pytest.fixture
def raid_case(native_case, tmp_path, monkeypatch):
    """Replace only the test capability; production protections remain unchanged."""
    adapter, _, _, mutations = native_case
    monkeypatch.setattr(raid, "instance_dir", lambda instance: tmp_path / instance)
    store = raid.RaidStore(adapter.config["id"])
    now = time.time()
    store.configure("1", "5", True, now=now - 2)
    store.join("1", "5", "2", "22", "901", now - 1, guard=lambda: None, now=now)
    intent = store.reserve(
        "1", "5", "2", kind="burst", trigger="902", guard=lambda: None
    )
    adapter.config["moderation"]["raid_defense_enabled"] = True
    return adapter, store, intent, mutations


@pytest.mark.asyncio
async def test_stored_intent_reaches_native_short_mute_once(raid_case):
    adapter, _, intent, mutations = raid_case
    request = ActionRequest.bind(
        adapter,
        intent.operation,
        "mute",
        5,
        member=2,
        minutes=10,
        expected_nim="22",
        raid_intent=intent,
    )
    result = await adapter.execute_action(request, authorize=lambda: None)
    assert result["status"] == "accepted"
    assert result["minutes"] == 10
    assert len(mutations) == 1
    await adapter.execute_action(request, authorize=lambda: None)
    assert len(mutations) == 1


@pytest.mark.asyncio
async def test_runtime_and_native_fences_share_one_final_admission_read(
    raid_case, monkeypatch
):
    adapter, store, old, mutations = raid_case
    store.finish(old, "rejected")
    adapter.connection_state = "online"
    adapter.get_moderation_members.return_value = {
        "complete": True,
        "groupMemberInfo": [
            {"userId": "2", "nimId": "22", "groupRole": "GROUP_ROLE_MEMBER"}
        ],
    }
    service = SimpleNamespace(
        active=True,
        settings={"paused": False},
        capacity_paused=False,
        suspended_groups=set(),
        membership_receiving={},
        reconciling_groups={},
        tenants={
            ("1", "5"): {
                "active": True,
                "present": True,
                "paused": False,
                "native_role": "GROUP_ROLE_ADMIN",
                "rules": {"raid_defense_enabled": True},
            }
        },
    )
    runtime = raid_defense.RaidDefense(service)
    runtime.protected = AsyncMock(return_value=False)
    monkeypatch.setattr(raid_defense, "muted_member_ids", AsyncMock(return_value=set()))
    original = adapter.business.request.side_effect

    async def request(route, parameters, *, authorize=None):
        if route == "/v1/group/get-apply-logs":
            assert adapter.business._authorization_task is asyncio.current_task()
            return {
                "list": [
                    {
                        "groupId": "5",
                        "applyId": "901",
                        "applyTime": datetime.fromtimestamp(
                            time.time() - 1, timezone.utc
                        ).isoformat(),
                        "state": "MEMBER_STATE_GOOD",
                        "groupApplyType": "APPLY",
                        "applicant": {"userId": "2", "nimId": "22"},
                    }
                ]
            }
        previous = getattr(adapter.business, "_authorization_task", None)
        if authorize is not None:
            adapter.business._authorization_task = asyncio.current_task()
        try:
            return await original(route, parameters, authorize=authorize)
        finally:
            adapter.business._authorization_task = previous

    adapter.business.request.side_effect = request
    try:
        result = await runtime.mute(
            adapter, "5", "2", kind="burst", trigger="new", guard=lambda: None
        )
        assert result == "accepted"
        assert len(mutations) == 1
        assert (
            sum(
                call.args[0] == "/v1/group/get-apply-logs"
                for call in adapter.business.request.await_args_list
            )
            == 1
        )
        assert store.snapshot("1", "5")["members"][0]["mute_until"] > time.time() + 590
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["disabled", "permission", "identity", "stopping"])
async def test_revocation_during_native_database_wait_blocks_submission(
    raid_case,
    monkeypatch,
    change,
):
    adapter, _, intent, mutations = raid_case
    entered, release = threading.Event(), threading.Event()
    original = raid.authorize_raid
    blocked = False

    def contended(*args):
        nonlocal blocked
        if adapter.business.lock.locked() and not blocked:
            blocked = True
            entered.set()
            assert release.wait(5)
        original(*args)

    monkeypatch.setattr(raid, "authorize_raid", contended)
    task = asyncio.create_task(
        adapter.execute_action(
            ActionRequest.bind(
                adapter,
                intent.operation,
                "mute",
                5,
                member=2,
                minutes=10,
                expected_nim="22",
                raid_intent=intent,
            ),
            authorize=lambda: None,
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        if change == "disabled":
            adapter.config["moderation"]["raid_defense_enabled"] = False
        elif change == "permission":
            adapter.config["moderation"]["permissions"]["5"] = []
        elif change == "identity":
            adapter.business.fields[3] = "3"
        else:
            adapter.stopping.set()
        release.set()
        with pytest.raises(ProtocolError):
            await asyncio.wait_for(task, 5)
        assert mutations == []
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
