"""Read-only native/cash acceptance must not mistake partial evidence for a pass."""

import json
import sqlite3
import time
from contextlib import closing

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.cashback_store import (
    REQUEST_TRACE,
    CashbackStore,
)
from scripts.check_wsl_invitation_history import check_history


@pytest.fixture
def history(tmp_path):
    store = CashbackStore(tmp_path / "cash.sqlite3")
    owner = "owner@example.com"
    store.bootstrap([{"email": owner, "name": "Owner"}])
    now = time.time()
    members = {
        "1": {"name": "Inviter", "peer": "11", "eligible": True},
        "3": {"name": "Approver", "peer": "33", "eligible": True},
    }
    store.configure(
        owner,
        {
            "id": "5",
            "name": "Group",
            "bot_id": "fixture",
            "account": "99",
            "contact": "Contact",
            "enabled": True,
            "rate": 500,
            "version": 0,
        },
        members,
        set(),
        now=now - 20,
    )
    members["2"] = {"name": "Member", "peer": "22", "eligible": True}
    evidence = [
        {
            "member": "2",
            "inviter": "1",
            "source": "business_apply_log",
            "event_id": "m",
            "event_at": now - 10,
        }
    ]
    evidence.append({**evidence[0], "source": "nim_notification"})
    store.snapshot("5", "fixture", "99", members, evidence, now=now - 5)
    inbox = tmp_path / "messages.sqlite3"
    payload = {
        "kind": "team_invite",
        "inviter_peer": "11",
        "member_peers": ["22"],
        "event_at": int((now - 10) * 1000),
    }
    with closing(sqlite3.connect(inbox)) as db:
        db.execute(
            "CREATE TABLE inbox(account TEXT,team TEXT,message TEXT,state TEXT,payload TEXT)"
        )
        db.execute(
            "INSERT INTO inbox VALUES('99','5','m','invite_notification',?)",
            (json.dumps(payload),),
        )
        db.commit()
    return store, inbox, members, evidence, now


def test_live_join_does_not_claim_departure_rejoin_or_payment(history):
    store, inbox, _, _, _ = history
    result = check_history(store.path, [inbox], "5")
    assert result["verified_native_invitations"] == 1
    assert result["pending_count"] == 1 and result["pending_cents"] == 500
    assert result["registered_count"] == 0
    assert result["remaining"] == [
        "live_departure_voiding",
        "live_rejoin_no_second_reward",
        "access_already_settled_registration",
    ]


def test_join_operator_differing_from_business_inviter_is_not_a_conflict(history):
    store, inbox, _, _, _ = history
    with store.connect(True) as db:
        db.execute(
            "UPDATE evidence SET inviter='3',inviter_peer='33' WHERE source='nim_notification'"
        )
    with closing(sqlite3.connect(inbox)) as db:
        payload = json.loads(db.execute("SELECT payload FROM inbox").fetchone()[0])
        payload["inviter_peer"] = "33"
        db.execute("UPDATE inbox SET payload=?", (json.dumps(payload),))
        db.commit()
    result = check_history(store.path, [inbox], "5")
    assert result["verified_native_invitations"] == 1
    assert result["verified_business_invitations"] == 1


def test_native_only_history_does_not_prove_the_recruiter(history):
    store, inbox, _, _, _ = history
    with store.connect(True) as db:
        db.execute("DELETE FROM evidence WHERE source='business_apply_log'")
    result = check_history(store.path, [inbox], "5")
    assert result["verified_native_invitations"] == 1
    assert result["verified_business_invitations"] == 0
    assert "business_invitation_attribution" in result["remaining"]


@pytest.fixture
def corrected_history(history):
    store, *_ = history
    token = REQUEST_TRACE.set("a" * 32)
    try:
        with store.connect(True) as db:
            before = dict(
                db.execute("SELECT * FROM entries WHERE member='2'").fetchone()
            )
            db.execute(
                "UPDATE entries SET amount=300,version=version+1 WHERE id=?",
                (before["id"],),
            )
            fields = ("member", "amount", "state", "paid_at", "paid_by", "version")
            old = {field: before[field] for field in fields}
            store._audit(
                db,
                "5",
                before["id"],
                "system",
                "amount_corrected",
                old,
                {**old, "amount": 300, "version": old["version"] + 1},
                "Explicit user-requested amount correction; no payment registered.",
            )
    finally:
        REQUEST_TRACE.reset(token)
    return history


def test_audited_correction_keeps_original_native_attribution(corrected_history):
    store, inbox, *_ = corrected_history
    result = check_history(store.path, [inbox], "5")
    assert result["verified_native_invitations"] == 1
    assert result["audited_amount_corrections"] == 1
    assert result["pending_count"] == 1 and result["pending_cents"] == 300
    assert result["registered_count"] == 0


def test_amount_correction_survives_repair_of_the_native_join_operator(
    corrected_history,
):
    store, inbox, members, evidence, now = corrected_history
    with store.connect(True) as db:
        db.execute("DELETE FROM evidence WHERE source='business_apply_log'")
        original = db.execute(
            "SELECT id,after_json FROM audit WHERE action='invitation_verified'"
        ).fetchone()
        detail = json.loads(original["after_json"])
        detail["source"] = "nim_notification"
        db.execute(
            "UPDATE audit SET after_json=? WHERE id=?",
            (json.dumps(detail), original["id"]),
        )
    members["3"] = {"name": "Recruiter", "peer": "33", "eligible": True}
    store.snapshot(
        "5",
        "fixture",
        "99",
        members,
        [{**evidence[0], "inviter": "3", "event_id": "approved-recruiter"}],
        now=now,
    )
    result = check_history(store.path, [inbox], "5")
    assert result["verified_business_invitations"] == 1
    assert result["audited_amount_corrections"] == 1
    assert result["pending_cents"] == 300


def test_unaudited_amount_change_rejects(history):
    store, inbox, *_ = history
    with store.connect(True) as db:
        db.execute("UPDATE entries SET amount=300 WHERE member='2'")
    with pytest.raises(AssertionError, match="Stored amount differs"):
        check_history(store.path, [inbox], "5")


@pytest.mark.parametrize(
    "side,field,value",
    [
        ("before", "amount", 400),
        ("before", "member", "other"),
        ("after", "amount", 250),
        ("after", "state", "paid"),
        ("after", "paid_by", "other"),
        ("after", "version", 2),
    ],
)
def test_broken_correction_chain_rejects(corrected_history, side, field, value):
    store, inbox, *_ = corrected_history
    with store.connect(True) as db:
        row = db.execute(
            "SELECT id,before_json,after_json FROM audit WHERE action='amount_corrected'"
        ).fetchone()
        snapshot = json.loads(row[f"{side}_json"])
        snapshot[field] = value
        db.execute(
            f"UPDATE audit SET {side}_json=? WHERE id=?",
            (json.dumps(snapshot), row["id"]),
        )
    with pytest.raises(AssertionError):
        check_history(store.path, [inbox], "5")


@pytest.mark.parametrize(
    "field,value",
    [("reason", ""), ("trace", ""), ("actor", "untrusted"), ("created", 0)],
)
def test_unattributable_correction_rejects(corrected_history, field, value):
    store, inbox, *_ = corrected_history
    with store.connect(True) as db:
        db.execute(
            f"UPDATE audit SET {field}=? WHERE action='amount_corrected'", (value,)
        )
    with pytest.raises(AssertionError):
        check_history(store.path, [inbox], "5")


def test_departure_does_not_claim_rejoin(history):
    store, inbox, members, _, now = history
    store.snapshot("5", "fixture", "99", {"1": members["1"]}, [], now=now)
    result = check_history(store.path, [inbox], "5")
    assert result["departures_voided"] == 1
    assert result["rejoins_still_void"] == 0
    assert "live_rejoin_no_second_reward" in result["remaining"]


def test_rejoin_requires_second_exact_native_notification_and_keeps_void(history):
    store, inbox, members, evidence, now = history
    store.snapshot("5", "fixture", "99", {"1": members["1"]}, [], now=now)
    rejoin = {**evidence[1], "event_id": "rejoin", "event_at": now + 1}
    store.snapshot("5", "fixture", "99", members, [rejoin], now=now + 2)
    assert check_history(store.path, [inbox], "5")["rejoins_still_void"] == 0
    with closing(sqlite3.connect(inbox)) as db:
        db.execute(
            "INSERT INTO inbox VALUES('99','5','rejoin','invite_notification',?)",
            (
                json.dumps(
                    {
                        "kind": "team_invite",
                        "inviter_peer": "11",
                        "member_peers": ["22"],
                        "event_at": int((now + 1) * 1000),
                    }
                ),
            ),
        )
        db.commit()
    result = check_history(store.path, [inbox], "5")
    assert result["rejoins_still_void"] == 1
    assert result["pending_count"] == 0


def test_rejoin_after_rate_change_keeps_original_void_amount(history):
    store, inbox, members, evidence, now = history
    with store.connect() as db:
        current = dict(db.execute("SELECT * FROM groups WHERE id='5'").fetchone())
    store.configure(
        "owner@example.com",
        {**current, "enabled": bool(current["enabled"]), "rate": 300},
        members,
        set(),
        now=now,
    )
    store.snapshot("5", "fixture", "99", {"1": members["1"]}, [], now=now + 1)
    store.snapshot(
        "5",
        "fixture",
        "99",
        members,
        [{**evidence[1], "event_id": "rejoin", "event_at": now + 2}],
        now=now + 3,
    )
    with closing(sqlite3.connect(inbox)) as db:
        db.execute(
            "INSERT INTO inbox VALUES('99','5','rejoin','invite_notification',?)",
            (
                json.dumps(
                    {
                        "kind": "team_invite",
                        "inviter_peer": "11",
                        "member_peers": ["22"],
                        "event_at": int((now + 2) * 1000),
                    }
                ),
            ),
        )
        db.commit()
    result = check_history(store.path, [inbox], "5")
    assert result["rejoins_still_void"] == 1
    assert result["pending_cents"] == 0
    with store.connect() as db:
        assert (
            db.execute("SELECT amount FROM entries WHERE member='2'").fetchone()[0]
            == 500
        )


def test_existing_paid_row_is_not_access_browser_acceptance(history):
    store, inbox, _, _, _ = history
    with store.connect() as db:
        row = db.execute("SELECT id,version FROM entries WHERE member='2'").fetchone()
    store.mutate(
        "owner@example.com",
        "5",
        "pay",
        [{"id": row["id"], "version": row["version"]}],
        "isolated-payment-fixture",
    )
    result = check_history(store.path, [inbox], "5")
    assert result["registered_count"] == 1
    assert "access_already_settled_registration" in result["remaining"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("kind", "group_message"),
        ("inviter_peer", "33"),
        ("member_peers", ["44"]),
        ("event_at", None),
        ("event_at", "1790000000"),
    ],
)
def test_mismatched_or_missing_native_identity_rejects(history, field, value):
    store, inbox, _, _, _ = history
    with closing(sqlite3.connect(inbox)) as db:
        payload = json.loads(db.execute("SELECT payload FROM inbox").fetchone()[0])
        payload[field] = value
        db.execute("UPDATE inbox SET payload=?", (json.dumps(payload),))
        db.commit()
    with pytest.raises(AssertionError):
        check_history(store.path, [inbox], "5")


@pytest.mark.parametrize("field,value", [("account", "other"), ("team", "6")])
def test_other_account_or_group_not_used_as_proof(history, field, value):
    store, inbox, _, _, _ = history
    with closing(sqlite3.connect(inbox)) as db:
        db.execute(f"UPDATE inbox SET {field}=?", (value,))
        db.commit()
    result = check_history(store.path, [inbox], "5")
    assert result["verified_native_invitations"] == 0


def test_check_preserves_every_business_table(history):
    store, inbox, _, _, _ = history
    with store.connect() as db:
        tables = [
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name!='sqlite_sequence'"
            )
        ]
        before = {
            name: db.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()
            for name in tables
        }
    check_history(store.path, [inbox], "5")
    with store.connect() as db:
        after = {
            name: db.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()
            for name in tables
        }
    assert before == after
