"""Versioned actions reject stale scope without discarding completed receipts."""

import asyncio
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.core.platform.sources.wangshangliao import moderation
from astrbot.core.platform.sources.wangshangliao.actions import (
    ACTIONS,
    ActionRequest,
    dispatch,
)
from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError


@pytest.mark.asyncio
async def test_offline_suite_does_not_open_global_metrics_storage(monkeypatch):
    from astrbot.core.utils import metrics

    insert = AsyncMock()
    post = AsyncMock()
    monkeypatch.setattr(metrics.db_helper, "insert_platform_stats", insert)
    monkeypatch.setattr(metrics.Metric, "_post_metrics", post)
    await metrics.Metric.upload(msg_event_tick=1, adapter_name="wangshangliao")
    insert.assert_not_awaited()
    post.assert_not_awaited()


@pytest.fixture
def adapter():
    value = SimpleNamespace(
        account="1",
        config={
            "id": "fixture",
            "enable": True,
            "_business_active": True,
            "_business_epoch": "epoch-1",
            "_business_revision": "revision-1",
            "enabled_groups": ["5"],
            "moderation": {
                "enabled": True,
                "automation_enabled": True,
                "permissions": {"5": ["mute", "recall"]},
            },
        },
        members={"5": {"2": "n2"}},
        recall_violation=AsyncMock(return_value="accepted"),
        send_text=AsyncMock(return_value="accepted"),
        execute_moderation=AsyncMock(return_value={"status": "accepted"}),
        rename_member=AsyncMock(return_value={"status": "verified"}),
        cleanup_member=AsyncMock(return_value={"status": "unknown"}),
    )
    value.execute_action = WangshangliaoAdapter.execute_action.__get__(value)
    return value


def request(adapter, action="mute", **changes):
    values = {
        "member": 2
        if action
        in {"mute", "unmute", "kick", "rename", "cleanup", "recall", "warning"}
        else 0
    }
    if action in {"rename", "cleanup", "recall", "warning"}:
        values["expected_nim"] = "n2"
    if action == "rename":
        values.update(text="New", expected_card="Old")
    if action == "warning":
        values["text"] = "Warning"
    if action == "recall":
        values["message"] = "10"
    values.update(changes)
    operation = "recall/1/5/10" if action == "recall" else "operation-1"
    return ActionRequest.bind(adapter, operation, action, 5, **values)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", sorted(ACTIONS))
async def test_action_routes_preserve_operation_and_evidence(adapter, action):
    intent = request(adapter, action)
    receipt = await adapter.execute_action(intent)
    method = (
        adapter.rename_member
        if action == "rename"
        else adapter.cleanup_member
        if action == "cleanup"
        else adapter.recall_violation
        if action == "recall"
        else adapter.send_text
        if action == "warning"
        else adapter.execute_moderation
    )
    method.assert_awaited_once()
    if action == "recall":
        assert method.await_args.args == ("5", "10", "2")
    else:
        assert (
            method.await_args.args[0 if action != "warning" else 1] == intent.operation
        )
    assert receipt["version"] == 1
    assert receipt["instance"] == "fixture"
    assert receipt["account"] == "1"
    assert receipt["operation"] == intent.operation
    assert receipt["action"] == action


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"version": True},
        {"version": 2},
        {"version": "1"},
        {"instance": "another"},
        {"account": "another"},
        {"epoch": "expired"},
        {"revision": "expired"},
        {"operation": ""},
        {"operation": "a\nb"},
        {"action": "arbitrary"},
        {"action": []},
        {"group": True},
        {"group": 0},
        {"member": True},
        {"member": 0},
        {"minutes": False},
        {"minutes": 1441},
        {"text": None},
    ],
)
async def test_invalid_or_stale_requests_never_reach_transport(adapter, changes):
    with pytest.raises(ProtocolError):
        await adapter.execute_action(replace(request(adapter), **changes))
    adapter.execute_moderation.assert_not_awaited()
    adapter.rename_member.assert_not_awaited()
    adapter.cleanup_member.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation", ["automatic-kick/old", "message/5/2", "content/5/2", "progressive/old"]
)
async def test_legacy_automatic_kick_intent_cannot_use_v1(adapter, operation):
    with pytest.raises(ProtocolError, match="automatic_kick_not_authorized"):
        await adapter.execute_action(
            replace(request(adapter, "kick"), operation=operation)
        )
    adapter.execute_moderation.assert_not_awaited()


def test_request_is_immutable(adapter):
    intent = request(adapter)
    with pytest.raises(FrozenInstanceError):
        intent.account = "another"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["account", "epoch", "revision", "disabled"])
async def test_authority_wait_cannot_move_action_to_another_generation(adapter, change):
    entered, release = asyncio.Event(), asyncio.Event()

    async def authorize():
        entered.set()
        await release.wait()

    work = asyncio.create_task(
        adapter.execute_action(request(adapter), authorize=authorize)
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if change == "account":
            adapter.account = "another"
        elif change == "disabled":
            adapter.config["enable"] = False
        else:
            adapter.config["_business_" + change] = "new"
        release.set()
        with pytest.raises(ProtocolError):
            await asyncio.wait_for(work, 1)
        adapter.execute_moderation.assert_not_awaited()
    finally:
        release.set()
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)


@pytest.mark.asyncio
async def test_schedule_claim_is_deferred_until_transport_lock(adapter):
    locked = False

    async def authorize():
        assert locked

    async def execute(*args, scheduled_check):
        nonlocal locked
        locked = True
        await scheduled_check()
        return {"status": "accepted"}

    adapter.execute_moderation.side_effect = execute
    intent = replace(request(adapter, "mute_all"), operation="schedule/one")
    with pytest.raises(ProtocolError, match="schedule_authority"):
        await adapter.execute_action(intent)
    result = await adapter.execute_action(intent, authorize=authorize)
    assert result["status"] == "accepted"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [None, "unsupported", [], {}])
async def test_unrecognized_receipt_is_unknown_not_retryable(adapter, status):
    adapter.execute_moderation.return_value = {"status": status}
    result = await adapter.execute_action(request(adapter))
    assert result["status"] == "unknown"


@pytest.mark.asyncio
async def test_receipt_keeps_original_account_after_upstream_revocation(adapter):
    async def execute(*args, action_check, **kwargs):
        await action_check()
        adapter.account = "another"
        return {"status": "accepted", "account": "untrusted", "operation": "wrong"}

    adapter.execute_moderation.side_effect = execute
    result = await dispatch(adapter, request(adapter))
    assert result["status"] == "accepted"
    assert result["account"] == "1"
    assert result["operation"] == "operation-1"


@pytest.mark.asyncio
async def test_native_callback_revalidates_after_transport_wait(adapter):
    async def execute(*args, action_check, **kwargs):
        adapter.config["_business_revision"] = "new"
        await action_check()
        pytest.fail("Stale action reached transport")

    adapter.execute_moderation.side_effect = execute
    with pytest.raises(ProtocolError):
        await adapter.execute_action(request(adapter))


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["rename", "cleanup", "mute"])
async def test_real_native_methods_reject_revocation_before_upstream(
    adapter, monkeypatch, action
):
    adapter.config.update(
        enabled_groups=["5"],
        moderation={"enabled": True, "permissions": {"5": [action]}},
    )
    adapter.stopping = asyncio.Event()
    adapter.connection_state = "online"
    adapter.business = object()
    adapter.send_lock = asyncio.Lock()
    adapter.members = {"5": {"2": "n2"}}
    adapter.get_moderation_members = AsyncMock()
    for name in (
        "execute_moderation",
        "_execute_moderation",
        "rename_member",
        "cleanup_member",
    ):
        setattr(adapter, name, getattr(WangshangliaoAdapter, name).__get__(adapter))
    entered, release = asyncio.Event(), asyncio.Event()
    submitted = []

    async def execute(*args, authorize, **kwargs):
        entered.set()
        await release.wait()
        await authorize()
        submitted.append(args)
        return {"status": "accepted"}

    monkeypatch.setattr(moderation, "execute", execute)
    work = asyncio.create_task(adapter.execute_action(request(adapter, action)))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        adapter.config["_business_epoch"] = "revoked"
        release.set()
        with pytest.raises(ProtocolError):
            await asyncio.wait_for(work, 1)
        assert submitted == []
    finally:
        release.set()
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,changes",
    [
        ("recall", {"message": ""}),
        ("recall", {"message": "x" * 129}),
        ("recall", {"message": None}),
        ("recall", {"operation": "recall/another/5/10"}),
        ("recall", {"expected_nim": None}),
        ("recall", {"mention_name": "Member"}),
        ("warning", {"message": "10"}),
        ("warning", {"text": ""}),
        ("warning", {"text": "x" * 4097}),
        ("warning", {"mention_name": ""}),
        ("warning", {"mention_name": ["Member"]}),
        ("warning", {"mention_name": "Member", "text": "No leading mention"}),
        ("warning", {"mention_name": "Member", "text": "@Member Warning"}),
        (
            "warning",
            {"mention_name": "Member", "text": "@Member Warning", "expected_nim": "0"},
        ),
        (
            "warning",
            {
                "mention_name": "Member",
                "text": "@Member Warning",
                "expected_nim": str(1 << 32),
            },
        ),
    ],
)
async def test_malformed_rule_actions_never_submit(adapter, action, changes):
    with pytest.raises(ProtocolError):
        await adapter.execute_action(replace(request(adapter, action), **changes))
    adapter.recall_violation.assert_not_awaited()
    adapter.send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_warning_mention_is_frozen_and_uses_utf16_offsets(adapter):
    adapter.members["5"]["2"] = "902"
    name = "Member\U0001f600"
    adapter.get_moderation_members = AsyncMock(
        return_value={
            "complete": True,
            "groupMemberInfo": [{"userId": "2", "nimId": "902", "userNick": name}],
        }
    )
    intent = request(
        adapter,
        "warning",
        text=f"@{name} Warning",
        expected_nim="902",
        mention_name=name,
    )
    await adapter.execute_action(intent)
    call = adapter.send_text.await_args
    assert call.kwargs["auto_recall"] is True
    assert call.kwargs["mentions"] == [
        {
            "uid": 902,
            "nick": name,
            "start": 0,
            "end": len(f"@{name} ".encode("utf-16-le")) // 2,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "labels",
    [
        {"userNick": "Real\U0001f600 Member"},
        {"userNick": " ", "groupMemberNick": "Real\U0001f600 Member"},
        {"userNick": "902", "groupMemberNick": "Real\U0001f600 Member"},
        {"userNick": "2", "nick": "Real\U0001f600 Member"},
        {"name": "\x00Real\U0001f600  Member\n"},
    ],
)
async def test_warning_replaces_opaque_label_with_exact_members_nickname(
    adapter, labels
):
    adapter.members["5"]["2"] = "902"
    opaque = "e15ef91a04b74a41b64a3fa6e17f3c8d"
    adapter.get_moderation_members = AsyncMock(
        return_value={
            "complete": True,
            "groupMemberInfo": [
                {"userId": "3", "nimId": "903", "userNick": "Another Member"},
                {"userId": "2", "nimId": "902", "userName": opaque, **labels},
            ],
        }
    )
    intent = request(
        adapter,
        "warning",
        text=f"@{opaque} Warning body",
        expected_nim="902",
        mention_name=opaque,
    )
    await adapter.execute_action(intent)
    call = adapter.send_text.await_args
    name = "Real\U0001f600 Member"
    assert call.args == ("5", intent.operation, f"@{name} Warning body")
    assert opaque not in call.args[2]
    assert call.kwargs["mentions"] == [
        {
            "uid": 902,
            "nick": name,
            "start": 0,
            "end": len(f"@{name} ".encode("utf-16-le")) // 2,
        }
    ]
    assert call.kwargs["auto_recall"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("labels", ["login_only", "ids_only"])
async def test_verified_warning_without_nickname_sends_safe_body(adapter, labels):
    adapter.members["5"]["2"] = "902"
    opaque = "e15ef91a04b74a41b64a3fa6e17f3c8d"
    member = {"userId": "2", "nimId": "902", "userName": opaque}
    label = opaque
    if labels == "ids_only":
        member.update(userNick="902", groupMemberNick="2")
        label = "902"
    adapter.get_moderation_members = AsyncMock(
        return_value={
            "complete": True,
            "groupMemberInfo": [
                {"userId": "3", "nimId": "903", "userNick": "Another Member"},
                member,
            ],
        }
    )
    intent = request(
        adapter,
        "warning",
        text=f"@{label} Warning body",
        expected_nim="902",
        mention_name=label,
    )
    assert (await adapter.execute_action(intent))["status"] == "accepted"
    adapter.send_text.assert_awaited_once()
    call = adapter.send_text.await_args
    assert call.args == ("5", intent.operation, "Warning body")
    assert opaque not in call.args[2]
    assert call.kwargs["mentions"] is None
    assert call.kwargs["auto_recall"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["partial", "missing", "duplicate", "peer", "revoked"],
)
async def test_warning_never_substitutes_an_identifier_or_another_members_name(
    adapter, change
):
    adapter.members["5"]["2"] = "902"
    member = {"userId": "2", "nimId": "902", "userNick": "Member"}
    roster = {"complete": True, "groupMemberInfo": [member]}
    if change == "partial":
        roster["complete"] = False
    elif change == "missing":
        member["userId"] = "3"
    elif change == "duplicate":
        roster["groupMemberInfo"].append(dict(member))
    elif change == "peer":
        member["nimId"] = "903"

    async def directory(group):
        if change == "revoked":
            adapter.config["_business_epoch"] = "new"
        return roster

    adapter.get_moderation_members = AsyncMock(side_effect=directory)
    with pytest.raises(ProtocolError):
        await adapter.execute_action(
            request(
                adapter,
                "warning",
                text="@2 Warning",
                expected_nim="902",
                mention_name="2",
            )
        )
    adapter.send_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["recall", "warning"])
@pytest.mark.parametrize("change", ["permission", "automation", "peer"])
async def test_rule_authority_rechecked_after_async_business_callback(
    adapter, action, change
):
    async def authorize():
        await asyncio.sleep(0)
        if change == "permission":
            adapter.config["moderation"]["permissions"] = {}
        elif change == "automation":
            adapter.config["moderation"]["automation_enabled"] = False
        else:
            adapter.members["5"]["2"] = "another"

    with pytest.raises(ProtocolError):
        await adapter.execute_action(request(adapter, action), authorize=authorize)
    adapter.recall_violation.assert_not_awaited()
    adapter.send_text.assert_not_awaited()
