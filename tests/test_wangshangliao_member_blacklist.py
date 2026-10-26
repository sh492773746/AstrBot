"""Native and dashboard blacklist commands preserve identity and rejoin scope."""

import asyncio
import copy
import errno
import json
import re
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from pydantic import ValidationError

from astrbot.builtin_stars.wangshangliao_moderation import dashboard, member_blacklist
from astrbot.builtin_stars.wangshangliao_moderation.command_capabilities import (
    CAPABILITIES,
    command_help,
)
from astrbot.builtin_stars.wangshangliao_moderation.conversation_guidance import (
    GUIDANCE_REPLY_CONTRACT,
    MANUAL_PREFIX,
    READ_ONLY_GUIDANCE,
    admin_manual_documents,
    consultation_prompt,
    routed_consultation_prompt,
)
from astrbot.builtin_stars.wangshangliao_moderation.member_blacklist import REQUEST
from astrbot.builtin_stars.wangshangliao_moderation.open_rpc import OpenCall, OpenRPC
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from astrbot.builtin_stars.wangshangliao_moderation.syntax import recognize_command
from astrbot.core.platform.sources.wangshangliao import actions, blacklist
from astrbot.core.platform.sources.wangshangliao.event import managed_group_control
from astrbot.core.platform.sources.wangshangliao.group_scope import group_revision
from astrbot.core.platform.sources.wangshangliao.storage import Ledger
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from astrbot.dashboard.responses import ApiError
from tests.test_wangshangliao_command_menu import leaves, step
from tests.test_wangshangliao_command_parity import setup as setup

pytestmark = pytest.mark.usefixtures("wsl_active_policy", "wsl_command_classifier")


@pytest_asyncio.fixture
async def scope(setup, tmp_path, monkeypatch):
    """Isolate native membership and blacklist storage from real accounts.

    Args:
        setup: Existing two-group native command service.
        tmp_path: Private test persistence.
        monkeypatch: Scoped storage replacement.

    Yields:
        Service, event, mutable roster and blacklist ledger.
    """
    monkeypatch.setattr(blacklist, "instance_dir", lambda _: tmp_path / "blacklist")
    value, _, event = setup
    rows = copy.deepcopy(value.roster.return_value)

    async def roster(adapter, group):
        current = copy.deepcopy(rows)
        value.member_evidence[(str(adapter.account), group)] = current
        return current

    value.roster = AsyncMock(side_effect=roster)
    value.verify_actor = AsyncMock()
    value.context.get_provider = AsyncMock()
    value.member_evidence[("1", "5")] = copy.deepcopy(rows)
    adapter = event.platform
    adapter.ledger = Ledger(tmp_path / "inbox.sqlite3")
    await adapter.ledger.open()
    yield value, event, rows, blacklist.BlacklistStore(adapter.config["id"])
    await value.member_blacklist.close()
    await adapter.ledger.db.close()


@pytest_asyncio.fixture
async def release_scope(scope):
    """Provide independently queryable native mute state for blacklist release.

    Args:
        scope: Authenticated native service with temporary persistence.

    Returns:
        Service, event, roster, store and mutable native mute state.
    """
    value, event, rows, store = scope
    enroll(event, store)
    mention(event, text="解除黑名单 @Target")
    muted = {"903"}
    adapter = event.platform
    adapter.nim = SimpleNamespace(
        get_team_muted_members=AsyncMock(side_effect=lambda _: set(muted))
    )
    adapter.business = SimpleNamespace(
        request=AsyncMock(
            return_value={"complete": True, "groupMemberInfo": list(rows.values())}
        )
    )

    async def execute(request, *, authorize):
        await authorize()
        muted.discard(request.expected_nim)
        return {
            "operation": request.operation,
            "action": request.action,
            "group": request.group,
            "member": request.member,
            "expected_nim": request.expected_nim,
            "status": "accepted",
            "submitted": True,
        }

    adapter.execute_action.side_effect = execute
    return value, event, rows, store, muted


@pytest_asyncio.fixture
async def native_join(scope):
    """Queue one real join with an isolated, initialized native action ledger.

    Args:
        scope: Authenticated service with temporary blacklist persistence.

    Returns:
        Service, event, mutable roster, store and current membership evidence.
    """
    value, event, rows, store = scope
    enroll(event, store)
    adapter, stamp = event.platform, time.time()
    current = {
        "groupId": "5",
        "applyId": "101",
        "applyTime": datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
        "applicant": {"userId": "3", "nimId": "903"},
        "groupApplyType": "INVITATION",
        "state": "MEMBER_STATE_GOOD",
    }
    adapter.members["5"]["3"] = "903"
    adapter.business = SimpleNamespace(
        request=AsyncMock(return_value={"list": [current]})
    )
    adapter.diagnostics = SimpleNamespace(emit=lambda *a, **kw: None)
    with closing(sqlite3.connect(store.path.with_name("moderation.sqlite3"))) as db:
        db.execute(
            "CREATE TABLE operations "
            "(id TEXT PRIMARY KEY,digest TEXT NOT NULL,result TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO operations VALUES(?,?,?)",
            ("unrelated-operation", "digest", '{"status":"unknown"}'),
        )
        db.commit()
    store.observe(
        "1",
        "5",
        "notice-101",
        ["903"],
        stamp,
        guard=lambda: None,
        scope=value.member_blacklist.notice_scope(adapter, value.tenants[("1", "5")]),
    )
    return value, event, rows, store, current


def mention(event, *, peer="903", text="黑名单 @Target", nick="Target"):
    """Install authenticated structured mention evidence.

    Args:
        event: Original isolated native event.
        peer: Proven native peer.
        text: Original complete visible message.
        nick: Native display label.
    """
    start = len(text[: text.index("@")].encode("utf-16-le")) // 2
    end = start + len(("@" + nick).encode("utf-16-le")) // 2
    event.set_extra(
        "wangshangliao_payload",
        {
            "text": text,
            "mentions": [peer],
            "mention_spans": [{"uid": peer, "nick": nick, "start": start, "end": end}],
        },
    )


async def direct(value, event, mode="add"):
    """Use the same guarded internal direct operation as a real group command.

    Args:
        value: Isolated open service.
        event: Authenticated group command.
        mode: Add or remove.

    Returns:
        Persisted result.
    """
    return await value.member_blacklist.run(
        event.platform,
        "5",
        "direct",
        actor=event.actor_identity.principal,
        session=value.navigation.key(event),
        event=event,
        mode=mode,
    )


def enroll(event, store):
    """Seed an existing enrollment without issuing a new mute command."""
    store.set_member(
        "1", "5", "3", "903", "Target", True, event.actor_identity, lambda: None
    )


@pytest.mark.asyncio
async def test_real_mention_add_immediately_mutes_once_without_kicking(scope):
    value, event, _, store = scope
    mention(event)

    async def execute(request, *, authorize):
        await authorize()
        assert request.action == "mute"
        assert request.blacklist_intent.trigger == "command"
        return {
            "status": "accepted",
            "submitted": True,
            "operation": request.operation,
            "action": request.action,
            "group": request.group,
            "member": request.member,
            "minutes": request.minutes,
            "expected_nim": request.expected_nim,
        }

    event.platform.execute_action.side_effect = execute
    result = await direct(value, event)
    assert result["saved"] and result["member"] == "Target"
    assert result["enforcement"]["status"] == "accepted"
    version, entries = store.read("1", "5")
    assert version == 1 and entries[0]["kicks"] == 0
    assert entries[0]["actor"] == "2" and entries[0]["actor_peer"] == "902"
    await direct(value, event)
    assert store.read("1", "5")[0] == 1
    assert not store.read("1", "6")[1]
    event.platform.execute_action.assert_awaited_once()
    value.context.get_provider.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["plain", "many", "span", "admin", "own", "manager"])
async def test_unproven_or_protected_targets_are_rejected(scope, change):
    value, event, rows, store = scope
    mention(event)
    payload = event.get_extra("wangshangliao_payload")
    if change == "plain":
        payload["mentions"] = []
        payload["mention_spans"] = []
    elif change == "many":
        payload["mentions"].append("902")
    elif change == "span":
        payload["mention_spans"][0]["start"] += 1
    elif change == "admin":
        rows["3"]["groupRole"] = "GROUP_ROLE_ADMIN"
    elif change == "own":
        mention(event, peer="901")
    else:
        rows["2"]["groupRole"] = "GROUP_ROLE_MEMBER"
    with pytest.raises(OpenError):
        await direct(value, event)
    assert store.read("1", "5")[1] == []
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_reply_resolves_original_author_not_quoted_display_text(scope):
    value, event, rows, store = scope
    await event.platform.ledger.ingest(
        "1",
        "5",
        "original",
        {
            "sender": "3",
            "text": "ordinary message",
            "recall_route": {"peer": "903", "client": "client-original"},
        },
        state="done",
    )
    event.set_extra(
        "wangshangliao_payload",
        {
            "text": "黑名单",
            "reply": {"server": "original", "client": "client-original"},
        },
    )
    await direct(value, event)
    assert store.read("1", "5")[1][0]["member"] == "3"
    event.platform.execute_action.assert_awaited_once()
    assert event.platform.execute_action.call_args.args[0].action == "mute"
    rows.pop("3")
    result = await value.member_blacklist.run(
        event.platform,
        "5",
        "read",
        actor=event.actor_identity.principal,
        session=value.navigation.key(event),
        event=event,
    )
    assert result["members"][0]["name"] == "Target"


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", [1, 10080, 41760])
async def test_direct_mute_uses_current_duration_and_complete_receipt(scope, minutes):
    value, event, _, store = scope
    store.configure("1", "5", minutes, actor="dashboard/test", guard=lambda: None)
    mention(event)

    async def execute(request, *, authorize):
        await authorize()
        request.blacklist_guard()
        blacklist.authorize_intent(
            request.instance,
            request.account,
            request.operation,
            request.action,
            request.group,
            request.member,
            request.minutes,
            request.blacklist_intent,
        )
        assert request.action == "mute" and request.minutes == minutes
        return {
            "status": "accepted",
            "submitted": True,
            "operation": request.operation,
            "action": "mute",
            "group": request.group,
            "member": request.member,
            "expected_nim": "903",
            "minutes": minutes,
        }

    event.platform.execute_action.side_effect = execute
    reply = await value.member_blacklist.command(event, "黑名单")
    assert reply.startswith("已将Target拉入黑名单。\n")
    assert f"平台已受理禁言{minutes}分钟" in reply
    assert "实际到期时间以平台为准" in reply
    with store._transaction() as db:
        row = db.execute(
            "SELECT trigger,join_id,action,minutes,status,finished FROM blacklist_intents"
        ).fetchone()
        assert tuple(row) == (
            "command",
            "command/message",
            "mute",
            minutes,
            "accepted",
            1,
        )
    event.platform.nim = SimpleNamespace(
        get_team_muted_members=AsyncMock(return_value=set())
    )
    event.platform.business = SimpleNamespace(
        request=AsyncMock(
            return_value=event.platform.get_moderation_members.return_value
        )
    )
    removed = await direct(value, event, mode="remove")
    assert removed["release"]["verified"]
    assert store.read("1", "5")[1] == []
    event.platform.execute_action.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["mention_before", "mention_after", "reply"])
@pytest.mark.parametrize("outcome", ["accepted", "unknown", "rejected"])
async def test_bound_bot_admin_blacklist_runs_through_public_event_handler(
    scope, monkeypatch, source, outcome
):
    from astrbot.core import astrbot_config

    value, event, rows, store = scope
    rows["3"]["userNick"] = "Target (current)"
    monkeypatch.setitem(
        astrbot_config,
        "platform",
        [{"type": "wangshangliao", "account_id": uid} for uid in ("1", "2")],
    )
    adapter = event.platform
    adapter.config.update(_business_active=True)
    if source == "reply":
        await adapter.ledger.ingest(
            "1",
            "5",
            "original",
            {
                "sender": "3",
                "text": "ordinary message",
                "recall_route": {"peer": "903", "client": "client-original"},
            },
            state="done",
        )
        event.set_extra(
            "wangshangliao_payload",
            {
                "text": "黑名单",
                "reply": {"server": "original", "client": "client-original"},
            },
        )
    else:
        mention(
            event,
            text="@Target 黑名单" if source == "mention_before" else "黑名单 @Target",
        )
    incoming = event.get_extra("wangshangliao_payload")
    incoming.update(
        sender="2",
        name="Manager",
        created_at=int(time.time() * 1000),
        recall_route={
            "peer": "902",
            "client": "test-command",
            "time": str(int(time.time() * 1000)),
        },
        _open_revision=group_revision(adapter.config, "5"),
    )
    event.message_obj.message_id = "123"
    adapter.enabled_groups = set(adapter.config["enabled_groups"])
    await adapter.ledger.ingest("1", "5", "123", incoming)
    event.message_str = incoming["text"]
    command = managed_group_control(adapter, "5", incoming)
    assert command
    event.set_extra("_wsl_managed_group_control", command)

    async def execute(request, *, authorize):
        await authorize()
        return {
            "status": outcome,
            "submitted": outcome != "rejected",
            "operation": request.operation,
            "action": request.action,
            "group": request.group,
            "member": request.member,
            "minutes": request.minutes,
            "expected_nim": request.expected_nim,
        }

    adapter.execute_action.side_effect = execute
    adapter.send_reply_text.return_value = "accepted"
    await value.on_event(event)
    assert store.read("1", "5")[1][0]["member"] == "3"
    adapter.execute_action.assert_awaited_once()
    assert adapter.execute_action.call_args.args[0].action == "mute"
    adapter.send_reply_text.assert_awaited_once()
    reply = adapter.send_reply_text.call_args.args[2]
    assert "已将Target (current)拉入黑名单。\n" in reply
    assert {
        "accepted": "平台已受理禁言",
        "unknown": "禁言结果待核对",
        "rejected": "本次禁言被拒绝",
    }[outcome] in reply
    if outcome != "accepted":
        assert "平台已受理禁言" not in reply
    assert adapter.send_reply_text.call_args.kwargs["auto_recall"] is True
    async with adapter.ledger.db.execute(
        "SELECT key FROM reply_recalls WHERE account='1' AND group_id='5' "
        "AND server_id='123' AND sender='2'"
    ) as cursor:
        assert await cursor.fetchone() is not None
    value.context.get_provider.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source", ["mention_before", "mention_after", "compact", "reply"]
)
async def test_remove_alias_uses_verified_target_and_cleans_admin_command(
    release_scope, monkeypatch, source
):
    from astrbot.core import astrbot_config

    value, event, _, store, muted = release_scope
    store.set_member(
        "1", "6", "3", "903", "Target", True, event.actor_identity, lambda: None
    )
    monkeypatch.setitem(
        astrbot_config,
        "platform",
        [{"type": "wangshangliao", "account_id": uid} for uid in ("1", "2")],
    )
    adapter = event.platform
    adapter.config.update(_business_active=True)
    adapter.enabled_groups = set(adapter.config["enabled_groups"])
    if source == "reply":
        await adapter.ledger.ingest(
            "1",
            "5",
            "original",
            {
                "sender": "3",
                "text": "ordinary message",
                "recall_route": {"peer": "903", "client": "client-original"},
            },
            state="done",
        )
        event.set_extra(
            "wangshangliao_payload",
            {
                "text": "解除黑名单",
                "reply": {"server": "original", "client": "client-original"},
            },
        )
    else:
        mention(
            event,
            text={
                "mention_before": "@Target 解除黑名单",
                "mention_after": "解除黑名单 @Target",
                "compact": "解除黑名单@Target",
            }[source],
        )
    incoming = event.get_extra("wangshangliao_payload")
    incoming.update(
        sender="2",
        name="Manager",
        created_at=int(time.time() * 1000),
        recall_route={
            "peer": "902",
            "client": "test-command",
            "time": str(int(time.time() * 1000)),
        },
        _open_revision=group_revision(adapter.config, "5"),
    )
    event.message_obj.message_id = "123"
    event.message_str = incoming["text"]
    await adapter.ledger.ingest("1", "5", "123", incoming)
    command = managed_group_control(adapter, "5", incoming)
    assert command.startswith("移除黑名单")
    event.set_extra("_wsl_managed_group_control", command)
    adapter.send_reply_text.return_value = "accepted"
    await value.on_event(event)
    assert not store.read("1", "5")[1]
    assert store.read("1", "6")[1][0]["member"] == "3"
    adapter.execute_action.assert_awaited_once()
    request = adapter.execute_action.call_args.args[0]
    assert (request.action, request.group, request.member, request.expected_nim) == (
        "unmute",
        5,
        3,
        "903",
    )
    assert not muted
    adapter.send_reply_text.assert_awaited_once()
    reply = adapter.send_reply_text.call_args.args[2]
    assert "已移出本群黑名单：Target。" in reply
    assert "已解除禁言，恢复发言。" in reply
    assert adapter.send_reply_text.call_args.kwargs["auto_recall"] is True
    async with adapter.ledger.db.execute(
        "SELECT key FROM reply_recalls WHERE account='1' AND group_id='5' "
        "AND server_id='123' AND sender='2'"
    ) as cursor:
        assert await cursor.fetchone() is not None
    value.context.get_provider.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["移除黑名单 @Target", "解除黑名单 @Target"])
async def test_release_already_unmuted_is_verified_without_submitting(
    release_scope, command
):
    value, event, _, store, muted = release_scope
    muted.clear()
    mention(event, text=command)
    reply = await value.member_blacklist.command(event, recognize_command(command))
    assert not store.read("1", "5")[1]
    assert "已核实当前未被禁言，无需重复解禁" in reply
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome,expected",
    [
        ("rejected", "本次解禁未成功"),
        ("unknown", "解禁结果待核对，未自动重试"),
        ("accepted", "平台已受理解禁，但尚未核实恢复发言"),
        ("verified", "平台返回解禁完成，但尚未核实恢复发言"),
        ("timeout", "解禁结果待核对，未自动重试"),
        ("verify_failed", "平台已受理解禁，但尚未核实恢复发言"),
    ],
)
async def test_release_reports_partial_failure_without_claiming_restored_speech(
    release_scope, outcome, expected
):
    value, event, _, store, _ = release_scope
    adapter = event.platform

    async def execute(request, *, authorize):
        await authorize()
        if outcome == "timeout":
            raise TimeoutError()
        if outcome == "verify_failed":
            adapter.nim.get_team_muted_members.side_effect = ProtocolError("offline")
        return {"status": "accepted" if outcome == "verify_failed" else outcome}

    adapter.execute_action.side_effect = execute
    reply = await value.member_blacklist.command(event, "移除黑名单 @Target")
    assert not store.read("1", "5")[1]
    assert "已移出本群黑名单：Target。" in reply
    assert expected in reply
    assert "已解除禁言，恢复发言" not in reply
    adapter.execute_action.assert_awaited_once()


@pytest.mark.asyncio
async def test_release_unavailable_directory_does_not_guess_or_submit(release_scope):
    value, event, _, store, _ = release_scope
    event.platform.nim.get_team_muted_members.side_effect = ProtocolError("offline")
    reply = await value.member_blacklist.command(event, "移除黑名单 @Target")
    assert not store.read("1", "5")[1]
    assert "已移出本群黑名单：Target。" in reply
    assert "本次解禁未成功" in reply
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_release_retains_native_idempotency_key(release_scope):
    value, event, _, store, _ = release_scope
    operations = []

    async def execute(request, *, authorize):
        await authorize()
        operations.append(request.operation)
        return {"status": "unknown"}

    event.platform.execute_action.side_effect = execute
    for _ in range(2):
        reply = await value.member_blacklist.command(event, "移除黑名单 @Target")
        assert "未自动重试" in reply
    assert not store.read("1", "5")[1]
    assert len(operations) == 2 and operations[0] == operations[1]
    assert operations[0].startswith("blacklist-release/")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["reenroll", "revoke", "promote", "peer", "pause", "target"]
)
async def test_pending_release_rechecks_blacklist_and_native_authority(
    release_scope, change
):
    value, event, rows, store, muted = release_scope
    submitted = False

    async def execute(request, *, authorize):
        nonlocal submitted
        if change == "reenroll":
            enroll(event, store)
        elif change == "revoke":
            rows["2"]["groupRole"] = "GROUP_ROLE_MEMBER"
        elif change == "promote":
            rows["3"]["groupRole"] = "GROUP_ROLE_ADMIN"
        elif change == "peer":
            rows["3"]["nimId"] = "904"
        elif change == "pause":
            value.settings["paused"] = True
        else:
            mention(event, peer="902", text="解除黑名单 @Manager", nick="Manager")
        await authorize()
        submitted = True
        muted.clear()
        return {"status": "accepted"}

    event.platform.execute_action.side_effect = execute
    reply = await value.member_blacklist.command(event, "移除黑名单 @Target")
    assert not submitted and muted == {"903"}
    assert "已解除禁言，恢复发言" not in reply
    assert bool(store.read("1", "5")[1]) == (change == "reenroll")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command,recall",
    [
        ("黑名单列表", True),
        ("移除黑名单 @Target", True),
        ("解除黑名单 @Target", True),
        ("黑名单禁言时长 7天", True),
        ("规则", True),
    ],
)
async def test_blacklist_commands_follow_current_command_cleanup(
    scope, command, recall
):
    value, event, _, _ = scope
    adapter = event.platform
    adapter.config.update(_business_active=True)
    adapter.enabled_groups = set(adapter.config["enabled_groups"])
    event.message_obj.message_id = "123"
    event.message_str = command
    incoming = event.get_extra("wangshangliao_payload")
    incoming.update(
        text=command,
        created_at=int(time.time() * 1000),
        recall_route={
            "peer": "902",
            "client": "test-command",
            "time": str(int(time.time() * 1000)),
        },
        _open_revision=group_revision(adapter.config, "5"),
    )
    await adapter.ledger.ingest("1", "5", "123", incoming)
    value.manager_command = AsyncMock(return_value="Command result")
    await value.on_event(event)
    value.manager_command.assert_awaited_once()
    async with adapter.ledger.db.execute(
        "SELECT key FROM reply_recalls WHERE account='1' AND group_id='5' "
        "AND server_id='123' AND sender='2'"
    ) as cursor:
        assert bool(await cursor.fetchone()) is recall
    adapter.send_reply_text.assert_awaited_once()
    assert adapter.send_reply_text.call_args.kwargs["auto_recall"] is True
    adapter.execute_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["unknown", "partial", "timeout", "rejected"])
async def test_direct_command_unknown_never_retries_or_claims_success(scope, outcome):
    value, event, _, store = scope
    mention(event)

    async def execute(request, *, authorize):
        await authorize()
        if outcome == "timeout":
            raise TimeoutError("test timeout")
        if outcome == "rejected":
            return {"status": "rejected", "submitted": False}
        return {"status": "accepted" if outcome == "partial" else "unknown"}

    event.platform.execute_action.side_effect = execute
    reply = await value.member_blacklist.command(event, "黑名单")
    assert reply.startswith("已将Target拉入黑名单。\n") and "平台已受理" not in reply
    assert ("被拒绝" if outcome == "rejected" else "待核对") in reply
    await direct(value, event)
    if outcome != "rejected":
        event.message_obj.message_id = "another-command"
        await direct(value, event)
    event.platform.execute_action.assert_awaited_once()
    assert store.read("1", "5")[1][0]["enforcement_status"] == (
        "rejected" if outcome == "rejected" else "unknown"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["removed", "duration", "manager", "target", "pause"])
async def test_direct_mute_rechecks_authority_after_reservation(scope, fault):
    value, event, rows, store = scope
    mention(event)
    submitted = False

    async def execute(request, *, authorize):
        nonlocal submitted
        if fault == "removed":
            store.set_member(
                "1", "5", "3", "903", "Target", False, "dashboard/test", lambda: None
            )
        elif fault == "duration":
            store.configure("1", "5", 60, actor="dashboard/test", guard=lambda: None)
        elif fault == "manager":
            rows["2"]["groupRole"] = "GROUP_ROLE_MEMBER"
        elif fault == "target":
            rows["3"]["groupRole"] = "GROUP_ROLE_ADMIN"
        else:
            value.settings["paused"] = True
        await authorize()
        blacklist.authorize_intent(
            request.instance,
            request.account,
            request.operation,
            request.action,
            request.group,
            request.member,
            request.minutes,
            request.blacklist_intent,
        )
        submitted = True
        raise AssertionError("Revoked action must never reach the network")

    event.platform.execute_action.side_effect = execute
    result = await direct(value, event)
    assert not submitted
    assert result["enforcement"]["status"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["wrong_group", "wrong_client", "conflict", "missing"]
)
async def test_reply_reference_must_be_same_group_and_agree_with_mentions(
    scope, change
):
    value, event, rows, store = scope
    await event.platform.ledger.ingest(
        "1",
        "6" if change == "wrong_group" else "5",
        "original",
        {"sender": "3", "recall_route": {"peer": "903", "client": "client"}},
        state="done",
    )
    payload = {
        "text": "黑名单",
        "reply": {
            "server": "missing" if change == "missing" else "original",
            "client": "wrong" if change == "wrong_client" else "client",
        },
    }
    if change == "conflict":
        rows["4"] = {**rows["3"], "userId": "4", "nimId": "904"}
        mention(event, peer="904")
        payload.update(event.get_extra("wangshangliao_payload"))
    event.set_extra("wangshangliao_payload", payload)
    with pytest.raises(OpenError):
        await direct(value, event)
    assert not store.read("1", "5")[1]


@pytest.mark.asyncio
async def test_private_search_preview_confirmation_and_absent_member_removal(scope):
    value, event, rows, store = scope
    event.is_private_chat = lambda: True
    event.get_group_id = lambda: ""
    event.eligible = True
    session = value.navigation.key(event)
    args = {
        "actor": event.actor_identity.principal,
        "session": session,
        "event": event,
    }
    searched = await value.member_blacklist.run(
        event.platform, "5", "search", mode="add", query="Target", **args
    )
    preview = await value.member_blacklist.run(
        event.platform, "5", "preview", selection=searched["selection"], index=1, **args
    )
    with pytest.raises(OpenError, match="blacklist_confirmation"):
        await value.member_blacklist.run(
            event.platform, "5", "commit", token=preview["token"], **args
        )
    event.message_obj.message_id = "fresh-confirmation"
    await value.member_blacklist.run(
        event.platform, "5", "commit", token=preview["token"], **args
    )
    rows.pop("3")
    searched = await value.member_blacklist.run(
        event.platform, "5", "search", mode="remove", query="Target", **args
    )
    preview = await value.member_blacklist.run(
        event.platform, "5", "preview", selection=searched["selection"], index=1, **args
    )
    event.message_obj.message_id = "fresh-removal"
    await value.member_blacklist.run(
        event.platform, "5", "commit", token=preview["token"], **args
    )
    assert store.read("1", "5") == (2, [])
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_every_real_rejoin_mutes_with_current_policy_and_no_kicks(scope):
    value, event, rows, store = scope
    enroll(event, store)
    adapter = event.platform
    current = {}

    async def request(route, params):
        assert route == "/v1/group/get-apply-logs" and params == {}
        return {"list": [current]}

    adapter.business = SimpleNamespace(request=AsyncMock(side_effect=request))
    adapter.diagnostics = type("Diagnostics", (), {"emit": lambda *a, **kw: None})()

    async def execute(request, *, authorize):
        await authorize()
        return {
            "operation": request.operation,
            "action": request.action,
            "group": request.group,
            "member": request.member,
            "status": "accepted",
            "submitted": True,
            "minutes": request.minutes,
            "expected_nim": "903",
        }

    adapter.execute_action = AsyncMock(side_effect=execute)
    for count in range(1, 6):
        if count == 3:
            store.configure("1", "5", 120, actor="dashboard/test", guard=lambda: None)
            with store._transaction() as db:
                assert [
                    tuple(row)
                    for row in db.execute(
                        "SELECT action,minutes,status FROM blacklist_intents"
                    ).fetchall()
                ] == [("mute", 41760, "accepted")] * 2
        stamp = time.time()
        current = {
            "groupId": "5",
            "applyId": str(100 + count),
            "applyTime": datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
            "applicant": {"userId": "3", "nimId": "903"},
            "groupApplyType": "INVITATION",
            "state": "MEMBER_STATE_GOOD",
        }
        store.observe(
            "1",
            "5",
            f"notice-{count}",
            ["903"],
            stamp,
            lambda: None,
            scope=value.member_blacklist.notice_scope(
                adapter, value.tenants[("1", "5")]
            ),
        )
        await value.member_blacklist.process(adapter)
        request = adapter.execute_action.call_args.args[0]
        assert request.action == "mute"
        assert request.minutes == (41760 if count < 3 else 120)
        assert store.read("1", "5")[1][0]["kicks"] == 0
        await value.member_blacklist.process(adapter)
        assert adapter.execute_action.await_count == count


@pytest.mark.asyncio
async def test_read_exposes_duration_and_hides_historical_kick_phase(scope):
    value, event, _, store = scope
    enroll(event, store)
    with store._transaction() as db:
        db.execute("UPDATE blacklist_entries SET kicks=2")
    result = await value.member_blacklist.run(
        event.platform,
        "5",
        "read",
        actor=event.actor_identity.principal,
        session=value.navigation.key(event),
        event=event,
    )
    assert result["mute_minutes"] == 41760
    assert result["members"][0]["phase"] == "mute"
    assert result["members"][0]["kicks"] == 2
    reply = await value.member_blacklist.command(event, "黑名单列表")
    assert "每次重进禁言41760分钟" in reply
    assert "成功踢出" not in reply and "前三次" not in reply
    assert store.read("1", "5")[1][0]["kicks"] == 2
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize(
    "duration,minutes", [("1分钟", 1), ("24小时", 1440), ("29天", 41760)]
)
async def test_native_duration_preview_requires_fresh_confirmation(
    scope, private, duration, minutes
):
    value, event, _, store = scope
    if private:
        event.is_private_chat = lambda: True
        event.get_group_id = lambda: ""
        event.eligible = True
        await step(value, event, "群列表", native=True)
        await step(value, event, "选择群 1", native=True)
    before = store.settings("1", "5")
    reply = await step(value, event, "黑名单禁言时长 " + duration, native=True)
    assert "预览" in reply and f"改为{minutes}分钟" in reply
    assert "不追罚旧名单或重设已有禁言" in reply
    token = re.search(r"确认黑名单 ([A-Za-z0-9_-]+)", reply)[1]
    assert store.settings("1", "5") == before
    with pytest.raises(OpenError, match="blacklist_confirmation"):
        await value.member_blacklist.command(event, "确认黑名单 " + token)
    reply = await step(value, event, "确认黑名单 " + token, native=True)
    assert "已保存" in reply and f"{minutes}分钟" in reply
    assert store.settings("1", "5")["mute_minutes"] == minutes
    assert not store.read("1", "5")[1]
    assert store.settings("1", "6")["mute_minutes"] == 41760
    assert "已保存" not in await step(value, event, "确认黑名单 " + token, native=True)
    event.platform.execute_action.assert_not_awaited()
    value.context.get_provider.assert_not_called()


@pytest.mark.asyncio
async def test_private_duration_requires_selected_group(scope):
    value, event, _, store = scope
    event.is_private_chat = lambda: True
    event.get_group_id = lambda: ""
    event.eligible = True
    reply = await value.member_blacklist.command(event, "黑名单禁言时长 1小时")
    assert "请先发送「群列表」「选择群 序号」" in reply
    assert not value.member_blacklist.pending
    assert store.settings("1", "5")["mute_minutes"] == 41760
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", [0, -1, 41761, 1.5, True, "60", None])
async def test_settings_preview_rejects_non_whole_or_out_of_range_minutes(
    scope, minutes
):
    value, event, _, store = scope
    with pytest.raises(OpenError, match="blacklist_minutes"):
        await value.member_blacklist.run(
            event.platform,
            "5",
            "settings_preview",
            minutes=minutes,
            actor=event.actor_identity.principal,
            session=value.navigation.key(event),
            event=event,
        )
    assert not value.member_blacklist.pending
    assert store.settings("1", "5")["mute_minutes"] == 41760
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "duration", ["0分钟", "41761分钟", "697小时", "30天", "1.5小时"]
)
async def test_native_duration_range_errors_never_create_drafts(scope, duration):
    value, event, _, store = scope
    reply = await value.member_blacklist.command(event, "黑名单禁言时长 " + duration)
    assert "1—41760整分钟" in reply
    assert not value.member_blacklist.pending
    assert store.settings("1", "5")["mute_minutes"] == 41760


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["list", "duration", "grant", "role", "cancel", "expired", "session", "group"],
)
async def test_duration_draft_is_bound_to_authority_scope_and_list_version(
    scope, change
):
    value, event, rows, store = scope
    args = {
        "actor": event.actor_identity.principal,
        "session": value.navigation.key(event),
        "event": event,
    }
    preview = await value.member_blacklist.run(
        event.platform, "5", "settings_preview", minutes=60, **args
    )
    group = "5"
    if change == "list":
        enroll(event, store)
    elif change == "duration":
        store.configure("1", "5", 120, actor="dashboard/test", guard=lambda: None)
    elif change == "grant":
        value.management.assignments[value.management.key(event.platform, "5")] = ()
    elif change == "role":
        rows["2"]["groupRole"] = "GROUP_ROLE_MEMBER"
    elif change == "cancel":
        value.member_blacklist.cancel(event)
    elif change == "expired":
        value.member_blacklist.pending[preview["token"]]["expires"] = 0
    elif change == "session":
        args["session"] = ("another", "conversation")
    else:
        group = "6"
    before = store.settings("1", "5")
    event.message_obj.message_id = "fresh-settings-confirmation"
    with pytest.raises((OpenError, ProtocolError)):
        await value.member_blacklist.run(
            event.platform, group, "commit", token=preview["token"], **args
        )
    assert store.settings("1", "5") == before
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_duration_change_invalidates_member_preview(scope):
    value, event, _, store = scope
    args = {
        "actor": event.actor_identity.principal,
        "session": value.navigation.key(event),
        "event": event,
    }
    searched = await value.member_blacklist.run(
        event.platform, "5", "search", mode="add", query="Target", **args
    )
    preview = await value.member_blacklist.run(
        event.platform, "5", "preview", selection=searched["selection"], index=1, **args
    )
    store.configure("1", "5", 60, actor="dashboard/test", guard=lambda: None)
    event.message_obj.message_id = "fresh-member-confirmation"
    with pytest.raises(OpenError, match="blacklist_changed"):
        await value.member_blacklist.run(
            event.platform, "5", "commit", token=preview["token"], **args
        )
    assert not store.read("1", "5")[1]
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["admission", "commit"])
async def test_duration_commit_guard_rolls_back_last_moment_revocation(
    scope, monkeypatch, phase
):
    value, event, _, store = scope
    args = {
        "actor": event.actor_identity.principal,
        "session": value.navigation.key(event),
        "event": event,
    }
    preview = await value.member_blacklist.run(
        event.platform, "5", "settings_preview", minutes=60, **args
    )
    configure = blacklist.BlacklistStore.configure

    def revoked(current, *values, **options):
        guard, checks = options["guard"], 0

        def check():
            nonlocal checks
            checks += 1
            if checks == (1 if phase == "admission" else 2):
                value.member_evidence[("1", "5")]["2"]["groupRole"] = (
                    "GROUP_ROLE_MEMBER"
                )
            guard()

        options["guard"] = check
        return configure(current, *values, **options)

    monkeypatch.setattr(blacklist.BlacklistStore, "configure", revoked)
    before = store.settings("1", "5")
    event.message_obj.message_id = "fresh-settings-confirmation"
    with pytest.raises((OpenError, ProtocolError)):
        await value.member_blacklist.run(
            event.platform, "5", "commit", token=preview["token"], **args
        )
    assert store.settings("1", "5") == before
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["read", "settings_preview"])
async def test_duration_snapshot_race_never_returns_an_incoherent_preview(
    scope, monkeypatch, operation
):
    value, event, _, store = scope
    read = blacklist.BlacklistStore.read

    def changed(current, account, group):
        current.configure(
            account, group, 120, actor="dashboard/test", guard=lambda: None
        )
        return read(current, account, group)

    monkeypatch.setattr(blacklist.BlacklistStore, "read", changed)
    with pytest.raises(OpenError, match="blacklist_changed"):
        await value.member_blacklist.run(
            event.platform,
            "5",
            operation,
            minutes=60,
            actor=event.actor_identity.principal,
            session=value.navigation.key(event),
            event=event,
        )
    assert not value.member_blacklist.pending
    assert store.settings("1", "5")["mute_minutes"] == 120
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_dashboard_rpc_duration_preview_and_commit_preserve_member_history(scope):
    value, event, _, store = scope
    enroll(event, store)
    entries = store.read("1", "5")[1]
    value.adapters = lambda: [event.platform]
    rpc = OpenRPC(value)
    payload = {
        "account": "1",
        "group_id": "5",
        "tenant_version": value.tenants[("1", "5")]["version"],
    }
    preview = await rpc.dispatch(
        OpenCall(
            action="member_blacklist",
            actor="dashboard/test",
            payload={**payload, "operation": "settings_preview", "minutes": 60},
        )
    )
    assert preview["mute_minutes"] == 60
    assert store.settings("1", "5")["mute_minutes"] == 41760
    result = await rpc.dispatch(
        OpenCall(
            action="member_blacklist",
            actor="dashboard/test",
            payload={**payload, "operation": "commit", "token": preview["token"]},
        )
    )
    assert result["saved"] and result["mute_minutes"] == 60
    assert result["version"] == 2 and result["policy_version"] > 0
    assert store.read("1", "5")[1] == entries
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
async def test_menu_duration_setting_uses_group_selection_and_token_confirmation(
    scope, private
):
    value, event, _, store = scope
    if private:
        event.is_private_chat = lambda: True
        event.get_group_id = lambda: ""
        event.eligible = True
    path = next(path for item, path in leaves() if item.command == "黑名单禁言时长")
    await step(value, event, "菜单", native=True)
    for number in path:
        reply = await step(value, event, str(number), native=True)
    if private:
        assert "授权群" in reply and "Group 5" in reply
        reply = await step(value, event, "1", native=True)
    assert "时长" in reply and "41760" in reply
    reply = await step(value, event, "24小时", native=True)
    assert "预览" in reply and "1440分钟" in reply and "确认执行" in reply
    assert store.settings("1", "5")["mute_minutes"] == 41760
    reply = await step(value, event, "1", native=True)
    assert "已保存" in reply and "1440分钟" in reply
    assert store.settings("1", "5")["mute_minutes"] == 1440
    event.platform.execute_action.assert_not_awaited()
    value.context.get_provider.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["accepted", "unknown", "partial"])
async def test_incomplete_receipt_deduplicates_join_but_allows_a_new_admission(
    scope, status
):
    value, event, _, store = scope
    enroll(event, store)
    adapter, stamp = event.platform, time.time()
    current = {
        "groupId": "5",
        "applyId": "101",
        "applyTime": datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
        "applicant": {"userId": "3", "nimId": "903"},
        "groupApplyType": "INVITATION",
        "state": "MEMBER_STATE_GOOD",
    }
    adapter.business = SimpleNamespace(
        request=AsyncMock(return_value={"list": [current]})
    )
    adapter.diagnostics = SimpleNamespace(emit=lambda *a, **kw: None)

    async def execute(request, *, authorize):
        await authorize()
        assert request.action == "mute" and request.minutes == 41760
        return {"status": status}

    adapter.execute_action = AsyncMock(side_effect=execute)
    for message in ("notice", "duplicate-notice", "new-join", "duplicate-new-join"):
        if message == "new-join":
            current["applyId"] = "102"
            stamp = time.time()
            current["applyTime"] = datetime.fromtimestamp(
                stamp, timezone.utc
            ).isoformat()
        store.observe(
            "1",
            "5",
            message,
            ["903"],
            stamp,
            guard=lambda: None,
            scope=value.member_blacklist.notice_scope(
                adapter, value.tenants[("1", "5")]
            ),
        )
        await value.member_blacklist.process(adapter)
    assert adapter.execute_action.await_count == 2
    requests = [call.args[0] for call in adapter.execute_action.await_args_list]
    assert requests[0].operation != requests[1].operation
    with store._transaction() as db:
        assert [
            tuple(row)
            for row in db.execute(
                "SELECT join_id,status,finished FROM blacklist_intents ORDER BY rowid"
            )
        ] == [("101", "unknown", 1), ("102", "unknown", 1)]
    assert store.read("1", "5")[1][0]["kicks"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["epoch", "paused", "demoted", "rejected", "ordinary"]
)
async def test_rejoin_revocation_and_explicit_rejection_are_not_ambiguous(scope, fault):
    value, event, rows, store = scope
    enroll(event, store)
    adapter, stamp = event.platform, time.time()
    current = {
        "groupId": "5",
        "applyId": "101",
        "applyTime": datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
        "applicant": {"userId": "3", "nimId": "903"},
        "groupApplyType": "INVITATION",
        "state": "MEMBER_STATE_GOOD",
    }

    async def request(route, params):
        if fault == "demoted":
            value.member_evidence[("1", "5")]["2"]["groupRole"] = "GROUP_ROLE_MEMBER"
        return {"list": [current]}

    adapter.business = SimpleNamespace(request=AsyncMock(side_effect=request))
    adapter.diagnostics = type("Diagnostics", (), {"emit": lambda *a, **kw: None})()
    store.observe(
        "1",
        "5",
        "notice",
        ["903"],
        stamp,
        lambda: None,
        scope=value.member_blacklist.notice_scope(adapter, value.tenants[("1", "5")]),
    )
    if fault == "epoch":
        adapter.config["_business_epoch"] = "new"
    if fault == "paused":
        value.tenants[("1", "5")]["paused"] = True
    value._blacklist_fixture = object()

    async def execute(request, *, authorize):
        await authorize()
        assert request.blacklist_fixture is None
        request.blacklist_guard()
        if fault == "rejected":
            error = OpenError("before_submission")
            error.submitted = False
            raise error
        return {"status": "rejected", "submitted": False}

    adapter.execute_action = AsyncMock(side_effect=execute)
    await value.member_blacklist.process(adapter)
    if fault in {"epoch", "paused", "demoted"}:
        adapter.execute_action.assert_not_awaited()
    else:
        adapter.execute_action.assert_awaited_once()
        with store._transaction() as db:
            assert db.execute(
                "SELECT status,finished FROM blacklist_intents"
            ).fetchall()[0][:] == ("rejected", 1)
    assert store.read("1", "5")[1][0]["kicks"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revision", "duration", "creator_role"])
async def test_reserved_rejoin_revocation_without_native_record_does_not_block(
    native_join, change
):
    value, event, rows, store, current = native_join
    adapter = event.platform

    async def native(operation, action, group, member, text, **options):
        return {
            "operation": operation,
            "action": action,
            "group": group,
            "member": member,
            "status": "accepted",
            "submitted": True,
            "minutes": options["minutes"],
            "expected_nim": "903",
        }

    adapter.execute_moderation = AsyncMock(side_effect=native)

    async def execute(request, *, authorize):
        if adapter.execute_action.await_count == 1:
            if change == "revision":
                adapter.config["_business_revision"] = "changed-after-reservation"
            elif change == "duration":
                await asyncio.to_thread(
                    store.configure,
                    "1",
                    "5",
                    60,
                    actor="dashboard/test",
                    guard=lambda: None,
                )
            else:
                rows["2"]["groupRole"] = "GROUP_ROLE_MEMBER"
        return await actions.dispatch(adapter, request, authorize=authorize)

    adapter.execute_action = AsyncMock(side_effect=execute)
    await value.member_blacklist.process(adapter)
    adapter.execute_moderation.assert_not_awaited()
    with store._transaction() as db:
        assert [
            tuple(row)
            for row in db.execute(
                "SELECT status,finished FROM blacklist_intents"
            ).fetchall()
        ] == [("rejected", 1)]
    await value.member_blacklist.process(adapter)
    adapter.execute_action.assert_awaited_once()
    rows["2"]["groupRole"] = "GROUP_ROLE_OWNER"
    stamp = time.time()
    current.update(
        applyId="102",
        applyTime=datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
    )
    store.observe(
        "1",
        "5",
        "notice-102",
        ["903"],
        stamp,
        guard=lambda: None,
        scope=value.member_blacklist.notice_scope(adapter, value.tenants[("1", "5")]),
    )
    await value.member_blacklist.process(adapter)
    adapter.execute_moderation.assert_awaited_once()
    assert adapter.execute_moderation.call_args.kwargs["minutes"] == (
        60 if change == "duration" else 41760
    )
    with store._transaction() as db:
        assert [
            tuple(row)
            for row in db.execute(
                "SELECT join_id,status FROM blacklist_intents ORDER BY rowid"
            ).fetchall()
        ] == [("101", "rejected"), ("102", "accepted")]


@pytest.mark.asyncio
@pytest.mark.parametrize("native_status", ["unknown", "accepted"])
async def test_native_exception_stays_unknown_without_blocking_a_later_admission(
    native_join, native_status
):
    value, event, _, store, current = native_join
    adapter = event.platform

    async def execute(request, *, authorize):
        await authorize()
        with closing(sqlite3.connect(store.path.with_name("moderation.sqlite3"))) as db:
            db.execute(
                "INSERT INTO operations VALUES(?,?,?)",
                (request.operation, "digest", json.dumps({"status": native_status})),
            )
            db.commit()
        raise ProtocolError(
            "transport"
            if native_status == "unknown"
            else "moderation_operation_conflict"
        )

    adapter.execute_action = AsyncMock(side_effect=execute)
    await value.member_blacklist.process(adapter)
    with store._transaction() as db:
        assert tuple(
            db.execute("SELECT status,finished FROM blacklist_intents").fetchone()
        ) == ("unknown", 1)
    stamp = time.time()
    current.update(
        applyId="102",
        applyTime=datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
    )
    store.observe(
        "1",
        "5",
        "notice-102",
        ["903"],
        stamp,
        guard=lambda: None,
        scope=value.member_blacklist.notice_scope(adapter, value.tenants[("1", "5")]),
    )
    await value.member_blacklist.process(adapter)
    assert adapter.execute_action.await_count == 2
    with store._transaction() as db:
        assert [
            tuple(row)
            for row in db.execute(
                "SELECT join_id,status,finished FROM blacklist_intents ORDER BY rowid"
            )
        ] == [("101", "unknown", 1), ("102", "unknown", 1)]


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", ["missing", "empty", "missing_table", "corrupt"])
async def test_missing_native_ledger_or_schema_is_distinct_from_corruption(
    native_join, ledger
):
    value, event, _, store, _ = native_join
    path = store.path.with_name("moderation.sqlite3")
    if ledger == "missing":
        path.unlink()
    elif ledger == "empty":
        path.write_bytes(b"")
    elif ledger == "missing_table":
        with closing(sqlite3.connect(path)) as db:
            db.execute("DROP TABLE operations")
            db.commit()
    else:
        path.write_bytes(b"not a sqlite database")
    before = path.read_bytes() if path.exists() else None
    event.platform.execute_action = AsyncMock(side_effect=ProtocolError("transport"))
    await value.member_blacklist.process(event.platform)
    with store._transaction() as db:
        assert tuple(
            db.execute("SELECT status,finished FROM blacklist_intents").fetchone()
        ) == ("unknown" if ledger == "corrupt" else "rejected", 1)
    assert (path.read_bytes() if path.exists() else None) == before


@pytest.mark.asyncio
async def test_native_ledger_stat_permission_error_keeps_unknown(
    native_join, monkeypatch
):
    value, event, _, store, _ = native_join
    path = store.path.with_name("moderation.sqlite3")
    stat = type(path).stat

    def denied(current, **options):
        if current == path:
            raise PermissionError(errno.EACCES, "Permission denied")
        return stat(current, **options)

    monkeypatch.setattr(type(path), "stat", denied)
    event.platform.execute_action = AsyncMock(side_effect=ProtocolError("transport"))
    await value.member_blacklist.process(event.platform)
    with store._transaction() as db:
        assert tuple(
            db.execute("SELECT status,finished FROM blacklist_intents").fetchone()
        ) == ("unknown", 1)


@pytest.mark.asyncio
async def test_locked_native_ledger_keeps_unknown(native_join, monkeypatch):
    value, event, _, store, _ = native_join
    path = store.path.with_name("moderation.sqlite3")

    def connect(database, **options):
        return sqlite3.connect(database, timeout=0, **options)

    monkeypatch.setattr(
        member_blacklist,
        "sqlite3",
        SimpleNamespace(connect=connect, Error=sqlite3.Error),
    )
    event.platform.execute_action = AsyncMock(side_effect=ProtocolError("transport"))
    with closing(sqlite3.connect(path)) as lock:
        lock.execute("BEGIN EXCLUSIVE")
        try:
            await value.member_blacklist.process(event.platform)
        finally:
            lock.rollback()
    with store._transaction() as db:
        assert tuple(
            db.execute("SELECT status,finished FROM blacklist_intents").fetchone()
        ) == ("unknown", 1)


@pytest.mark.asyncio
async def test_submission_evidence_uses_original_instance_after_config_switch(
    native_join, monkeypatch
):
    value, event, _, store, _ = native_join
    queried = []

    def connect(database, **options):
        assert database == (
            store.path.with_name("moderation.sqlite3").resolve().as_uri() + "?mode=ro"
        )
        assert options == {"uri": True}
        queried.append(database)
        return sqlite3.connect(database, **options)

    monkeypatch.setattr(
        member_blacklist,
        "sqlite3",
        SimpleNamespace(connect=connect, Error=sqlite3.Error),
    )

    async def execute(request, *, authorize):
        assert request.instance == store.instance
        event.platform.config["id"] = "replacement-instance"
        raise ProtocolError("action_scope_changed")

    event.platform.execute_action = AsyncMock(side_effect=execute)
    await value.member_blacklist.process(event.platform)
    assert len(queried) == 1
    with store._transaction() as db:
        assert tuple(
            db.execute("SELECT status,finished FROM blacklist_intents").fetchone()
        ) == ("rejected", 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("native_record", [False, True])
async def test_cancelled_native_check_is_off_loop_and_shielded_through_cleanup(
    native_join, monkeypatch, native_record
):
    value, event, _, store, _ = native_join
    started = asyncio.Event()
    queried, release, finished = threading.Event(), threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    done = blacklist.BlacklistStore.done

    def connect(database, **options):
        assert threading.get_ident() != loop_thread
        queried.set()
        assert release.wait(5)
        return sqlite3.connect(database, **options)

    def finish(current, intent, receipt):
        done(current, intent, receipt)
        finished.set()

    monkeypatch.setattr(
        member_blacklist,
        "sqlite3",
        SimpleNamespace(connect=connect, Error=sqlite3.Error),
    )
    monkeypatch.setattr(blacklist.BlacklistStore, "done", finish)

    async def execute(request, *, authorize):
        if native_record:
            with closing(
                sqlite3.connect(store.path.with_name("moderation.sqlite3"))
            ) as db:
                db.execute(
                    "INSERT INTO operations VALUES(?,?,?)",
                    (request.operation, "digest", '{"status":"unknown"}'),
                )
                db.commit()
        started.set()
        await asyncio.Event().wait()

    event.platform.execute_action = AsyncMock(side_effect=execute)
    worker = asyncio.create_task(value.member_blacklist.process(event.platform))
    try:
        await asyncio.wait_for(started.wait(), 3)
        worker.cancel()
        assert await asyncio.wait_for(asyncio.to_thread(queried.wait, 3), 4)
        assert not worker.done()
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
    finally:
        release.set()
        await asyncio.gather(worker, return_exceptions=True)
    assert await asyncio.wait_for(asyncio.to_thread(finished.wait, 3), 4)
    with store._transaction() as db:
        assert tuple(
            db.execute("SELECT status,finished FROM blacklist_intents").fetchone()
        ) == ("unknown" if native_record else "rejected", 1)


@pytest.mark.asyncio
async def test_accepted_receipt_is_not_overwritten_when_cleanup_is_cancelled(
    native_join, monkeypatch
):
    value, event, _, store, _ = native_join
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    done = blacklist.BlacklistStore.done

    def finish(current, intent, receipt):
        entered.set()
        assert release.wait(5)
        done(current, intent, receipt)
        finished.set()

    monkeypatch.setattr(blacklist.BlacklistStore, "done", finish)

    async def execute(request, *, authorize):
        await authorize()
        event.platform.config["_business_revision"] = "revoked-after-acceptance"
        return {
            "operation": request.operation,
            "action": request.action,
            "group": request.group,
            "member": request.member,
            "status": "accepted",
            "submitted": True,
            "minutes": request.minutes,
            "expected_nim": "903",
        }

    event.platform.execute_action = AsyncMock(side_effect=execute)
    worker = asyncio.create_task(value.member_blacklist.process(event.platform))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 3), 4)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
    finally:
        release.set()
        await asyncio.gather(worker, return_exceptions=True)
    assert await asyncio.wait_for(asyncio.to_thread(finished.wait, 3), 4)
    with store._transaction() as db:
        assert tuple(
            db.execute("SELECT status,finished FROM blacklist_intents").fetchone()
        ) == ("accepted", 1)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("黑名单", "黑名单"),
        ("黑名单 @Target", "黑名单 @Target"),
        ("@Target 黑名单", "黑名单 @Target"),
        ("移除黑名单 @Target", "移除黑名单 @Target"),
        ("解除黑名单 @Target", "移除黑名单 @Target"),
        ("@Target 解除黑名单", "移除黑名单 @Target"),
        ("解除黑名单@Target", "移除黑名单 @Target"),
        ("黑名单列表", "黑名单列表"),
        ("添加黑名单 Target", "添加黑名单 Target"),
        ("选择黑名单 1", "选择黑名单 1"),
        ("黑名单禁言时长 60分钟", "黑名单禁言时长 60分钟"),
        ("黑名单禁言时长 24小时", "黑名单禁言时长 24小时"),
        ("黑名单禁言时长 29天", "黑名单禁言时长 29天"),
    ],
)
def test_fixed_syntax_retains_original_target_for_verification(text, expected):
    assert recognize_command(text) == expected


def test_rpc_payload_is_strict_and_never_accepts_raw_member_identity():
    with pytest.raises(ValidationError):
        REQUEST.validate_python(
            {
                "operation": "commit",
                "account": "1",
                "group_id": "5",
                "tenant_version": 1,
                "token": "A" * 24,
                "member": "3",
            }
        )


@pytest.mark.parametrize("minutes", [0, -1, 41761, 1.5, True, "60"])
def test_rpc_duration_payload_rejects_invalid_whole_minutes(minutes):
    with pytest.raises(ValidationError):
        REQUEST.validate_python(
            {
                "operation": "settings_preview",
                "account": "1",
                "group_id": "5",
                "tenant_version": 1,
                "minutes": minutes,
            }
        )


@pytest.mark.parametrize("minutes", [1, 41760])
def test_rpc_duration_payload_accepts_safe_boundaries(minutes):
    result = REQUEST.validate_python(
        {
            "operation": "settings_preview",
            "account": "1",
            "group_id": "5",
            "tenant_version": 1,
            "minutes": minutes,
        }
    )
    assert result.minutes == minutes


def test_blacklist_guidance_catalog_and_manual_describe_mute_only_policy():
    capability = CAPABILITIES["黑名单禁言时长"]
    assert capability["role"] == "group_manager" and capability["preview"]
    assert set(capability["locations"]) == {"private", "group"}
    texts = [
        command_help("group", {"group_manager"}),
        admin_manual_documents()[MANUAL_PREFIX + "成员黑名单"],
        READ_ONLY_GUIDANCE,
        GUIDANCE_REPLY_CONTRACT,
        consultation_prompt(""),
        routed_consultation_prompt({"kind": "admin", "management": [], "finance": []}),
    ]
    for text in texts:
        assert "41760" in text and "不踢人" in text
        assert "不追罚" in text and "已有禁言" in text
        assert "前三次" not in text and "第四次" not in text
        assert "解除黑名单" in text and "分别反馈结果" in text
        assert "私信或后台移除只移出名单" in text
    assert "黑名单禁言时长 N分钟/小时/天" in texts[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_type,reason,expected",
    [
        (OpenError, "blacklist_minutes", "1—41760整分钟"),
        (ProtocolError, "blacklist_minutes", "1—41760整分钟"),
        (OpenError, "blacklist_version_changed", "重新预览"),
        (ProtocolError, "blacklist_version_changed", "重新预览"),
    ],
)
async def test_dashboard_duration_errors_give_specific_correction(
    monkeypatch, error_type, reason, expected
):
    request = SimpleNamespace(_request=SimpleNamespace(method="GET"), username="test")
    monkeypatch.setattr(dashboard, "request", request)
    integrated = SimpleNamespace(
        active=True,
        integrated=True,
        rpc=SimpleNamespace(
            lock=asyncio.Lock(),
            dispatch=AsyncMock(side_effect=error_type(reason)),
        ),
    )
    page = dashboard.DashboardPage(SimpleNamespace())
    page.authorize = AsyncMock()
    page.plugin = SimpleNamespace(open_service=integrated)
    with pytest.raises(ApiError, match=expected):
        await page.open_service()


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", [0, 41761, True, 1.5, "60"])
async def test_dashboard_rpc_schema_failure_explains_duration_range(
    monkeypatch, minutes
):
    payload = {
        "account": "1",
        "group_id": "5",
        "tenant_version": 1,
        "operation": "settings_preview",
        "minutes": minutes,
    }
    with pytest.raises(ValidationError) as failure:
        REQUEST.validate_python(payload)

    async def stream():
        yield json.dumps({"action": "member_blacklist", "payload": payload}).encode()

    monkeypatch.setattr(
        dashboard,
        "request",
        SimpleNamespace(
            _request=SimpleNamespace(method="POST", stream=stream),
            username="test",
        ),
    )
    page = dashboard.DashboardPage(SimpleNamespace())
    page.authorize = AsyncMock()
    page.plugin = SimpleNamespace(
        open_service=SimpleNamespace(
            active=True,
            integrated=True,
            rpc=SimpleNamespace(
                lock=asyncio.Lock(),
                dispatch=AsyncMock(side_effect=failure.value),
            ),
        )
    )
    with pytest.raises(ApiError, match="1—41760整分钟"):
        await page.open_service()
