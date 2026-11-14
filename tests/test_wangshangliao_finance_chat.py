"""Offline scope, confirmation, transaction and native finance regressions."""

import hashlib
import json
import re
from unittest.mock import Mock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.cashback_finance import (
    FINANCE_TRANSACTION,
    FinanceActor,
)
from astrbot.builtin_stars.wangshangliao_moderation.cashback_store import (
    WRITE_GUARD,
    CashbackError,
)
from tests.test_wangshangliao_cashback_qualification import (
    OWNER,
    entry,
    mature,
    scan,
)
from tests.test_wangshangliao_cashback_qualification import (
    scenario as scenario,
)


def grant_data(**updates):
    """Build a control-plane request for an offline verified identity.

    Args:
        **updates: Explicit field overrides.

    Returns:
        A complete grant request.
    """
    return {
        "account": "99",
        "group_id": "5",
        "uid": "1",
        "peer": "11",
        "name": "Finance operator",
        "role": "finance_admin",
        "active": True,
        "version": 0,
        "request_id": "finance-grant-0001",
        **updates,
    }


@pytest.fixture
def finance(scenario):
    store, *_ = scenario
    store.finance_grant("dashboard/test", grant_data())
    return store, FinanceActor("99", "1", "11", "5", 1)


@pytest.fixture
def ready(finance, scenario):
    store, actor = finance
    return store, actor, mature(scenario)


def payment_preview(store, actor, row, session="session", boot="boot"):
    """Preview one real fixture ledger entry.

    Args:
        store: Temporary ledger.
        actor: Scoped financial actor.
        row: Exact current entry.
        session: Private session isolation key.
        boot: Process nonce.

    Returns:
        Persisted confirmation and cleartext one-time code.
    """
    return store.finance_preview(
        actor,
        session,
        boot,
        {
            "operation": "pay",
            "items": [{"id": row["id"], "version": row["version"]}],
            "reason": "",
        },
    )


def payment_callback(store, actor, ticket):
    """Bind a trusted callback to the existing domain mutation.

    Args:
        store: Temporary ledger.
        actor: Scoped financial actor.
        ticket: Confirmation ID used for domain idempotency.

    Returns:
        A synchronous domain callback.
    """
    return lambda payload: store.mutate(
        actor,
        actor.group,
        payload["operation"],
        payload["items"],
        ticket,
        payload["reason"],
    )


def confirmation(store, ticket):
    """Read one persisted confirmation from the temporary ledger.

    Args:
        store: Temporary ledger.
        ticket: Confirmation ID.

    Returns:
        The complete stored confirmation.
    """
    with store.connect() as db:
        return dict(
            db.execute(
                "SELECT * FROM chat_finance_confirmations WHERE id=?", (ticket,)
            ).fetchone()
        )


def database_snapshot(store):
    """Capture all persisted state to detect partial writes.

    Args:
        store: Temporary ledger.

    Returns:
        SQLite's complete logical dump.
    """
    with store.connect() as db:
        return tuple(db.iterdump())


@pytest.mark.parametrize(
    "fields",
    [
        ("100", "1", "11", "5", 1),
        ("99", "2", "11", "5", 1),
        ("99", "1", "12", "5", 1),
        ("99", "1", "11", "6", 1),
        ("99", "1", "11", "5", 0),
        ("99", "1", "11", "5", 2),
    ],
    ids=["account", "uid", "peer", "group", "old-version", "future-version"],
)
def test_finance_actor_requires_exact_persisted_grant(finance, fields):
    store, _ = finance
    actor = FinanceActor(*fields)
    with pytest.raises(CashbackError, match="finance_forbidden"):
        store.groups(actor)


def test_finance_actor_scopes_reads_and_rejects_string_web_principal(finance, scenario):
    store, actor = finance
    _, members, *_ = scenario
    rule = store.get_group("5")
    store.configure(
        OWNER,
        {**rule, "id": "6", "name": "Other group", "enabled": True, "version": 0},
        members,
        set(),
    )
    assert [row["id"] for row in store.groups(actor)] == ["5"]
    assert store.get_group("5", actor)["account"] == "99"
    with pytest.raises(CashbackError, match="finance_forbidden"):
        store.get_group("6", actor)
    with pytest.raises(CashbackError, match="finance_forbidden"):
        store.list_entries(actor, "6", "pending")
    with pytest.raises(CashbackError, match="forbidden"):
        store.groups(str(actor))
    with store.connect() as db:
        assert (
            db.execute(
                "SELECT role FROM users WHERE email=?", (str(actor),)
            ).fetchone()[0]
            == "chat"
        )


@pytest.mark.parametrize("method", ["users", "cashier_profiles", "audit"])
def test_group_finance_admin_cannot_read_global_admin_data(finance, method):
    store, actor = finance
    args = (actor, "") if method == "audit" else (actor,)
    with pytest.raises(CashbackError, match="forbidden"):
        getattr(store, method)(*args)


@pytest.mark.parametrize("method", ["save_user", "save_cashier"])
def test_group_finance_admin_cannot_write_global_identities(finance, method):
    store, actor = finance
    before = database_snapshot(store)
    args = (
        (actor, "outsider@example.com", "Unauthorized web cashier", True, 0)
        if method == "save_user"
        else (actor, "2", "Unauthorized global contact", True, 0)
    )
    with pytest.raises(CashbackError, match="forbidden"):
        getattr(store, method)(*args)
    assert database_snapshot(store) == before


def test_group_finance_admin_cannot_read_other_group_or_global_revisions(
    finance, scenario
):
    store, actor = finance
    _, members, *_ = scenario
    store.configure(
        OWNER,
        {
            **store.get_group("5"),
            "id": "6",
            "name": "Other group",
            "enabled": True,
            "version": 0,
        },
        members,
        set(),
    )
    assert store.live_revision(OWNER, ["6"])
    with pytest.raises(CashbackError, match="forbidden"):
        store.live_revision(actor, ["6"])
    assert all(row[0] == "5" for row in store.live_revision(actor, ["5"]))


def test_finance_actor_cannot_configure_another_group_or_account(finance, scenario):
    store, actor = finance
    _, members, *_ = scenario
    rule = store.get_group("5")
    before = database_snapshot(store)
    for overrides in ({"id": "6", "version": 0}, {"account": "100"}):
        with pytest.raises(CashbackError, match="finance_forbidden"):
            store.configure(
                actor, {**rule, "enabled": True, **overrides}, members, set()
            )
    assert database_snapshot(store) == before


@pytest.mark.parametrize(
    "operation", ["configure", "assign", "exception", "risk_release"]
)
def test_cashier_cannot_preview_finance_admin_operations(finance, operation):
    store, _ = finance
    store.finance_grant(
        "dashboard/test",
        grant_data(role="cashier", version=1, request_id="finance-cashier-0002"),
    )
    actor = FinanceActor("99", "1", "11", "5", 2)
    with pytest.raises(CashbackError, match="finance_forbidden"):
        store.finance_preview(actor, "session", "boot", {"operation": operation})


def test_grants_are_idempotent_versioned_and_revocable(finance, scenario):
    store, actor = finance
    data = grant_data()
    before = database_snapshot(store)
    assert store.finance_grant("dashboard/test", data) == {"status": "saved"}
    assert database_snapshot(store) == before
    with pytest.raises(CashbackError, match="conflict"):
        store.finance_grant("dashboard/test", {**data, "role": "cashier"})
    with pytest.raises(CashbackError, match="conflict"):
        store.finance_grant(
            "dashboard/test", grant_data(request_id="finance-stale-0002")
        )
    ticket = payment_preview(store, actor, scan(scenario))
    store.finance_grant(
        "dashboard/test",
        grant_data(active=False, version=1, request_id="finance-revoke-0002"),
    )
    assert store.finance_grants("99", "1", "11") == []
    assert store.finance_grants()[0]["version"] == 2
    assert confirmation(store, ticket["id"])["status"] == "cancelled"
    with pytest.raises(CashbackError, match="finance_forbidden"):
        store.groups(actor)
    with pytest.raises(CashbackError, match="finance_forbidden"):
        store.groups(FinanceActor("99", "1", "11", "5", 2))
    store.finance_grant(
        "dashboard/test",
        grant_data(version=2, request_id="finance-restore-0003"),
    )
    assert len(store.groups(FinanceActor("99", "1", "11", "5", 3))) == 1
    assert confirmation(store, ticket["id"])["status"] == "cancelled"


def test_grants_reject_non_dashboard_controllers(finance):
    store, actor = finance
    before = database_snapshot(store)
    for controller in (actor, OWNER, "admin", "chat/forged"):
        with pytest.raises(CashbackError, match="invalid_grant"):
            store.finance_grant(controller, grant_data())
    assert database_snapshot(store) == before


def test_grant_discovery_requires_exact_account_uid_and_peer(finance):
    store, _ = finance
    assert len(store.finance_grants("99", "1", "11")) == 1
    for args in (("100", "1", "11"), ("99", "2", "11"), ("99", "1", "12")):
        assert store.finance_grants(*args) == []


def test_native_grant_discovery_hides_reassigned_but_keeps_unconfigured_groups(
    finance, scenario
):
    store, _ = finance
    store.finance_grant(
        "dashboard/test",
        grant_data(group_id="6", request_id="finance-unconfigured-6"),
    )
    store.configure(
        OWNER,
        {**store.get_group("5"), "account": "100", "enabled": True},
        scenario[1],
        set(),
    )
    assert [row["group_id"] for row in store.finance_grants("99", "1", "11")] == ["6"]
    assert [row["group_id"] for row in store.finance_grants()] == ["5", "6"]


def test_preview_persists_only_salted_code_hash_and_frozen_payload(ready, scenario):
    store, actor, row = ready
    before = entry(store)
    ticket = payment_preview(store, actor, row)
    saved = confirmation(store, ticket["id"])
    assert re.fullmatch(r"[0-9]{6}", ticket["code"])
    assert (
        saved["code_hash"]
        == hashlib.sha256((ticket["id"] + ticket["code"]).encode()).hexdigest()
    )
    assert "code" not in saved
    assert saved["expires"] - saved["created"] == 120
    assert saved["created"] == scenario[3][0]
    frozen = json.loads(saved["payload"])
    assert frozen["amount"] == 300 and frozen["recipient"] == "1"
    assert frozen["grant_version"] == actor.version
    assert frozen["rule_version"] == store.get_group("5")["version"]
    ticket["payload"]["items"][0]["version"] = -1
    assert json.loads(confirmation(store, ticket["id"])["payload"]) == frozen
    assert entry(store) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"boot": "different-boot"},
        {"session": "other-private-session"},
        {"elapsed": 120},
        {"elapsed": 121},
    ],
    ids=["boot", "session", "exact-ttl-boundary", "expired"],
)
def test_pending_confirmation_rejects_wrong_scope_and_expiry(ready, scenario, changes):
    store, actor, row = ready
    ticket = payment_preview(store, actor, row)
    scenario[3][0] += changes.get("elapsed", 0)
    callback = Mock()
    with pytest.raises(CashbackError, match="confirmation_invalid"):
        store.finance_apply(
            actor,
            ticket["id"],
            ticket["code"],
            changes.get("session", "session"),
            changes.get("boot", "boot"),
            callback,
        )
    callback.assert_not_called()
    assert entry(store)["state"] == "pending"
    assert confirmation(store, ticket["id"])["attempts"] == 0


def test_wrong_codes_persist_five_attempt_lockout_even_for_correct_code(ready):
    store, actor, row = ready
    ticket = payment_preview(store, actor, row)
    wrong = f"{(int(ticket['code']) + 1) % 1000000:06d}"
    callback = Mock()
    for attempt in range(1, 6):
        with pytest.raises(CashbackError, match="confirmation_invalid"):
            store.finance_apply(actor, ticket["id"], wrong, "session", "boot", callback)
        assert confirmation(store, ticket["id"])["attempts"] == attempt
    with pytest.raises(CashbackError, match="confirmation_invalid"):
        store.finance_apply(
            actor, ticket["id"], ticket["code"], "session", "boot", callback
        )
    callback.assert_not_called()
    assert confirmation(store, ticket["id"])["attempts"] == 5
    assert entry(store)["state"] == "pending"


def test_valid_code_after_one_mistake_pays_once_and_replays_receipt(ready):
    store, actor, row = ready
    ticket = payment_preview(store, actor, row)
    callback = Mock(wraps=payment_callback(store, actor, ticket["id"]))
    wrong = f"{(int(ticket['code']) + 1) % 1000000:06d}"
    with pytest.raises(CashbackError, match="confirmation_invalid"):
        store.finance_apply(actor, ticket["id"], wrong, "session", "boot", callback)
    result = store.finance_apply(
        actor, ticket["id"], ticket["code"], "session", "boot", callback
    )
    assert result["amount"] == 300 and entry(store)["state"] == "paid"
    assert entry(store)["paid_by"] == str(actor)
    before_replay = database_snapshot(store)
    assert (
        store.finance_apply(
            actor, ticket["id"], ticket["code"], "session", "boot", callback
        )
        == result
    )
    callback.assert_called_once()
    assert database_snapshot(store) == before_replay
    receipt = store.finance_result(actor, ticket["id"])
    assert receipt["status"] == "completed" and json.loads(receipt["result"]) == result
    with pytest.raises(CashbackError, match="confirmation_invalid"):
        store.finance_apply(actor, ticket["id"], wrong, "session", "boot", callback)
    assert database_snapshot(store) == before_replay


def test_confirmation_cannot_be_applied_or_read_by_another_actor(ready):
    store, actor, row = ready
    ticket = payment_preview(store, actor, row)
    store.finance_grant(
        "dashboard/test",
        grant_data(uid="2", peer="22", request_id="finance-other-0001"),
    )
    other = FinanceActor("99", "2", "22", "5", 1)
    callback = Mock()
    with pytest.raises(CashbackError, match="confirmation_invalid"):
        store.finance_apply(
            other, ticket["id"], ticket["code"], "session", "boot", callback
        )
    assert store.finance_result(other, ticket["id"]) is None
    callback.assert_not_called()
    assert confirmation(store, ticket["id"])["attempts"] == 0


def test_replacement_and_cancel_invalidate_only_exact_private_session(ready):
    store, actor, row = ready
    old = payment_preview(store, actor, row)
    other_session = payment_preview(store, actor, row, session="other")
    new = payment_preview(store, actor, row)
    assert confirmation(store, old["id"])["status"] == "cancelled"
    assert confirmation(store, other_session["id"])["status"] == "pending"
    store.finance_cancel(actor, "session")
    assert confirmation(store, new["id"])["status"] == "cancelled"
    assert confirmation(store, other_session["id"])["status"] == "pending"
    callback = Mock()
    for ticket in (old, new):
        with pytest.raises(CashbackError, match="confirmation_invalid"):
            store.finance_apply(
                actor, ticket["id"], ticket["code"], "session", "boot", callback
            )
    callback.assert_not_called()


@pytest.mark.parametrize("change", ["entry", "rule", "grant", "account"])
def test_apply_rechecks_versions_and_account_before_domain_writes(ready, change):
    store, actor, row = ready
    ticket = payment_preview(store, actor, row)
    with store.connect(True) as db:
        if change == "entry":
            db.execute("UPDATE entries SET version=version+1 WHERE id=?", (row["id"],))
        elif change == "rule":
            db.execute("UPDATE groups SET version=version+1 WHERE id='5'")
        elif change == "grant":
            db.execute("UPDATE chat_finance_grants SET version=version+1")
        else:
            db.execute("UPDATE groups SET account='100' WHERE id='5'")
    before = database_snapshot(store)
    with pytest.raises(CashbackError, match="conflict|finance_forbidden"):
        store.finance_apply(
            actor,
            ticket["id"],
            ticket["code"],
            "session",
            "boot",
            payment_callback(store, actor, ticket["id"]),
        )
    assert database_snapshot(store) == before
    assert entry(store)["state"] == "pending"


def test_chat_confirmation_cannot_bypass_domain_qualification(finance, scenario):
    store, actor = finance
    row = scan(scenario)
    ticket = payment_preview(store, actor, row)
    before = database_snapshot(store)
    with pytest.raises(CashbackError, match="qualification_required"):
        store.finance_apply(
            actor,
            ticket["id"],
            ticket["code"],
            "session",
            "boot",
            payment_callback(store, actor, ticket["id"]),
        )
    assert database_snapshot(store) == before
    assert entry(store)["state"] == "verifying"
    assert FINANCE_TRANSACTION.get() is None


def test_risk_release_rejects_changed_full_risk_snapshot(finance):
    store, actor = finance
    with store.connect(True) as db:
        db.execute(
            "INSERT INTO cashback_inviter_risk"
            "(group_id,account,inviter,warning_count,processed_event_at,"
            "active_until,blocked_at) VALUES('5','99','1',2,1990,2600,1995)"
        )
        original = dict(
            db.execute(
                "SELECT * FROM cashback_inviter_risk "
                "WHERE group_id='5' AND account='99' AND inviter='1'"
            ).fetchone()
        )
    payload = {
        "operation": "risk_release",
        "inviter": "1",
        "reason": "Offline review evidence",
    }
    ticket = store.finance_preview(actor, "session", "boot", payload)
    assert ticket["payload"]["risk_snapshot"] == original
    with store.connect(True) as db:
        db.execute(
            "UPDATE cashback_inviter_risk SET processed_event_at=processed_event_at+1,"
            "active_until=active_until+60 "
            "WHERE group_id='5' AND account='99' AND inviter='1'"
        )
    callback = Mock(
        wraps=lambda snapshot: store.release_risk(
            actor, actor.group, snapshot["inviter"], snapshot["reason"]
        )
    )
    before = database_snapshot(store)
    with pytest.raises(CashbackError, match="conflict"):
        store.finance_apply(
            actor, ticket["id"], ticket["code"], "session", "boot", callback
        )
    callback.assert_not_called()
    assert database_snapshot(store) == before
    assert confirmation(store, ticket["id"])["status"] == "pending"
    fresh = store.finance_preview(actor, "session", "boot", payload)
    store.finance_apply(actor, fresh["id"], fresh["code"], "session", "boot", callback)
    callback.assert_called_once()
    assert confirmation(store, fresh["id"])["status"] == "completed"
    with store.connect() as db:
        assert (
            db.execute(
                "SELECT blocked_at FROM cashback_inviter_risk "
                "WHERE group_id='5' AND account='99' AND inviter='1'"
            ).fetchone()[0]
            is None
        )


@pytest.mark.parametrize("failure", ["callback", "grant-recheck", "commit-guard"])
def test_domain_write_confirmation_and_audits_roll_back_together(ready, failure):
    store, actor, row = ready
    ticket = payment_preview(store, actor, row)
    before = database_snapshot(store)
    callback_ran = []
    deny_commit = [False]

    def guard():
        if deny_commit[0]:
            raise CashbackError("finance_forbidden", "Offline commit rejection", 403)

    def failing_callback(payload):
        result = payment_callback(store, actor, ticket["id"])(payload)
        assert entry(store)["state"] == "paid"
        callback_ran.append(result)
        if failure == "callback":
            raise RuntimeError("offline failure after domain write")
        if failure == "grant-recheck":
            with store.connect(True) as db:
                db.execute("UPDATE chat_finance_grants SET active=0")
        else:
            deny_commit[0] = True
        return result

    token = WRITE_GUARD.set(guard)
    try:
        with pytest.raises(
            RuntimeError if failure == "callback" else CashbackError,
            match="offline failure|finance_forbidden",
        ):
            store.finance_apply(
                actor,
                ticket["id"],
                ticket["code"],
                "session",
                "boot",
                failing_callback,
            )
    finally:
        WRITE_GUARD.reset(token)
    assert len(callback_ran) == 1
    assert FINANCE_TRANSACTION.get() is None
    assert database_snapshot(store) == before
    assert entry(store)["state"] == "pending"
    assert confirmation(store, ticket["id"])["status"] == "pending"
    result = store.finance_apply(
        actor,
        ticket["id"],
        ticket["code"],
        "session",
        "boot",
        payment_callback(store, actor, ticket["id"]),
    )
    assert result["amount"] == 300 and entry(store)["state"] == "paid"
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1
        assert (
            db.execute(
                "SELECT COUNT(*) FROM audit WHERE action='chat_finance_completed'"
            ).fetchone()[0]
            == 1
        )
