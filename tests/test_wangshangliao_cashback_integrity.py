"""Admission cycles, atomic price confirmation, and audited external payments."""

import json
from concurrent.futures import ThreadPoolExecutor
from time import time as wall_time

import pytest
from test_wangshangliao_cashback_qualification import (
    OWNER,
    entry,
    mature,
    scan,
)
from test_wangshangliao_cashback_qualification import scenario as qualification_scenario

from astrbot.builtin_stars.wangshangliao_moderation import cashback_store
from astrbot.builtin_stars.wangshangliao_moderation.cashback_store import (
    CashbackError,
    CashbackStore,
)
from astrbot.builtin_stars.wangshangliao_moderation.cashback_models import Payment

scenario = qualification_scenario


def test_populated_index_migration_analyzes_once_without_changing_ledger(
    scenario, monkeypatch
):
    store, *_ = scenario
    scan(scenario)
    tables = ("entries", "groups", "users", "evidence", "audit", "reward_rule_history")
    with store.connect(True) as db:
        before = {
            name: [
                tuple(row) for row in db.execute(f"SELECT * FROM {name} ORDER BY rowid")
            ]
            for name in tables
        }
        db.execute("DROP INDEX entry_settlement_summary")
    statements = []
    original = cashback_store.sqlite3.connect

    def traced(*args, **kwargs):
        db = original(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(cashback_store.sqlite3, "connect", traced)
    CashbackStore(store.path).close()
    assert "ANALYZE entries" in statements
    with store.connect() as db:
        assert before == {
            name: [
                tuple(row) for row in db.execute(f"SELECT * FROM {name} ORDER BY rowid")
            ]
            for name in tables
        }
        assert db.execute(
            "SELECT 1 FROM sqlite_stat1 WHERE idx='entry_settlement_summary'"
        ).fetchone()
    statements.clear()
    CashbackStore(store.path).close()
    assert not any(sql.startswith("ANALYZE") for sql in statements)


def selection(store):
    row = entry(store)
    return [{"id": row["id"], "version": row["version"]}]


def source_event(event="10", member="2", kind="INVITATION", stamp=2000, inviter="1"):
    return {
        "apply_id": event,
        "member_id": member,
        "member_nim_id": str(int(member) * 11),
        "inviter_id": inviter,
        "inviter_nim_id": str(int(inviter) * 11) if inviter else "",
        "apply_type": kind,
        "apply_time": str(stamp),
        "state": "MEMBER_STATE_LEFT" if kind == "LEAVE" else "MEMBER_STATE_GOOD",
    }


@pytest.mark.parametrize("left", [False, True])
def test_exception_requires_explicit_external_payment_and_preserves_evidence(
    scenario, left
):
    store, members, _, clock = scenario
    store.configure(
        OWNER,
        {**store.get_group("5"), "enabled": True, "require_speech": True},
        members,
        set(),
        now=1500,
    )
    scan(scenario)
    clock[0] = 2060
    store.snapshot(
        "5", "bot", "99", {"1": members["1"]} if left else members, [], now=2060
    )
    before = entry(store)
    with pytest.raises(CashbackError, match="actual_payment_confirmation_required"):
        store.mutate(
            OWNER,
            "5",
            "exception",
            selection(store),
            "exception-needs-confirmation",
            "Already paid outside the system",
        )
    result = store.mutate(
        OWNER,
        "5",
        "exception",
        selection(store),
        "exception-confirmed-payment",
        "Already paid outside the system",
        actual_payment_confirmed=True,
    )
    assert result["amount"] == 300 and entry(store)["state"] == "paid"
    assert result["status"] == "exception_registered"
    assert result["actual_payment_confirmed"] is True
    assert (
        store.list_entries(OWNER, "5", "paid")["items"][0]["payment_kind"]
        == "exception"
    )
    audit = next(row for row in store.audit(OWNER, "5") if row["action"] == "exception")
    details = json.loads(audit["after_json"])
    assert details["actual_payment_confirmed"] is True
    assert details["qualification_evidence"]["hold_until"] == before["hold_until"]
    assert details["qualification_evidence"]["first_spoke_at"] == 0
    store.mutate(
        OWNER,
        "5",
        "correct",
        selection(store),
        "revoke-exception-registration",
        "Reverse the registration only",
    )
    assert entry(store)["state"] == ("void" if left else "verifying")
    assert (
        store.list_entries(OWNER, "5", entry(store)["state"])["items"][0][
            "payment_kind"
        ]
        == ""
    )


def test_exception_cannot_waive_recipient_identity(scenario):
    store, members, _, _ = scenario
    scan(scenario)
    members["1"]["peer"] = "33"
    scan(scenario, 2060)
    with pytest.raises(CashbackError, match="identity_conflict"):
        store.mutate(
            OWNER,
            "5",
            "exception",
            selection(store),
            "exception-identity-conflict",
            "Externally paid but conflicting identity",
            actual_payment_confirmed=True,
        )
    assert entry(store)["paid_at"] is None


@pytest.mark.parametrize("pagination", ["entries", "inviters"])
def test_listing_reads_latest_cycle_and_payment_independently(scenario, pagination):
    store, *_ = scenario
    row = scan(scenario)
    store.mutate(
        OWNER,
        "5",
        "exception",
        selection(store),
        "latest-payment-evidence",
        "Already paid outside the system",
        actual_payment_confirmed=True,
    )
    with store.connect(True) as db:
        for index in range(100):
            store._audit(
                db,
                "5",
                row["id"],
                OWNER,
                "qualification_restarted",
                {},
                {"reason": "manual_confirmation" if index == 99 else "observation_gap"},
            )
            store._audit(db, "5", row["id"], OWNER, "unrelated_diagnostic", {}, {})
        statements = []
    reader = CashbackStore(store.path, read_only=True, reuse_reads=True)
    try:
        with reader.connect() as db:
            db.set_trace_callback(statements.append)
        result = reader.list_entries(OWNER, "5", "paid", pagination=pagination)
        item = result["items"][0]
        assert item["payment_kind"] == "exception"
        assert item["qualification_reason"] == "manual_confirmation"
        query = next(q for q in statements if q.startswith("WITH latest AS"))
        with reader.connect() as db:
            db.set_trace_callback(None)
            latest = db.execute(query).fetchall()
        assert len(latest) == 2
        assert {r["action"] for r in latest} == {
            "exception",
            "qualification_restarted",
        }
    finally:
        reader.close()


@pytest.mark.parametrize("reuse_reads", [False, True])
def test_payment_kind_uses_settlement_timestamp_not_audit_clock(
    scenario, monkeypatch, reuse_reads
):
    store, members, proofs, _ = scenario
    store.configure(
        OWNER,
        {**store.get_group("5"), "enabled": True, "qualification_seconds": 0},
        {"1": members["1"]},
        set(),
        now=1500,
    )
    monkeypatch.setattr("time.time", wall_time)
    store.snapshot("5", "bot", "99", members, proofs)
    reader = CashbackStore(store.path, read_only=True, reuse_reads=reuse_reads)
    try:
        for index, (operation, expected_kind) in enumerate(
            (
                ("exception", "exception"),
                ("pay", "pay"),
                ("exception", "exception"),
            )
        ):
            result = store.mutate(
                OWNER,
                "5",
                operation,
                selection(store),
                f"payment-kind-{operation}-{index}",
                "Externally paid and confirmed",
                actual_payment_confirmed=operation == "exception",
            )
            assert result["status"] == (
                "exception_registered" if operation == "exception" else "registered"
            )
            audit = next(
                row for row in store.audit(OWNER, "5") if row["action"] == operation
            )
            assert audit["created"] != entry(store)["paid_at"]
            assert json.loads(audit["after_json"])["paid_at"] == entry(store)["paid_at"]
            for pagination in ("entries", "inviters"):
                assert (
                    reader.list_entries(OWNER, "5", "paid", pagination=pagination)[
                        "items"
                    ][0]["payment_kind"]
                    == expected_kind
                )
            store.mutate(
                OWNER,
                "5",
                "correct",
                selection(store),
                f"payment-kind-withdraw-{index}",
                "Reverse registration only",
            )
            assert (
                reader.list_entries(OWNER, "5", "pending")["items"][0]["payment_kind"]
                == ""
            )
        reopened = CashbackStore(store.path, read_only=True, reuse_reads=reuse_reads)
        try:
            store.mutate(
                OWNER,
                "5",
                "exception",
                selection(store),
                "payment-kind-reopened-exception",
                "Externally paid and confirmed",
                actual_payment_confirmed=True,
            )
            assert (
                reopened.list_entries(OWNER, "5", "paid")["items"][0]["payment_kind"]
                == "exception"
            )
        finally:
            reopened.close()
    finally:
        reader.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("paid_at", 999),
        ("paid_by", "different@example.com"),
        ("state", "pending"),
        ("actor", "other@example.com"),
    ],
)
def test_exception_badge_requires_exact_current_payment_evidence(
    scenario, field, value
):
    store, *_ = scenario
    scan(scenario)
    store.mutate(
        OWNER,
        "5",
        "exception",
        selection(store),
        "payment-kind-exact-evidence",
        "Externally paid and confirmed",
        actual_payment_confirmed=True,
    )
    with store.connect(True) as db:
        audit = db.execute(
            "SELECT id,after_json FROM audit WHERE action='exception' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if field == "actor":
            db.execute("UPDATE audit SET actor=? WHERE id=?", (value, audit["id"]))
        else:
            details = json.loads(audit["after_json"])
            details[field] = value
            db.execute(
                "UPDATE audit SET after_json=? WHERE id=?",
                (json.dumps(details), audit["id"]),
            )
    assert store.list_entries(OWNER, "5", "paid")["items"][0]["payment_kind"] == "pay"


def test_missing_inviter_isolated_without_poisoning_page_or_existing_debt(scenario):
    store, *_ = scenario
    mature(scenario)
    store.remember_source_page(
        "5",
        "bot",
        "99",
        1000,
        {
            "items": [
                source_event(event="11", member="3", inviter=""),
                source_event(event="12", member="4"),
            ],
            "last_id": "12800",
        },
    )
    assert store.get_group("5")["source_error"] == ""
    assert len(store.source_records("5", "bot", "99", 1000)) == 2
    with store.connect() as db:
        assert (
            db.execute("SELECT reason FROM cashback_source_quarantine").fetchone()[0]
            == "missing_inviter"
        )
    assert (
        store.mutate(
            OWNER, "5", "pay", selection(store), "unrelated-debt-remains-payable"
        )["amount"]
        == 300
    )


@pytest.mark.parametrize("kind", ["APPLY", "INVITATION"])
def test_new_admission_cannot_reuse_old_invitation(scenario, kind):
    store, _, proofs, _ = scenario
    proofs[0]["event_id"] = "10"
    scan(scenario)
    before = entry(store)
    store.remember_source_page(
        "5",
        "bot",
        "99",
        1000,
        {
            "items": [
                source_event(),
                source_event(event="11", kind="LEAVE", stamp=2040, inviter=""),
                source_event(
                    event="12",
                    kind=kind,
                    stamp=2050,
                    inviter="" if kind == "APPLY" else "1",
                ),
            ],
            "last_id": "2000",
        },
    )
    for stamp in range(2060, 12801, 60):
        scan(scenario, stamp)
    after = entry(store)
    assert (after["amount"], after["inviter"]) == (before["amount"], before["inviter"])
    for operation in ("pay", "exception"):
        with pytest.raises(CashbackError, match="not_payable"):
            store.mutate(
                OWNER,
                "5",
                operation,
                selection(store),
                f"new-admission-{operation}-rejected",
                "Old cycle cannot establish new attribution",
                actual_payment_confirmed=True,
            )
    assert entry(store)["paid_at"] is None


def test_reenable_uses_new_baseline_without_stranding_old_debt(scenario):
    store, members, _, clock = scenario
    mature(scenario)
    original = entry(store)
    for stamp, enabled in ((12801, False), (12802, True)):
        clock[0] = stamp
        store.configure(
            OWNER,
            {**store.get_group("5"), "enabled": enabled},
            members,
            set(),
            now=stamp,
        )
    assert store.get_group("5")["started"] == 1000
    assert store.get_group("5")["baseline_started"] == 12802
    scan(scenario, 12803)
    assert entry(store)["evidence_started"] == original["evidence_started"]
    assert (
        store.mutate(OWNER, "5", "pay", selection(store), "old-debt-after-reenable")[
            "amount"
        ]
        == 300
    )


def test_price_preview_binds_rule_versions_targets_and_idempotency(scenario):
    store, members, _, clock = scenario
    scan(scenario)
    items = selection(store)
    preview = store.price_preview(OWNER, "5", items, "agent")
    assert preview["items"][0]["original_amount"] == 300
    assert preview["items"][0]["target_amount"] == 100
    with pytest.raises(CashbackError, match="price_confirmation_required"):
        store.mutate(
            OWNER,
            "5",
            "reprice",
            items,
            "old-client-must-refresh",
            "Proof checked",
            "agent",
        )
    clock[0] = 2001
    store.configure(
        OWNER,
        {**store.get_group("5"), "enabled": True, "agent_rate": 500},
        members,
        set(),
        now=2001,
    )
    assert selection(store) == items
    with pytest.raises(CashbackError, match="price_confirmation_changed"):
        store.mutate(
            OWNER,
            "5",
            "reprice",
            items,
            "stale-preview-rejected",
            "Proof checked",
            "agent",
            price_confirmation=preview,
        )
    assert entry(store)["amount"] == 300
    fresh = store.price_preview(OWNER, "5", items, "agent")
    kwargs = {"price_confirmation": fresh}
    result = store.mutate(
        OWNER,
        "5",
        "reprice",
        items,
        "fresh-preview-idempotent",
        "Proof checked",
        "agent",
        **kwargs,
    )
    assert result["amount"] == 500
    assert (
        store.mutate(
            OWNER,
            "5",
            "reprice",
            items,
            "fresh-preview-idempotent",
            "Proof checked",
            "agent",
            **kwargs,
        )
        == result
    )
    fresh["items"][0]["target_amount"] = 600
    with pytest.raises(CashbackError, match="idempotency_conflict"):
        store.prior_request(
            OWNER,
            "5",
            "reprice",
            items,
            "fresh-preview-idempotent",
            "Proof checked",
            "agent",
            price_confirmation=fresh,
        )


def test_two_connections_cannot_register_payment_twice(scenario):
    store, *_ = scenario
    mature(scenario)
    items = selection(store)
    other = CashbackStore(store.path)

    def pay(index):
        try:
            return (store if index == 0 else other).mutate(
                OWNER, "5", "pay", items, f"concurrent-payment-request-{index}"
            )["amount"]
        except CashbackError as exc:
            return exc.code

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(pay, (0, 1)))
        assert sorted(results, key=str) == [300, "conflict"]
        with store.connect() as db:
            assert (
                db.execute("SELECT COUNT(*) FROM audit WHERE action='pay'").fetchone()[
                    0
                ]
                == 1
            )
    finally:
        other.close()


def test_additive_migration_never_rewrites_historical_financial_fields(scenario):
    store, *_ = scenario
    scan(scenario)
    metadata = {
        "evidence_started",
        "evidence_event_id",
        "evidence_event_at",
        "integrity_version",
    }
    with store.connect(True) as db:
        db.execute(
            "UPDATE entries SET amount=777,member_class='agent',"
            "member_avatar_status='detected' WHERE member='2'"
        )
        db.execute(
            "UPDATE groups SET started=12802,source_cursor='12800',source_scanned=12801"
        )
        before = [
            {key: row[key] for key in row.keys() if key not in metadata}
            for row in db.execute("SELECT * FROM entries ORDER BY id")
        ]
        group_before = {
            key: value
            for key, value in dict(
                db.execute("SELECT * FROM groups").fetchone()
            ).items()
            if key not in {"baseline_started", "source_cursor", "source_scanned"}
        }
        db.execute("DROP VIEW cashback_admission_checks")
        db.execute("ALTER TABLE groups DROP COLUMN baseline_started")
        for field in metadata:
            db.execute(f"ALTER TABLE entries DROP COLUMN {field}")
    store.close()
    migrated = CashbackStore(store.path)
    try:
        with migrated.connect() as db:
            after = [
                {key: row[key] for key in row.keys() if key not in metadata}
                for row in db.execute("SELECT * FROM entries ORDER BY id")
            ]
            assert after == before
            assert {
                key: value
                for key, value in dict(
                    db.execute("SELECT * FROM groups").fetchone()
                ).items()
                if key not in {"baseline_started", "source_cursor", "source_scanned"}
            } == group_before
            assert (
                db.execute(
                    "SELECT evidence_started FROM entries WHERE member='2'"
                ).fetchone()[0]
                == 2000
            )
            assert (
                db.execute("SELECT SUM(integrity_version) FROM entries").fetchone()[0]
                == 0
            )
        checkpoint = migrated.source_checkpoint("5", "bot", "99", 12802)
        assert checkpoint["baseline_started"] == 12802
        assert checkpoint["evidence_floor"] == 2000
        assert checkpoint["source_cursor"] == ""
        assert checkpoint["source_scanned"] == 0
        reopened = CashbackStore(store.path)
        reopened.close()
        assert entry(migrated)["amount"] == 777
    finally:
        migrated.close()


def test_source_name_updates_do_not_create_ambiguous_membership(scenario):
    store, _, proofs, _ = scenario
    proofs[0]["event_id"] = "10"
    scan(scenario)
    original = source_event()
    store.remember_source_page(
        "5",
        "bot",
        "99",
        1000,
        {
            "items": [original, {**original, "member_name": "Updated name"}],
            "last_id": "2000",
        },
    )
    item = store.list_entries(OWNER, "5", "verifying")["items"][0]
    assert item["review_reason"] == ""
    with store.connect() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM cashback_membership_events").fetchone()[0]
            == 1
        )


def test_batched_integrity_uses_one_snapshot_and_next_read_sees_new_admission(
    scenario, monkeypatch
):
    store, _, proofs, clock = scenario
    proofs[0]["event_id"] = "10"
    scan(scenario)
    clock[0] = 2060
    store.remember_source_page(
        "5", "bot", "99", 1000, {"items": [source_event()], "last_id": "2000"}
    )
    original_integrity = store._entry_integrity
    changed = False

    def concurrent_admission(db, rule, row, **kwargs):
        nonlocal changed
        assert kwargs.get("admission") is not None
        if not changed:
            changed = True
            store.remember_source_page(
                "5",
                "bot",
                "99",
                1000,
                {
                    "items": [
                        source_event(event="11", kind="APPLY", stamp=2050, inviter="")
                    ],
                    "last_id": "2060",
                },
            )
        return original_integrity(db, rule, row, **kwargs)

    monkeypatch.setattr(store, "_entry_integrity", concurrent_admission)
    before = store.list_entries(OWNER, "5", "verifying")["items"][0]
    assert before["review_reason"] == ""
    after = store.list_entries(OWNER, "5", "verifying")["items"][0]
    assert after["review_reason"] == "admission_superseded"
    for field in ("id", "amount", "inviter", "state", "paid_at", "version"):
        assert before[field] == after[field]


def test_unattributed_same_member_cannot_reuse_original_recipient(scenario):
    store, _, proofs, _ = scenario
    proofs[0]["event_id"] = "10"
    scan(scenario)
    store.remember_source_page(
        "5",
        "bot",
        "99",
        1000,
        {
            "items": [source_event(event="12", stamp=2050, inviter="")],
            "last_id": "2000",
        },
    )
    with pytest.raises(CashbackError, match="not_payable"):
        store.mutate(
            OWNER,
            "5",
            "exception",
            selection(store),
            "missing-source-same-member",
            "External payment cannot waive attribution",
            actual_payment_confirmed=True,
        )


def test_actual_payment_confirmation_is_strict_boolean():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Payment.model_validate(
            {
                "group": "5",
                "operation": "exception",
                "items": [{"id": "a" * 32, "version": 1}],
                "request_id": "strict-boolean-confirmation",
                "reason": "External payment",
                "actual_payment_confirmed": "true",
            }
        )
