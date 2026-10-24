"""Offline total-control authorization and isolated finance rehearsal regressions."""

import asyncio
import json
import socket
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.cashback import CashbackService
from astrbot.builtin_stars.wangshangliao_moderation.cashback_finance import FinanceActor
from astrbot.builtin_stars.wangshangliao_moderation.cashback_store import CashbackError
from astrbot.builtin_stars.wangshangliao_moderation.finance_chat import FinanceChat
from astrbot.builtin_stars.wangshangliao_moderation.finance_sandbox import (
    TEST_ACCOUNT,
    TEST_GROUP,
    TEST_MANAGER,
    FinanceSandboxService,
)
from astrbot.builtin_stars.wangshangliao_moderation.open_rpc import OpenCall, OpenRPC
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.platform.sources.wangshangliao.event import WangshangliaoEvent
from astrbot.core.platform.sources.wangshangliao.identity import ActorIdentity
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from tests.test_wangshangliao_open_rpc_guards import rpc_scope as rpc_scope


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    """Fail any accidental network connection in these offline regressions.

    Args:
        monkeypatch: Test-local restoration of socket methods.
    """
    monkeypatch.setattr(
        socket.socket, "connect", Mock(side_effect=AssertionError("offline test"))
    )
    monkeypatch.setattr(
        socket.socket, "connect_ex", Mock(side_effect=AssertionError("offline test"))
    )


@pytest.fixture
def control(rpc_scope, tmp_path):
    """Attach a real temporary financial ledger to the offline control service.

    Args:
        rpc_scope: Existing isolated tenant, adapter and member-directory fixture.
        tmp_path: Test-owned filesystem root.

    Returns:
        Control dependencies with no production configuration or service access.
    """
    owner, adapter, rows = rpc_scope
    domain = CashbackService(owner.context, tmp_path / "finance", "rpc-offline")
    domain.store.bootstrap([{"email": "owner@example.invalid", "name": "Offline"}])
    owner.plugin.cashback.service = domain
    return SimpleNamespace(
        owner=owner,
        adapter=adapter,
        rows=rows,
        domain=domain,
        rpc=OpenRPC(owner),
    )


@pytest.fixture
def sandbox_control(control, monkeypatch):
    """Supply the exact rehearsal identities without contacting their platform.

    Args:
        control: Temporary control-plane dependencies.
        monkeypatch: Test-local method and account replacement.

    Returns:
        Control dependencies bound to a synthetic fixed-account test window.
    """
    window = SimpleNamespace(
        active=Mock(return_value=True),
        sender=SimpleNamespace(account=TEST_MANAGER),
        groups={TEST_GROUP},
        scopes={"private_commands"},
        status=Mock(return_value={"seconds": 240}),
    )
    adapter = SimpleNamespace(
        account=TEST_ACCOUNT,
        config={
            "id": "rpc-offline",
            "type": "wangshangliao",
            "enable": True,
            "enabled_groups": [TEST_GROUP],
            "reply_private": True,
            "_business_active": True,
            "_business_epoch": 1,
            "_business_revision": "offline-finance",
        },
        test_window=window,
        meta=lambda: PlatformMetadata("wangshangliao", "offline", id="rpc-offline"),
    )
    monkeypatch.setattr(control.owner, "adapters", lambda: [adapter])
    monkeypatch.setattr(
        control.owner,
        "roster",
        AsyncMock(
            return_value={
                TEST_MANAGER: {"nimId": "offline-manager-peer", "name": "Test manager"}
            }
        ),
    )
    control.owner.context.platform_manager.platform_insts = [adapter]
    control.owner.store.discover(
        TEST_ACCOUNT,
        {TEST_GROUP: {"name": "Offline sandbox", "team": "100" + TEST_GROUP}},
    )
    control.owner.store.import_group(
        TEST_ACCOUNT,
        TEST_GROUP,
        {"advertising": True, "flood": True, "keywords": []},
        [],
        "system/offline",
    )
    control.owner.tenants[(TEST_ACCOUNT, TEST_GROUP)] = control.owner.store.tenant(
        TEST_ACCOUNT, TEST_GROUP
    )
    control.adapter = adapter
    return control


def grant_payload(**updates):
    """Build a client request that cannot choose its transport identity.

    Args:
        **updates: Explicit field overrides.

    Returns:
        Valid total-control grant payload.
    """
    return {
        "account": "99",
        "group_id": "5",
        "uid": "2",
        "role": "cashier",
        "active": True,
        "version": 0,
        "request_id": "offline-grant-0001",
        **updates,
    }


def test_call(*, active=True, version=0, request_id="offline-window-0001", actor=None):
    """Build a strict rehearsal-control request.

    Args:
        active: Requested window state.
        version: Expected current control generation.
        request_id: Actor-scoped retry identifier.
        actor: Optional dashboard principal override.

    Returns:
        Versioned RPC request.
    """
    return OpenCall(
        action="finance_test",
        actor=actor or "dashboard/offline",
        payload={"active": active, "version": version, "request_id": request_id},
    )


test_call.__test__ = False


def ledger_dump(store):
    """Capture all ledger rows to detect unintended writes.

    Args:
        store: Test-owned SQLite store.

    Returns:
        Complete logical database contents.
    """
    with store.connect() as db:
        return tuple(db.iterdump())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "actor",
    ["manager/1", "chat/1", "owner@example.invalid", "existing-framework-admin"],
)
@pytest.mark.parametrize(
    "action", ["finance_status", "finance_members", "finance_grant", "finance_test"]
)
async def test_finance_control_requires_dashboard_for_every_action(
    control, actor, action
):
    before = ledger_dump(control.domain.store)
    with pytest.raises(OpenError, match="^finance_unavailable$"):
        await control.rpc.dispatch(OpenCall(action=action, actor=actor, payload={}))
    assert ledger_dump(control.domain.store) == before
    control.adapter.business.request.assert_not_awaited()
    assert not getattr(control.owner, "finance_chat", None)


@pytest.mark.asyncio
async def test_ordinary_tenant_managers_do_not_inherit_financial_grants(control):
    assert control.owner.tenants[("99", "5")]["managers"] == ["1"]
    result = await control.rpc.dispatch(
        OpenCall(action="finance_status", actor="dashboard/offline", payload={})
    )
    assert result["grants"] == []
    assert result["test_active"] is False
    assert control.domain.store.finance_grants("99", "1", "1001") == []
    control.adapter.business.request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["finance_members", "finance_grant"])
@pytest.mark.parametrize(
    "scope", [{"account": "199"}, {"group_id": "7"}, {"account": "777"}]
)
async def test_finance_control_rejects_unknown_or_other_account_scope(
    control, action, scope
):
    payload = grant_payload(**scope)
    if action == "finance_members":
        payload = {key: payload[key] for key in ("account", "group_id")}
    before = ledger_dump(control.domain.store)
    with pytest.raises(OpenError, match="^finance_scope$"):
        await control.rpc.dispatch(
            OpenCall(action=action, actor="dashboard/offline", payload=payload)
        )
    assert ledger_dump(control.domain.store) == before
    control.adapter.business.request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["peer", "name", "principal"])
async def test_grant_rejects_client_forged_identity_fields(control, field):
    before = ledger_dump(control.domain.store)
    with pytest.raises(OpenError, match="^finance_arguments$"):
        await control.rpc.dispatch(
            OpenCall(
                action="finance_grant",
                actor="dashboard/offline",
                payload=grant_payload(**{field: "forged-identity"}),
            )
        )
    assert ledger_dump(control.domain.store) == before
    control.adapter.business.request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        (field, value)
        for field in ("account", "group_id", "uid")
        for value in (None, 99, True, [], {}, "", "0", "01", "-1", "9" * 21, "99 ")
    ]
    + [
        ("role", None),
        ("role", []),
        ("role", "admin"),
        ("role", "ordinary_manager"),
        ("active", 0),
        ("active", 1),
        ("active", "false"),
        ("active", None),
        ("version", True),
        ("version", -1),
        ("version", "0"),
        ("version", 0.0),
        ("version", None),
        ("request_id", None),
        ("request_id", 123),
        ("request_id", []),
        ("request_id", "short"),
        ("request_id", "x" * 81),
        ("request_id", "invalid/request-id"),
    ],
)
async def test_malformed_grant_payload_is_rejected_before_platform_io(
    control, monkeypatch, field, value
):
    roster = AsyncMock(side_effect=AssertionError("malformed grant reached platform"))
    monkeypatch.setattr(control.owner, "roster", roster)
    before = ledger_dump(control.domain.store)
    control_before = ledger_dump(control.owner.store)
    with pytest.raises(OpenError, match="^finance_arguments$"):
        await control.rpc.dispatch(
            OpenCall(
                action="finance_grant",
                actor="dashboard/offline",
                payload=grant_payload(**{field: value}),
            )
        )
    roster.assert_not_awaited()
    control.adapter.business.request.assert_not_awaited()
    assert ledger_dump(control.domain.store) == before
    assert ledger_dump(control.owner.store) == control_before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    ["account", "group_id", "uid", "role", "active", "version", "request_id"],
)
async def test_missing_grant_fields_are_rejected_before_platform_io(
    control, monkeypatch, field
):
    roster = AsyncMock(side_effect=AssertionError("incomplete grant reached platform"))
    monkeypatch.setattr(control.owner, "roster", roster)
    payload = grant_payload()
    del payload[field]
    before = ledger_dump(control.domain.store)
    with pytest.raises(OpenError, match="^finance_arguments$"):
        await control.rpc.dispatch(
            OpenCall(action="finance_grant", actor="dashboard/offline", payload=payload)
        )
    roster.assert_not_awaited()
    control.adapter.business.request.assert_not_awaited()
    assert ledger_dump(control.domain.store) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["account", "group_id"])
@pytest.mark.parametrize("value", [None, 99, True, [], {}, "", "0", "01", "9" * 21])
async def test_malformed_directory_scope_is_rejected_before_platform_io(
    control, monkeypatch, field, value
):
    roster = AsyncMock(side_effect=AssertionError("invalid scope reached platform"))
    monkeypatch.setattr(control.owner, "roster", roster)
    payload = {"account": "99", "group_id": "5", field: value}
    before = ledger_dump(control.domain.store)
    with pytest.raises(OpenError, match="^finance_arguments$"):
        await control.rpc.dispatch(
            OpenCall(
                action="finance_members", actor="dashboard/offline", payload=payload
            )
        )
    roster.assert_not_awaited()
    control.adapter.business.request.assert_not_awaited()
    assert ledger_dump(control.domain.store) == before


@pytest.mark.asyncio
async def test_grant_binds_peer_from_fresh_directory_and_replays_once(control):
    call = OpenCall(
        action="finance_grant",
        actor="dashboard/offline",
        payload=grant_payload(),
    )
    assert await control.rpc.dispatch(call) == {"status": "saved"}
    before = ledger_dump(control.domain.store)
    assert await control.rpc.dispatch(call) == {"status": "saved"}
    assert ledger_dump(control.domain.store) == before
    grant = control.domain.store.finance_grants()[0]
    assert (grant["account"], grant["group_id"], grant["uid"], grant["peer"]) == (
        "99",
        "5",
        "2",
        "1002",
    )
    assert grant["role"] == "cashier" and grant["version"] == 1
    assert control.domain.store.finance_grants("99", "2", "forged-peer") == []
    with pytest.raises(CashbackError, match="finance_forbidden"):
        control.domain.store.groups(FinanceActor("99", "2", "forged-peer", "5", 1))
    with control.domain.store.connect() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM users WHERE role='chat'").fetchone()[0]
            == 1
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM audit WHERE action='chat_finance_grant'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.asyncio
async def test_member_and_grant_display_names_prefer_actual_user_nick(control):
    member = next(row for row in control.rows if row["userId"] == "2")
    member.update(
        userNick="Actual display nickname",
        name="Older display name",
        userName="Login alias",
    )
    directory = await control.rpc.dispatch(
        OpenCall(
            action="finance_members",
            actor="dashboard/offline",
            payload={"account": "99", "group_id": "5"},
        )
    )
    assert next(row for row in directory["members"] if row["uid"] == "2") == {
        "uid": "2",
        "name": "Actual display nickname",
    }
    await control.rpc.dispatch(
        OpenCall(
            action="finance_grant",
            actor="dashboard/offline",
            payload=grant_payload(),
        )
    )
    grant = control.domain.store.finance_grants()[0]
    assert grant["name"] == "Actual display nickname"
    assert grant["peer"] == "1002"


@pytest.mark.asyncio
async def test_grant_requires_target_in_verified_roster(control):
    before = ledger_dump(control.domain.store)
    with pytest.raises(OpenError, match="^finance_identity_unverified$"):
        await control.rpc.dispatch(
            OpenCall(
                action="finance_grant",
                actor="dashboard/offline",
                payload=grant_payload(uid="777"),
            )
        )
    assert ledger_dump(control.domain.store) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["finance_members", "finance_grant"])
async def test_secondary_security_code_is_clean_error_without_grants(control, action):
    control.adapter.business.request.side_effect = ProtocolError(
        "secondary_security_code_required"
    )
    payload = (
        {"account": "99", "group_id": "5"}
        if action == "finance_members"
        else grant_payload()
    )
    before = ledger_dump(control.domain.store)
    with pytest.raises(
        OpenError, match="^finance_secondary_security_required$"
    ) as error:
        await control.rpc.dispatch(
            OpenCall(action=action, actor="dashboard/offline", payload=payload)
        )
    assert error.value.__suppress_context__ is True
    assert ledger_dump(control.domain.store) == before
    assert control.domain.store.finance_grants() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["cashier", "finance_admin"])
@pytest.mark.parametrize(
    "unavailable", ["offline", "no-adapter", "no-tenant", "account-removed"]
)
async def test_existing_grant_can_be_revoked_without_platform_scope(
    control, monkeypatch, role, unavailable
):
    payload = grant_payload(role=role)
    await control.rpc.dispatch(
        OpenCall(action="finance_grant", actor="dashboard/offline", payload=payload)
    )
    original = control.domain.store.finance_grants()[0]
    control.adapter.connection_state = "offline"
    if unavailable in {"no-adapter", "account-removed"}:
        monkeypatch.setattr(control.owner, "adapters", lambda: [])
    if unavailable in {"no-tenant", "account-removed"}:
        del control.owner.tenants[("99", "5")]
    roster = AsyncMock(side_effect=AssertionError("revocation reached platform"))
    monkeypatch.setattr(control.owner, "roster", roster)
    monkeypatch.setattr(
        control.owner,
        "write_guard",
        Mock(side_effect=AssertionError("revocation requires missing adapter")),
    )
    control.adapter.business.request.reset_mock()
    control.adapter.business.request.side_effect = ProtocolError(
        "secondary_security_code_required"
    )
    call = OpenCall(
        action="finance_grant",
        actor="dashboard/offline",
        payload={
            **payload,
            "active": False,
            "version": 1,
            "request_id": "offline-revoke-0001",
        },
    )
    result = await control.rpc.dispatch(call)
    assert result == {"status": "saved"}
    after = ledger_dump(control.domain.store)
    assert await control.rpc.dispatch(call) == result
    assert ledger_dump(control.domain.store) == after
    roster.assert_not_awaited()
    control.adapter.business.request.assert_not_awaited()
    grant = control.domain.store.finance_grants()[0]
    assert grant == {**original, "active": 0, "version": 2}
    assert control.domain.store.finance_grants("99", "2", "1002") == []
    with pytest.raises(CashbackError, match="finance_forbidden"):
        control.domain.store.groups(FinanceActor("99", "2", "1002", "5", 1))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("updates", "error_type", "error"),
    [
        ({"account": "199"}, OpenError, "finance_identity_unverified"),
        ({"group_id": "6"}, OpenError, "finance_identity_unverified"),
        ({"uid": "1"}, OpenError, "finance_identity_unverified"),
        ({"version": 0}, CashbackError, "conflict"),
        ({"active": True, "role": "finance_admin"}, OpenError, "finance_scope"),
    ],
)
async def test_removed_account_revocation_fallback_cannot_change_scope_or_reactivate(
    control, monkeypatch, updates, error_type, error
):
    await control.rpc.dispatch(
        OpenCall(
            action="finance_grant",
            actor="dashboard/offline",
            payload=grant_payload(),
        )
    )
    monkeypatch.setattr(control.owner, "adapters", lambda: [])
    del control.owner.tenants[("99", "5")]
    roster = AsyncMock(side_effect=AssertionError("removed account reached platform"))
    monkeypatch.setattr(control.owner, "roster", roster)
    control.adapter.business.request.reset_mock()
    before = ledger_dump(control.domain.store)
    with pytest.raises(error_type, match=f"^{error}$"):
        await control.rpc.dispatch(
            OpenCall(
                action="finance_grant",
                actor="dashboard/offline",
                payload=grant_payload(
                    active=False,
                    version=1,
                    request_id="offline-revoke-0001",
                )
                | updates,
            )
        )
    roster.assert_not_awaited()
    control.adapter.business.request.assert_not_awaited()
    assert ledger_dump(control.domain.store) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["admission", "commit"])
async def test_removed_account_revocation_requires_active_owner_at_both_barriers(
    control, monkeypatch, phase
):
    await control.rpc.dispatch(
        OpenCall(
            action="finance_grant",
            actor="dashboard/offline",
            payload=grant_payload(),
        )
    )
    monkeypatch.setattr(control.owner, "adapters", lambda: [])
    del control.owner.tenants[("99", "5")]
    roster = AsyncMock(side_effect=AssertionError("revocation reached platform"))
    monkeypatch.setattr(control.owner, "roster", roster)
    control.adapter.business.request.reset_mock()
    before = ledger_dump(control.domain.store)
    if phase == "admission":
        control.owner.active = False
    else:
        original = control.domain.store._audit

        def deactivate(*args, **kwargs):
            """Stop the temporary service after mutation but before commit."""
            original(*args, **kwargs)
            control.owner.active = False

        monkeypatch.setattr(control.domain.store, "_audit", deactivate)
    with pytest.raises(OpenError, match="^finance_unavailable$"):
        await control.rpc.dispatch(
            OpenCall(
                action="finance_grant",
                actor="dashboard/offline",
                payload=grant_payload(
                    active=False,
                    version=1,
                    request_id="offline-revoke-0001",
                ),
            )
        )
    roster.assert_not_awaited()
    control.adapter.business.request.assert_not_awaited()
    assert ledger_dump(control.domain.store) == before


@pytest.mark.asyncio
async def test_sandbox_is_fixed_scope_labeled_and_separate_from_live_ledger(
    sandbox_control, tmp_path
):
    control = sandbox_control
    before = ledger_dump(control.domain.store)
    sandbox = FinanceSandboxService(
        control.owner.context, tmp_path / "standalone-sandbox", "rpc-offline"
    )
    sandbox.seed("offline-manager-peer", "Offline manager")
    rule = sandbox.store.get_group(TEST_GROUP)
    assert rule["account"] == TEST_ACCOUNT and rule["id"] == TEST_GROUP
    assert "\u6d4b\u8bd5\u8d26\u672c" in rule["name"]
    assert "\u4e0d\u8981\u5b9e\u9645\u4ed8\u6b3e" in rule["contact"]
    assert {row["event_id"] for row in sandbox.test_evidence} == {
        "simulated-99000001",
        "simulated-99000002",
        "simulated-99000003",
    }
    assert await sandbox._scan(TEST_GROUP) == sandbox.test_members
    assert await sandbox.roster(control.adapter, TEST_GROUP) == sandbox.test_members
    assert sandbox.historical_members(TEST_GROUP) == set()
    seeded = ledger_dump(sandbox.store)
    for group in ("1516387", "1488006", "5"):
        with pytest.raises(CashbackError, match="test_scope"):
            await sandbox._scan(group)
        with pytest.raises(CashbackError, match="test_scope"):
            await sandbox.roster(control.adapter, group)
        with pytest.raises(CashbackError, match="test_scope"):
            sandbox.historical_members(group)
    with pytest.raises(CashbackError, match="test_scope"):
        await sandbox.roster(SimpleNamespace(account="99"), TEST_GROUP)
    assert ledger_dump(sandbox.store) == seeded
    assert ledger_dump(control.domain.store) == before
    assert sandbox.store.path != control.domain.store.path
    assert [
        (row["account"], row["group_id"], row["uid"], row["peer"], row["role"])
        for row in sandbox.store.finance_grants()
    ] == [
        (
            TEST_ACCOUNT,
            TEST_GROUP,
            TEST_MANAGER,
            "offline-manager-peer",
            "finance_admin",
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("write", [False, True], ids=["read", "write"])
async def test_sandbox_guard_rejects_expiry_close_and_replaced_ledger(
    sandbox_control, monkeypatch, write
):
    control = sandbox_control
    before = ledger_dump(control.domain.store)
    await control.rpc.dispatch(test_call())
    chat = control.owner.finance_chat
    old = chat.test_service
    message = AstrBotMessage()
    message.type = MessageType.FRIEND_MESSAGE
    message.self_id = TEST_ACCOUNT
    message.session_id = f"{TEST_ACCOUNT}/private/{TEST_MANAGER}/offline-manager-peer"
    message.group_id = ""
    message.message_id = "offline-finance-guard"
    message.message_str = "\u7ed3\u8d26"
    message.sender = MessageMember(TEST_MANAGER, "Offline manager")
    message.raw_message = {"_transport_peer": "offline-manager-peer"}
    event = WangshangliaoEvent(message, control.adapter, True)
    event.actor_identity = ActorIdentity(
        TEST_ACCOUNT, TEST_MANAGER, "offline-manager-peer", TEST_MANAGER
    )
    actor = FinanceActor(
        TEST_ACCOUNT, TEST_MANAGER, "offline-manager-peer", TEST_GROUP, 1
    )
    old_guard = chat.native_guard(event, actor, old, write=write)
    live_guard = chat.native_guard(event, actor, control.domain, write=write)
    old_guard()
    deadline = chat.test_until
    with monkeypatch.context() as patch:
        patch.setattr(time, "time", lambda: deadline)
        with pytest.raises(CashbackError, match="test_expired"):
            old_guard()
        live_guard()
    await control.rpc.dispatch(
        test_call(active=False, version=1, request_id="offline-close-0001")
    )
    with pytest.raises(CashbackError, match="test_expired"):
        old_guard()
    await control.rpc.dispatch(test_call(version=2, request_id="offline-reopen-0001"))
    assert chat.test_service is not old
    assert chat.test_service.store.path != old.store.path
    assert chat.test_until > time.time()
    with pytest.raises(CashbackError, match="test_expired"):
        old_guard()
    chat.native_guard(event, actor, chat.test_service, write=write)()
    assert ledger_dump(control.domain.store) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "updates",
    [
        {"version": True},
        {"version": -1},
        {"version": "0"},
        {"version": 0.0},
        {"request_id": None},
        {"request_id": 123},
        {"request_id": []},
        {"request_id": "short"},
        {"request_id": "x" * 81},
        {"request_id": "invalid/request-id"},
        {"active": 1},
        {"peer": "forged-peer"},
    ],
)
async def test_test_control_validates_ids_versions_and_exact_fields(
    sandbox_control, updates
):
    control = sandbox_control
    call = test_call()
    payload = {**call.payload, **updates}
    before = ledger_dump(control.domain.store)
    with pytest.raises(OpenError, match="^finance_arguments$"):
        await control.rpc.dispatch(
            OpenCall(action=call.action, actor=call.actor, payload=payload)
        )
    control.owner.roster.assert_not_awaited()
    assert not getattr(control.owner, "finance_chat", None)
    assert ledger_dump(control.domain.store) == before
    assert not (control.domain.store.path.parent / "chat-tests").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["account", "sender", "group", "scope", "inactive"])
async def test_test_control_requires_exact_native_test_window(sandbox_control, invalid):
    control = sandbox_control
    window = control.adapter.test_window
    if invalid == "account":
        control.adapter.account = "99"
    elif invalid == "sender":
        window.sender.account = "1"
    elif invalid == "group":
        window.groups = {"1516387"}
    elif invalid == "scope":
        window.scopes = {"private_ai"}
    else:
        window.active.return_value = False
    with pytest.raises(OpenError, match="^finance_test_window_required$"):
        await control.rpc.dispatch(test_call())
    control.owner.roster.assert_not_awaited()
    assert control.owner.finance_chat.test_service is None
    assert control.domain.store.finance_grants() == []
    assert not (control.domain.store.path.parent / "chat-tests").exists()


@pytest.mark.asyncio
async def test_test_control_secondary_code_error_does_not_seed_or_grant(
    sandbox_control,
):
    control = sandbox_control
    control.owner.roster.side_effect = ProtocolError("secondary_security_code_required")
    before = ledger_dump(control.domain.store)
    with pytest.raises(OpenError, match="^finance_secondary_security_required$"):
        await control.rpc.dispatch(test_call())
    chat = control.owner.finance_chat
    assert chat.test_service is None and chat.test_until == 0
    assert not chat.test_requests
    assert ledger_dump(control.domain.store) == before
    assert not (control.domain.store.path.parent / "chat-tests").exists()
    assert not any(
        row["action"].startswith("chat_finance_test_")
        for row in control.owner.store.audit()
    )


@pytest.mark.asyncio
async def test_dashboard_test_window_does_not_reenter_control_lock(
    sandbox_control, monkeypatch
):
    from astrbot.builtin_stars.wangshangliao_moderation import dashboard

    control = sandbox_control
    control.owner.rpc = control.rpc
    page = object.__new__(dashboard.DashboardPage)
    page.authorize = AsyncMock()
    page.plugin = SimpleNamespace(open_service=control.owner)
    call = test_call()
    async def stream():
        yield json.dumps({"action": call.action, "payload": call.payload}).encode()

    monkeypatch.setattr(
        dashboard,
        "request",
        SimpleNamespace(
            _request=SimpleNamespace(method="POST", stream=stream),
            username="offline",
            json=AsyncMock(
                return_value={"action": call.action, "payload": call.payload}
            ),
        ),
    )
    before = ledger_dump(control.domain.store)
    await asyncio.wait_for(page.open_service(), timeout=5)
    assert not control.rpc.lock.locked()
    assert control.owner.finance_chat.test_until > time.time()
    control.owner.roster.assert_awaited_once()
    assert ledger_dump(control.domain.store) == before


@pytest.mark.asyncio
async def test_test_control_concurrent_retries_are_idempotent_and_actor_bound(
    sandbox_control,
):
    control = sandbox_control
    before = ledger_dump(control.domain.store)
    call = test_call()
    first, replay = await asyncio.gather(
        control.rpc.dispatch(call), control.rpc.dispatch(call)
    )
    assert (
        first
        == replay
        == {
            "status": "opened",
            "version": 1,
            "test_ledger": True,
        }
    )
    chat = control.owner.finance_chat
    sandbox = chat.test_service
    until = chat.test_until
    assert time.time() < until <= time.time() + 240
    control.owner.roster.assert_awaited_once()
    assert len(list((control.domain.store.path.parent / "chat-tests").iterdir())) == 1
    with pytest.raises(OpenError, match="^finance_test_changed$"):
        await control.rpc.dispatch(
            test_call(active=False, request_id=call.payload["request_id"])
        )
    with pytest.raises(OpenError, match="^finance_test_changed$"):
        await control.rpc.dispatch(test_call(actor="dashboard/another"))
    assert chat.test_service is sandbox and chat.test_until == until
    close = test_call(active=False, version=1, request_id="offline-close-0001")
    closed = await control.rpc.dispatch(close)
    assert closed == {"status": "closed", "version": 2}
    assert await control.rpc.dispatch(close) == closed
    assert await control.rpc.dispatch(call) == first
    assert chat.test_until == 0 and chat.test_version == 2
    control.owner.roster.assert_awaited_once()
    audit = [
        row
        for row in control.owner.store.audit()
        if row["action"].startswith("chat_finance_test_")
    ]
    assert sorted(row["action"] for row in audit) == [
        "chat_finance_test_closed",
        "chat_finance_test_opened",
    ]
    assert all(
        (row["actor"], row["account"], row["group_id"])
        == ("dashboard/offline", TEST_ACCOUNT, TEST_GROUP)
        for row in audit
    )
    assert ledger_dump(control.domain.store) == before


@pytest.mark.asyncio
async def test_test_control_retry_cache_is_bounded_and_old_versions_fail_closed(
    control,
):
    before = ledger_dump(control.domain.store)
    calls = [
        test_call(active=False, version=index, request_id=f"offline-close-{index:04d}")
        for index in range(130)
    ]
    for index, call in enumerate(calls):
        assert await control.rpc.dispatch(call) == {
            "status": "closed",
            "version": index + 1,
        }
    chat = control.owner.finance_chat
    assert isinstance(chat, FinanceChat)
    assert len(chat.test_requests) == 128
    assert (
        "dashboard/offline",
        calls[0].payload["request_id"],
    ) not in chat.test_requests
    assert await control.rpc.dispatch(calls[-1]) == {"status": "closed", "version": 130}
    with pytest.raises(OpenError, match="^finance_test_changed$"):
        await control.rpc.dispatch(calls[0])
    assert chat.test_version == 130 and chat.test_until == 0
    with control.owner.store.connect() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM audit WHERE action='chat_finance_test_closed'"
            ).fetchone()[0]
            == 130
        )
    assert ledger_dump(control.domain.store) == before
