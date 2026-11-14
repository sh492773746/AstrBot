"""Offline total-control authorization and isolated finance rehearsal regressions."""

import socket
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from astrbot.builtin_stars.wangshangliao_moderation.cashback import CashbackService
from astrbot.builtin_stars.wangshangliao_moderation.cashback_store import CashbackError
from astrbot.builtin_stars.wangshangliao_moderation.finance_sandbox import (
    TEST_ACCOUNT,
    TEST_GROUP,
    TEST_MANAGER,
    FinanceSandboxService,
)
from astrbot.builtin_stars.wangshangliao_moderation.open_rpc import OpenCall, OpenRPC
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
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
@pytest.mark.parametrize("action", ["finance_status"])
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
async def test_finance_status_preserves_historical_grants_without_reopening_chat(
    control,
):
    control.domain.store.finance_grant(
        "dashboard/fixture",
        {
            "account": "99",
            "group_id": "5",
            "uid": "2",
            "peer": "1002",
            "name": "Historical cashier",
            "role": "cashier",
            "active": True,
            "version": 0,
            "request_id": "offline-historical-grant",
        },
    )
    before = ledger_dump(control.domain.store)
    grants = control.domain.store.finance_grants()
    result = await control.rpc.dispatch(
        OpenCall(action="finance_status", actor="dashboard/offline", payload={})
    )
    assert result["grants"] == grants
    assert grants[0]["name"] == "Historical cashier"
    assert result["test_active"] is False
    assert result["test_version"] == 0
    assert ledger_dump(control.domain.store) == before
    assert not hasattr(control.owner, "finance_chat")
    control.adapter.business.request.assert_not_awaited()


@pytest.mark.parametrize("action", ["finance_members", "finance_grant", "finance_test"])
def test_retired_finance_writes_are_absent_from_the_rpc_allowlist(control, action):
    before = ledger_dump(control.domain.store)
    with pytest.raises(ValidationError):
        OpenCall(action=action, actor="dashboard/offline", payload={})
    assert ledger_dump(control.domain.store) == before
    control.adapter.business.request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload", [{"role": "cashier"}, {"active": True}, {"version": 1}]
)
async def test_finance_status_cannot_mutate_grants_through_payload(control, payload):
    before = ledger_dump(control.domain.store)
    with pytest.raises(OpenError, match="^finance_arguments$"):
        await control.rpc.dispatch(
            OpenCall(
                action="finance_status", actor="dashboard/offline", payload=payload
            )
        )
    assert ledger_dump(control.domain.store) == before
    control.adapter.business.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_sandbox_is_fixed_scope_labeled_and_separate_from_live_ledger(
    control, tmp_path
):
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
    adapter = SimpleNamespace(account=TEST_ACCOUNT)
    assert await sandbox.roster(adapter, TEST_GROUP) == sandbox.test_members
    assert sandbox.historical_members(TEST_GROUP) == set()
    seeded = ledger_dump(sandbox.store)
    for group in ("1516387", "1488006", "5"):
        with pytest.raises(CashbackError, match="test_scope"):
            await sandbox._scan(group)
        with pytest.raises(CashbackError, match="test_scope"):
            await sandbox.roster(adapter, group)
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
