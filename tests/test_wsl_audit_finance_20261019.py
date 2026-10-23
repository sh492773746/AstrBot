"""Offline finance audit regressions using exclusively temporary ledgers."""

import asyncio
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.cashback import CashbackService
from astrbot.builtin_stars.wangshangliao_moderation.cashback_finance import FinanceActor
from astrbot.builtin_stars.wangshangliao_moderation.cashback_store import (
    WRITE_GUARD,
    CashbackError,
    CashbackStore,
)
from astrbot.builtin_stars.wangshangliao_moderation.finance_sandbox import (
    TEST_ACCOUNT,
    TEST_GROUP,
    TEST_MANAGER,
    FinanceSandboxService,
)

OWNER = "audit-owner@example.invalid"


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """Build synthetic legacy qualification data in a temporary ledger.

    Args:
        tmp_path: Isolated pytest directory.
        monkeypatch: Test-local clock and metrics isolation.

    Returns:
        Temporary store, complete roster, source proof and mutable clock.
    """
    monkeypatch.setenv("ASTRBOT_DISABLE_METRICS", "1")
    clock = [2000.0]
    monkeypatch.setattr("time.time", lambda: clock[0])
    store = CashbackStore(tmp_path / "cashback.sqlite3")
    store.bootstrap([{"email": OWNER, "name": "Audit owner"}])
    members = {"1": {"name": "Recipient", "peer": "11", "eligible": True}}
    store.configure(
        OWNER,
        {
            "id": "5",
            "name": "Audit group",
            "bot_id": "audit-bot",
            "account": "99",
            "contact": "Offline contact",
            "enabled": True,
            "rate": 500,
            "agent_rate": 900,
            "qualification_seconds": 0,
            "version": 0,
        },
        members,
        set(),
        now=1000,
    )
    members["2"] = {"name": "Invitee", "peer": "22", "eligible": True}
    proof = {
        "member": "2",
        "inviter": "1",
        "source": "business_apply_log",
        "event_id": "200",
        "event_at": clock[0],
        "member_peer": "22",
        "inviter_peer": "11",
    }
    store.snapshot("5", "audit-bot", "99", members, [proof], now=clock[0])
    yield store, members, proof, clock
    store.close()


def entry(store):
    """Read one synthetic invitee.

    Args:
        store: Temporary ledger.

    Returns:
        Exact persisted invitee row.
    """
    with store.connect() as db:
        return dict(db.execute("SELECT * FROM entries WHERE member='2'").fetchone())


def selection(store):
    """Select the current synthetic invitee version.

    Args:
        store: Temporary ledger.

    Returns:
        One versioned selection.
    """
    row = entry(store)
    return [{"id": row["id"], "version": row["version"]}]


def dump(store):
    """Capture all temporary ledger tables for rollback assertions.

    Args:
        store: Temporary ledger.

    Returns:
        Complete logical database dump.
    """
    with store.connect() as db:
        return tuple(db.iterdump())


@pytest.fixture
def audit_service(ledger):
    """Bind the real service to a temporary ledger and an offline adapter.

    Args:
        ledger: Synthetic temporary ledger, roster, proof and clock.

    Yields:
        Service and adapter with no real platform methods.
    """
    store, members, proof, _ = ledger
    adapter = SimpleNamespace(
        account="99",
        config={
            "id": "audit-bot",
            "type": "wangshangliao",
            "enable": True,
            "enabled_groups": ["5"],
            "_business_active": True,
            "_business_epoch": 1,
            "_business_revision": "offline-revision-1",
        },
    )
    context = SimpleNamespace(
        platform_manager=SimpleNamespace(platform_insts=[adapter])
    )
    service = CashbackService(context, store.path.parent, "audit-bot")
    service.roster = AsyncMock(return_value=members)
    service.evidence = AsyncMock(return_value=[proof])
    service.activities.authorize = Mock()
    service._deliver_risk_notices = AsyncMock()
    yield service, adapter
    service.store.close()


def financial_state(store):
    """Read financial rows, excluding the ASGI request diagnostic log.

    Args:
        store: Temporary ledger under test.

    Returns:
        Exact financial entries, receipts, pricing history and domain audit.
    """
    with store.connect() as db:
        return tuple(
            tuple(tuple(row) for row in db.execute(query))
            for query in (
                "SELECT * FROM entries ORDER BY id",
                "SELECT * FROM requests ORDER BY actor,request_id",
                "SELECT * FROM reward_rule_history ORDER BY group_id,version",
                "SELECT * FROM audit WHERE action IN "
                "('pay','correct','exception','price_class_changed',"
                "'inviter_assigned') ORDER BY id",
            )
        )


def change_lifecycle(service, adapter, change):
    """Apply a synthetic lifecycle change without changing any stored ledger.

    Args:
        service: Offline service whose adapter can be replaced.
        adapter: Original fake adapter.
        change: Explicit lifecycle or identity boundary.
    """
    if change == "pause":
        adapter.config["_business_active"] = False
    elif change == "epoch":
        adapter.config["_business_epoch"] += 1
    elif change == "revision":
        adapter.config["_business_revision"] = "offline-revision-2"
    elif change == "account":
        adapter.account = "100"
    elif change == "disable":
        adapter.config["enable"] = False
    elif change == "replacement":
        service.context.platform_manager.platform_insts = [
            SimpleNamespace(account=adapter.account, config=dict(adapter.config))
        ]
    elif change == "withdraw":
        adapter.config["enabled_groups"] = []
    else:
        raise AssertionError(change)


@pytest.mark.parametrize("operation", ["pay", "reprice"])
def test_missing_frozen_price_never_falls_back_to_current_rule(ledger, operation):
    store, members, _, clock = ledger
    if operation == "reprice":
        items = selection(store)
        store.mutate(
            OWNER,
            "5",
            "reprice",
            items,
            "audit-original-agent-price",
            "External agent proof",
            category="agent",
            price_confirmation=store.price_preview(OWNER, "5", items, "agent"),
        )
    clock[0] += 1
    store.configure(
        OWNER,
        {**store.get_group("5"), "enabled": True, "rate": 800, "agent_rate": 1200},
        members,
        set(),
    )
    with store.connect(True) as db:
        db.execute("DELETE FROM reward_rule_history WHERE version=1")
    before = dump(store)
    with pytest.raises(CashbackError, match="historical_price_unavailable"):
        if operation == "reprice":
            store.price_preview(OWNER, "5", selection(store), "ordinary")
        else:
            store.mutate(
                OWNER, "5", "pay", selection(store), "audit-missing-price-payment"
            )
    assert dump(store) == before


def test_complete_frozen_price_preserves_old_debt_after_rule_change(ledger):
    store, members, _, clock = ledger
    clock[0] += 1
    store.configure(
        OWNER,
        {
            **store.get_group("5"),
            "enabled": True,
            "rate": 800,
            "agent_only": True,
        },
        members,
        set(),
    )
    result = store.mutate(
        OWNER, "5", "pay", selection(store), "audit-old-debt-original-price"
    )
    assert result["amount"] == 500
    assert entry(store)["rule_version"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["pay", "reprice"])
async def test_group_withdrawal_during_sqlite_wait_prevents_commit(
    ledger, monkeypatch, operation, audit_service
):
    store, members, *_ = ledger
    selected = {"5"}
    service, _ = audit_service
    service.store = store
    service.selected_groups = lambda: set(selected)
    service._scan = AsyncMock(return_value=members)
    entered = threading.Event()
    mutate = store.mutate

    def waiting(*args, **kwargs):
        entered.set()
        return mutate(*args, **kwargs)

    monkeypatch.setattr(store, "mutate", waiting)
    kwargs = (
        {
            "category": "agent",
            "price_confirmation": store.price_preview(
                OWNER, "5", selection(store), "agent"
            ),
        }
        if operation == "reprice"
        else {}
    )
    before = dump(store)
    blocker = sqlite3.connect(store.path)
    blocker.execute("BEGIN IMMEDIATE")
    task = asyncio.create_task(
        service.mutate(
            OWNER,
            "5",
            operation,
            selection(store),
            "audit-withdraw-group-while-waiting",
            "Audit selection",
            **kwargs,
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        selected.clear()
    finally:
        blocker.rollback()
        blocker.close()
        await asyncio.gather(task, return_exceptions=True)
    with pytest.raises(CashbackError, match="group_not_selected"):
        task.result()
    assert dump(store) == before


@pytest.mark.asyncio
async def test_group_withdrawal_after_domain_write_rolls_back_everything(
    ledger, monkeypatch, audit_service
):
    store, members, *_ = ledger
    selected = {"5"}
    service, _ = audit_service
    service.store = store
    service.selected_groups = lambda: set(selected)
    service._scan = AsyncMock(return_value=members)
    audit = store._audit

    def withdraw(*args, **kwargs):
        audit(*args, **kwargs)
        if args[4] == "pay":
            selected.clear()

    monkeypatch.setattr(store, "_audit", withdraw)
    before = dump(store)
    with pytest.raises(CashbackError, match="group_not_selected"):
        await service.mutate(
            OWNER, "5", "pay", selection(store), "audit-withdraw-before-commit"
        )
    assert dump(store) == before


@pytest.mark.asyncio
async def test_service_keeps_existing_commit_guard(ledger, audit_service, monkeypatch):
    store, members, *_ = ledger
    service, _ = audit_service
    service.store = store
    service.selected_groups = lambda: {"5"}
    service._scan = AsyncMock(return_value=members)
    calls = []
    revoked = threading.Event()
    audit = store._audit

    def revoke_after_payment(*args, **kwargs):
        audit(*args, **kwargs)
        if args[4] == "pay":
            revoked.set()

    monkeypatch.setattr(store, "_audit", revoke_after_payment)

    def guard():
        calls.append(True)
        if revoked.is_set():
            raise CashbackError("audit_revoked", "Offline revocation", 403)

    before = dump(store)
    token = WRITE_GUARD.set(guard)
    try:
        with pytest.raises(CashbackError, match="audit_revoked"):
            await service.mutate(
                OWNER, "5", "pay", selection(store), "audit-outer-guard-rollback"
            )
    finally:
        WRITE_GUARD.reset(token)
    assert revoked.is_set() and len(calls) >= 3
    assert dump(store) == before


def test_same_request_across_store_connections_registers_payment_once(ledger):
    store, *_ = ledger
    other = CashbackStore(store.path)
    items = selection(store)
    barrier = threading.Barrier(2)

    def pay(connection):
        barrier.wait(timeout=3)
        return connection.mutate(
            OWNER, "5", "pay", items, "audit-shared-ledger-same-request"
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(pay, (store, other)))
        assert results[0] == results[1]
        assert results[0]["amount"] == 500
        with store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1
            assert (
                db.execute("SELECT COUNT(*) FROM audit WHERE action='pay'").fetchone()[
                    0
                ]
                == 1
            )
    finally:
        other.close()


def test_stale_price_confirmation_changes_no_financial_rows(ledger):
    store, members, _, clock = ledger
    items = selection(store)
    confirmation = store.price_preview(OWNER, "5", items, "agent")
    clock[0] += 1
    store.configure(
        OWNER,
        {**store.get_group("5"), "enabled": True, "agent_rate": 1200},
        members,
        set(),
    )
    before = dump(store)
    with pytest.raises(CashbackError, match="price_confirmation_changed"):
        store.mutate(
            OWNER,
            "5",
            "reprice",
            items,
            "audit-stale-price-confirmation",
            "External agent proof",
            category="agent",
            price_confirmation=confirmation,
        )
    assert dump(store) == before


def test_native_confirmation_and_domain_receipt_roll_back_together(ledger):
    store, *_ = ledger
    store.finance_grant(
        "dashboard/audit",
        {
            "account": "99",
            "group_id": "5",
            "uid": "1",
            "peer": "11",
            "name": "Native cashier",
            "role": "cashier",
            "active": True,
            "version": 0,
            "request_id": "audit-native-grant",
        },
    )
    actor = FinanceActor("99", "1", "11", "5", 1)
    ticket = store.finance_preview(
        actor,
        "audit/private/1",
        "audit-boot",
        {"operation": "pay", "items": selection(store), "reason": ""},
    )
    before = dump(store)

    def fail_after_payment(payload):
        store.mutate(
            actor, "5", "pay", payload["items"], ticket["id"], payload["reason"]
        )
        raise RuntimeError("Synthetic response failure")

    with pytest.raises(RuntimeError, match="Synthetic response failure"):
        store.finance_apply(
            actor,
            ticket["id"],
            ticket["code"],
            "audit/private/1",
            "audit-boot",
            fail_after_payment,
        )
    assert dump(store) == before
    with pytest.raises(CashbackError, match="finance_forbidden"):
        store.get_group("6", actor)
    with pytest.raises(CashbackError, match="forbidden"):
        store.groups(str(actor))


def test_private_totals_bind_sender_collector_and_account_without_writes(ledger):
    store, *_ = ledger
    before = dump(store)
    own = store.private_invitation_summaries("1", "audit-bot", "99", ["5"])
    other = store.private_invitation_summaries("2", "audit-bot", "99", ["5"])
    assert own[0]["total"] == 1 and own[0]["pending"] == 500
    assert other[0]["total"] == 0 and other[0]["pending"] == 0
    assert store.private_invitation_summaries("1", "other-bot", "99", ["5"]) == []
    assert store.private_invitation_summaries("1", "audit-bot", "100", ["5"]) == []
    assert dump(store) == before


@pytest.mark.asyncio
async def test_sandbox_payment_never_touches_shared_live_ledger(ledger, tmp_path):
    live, _, _, clock = ledger
    before = dump(live)
    clock[0] = 20000.0
    sandbox = FinanceSandboxService(
        SimpleNamespace(), tmp_path / "independent-rehearsal", "audit-bot"
    )
    try:
        sandbox.seed("11", "Synthetic manager")
        actor = FinanceActor(TEST_ACCOUNT, TEST_MANAGER, "11", TEST_GROUP, 1)
        with sandbox.store.connect() as db:
            items = [
                {"id": row["id"], "version": row["version"]}
                for row in db.execute("SELECT id,version FROM entries WHERE eligible=1")
            ]
        preview = sandbox.store.finance_preview(
            actor,
            "audit/test/private",
            "audit-test-boot",
            {"operation": "pay", "items": items, "reason": ""},
        )
        result = sandbox.store.finance_apply(
            actor,
            preview["id"],
            preview["code"],
            "audit/test/private",
            "audit-test-boot",
            lambda payload: sandbox.store.mutate(
                actor, TEST_GROUP, "pay", payload["items"], preview["id"]
            ),
        )
        assert result["count"] == 3 and result["amount"] == 1500
        assert sandbox.store.path.resolve() != live.path.resolve()
        assert json.loads(sandbox.store.finance_result(actor)["result"]) == result
        assert dump(live) == before
    finally:
        sandbox.store.close()


@pytest.fixture
def kick_event():
    """Build a synthetic, trusted membership-source event.

    Returns:
        An exact KICK_MEMBER event independent of membership state labels.
    """
    return {
        "apply_id": "201",
        "member_id": "2",
        "member_nim_id": "22",
        "inviter_id": "",
        "inviter_nim_id": "",
        "apply_time": "2010",
        "state": "MEMBER_STATE_GOOD",
        "apply_type": "KICK_MEMBER",
    }


@pytest.mark.parametrize(
    "state",
    ["MEMBER_STATE_GOOD", "MEMBER_STATE_KICKED", "MEMBER_STATE_BAD"],
)
@pytest.mark.parametrize("rejoin", [False, True])
def test_kick_member_invalidates_original_invitation_without_rewriting_history(
    ledger, kick_event, state, rejoin
):
    store, members, proof, clock = ledger
    original = entry(store)
    clock[0] = 2020.0
    event = {**kick_event, "state": state}
    events = [event]
    if rejoin:
        events.append(
            {
                **event,
                "apply_id": "202",
                "apply_time": "2020",
                "state": "MEMBER_STATE_GOOD",
                "apply_type": "APPLY",
            }
        )
    store.remember_source_page(
        "5", "audit-bot", "99", 1000, {"items": events, "last_id": "2020"}
    )
    before = dump(store)
    with store.connect() as db:
        integrity = store._entry_integrity(db, store.get_group("5"), original)
    assert integrity["membership_left_at"] == 2010
    assert integrity["review_reason"] == ("admission_superseded" if rejoin else "")
    for pagination in ("entries", "inviters"):
        page = store.list_entries(OWNER, "5", pagination=pagination)
        assert page["payable_count"] == 0 and page["payable_amount"] == 0
        assert page["items"][0]["membership_left_at"] == 2010
        assert page["items"][0]["review_reason"] == integrity["review_reason"]
    assert store.invitation_summary("5", "1")["pending"] == 0
    assert (
        store.private_invitation_summaries("1", "audit-bot", "99", ["5"])[0]["pending"]
        == 0
    )
    with pytest.raises(CashbackError, match="price_change_forbidden"):
        store.price_preview(OWNER, "5", selection(store), "agent")
    with pytest.raises(CashbackError, match="not_payable|qualification_required"):
        store.mutate(
            OWNER, "5", "pay", selection(store), "audit-kick-member-cannot-pay"
        )
    if rejoin:
        with pytest.raises(CashbackError, match="not_payable"):
            store.mutate(
                OWNER,
                "5",
                "exception",
                selection(store),
                "audit-kick-rejoin-cannot-waive-attribution",
                "Synthetic external payment",
                actual_payment_confirmed=True,
            )
    assert dump(store) == before
    assert entry(store) == original
    if rejoin:
        store.snapshot("5", "audit-bot", "99", members, [proof], now=clock[0])
        after = entry(store)
        fields = ("amount", "inviter", "inviter_peer", "state", "paid_by", "paid_at")
        assert {key: after[key] for key in fields} == {
            key: original[key] for key in fields
        }
        assert (
            store.list_entries(OWNER, "5")["items"][0]["review_reason"]
            == "admission_superseded"
        )


def test_existing_admission_view_stays_untouched_but_new_kicks_block_totals(
    ledger, kick_event
):
    store, _, _, clock = ledger
    original = entry(store)
    with store.connect(True) as db:
        db.execute("DROP VIEW cashback_admission_checks")
        db.execute(
            "CREATE VIEW cashback_admission_checks AS "
            "SELECT id,1 AS payable_evidence FROM entries"
        )
        view_before = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='cashback_admission_checks'"
        ).fetchone()[0]
        entries_before = [
            tuple(row) for row in db.execute("SELECT * FROM entries ORDER BY id")
        ]
    reopened = CashbackStore(store.path)
    try:
        clock[0] = 2010.0
        reopened.remember_source_page(
            "5",
            "audit-bot",
            "99",
            1000,
            {
                "items": [{**kick_event, "state": "MEMBER_STATE_BAD"}],
                "last_id": "2010",
            },
        )
        before = dump(reopened)
        assert reopened.list_entries(OWNER, "5")["payable_amount"] == 0
        assert reopened.invitation_summary("5", "1")["pending"] == 0
        assert (
            reopened.private_invitation_summaries(
                "1", "audit-bot", "99", ["5"], detail_group="5"
            )[0]["items"][0]["payable"]
            == 0
        )
        with reopened.connect() as db:
            assert (
                db.execute(
                    "SELECT sql FROM sqlite_master WHERE name='cashback_admission_checks'"
                ).fetchone()[0]
                == view_before
            )
            assert [
                tuple(row) for row in db.execute("SELECT * FROM entries ORDER BY id")
            ] == entries_before
        assert dump(reopened) == before
        assert entry(reopened) == original
    finally:
        reopened.close()


@pytest.mark.parametrize("rejoin", [False, True])
def test_kick_and_natural_rejoin_preserve_registered_payment(
    ledger, kick_event, rejoin
):
    store, _, _, clock = ledger
    items = selection(store)
    receipt = store.mutate(OWNER, "5", "pay", items, "audit-paid-before-kick-member")
    paid = entry(store)
    clock[0] = 2020.0
    events = [kick_event]
    if rejoin:
        events.append(
            {
                **kick_event,
                "apply_id": "202",
                "apply_time": "2020",
                "apply_type": "APPLY",
            }
        )
    store.remember_source_page(
        "5", "audit-bot", "99", 1000, {"items": events, "last_id": "2020"}
    )
    before = dump(store)
    for pagination in ("entries", "inviters"):
        page = store.list_entries(OWNER, "5", "paid", pagination=pagination)
        assert page["total"] == 1 and page["amount"] == 500
        assert page["items"][0]["membership_left_at"] == 2010
        assert page["items"][0]["paid_by"] == OWNER
    assert (
        store.mutate(OWNER, "5", "pay", items, "audit-paid-before-kick-member")
        == receipt
    )
    assert entry(store) == paid
    assert dump(store) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("account", "100"),
        ("bot_id", "other-bot"),
        ("group_id", "6"),
        ("member", "3"),
        ("event_at", 1999),
    ],
)
def test_kick_member_is_bound_to_exact_account_collector_member_and_cycle(
    ledger, kick_event, field, value
):
    store, _, _, clock = ledger
    clock[0] = 2010.0
    store.remember_source_page(
        "5",
        "audit-bot",
        "99",
        1000,
        {"items": [kick_event], "last_id": "2010"},
    )
    with store.connect(True) as db:
        db.execute(f"UPDATE cashback_membership_events SET {field}=?", (value,))
    before = dump(store)
    page = store.list_entries(OWNER, "5")
    assert page["payable_amount"] == 500
    assert page["items"][0]["membership_left_at"] == 0
    assert page["items"][0]["review_reason"] == ""
    assert store.invitation_summary("5", "1")["pending"] == 500
    assert (
        store.private_invitation_summaries("1", "audit-bot", "99", ["5"])[0]["pending"]
        == 500
    )
    assert dump(store) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["pay", "correct"])
async def test_initialized_service_revalidates_but_receipt_replay_never_rescans(
    ledger, audit_service, operation
):
    store, *_ = ledger
    service, _ = audit_service
    if operation == "correct":
        store.mutate(OWNER, "5", "pay", selection(store), "audit-before-correction")
    items = selection(store)
    receipt = await service.mutate(
        OWNER,
        "5",
        operation,
        items,
        "audit-initialized-service-replay",
        "Synthetic offline evidence",
    )
    service.roster.assert_awaited_once()
    after = financial_state(store)
    service.roster.side_effect = AssertionError("Receipt replay must not rescan")
    assert (
        await service.mutate(
            OWNER,
            "5",
            operation,
            items,
            "audit-initialized-service-replay",
            "Synthetic offline evidence",
        )
        == receipt
    )
    assert financial_state(store) == after


@pytest.mark.asyncio
async def test_price_preview_preserves_inherited_revocation_after_read(
    ledger, audit_service, monkeypatch
):
    store, *_ = ledger
    service, _ = audit_service
    revoked = threading.Event()
    preview = service.store.price_preview

    def revoke_after_read(*args, **kwargs):
        result = preview(*args, **kwargs)
        revoked.set()
        return result

    def guard():
        if revoked.is_set():
            raise CashbackError("audit_revoked", "Offline revocation", 403)

    monkeypatch.setattr(service.store, "price_preview", revoke_after_read)
    before = dump(store)
    token = WRITE_GUARD.set(guard)
    try:
        with pytest.raises(CashbackError, match="audit_revoked"):
            await service.price_preview(OWNER, "5", selection(store), "agent")
    finally:
        WRITE_GUARD.reset(token)
    assert revoked.is_set()
    assert dump(store) == before
