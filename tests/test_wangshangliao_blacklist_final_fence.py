"""Offline regressions for the blacklist's last synchronous submission fence."""

import asyncio
import inspect
import json
import sqlite3
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

from astrbot.core.platform.sources.wangshangliao import (
    adapter as adapter_module,
)
from astrbot.core.platform.sources.wangshangliao import (
    blacklist,
    moderation,
)
from astrbot.core.platform.sources.wangshangliao import (
    event as event_module,
)
from astrbot.core.platform.sources.wangshangliao.actions import ActionRequest
from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError


@pytest.fixture
def fence_case(tmp_path, monkeypatch):
    """Bind real execution methods to isolated ledgers and a mock native client.

    Args:
        tmp_path: Test-owned ledger directory.
        monkeypatch: Scoped replacements preventing production IO.

    Returns:
        Adapter, durable store, reserved intent and recorded mock mutations.
    """
    for module in (blacklist, moderation, adapter_module):
        monkeypatch.setattr(
            module, "instance_dir", lambda instance: tmp_path / instance
        )
    for module in (event_module, moderation):
        monkeypatch.setattr(module, "is_managed_account", lambda _: False)
    monkeypatch.setattr(
        aiohttp.ClientSession,
        "_request",
        AsyncMock(
            side_effect=AssertionError("Network access forbidden in offline tests")
        ),
    )
    store = blacklist.BlacklistStore("final-fence")
    store.set_member("1", "5", "2", "22", "Member", True, "9", lambda: None)
    intent = store.reserve("1", "5", "2", "22", "101", time.time(), lambda: None)
    mutations = []
    client = SimpleNamespace(fields=[0, 0, 0, "1"], lock=asyncio.Lock())

    async def request(route, parameters, *, authorize=None):
        if route == "/v1/group/get-group-list":
            return {"owner": [{"groupId": 5, "me": {"role": "GROUP_ROLE_ADMIN"}}]}
        if route == "/v1/group/get-group-members":
            return {
                "groupMemberInfo": [
                    {"userId": 2, "nimId": "22", "groupRole": "GROUP_ROLE_MEMBER"}
                ]
            }
        assert route == "/v1/group/set-member-mute"
        async with client.lock:
            try:
                if authorize is not None:
                    checked = authorize()
                    if inspect.isawaitable(checked):
                        await checked
            except BaseException as exc:
                exc.submitted = False
                raise
            mutations.append((str(client.fields[3]), route, dict(parameters)))
            return {}

    client.request = AsyncMock(side_effect=request)
    adapter = SimpleNamespace(
        account="1",
        config={
            "id": "final-fence",
            "enable": True,
            "_business_active": True,
            "_business_epoch": "epoch-1",
            "_business_revision": "revision-1",
            "_open_revisions": {"5": "group-1", "6": "other-group-1"},
            "enabled_groups": ["5"],
            "moderation": {
                "enabled": True,
                "permissions": {"5": ["mute"]},
            },
        },
        members={"5": {"2": "22"}},
        business=client,
        stopping=asyncio.Event(),
        get_moderation_members=AsyncMock(),
    )
    for name in ("execute_action", "execute_moderation", "_execute_moderation"):
        setattr(adapter, name, getattr(WangshangliaoAdapter, name).__get__(adapter))
    return adapter, store, intent, mutations


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["action", "adapter"])
@pytest.mark.parametrize(
    ("change", "error"),
    [
        ("identity", "moderation_identity"),
        ("group_revision", "action_scope_changed"),
        ("permission", "moderation_permission"),
    ],
)
async def test_final_fence_rejects_changes_during_native_db_wait(
    fence_case, monkeypatch, entrypoint, change, error
):
    adapter, store, intent, mutations = fence_case
    entered, release = threading.Event(), threading.Event()
    original = moderation.authorize_intent
    calls = 0

    def contended(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
            assert release.wait(5), "Final native authorization was not released"
        original(*args)

    monkeypatch.setattr(moderation, "authorize_intent", contended)
    if entrypoint == "action":
        execution = adapter.execute_action(
            ActionRequest.bind(
                adapter,
                intent.operation,
                "mute",
                5,
                member=2,
                minutes=intent.minutes,
                expected_nim=intent.peer,
                blacklist_intent=intent,
            )
        )
    else:
        execution = adapter.execute_moderation(
            intent.operation,
            "mute",
            5,
            2,
            minutes=intent.minutes,
            blacklist_intent=intent,
        )
    task = asyncio.create_task(execution)
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        if change == "identity":
            adapter.business.fields[3] = "3"
        elif change == "group_revision":
            adapter.config["_open_revisions"]["5"] = "group-2"
        else:
            adapter.config["moderation"]["permissions"]["5"] = []
        release.set()
        with pytest.raises(ProtocolError, match=error) as caught:
            await asyncio.wait_for(task, 5)
        assert caught.value.submitted is False
        assert mutations == []
        with sqlite3.connect(store.path.parent / "moderation.sqlite3") as db:
            evidence = json.loads(
                db.execute(
                    "SELECT result FROM operations WHERE id=?", (intent.operation,)
                ).fetchone()[0]
            )
        assert evidence["status"] == "rejected"
        assert evidence["submitted"] is False
        store.done(intent, evidence)
        assert (
            store.reserve("1", "5", "2", "22", "102", time.time(), lambda: None)
            is not None
        )
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("change_other_group", [False, True])
async def test_current_scope_and_unrelated_group_changes_still_submit(
    fence_case, monkeypatch, change_other_group
):
    adapter, store, intent, mutations = fence_case
    original = moderation.authorize_intent
    calls = 0

    def checked(*args):
        nonlocal calls
        calls += 1
        original(*args)
        if calls == 2 and change_other_group:
            adapter.config["_open_revisions"]["6"] = "other-group-2"

    monkeypatch.setattr(moderation, "authorize_intent", checked)
    result = await adapter.execute_moderation(
        intent.operation,
        "mute",
        5,
        2,
        minutes=intent.minutes,
        blacklist_intent=intent,
    )
    assert calls == 2
    assert result["status"] == "accepted"
    assert result["submitted"] is True
    assert mutations == [
        ("1", "/v1/group/set-member-mute", {"groupId": 5, "userId": 2, "min": 41760})
    ]
    store.done(intent, result)
    assert store.read("1", "5")[1][0]["kicks"] == 0


@pytest.mark.asyncio
async def test_group_revision_is_frozen_before_directory_wait(fence_case):
    adapter, _, intent, mutations = fence_case

    async def changed(_):
        adapter.config["_open_revisions"]["5"] = "group-2"

    adapter.get_moderation_members.side_effect = changed
    with pytest.raises(ProtocolError, match="action_scope_changed"):
        await adapter.execute_moderation(
            intent.operation,
            "mute",
            5,
            2,
            minutes=intent.minutes,
            blacklist_intent=intent,
        )
    assert mutations == []
