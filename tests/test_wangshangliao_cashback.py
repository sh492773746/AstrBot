"""Cash ledger invariants and independently authenticated cashier API."""

import asyncio
import json
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.cashback import CashbackService
from astrbot.builtin_stars.wangshangliao_moderation.cashback_store import (
    CashbackError,
    CashbackStore,
    entry_time_window,
)
from astrbot.builtin_stars.wangshangliao_moderation.migration import (
    detach_unverified_paid_inviters,
    migrate_cashback_collector,
    recover_cashback_migration_entries,
)
from astrbot.core.platform.sources.wangshangliao.storage import Ledger

OWNER = "owner@example.com"
CASHIER = "cashier@example.com"
OTHER = "other@example.com"


def member(uid):
    return {"name": f"成员{uid}", "peer": str(1000 + int(uid)), "eligible": True}


def evidence(uid="2", inviter="1", event="event-1", source="business_apply_log"):
    return {"member": uid, "inviter": inviter, "event_id": event, "source": source}


def row(store, uid="2", group="5"):
    with store.connect() as db:
        return dict(
            db.execute(
                "SELECT * FROM entries WHERE group_id=? AND member=?", (group, uid)
            ).fetchone()
        )


def select(store, uid="2", group="5"):
    r = row(store, uid, group)
    return {"id": r["id"], "version": r["version"]}


def risk_proof(uid, event_at, name, inviter="1"):
    return {
        "member": uid,
        "inviter": inviter,
        "event_id": f"risk-{uid}",
        "source": "business_apply_log",
        "event_at": event_at,
        "member_peer": member(uid)["peer"],
        "inviter_peer": member(inviter)["peer"],
        "member_name": name,
    }


@pytest.fixture
def store(tmp_path):
    store = CashbackStore(tmp_path / "cash.sqlite3")
    store.bootstrap([{"email": OWNER, "name": "管理员"}])
    for address in (CASHIER, OTHER):
        store.save_user(OWNER, address, address.split("@")[0], True, 0)
    store.configure(
        OWNER,
        {
            "id": "5",
            "name": "测试群",
            "bot_id": "bot-a",
            "account": "99",
            "cashier": CASHIER,
            "enabled": True,
            "rate": 1800,
            "version": 0,
        },
        {"1": member("1")},
        {"7"},
    )
    return store


def scan(store, members=("1", "2"), notices=None, bot="bot-a", account="99"):
    store.snapshot(
        "5",
        bot,
        account,
        {uid: member(uid) for uid in members},
        [evidence()] if notices is None else notices,
    )


def pay(
    store,
    actor=CASHIER,
    request="payment-request-001",
    items=None,
    operation="pay",
    reason="",
):
    return store.mutate(
        actor,
        "5",
        operation,
        items or [select(store)],
        request,
        reason,
        actual_payment_confirmed=operation == "exception",
    )


def replacement_roster(account="100", bot="bot-b"):
    return {
        "bot_id": bot,
        "account": account,
        "group": "5",
        "complete": True,
        "observed": time.time(),
        "members": {uid: member(uid) for uid in ("1", "2", "3", "4", account)},
    }


def test_collector_transfer_preserves_ledger_proof_and_frozen_prices(store):
    scan(store, ("1", "2", "3"), [evidence(), evidence("3", event="event-3")])
    pay(store, items=[select(store, "3")], request="payment-request-003")
    before = {uid: row(store, uid) for uid in ("1", "2", "3", "7")}
    with store.connect() as db:
        rule = dict(db.execute("SELECT * FROM groups WHERE id='5'").fetchone())
        proofs = [
            tuple(r) for r in db.execute("SELECT * FROM evidence ORDER BY member")
        ]
    result = migrate_cashback_collector(
        store, OWNER, "5", "bot-a", "99", rule["version"], replacement_roster()
    )
    assert result["baseline_added"] == 2
    assert {uid: row(store, uid) for uid in before} == before
    with store.connect() as db:
        assert [
            tuple(r) for r in db.execute("SELECT * FROM evidence ORDER BY member")
        ] == proofs
        migrated = dict(db.execute("SELECT * FROM groups WHERE id='5'").fetchone())
    for field in ("started", "rate", "rate_since", "cashier", "qualification_seconds"):
        assert migrated[field] == rule[field]
    assert migrated["scanned"] == 0
    assert migrated["error"] == "migration_verification_required"
    assert store.list_entries(CASHIER, "5")["total"] == 1
    assert (
        store.private_invitation_summaries("1", "bot-b", "100", ["5"])[0]["paid"]
        == 1800
    )
    assert store.private_invitation_summaries("1", "bot-a", "99", ["5"]) == []
    scan(store, ("1", "2", "3", "4", "100"), [], bot="bot-b", account="100")
    assert row(store)["verification"] == "verified"
    assert row(store, "4")["eligible"] == 0
    assert row(store, "3")["paid_at"] == before["3"]["paid_at"]
    with store.connect() as db:
        policy = store._entry_price_policy(db, migrated, row(store))
    assert policy["bot_id"] == "bot-a"
    assert policy["rate"] == 1800
    pay(store)
    assert row(store)["state"] == "paid"
    assert any(r["action"] == "collector_migrated" for r in store.audit(OWNER, "5"))


@pytest.mark.parametrize(
    "bad", ["partial", "stale", "missing_target", "peer", "foreign_group"]
)
def test_collector_transfer_rejects_unverified_roster_atomically(store, bad):
    scan(store)
    roster = replacement_roster()
    if bad == "partial":
        roster["complete"] = False
    elif bad == "stale":
        roster["observed"] -= 301
    elif bad == "missing_target":
        roster["members"].pop("100")
    elif bad == "peer":
        roster["members"]["2"]["peer"] = "changed-peer"
    else:
        roster["group"] = "6"
    before = row(store)
    with pytest.raises(CashbackError, match="migration_"):
        migrate_cashback_collector(store, OWNER, "5", "bot-a", "99", 1, roster)
    assert row(store) == before
    with store.connect() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM collector_migrations").fetchone()[0] == 0
        )
        assert (
            db.execute("SELECT bot_id FROM groups WHERE id='5'").fetchone()[0]
            == "bot-a"
        )


def test_collector_transfer_requires_admin_and_expected_binding(store):
    scan(store)
    with pytest.raises(CashbackError):
        migrate_cashback_collector(
            store, CASHIER, "5", "bot-a", "99", 1, replacement_roster()
        )
    for source, version in (("bot-wrong", 1), ("bot-a", 99)):
        with pytest.raises(CashbackError, match="migration_conflict"):
            migrate_cashback_collector(
                store, OWNER, "5", source, "99", version, replacement_roster()
            )
    migrate_cashback_collector(
        store, OWNER, "5", "bot-a", "99", 1, replacement_roster()
    )
    with pytest.raises(CashbackError, match="migration_conflict"):
        migrate_cashback_collector(
            store, OWNER, "5", "bot-a", "99", 1, replacement_roster()
        )


@pytest.mark.parametrize(
    "field,value",
    [("account", "98"), ("account", ""), ("observed", 1e12), ("event_at", 1e12)],
)
def test_collector_transfer_does_not_authorize_foreign_or_future_proof(
    store, field, value
):
    scan(store)
    with store.connect(True) as db:
        db.execute(f"UPDATE evidence SET {field}=?", (value,))
    migrate_cashback_collector(
        store, OWNER, "5", "bot-a", "99", 1, replacement_roster()
    )
    assert store.list_entries(CASHIER, "5")["total"] == 0
    assert (
        store.private_invitation_summaries("1", "bot-b", "100", ["5"])[0]["total"] == 0
    )
    scan(store, ("1", "2", "3", "4", "100"), [], bot="bot-b", account="100")
    with pytest.raises(CashbackError, match="qualification_required|not_payable"):
        pay(store)


def test_collector_transfer_chain_keeps_original_cutoff(store):
    scan(store)
    migrate_cashback_collector(
        store, OWNER, "5", "bot-a", "99", 1, replacement_roster()
    )
    with store.connect() as db:
        cutoff = db.execute("SELECT cutoff FROM collector_migrations").fetchone()[0]
    migrate_cashback_collector(
        store, OWNER, "5", "bot-b", "100", 2, replacement_roster("101", "bot-c")
    )
    with store.connect() as db:
        inherited = db.execute(
            "SELECT cutoff FROM collector_migrations WHERE source_account='99' AND target_account='101'"
        ).fetchone()[0]
        assert inherited == cutoff
    scan(store, ("1", "2", "3", "4", "101"), [], bot="bot-c", account="101")
    pay(store)
    assert row(store)["state"] == "paid"


def test_account_reconfiguration_without_transfer_cannot_inherit_proof(store):
    scan(store)
    store.configure(
        OWNER,
        {
            "id": "5",
            "name": "测试群",
            "bot_id": "bot-b",
            "account": "100",
            "cashier": CASHIER,
            "enabled": True,
            "rate": 1800,
            "version": 1,
        },
        replacement_roster()["members"],
        set(),
    )
    assert store.list_entries(CASHIER, "5")["total"] == 0
    assert (
        store.private_invitation_summaries("1", "bot-b", "100", ["5"])[0]["total"] == 0
    )
    with pytest.raises(CashbackError, match="not_payable"):
        pay(store)


def test_collector_transfer_and_restart_preserve_cashier_agent_price(store):
    scan(store)
    store.configure(
        OWNER,
        {
            "id": "5",
            "name": "测试群",
            "bot_id": "bot-a",
            "account": "99",
            "cashier": CASHIER,
            "enabled": True,
            "rate": 2400,
            "agent_rate": 3000,
            "version": 1,
        },
        replacement_roster()["members"],
        set(),
    )
    store.mutate(
        CASHIER,
        "5",
        "reprice",
        [select(store)],
        "agent-before-transfer-1",
        "已核对代理商证明",
        category="agent",
        price_confirmation=store.price_preview(CASHIER, "5", [select(store)], "agent"),
    )
    migrate_cashback_collector(
        store, OWNER, "5", "bot-a", "99", 2, replacement_roster()
    )
    store = CashbackStore(store.path)
    assert row(store)["amount"] == 3000
    assert row(store)["member_class"] == "agent"
    scan(store, ("1", "2", "3", "4", "100"), [], bot="bot-b", account="100")
    assert row(store)["amount"] == 3000
    store.mutate(
        CASHIER,
        "5",
        "reprice",
        [select(store)],
        "ordinary-after-transfer-1",
        "改回普通成员",
        category="ordinary",
        price_confirmation=store.price_preview(
            CASHIER, "5", [select(store)], "ordinary"
        ),
    )
    assert row(store)["amount"] == 1800


@pytest.fixture
def migration_recovery(store):
    scan(store, ("1", "2", "3"), [evidence(), evidence("3", event="paid-original")])
    pay(store, items=[select(store, "3")])
    roster = replacement_roster()
    migrate_cashback_collector(store, OWNER, "5", "bot-a", "99", 1, roster)
    scan(store, tuple(roster["members"]), [], bot="bot-b", account="100")
    stamp = row(store, "4")["first_seen"] - 0.001
    proof = {
        "apply_id": "400",
        "state": "MEMBER_STATE_GOOD",
        "apply_time": datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
        "member_id": "4",
        "member_nim_id": "1004",
        "inviter_id": "1",
        "inviter_nim_id": "1001",
    }
    store.remember_source_page(
        "5", "bot-b", "100", store.get_group("5")["started"], {"items": [proof]}
    )
    return store, roster, proof


def test_migration_recovery_restores_only_selected_baselines(migration_recovery):
    store, roster, _ = migration_recovery
    before = {uid: row(store, uid) for uid in ("1", "2", "3", "4", "100")}
    item = select(store, "4")
    preview = recover_cashback_migration_entries(
        store, OWNER, "5", "bot-a", "99", 2, roster, [item], dry_run=True
    )
    assert preview["count"] == 1 and preview["amount"] == 1800
    assert row(store, "4") == before["4"]
    result = recover_cashback_migration_entries(
        store, OWNER, "5", "bot-a", "99", 2, roster, [item]
    )
    recovered = row(store, "4")
    assert result["count"] == 1
    assert recovered["inviter"] == "1"
    assert recovered["verification"] == "verified"
    assert recovered["state"] == "verifying"
    assert recovered["amount"] == 1800 and recovered["member_class"] == "ordinary"
    assert recovered["first_seen"] == before["4"]["first_seen"]
    assert recovered["hold_until"] >= time.time() + 10799
    assert {uid: row(store, uid) for uid in ("1", "2", "3", "100")} == {
        uid: before[uid] for uid in ("1", "2", "3", "100")
    }
    assert store.list_entries(CASHIER, "5", "verifying")["total"] == 1
    assert (
        store.private_invitation_summaries("1", "bot-b", "100", ["5"])[0]["verifying"]
        == 1
    )
    with pytest.raises(CashbackError, match="recovery_entry_changed"):
        recover_cashback_migration_entries(
            store, OWNER, "5", "bot-a", "99", 2, roster, [item]
        )
    assert row(store, "4") == recovered
    assert (
        sum(
            r["action"] == "migration_invitation_recovered"
            for r in store.audit(OWNER, "5")
        )
        == 1
    )
    store = CashbackStore(store.path)
    assert row(store, "4") == recovered
    scan(store, tuple(roster["members"]), [], bot="bot-b", account="100")
    assert row(store, "4")["state"] == "verifying"


@pytest.mark.asyncio
async def test_cashback_risk_90_percent_warning_then_second_burst_freezes(store):
    started = store.get_group("5")["risk_started"]
    first_at = started + 1
    first_members = {"1": member("1")}
    first_proofs = []
    for index, uid in enumerate(range(20, 30)):
        uid = str(uid)
        first_members[uid] = member(uid)
        name = "abcd" if index < 9 else "abcde"
        first_proofs.append(risk_proof(uid, first_at + index, name))

    store.snapshot(
        "5",
        "bot-a",
        "99",
        first_members,
        first_proofs,
        now=first_at + 20,
    )
    pending = store.pending_risk_notices("5", "99")
    assert len(pending) == 1
    with store.connect() as db:
        incident = dict(
            db.execute(
                "SELECT * FROM cashback_risk_incidents WHERE id=?",
                (pending[0]["id"],),
            ).fetchone()
        )
        assert incident["member_count"] == 10
        assert incident["matching_count"] == 9
        assert incident["name_length"] == 4

    service = object.__new__(CashbackService)
    service.store = store
    service.check_selected = lambda _group: None
    service.activities = SimpleNamespace(authorize=lambda *_args: None)
    adapter = SimpleNamespace(
        account="99",
        config={
            "id": "bot-a",
            "proactive_send": {"enabled": False, "targets": []},
        },
        members={"5": {"1": member("1")["peer"]}},
        send_text=AsyncMock(return_value="accepted"),
    )
    await service._deliver_risk_notices(adapter, "5", {"1": member("1")})
    adapter.send_text.assert_not_awaited()
    assert len(store.pending_risk_notices("5", "99")) == 1

    adapter.config["proactive_send"] = {"enabled": True, "targets": ["5"]}
    await service._deliver_risk_notices(adapter, "5", {"1": member("1")})
    adapter.send_text.assert_awaited_once()
    assert adapter.send_text.await_args.kwargs["proactive"] is True
    notice_text = adapter.send_text.await_args.args[2]
    mention = adapter.send_text.await_args.kwargs["mentions"][0]
    assert notice_text == "@成员1 风控刷子，如果再次违规，直接拉黑，不予结账！"
    assert mention["uid"] == int(member("1")["peer"])
    assert mention["start"] == 0
    assert mention["end"] == len("@成员1 ".encode("utf-16-le")) // 2
    assert not store.finish_risk_notice(pending[0]["id"], "accepted")

    second_at = first_at + 320
    second_members = dict(first_members)
    second_proofs = []
    for uid in range(30, 40):
        uid = str(uid)
        second_members[uid] = member(uid)
        second_proofs.append(risk_proof(uid, second_at + int(uid) - 30, "wxyz"))
    store.snapshot(
        "5",
        "bot-a",
        "99",
        second_members,
        second_proofs,
        now=second_at + 30,
    )
    with store.connect() as db:
        risk = db.execute(
            "SELECT * FROM cashback_inviter_risk WHERE group_id='5' "
            "AND account='99' AND inviter='1'"
        ).fetchone()
        assert risk["warning_count"] == 1
        assert risk["blocked_at"] is not None
        incidents = db.execute(
            "SELECT status FROM cashback_risk_incidents WHERE group_id='5' "
            "AND inviter='1' ORDER BY detected"
        ).fetchall()
        assert [item["status"] for item in incidents] == ["warned", "blocked"]

    summaries = store.private_invitation_summaries("1", "bot-a", "99", ["5"])
    assert summaries[0]["risk_status"] == "blocked"
    assert summaries[0]["pending"] == 0
    assert summaries[0]["risk_blocked_count"] >= 1

    with store.connect(True) as db:
        db.execute(
            "UPDATE entries SET state='pending',verification='verified',"
            "present=1,ever_left=0,hold_until=0,pricing_status='priced',"
            "member_class='ordinary' WHERE group_id='5' AND member='20'"
        )
    with pytest.raises(CashbackError, match="cashback_risk_blocked"):
        pay(
            store,
            request="risk-blocked-payment-001",
            items=[select(store, "20")],
        )
    with pytest.raises(CashbackError, match="forbidden"):
        store.release_risk(CASHIER, "5", "1", "已人工复核")
    assert store.release_risk(OWNER, "5", "1", "邀请证据已核对")["status"] == "released"
    assert (
        pay(
            store,
            request="risk-released-payment-001",
            items=[select(store, "20")],
        )["status"]
        == "registered"
    )


def test_cashback_risk_requires_ten_unique_members_and_90_percent(store):
    started = store.get_group("5")["risk_started"]
    first_at = started + 1
    members = {"1": member("1")}
    proofs = []
    for uid in range(20, 31):
        uid = str(uid)
        members[uid] = member(uid)
        # Eight of eleven is below the configured 90% threshold.
        name = "abcd" if int(uid) < 28 else f"length-{uid}"
        proofs.append(risk_proof(uid, first_at + int(uid), name))
    store.snapshot("5", "bot-a", "99", members, proofs, now=first_at + 30)
    assert store.pending_risk_notices("5", "99") == []
    with store.connect() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM cashback_risk_incidents WHERE group_id='5'"
            ).fetchone()[0]
            == 0
        )


def test_cashback_risk_includes_exact_five_minute_boundary(store):
    started = store.get_group("5")["risk_started"]
    first_at = started + 1
    members = {"1": member("1")}
    proofs = []
    for uid in range(20, 30):
        uid = str(uid)
        members[uid] = member(uid)
        offset = 300 if uid == "29" else int(uid) - 20
        proofs.append(risk_proof(uid, first_at + offset, "same"))
    store.snapshot(
        "5",
        "bot-a",
        "99",
        members,
        proofs,
        now=first_at + 301,
    )
    pending = store.pending_risk_notices("5", "99")
    assert len(pending) == 1
    with store.connect() as db:
        incident = db.execute(
            "SELECT member_count,matching_count FROM cashback_risk_incidents "
            "WHERE id=?",
            (pending[0]["id"],),
        ).fetchone()
    assert tuple(incident) == (10, 10)


def test_cashback_risk_unknown_join_names_do_not_match(store):
    started = store.get_group("5")["risk_started"]
    first_at = started + 1
    proofs = [
        risk_proof(str(uid), first_at + index, "")
        for index, uid in enumerate(range(20, 30))
    ]
    store.snapshot("5", "bot-a", "99", {"1": member("1")}, proofs, now=first_at + 20)
    assert store.pending_risk_notices("5", "99") == []


def test_cashback_risk_counts_source_verified_invitees_who_left_before_roster_scan(
    store,
):
    started = store.get_group("5")["risk_started"]
    first_at = started + 1
    proofs = [
        risk_proof(str(uid), first_at + index, "same")
        for index, uid in enumerate(range(20, 30))
    ]
    store.snapshot(
        "5",
        "bot-a",
        "99",
        {"1": member("1")},
        proofs,
        now=first_at + 20,
    )
    assert len(store.pending_risk_notices("5", "99")) == 1
    with store.connect() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM entries WHERE group_id='5' AND member BETWEEN '20' AND '30'"
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM evidence WHERE group_id='5' "
                "AND source='business_apply_log' AND member_name_length=4"
            ).fetchone()[0]
            == 10
        )


@pytest.mark.parametrize(
    "bad",
    [
        "cashier",
        "version",
        "source_bot",
        "account",
        "partial",
        "stale",
        "peer",
        "bot_member",
        "inviter_ineligible",
        "missing_source",
        "duplicate_source",
        "wrong_member_peer",
        "wrong_inviter_peer",
        "unapproved",
        "old_source",
        "future_source",
        "missing_price",
        "left",
        "paid",
        "self_join",
        "audited",
        "preexisting_baseline",
        "duplicate_selection",
        "batch_conflict",
    ],
)
def test_migration_recovery_rejects_unsafe_changes_atomically(migration_recovery, bad):
    store, roster, proof = migration_recovery
    actor, source_bot, version = OWNER, "bot-a", 2
    items = [select(store, "4")]
    if bad == "cashier":
        actor = CASHIER
    elif bad == "version":
        version = 1
    elif bad == "source_bot":
        source_bot = "foreign"
    elif bad == "account":
        roster["account"] = "1"
    elif bad == "partial":
        roster["complete"] = False
    elif bad == "stale":
        roster["observed"] -= 61
    elif bad == "peer":
        roster["members"]["4"]["peer"] = "changed-peer"
    elif bad == "bot_member":
        roster["members"]["4"]["eligible"] = False
    elif bad == "inviter_ineligible":
        roster["members"]["1"]["eligible"] = False
    elif bad in {"duplicate_selection", "batch_conflict"}:
        items.append(items[0] if bad == "duplicate_selection" else select(store, "100"))
    else:
        with store.connect(True) as db:
            if bad == "missing_source":
                db.execute("DELETE FROM invitation_source_records")
            elif bad == "duplicate_source":
                db.execute(
                    "INSERT INTO invitation_source_records SELECT group_id,bot_id,account,"
                    "started,'another-source',payload,observed FROM invitation_source_records"
                )
            elif bad in {
                "wrong_member_peer",
                "wrong_inviter_peer",
                "unapproved",
                "old_source",
                "future_source",
            }:
                if bad == "wrong_member_peer":
                    proof["member_nim_id"] = "999"
                elif bad == "wrong_inviter_peer":
                    proof["inviter_nim_id"] = "999"
                elif bad == "unapproved":
                    proof["state"] = "MEMBER_STATE_INVITED"
                else:
                    stamp = 1 if bad == "old_source" else time.time() + 86400
                    proof["apply_time"] = datetime.fromtimestamp(
                        stamp, timezone.utc
                    ).isoformat()
                db.execute(
                    "UPDATE invitation_source_records SET payload=?",
                    (json.dumps(proof),),
                )
            elif bad == "missing_price":
                db.execute("DELETE FROM reward_rule_history")
            elif bad == "left":
                db.execute("UPDATE entries SET ever_left=1 WHERE member='4'")
            elif bad == "paid":
                db.execute("UPDATE entries SET state='paid',paid_at=1 WHERE member='4'")
            elif bad == "self_join":
                db.execute("UPDATE entries SET self_join=1 WHERE member='4'")
            elif bad == "audited":
                store._audit(db, "5", row(store, "4")["id"], OWNER, "correct", {}, {})
            else:
                items = [select(store, "1")]
    before = row(store, "4")
    with store.connect() as db:
        proofs_before = [tuple(r) for r in db.execute("SELECT * FROM evidence")]
    with pytest.raises(CashbackError):
        recover_cashback_migration_entries(
            store, actor, "5", source_bot, "99", version, roster, items
        )
    assert row(store, "4") == before
    with store.connect() as db:
        assert [tuple(r) for r in db.execute("SELECT * FROM evidence")] == proofs_before


def test_unproved_paid_owner_is_detached_without_repaying(store):
    scan(store)
    pay(store)
    before = row(store)
    with store.connect(True) as db:
        db.execute("UPDATE evidence SET source='nim_notification'")
        db.execute("UPDATE entries SET verification='conflict' WHERE member='2'")
    assert store.invitation_summary("5", "1")["paid"] == 0
    assert store.private_invitation_summaries("1", "bot-a", "99", ["5"])[0]["paid"] == 0
    assert detach_unverified_paid_inviters(store, OWNER, "5", 1, [select(store)]) == 1
    after = row(store)
    assert after["inviter"] == after["inviter_name"] == after["inviter_peer"] == ""
    for field in ("id", "member", "state", "amount", "paid_at", "paid_by"):
        assert after[field] == before[field]
    assert store.list_entries(CASHIER, "5", "paid")["total"] == 1
    assert store.list_entries(CASHIER, "5", "paid")["amount"] == before["amount"]
    assert store.invitation_summary("5", "1")["total"] == 0
    audit = next(
        r
        for r in store.audit(OWNER, "5")
        if r["action"] == "unverified_paid_inviter_detached"
    )
    assert json.loads(audit["before_json"])["inviter"] == "1"
    with pytest.raises(CashbackError):
        detach_unverified_paid_inviters(store, OWNER, "5", 1, [select(store)])


@pytest.mark.parametrize("bad", ["cashier", "version", "proof", "batch"])
def test_paid_detachment_requires_admin_and_missing_trusted_proof(store, bad):
    scan(store, ("1", "2", "3"), [evidence(), evidence("3", event="event-3")])
    pay(store, items=[select(store), select(store, "3")])
    with store.connect(True) as db:
        db.execute("UPDATE entries SET verification='conflict'")
        if bad != "proof":
            db.execute("DELETE FROM evidence WHERE member='2'")
    before = row(store)
    items = [select(store)]
    if bad == "batch":
        items.append(select(store, "3"))
    with pytest.raises(CashbackError):
        detach_unverified_paid_inviters(
            store,
            CASHIER if bad == "cashier" else OWNER,
            "5",
            99 if bad == "version" else 1,
            items,
        )
    assert row(store) == before


@pytest.mark.parametrize("departed", [False, True])
def test_later_rejoin_does_not_reassign_registered_payment(store, departed):
    scan(store, ("1", "2", "3"))
    pay(store)
    before = row(store)
    later = {**evidence(inviter="3", event="later-rejoin"), "event_at": time.time() + 1}
    scan(store, ("1", "2", "3"), [later])
    assert row(store)["verification"] == "verified"
    assert row(store)["ever_left"] == 1
    with store.connect(True) as db:
        db.execute("UPDATE entries SET verification='conflict' WHERE member='2'")
        if departed:
            db.execute("UPDATE entries SET present=0 WHERE member='2'")
    scan(store, ("1", "3") if departed else ("1", "2", "3"), [])
    after = row(store)
    assert after["verification"] == "verified"
    for field in ("inviter", "amount", "paid_at", "paid_by", "state"):
        assert after[field] == before[field]
    assert store.invitation_summary("5", "3")["paid"] == 0
    assert store.invitation_summary("5", "1")["paid"] == 1800
    assert (
        store.private_invitation_summaries("1", "bot-a", "99", ["5"])[0]["paid"] == 1800
    )


def test_baseline_and_old_history_never_qualify(store):
    scan(store, ("1", "2", "7"), [evidence(), evidence("7", event="event-7")])
    assert row(store, "1")["eligible"] == 0
    assert row(store, "7")["eligible"] == 0
    assert row(store)["eligible"] == 1
    assert store.list_entries(CASHIER, "5")["total"] == 1


def test_duplicate_event_and_restart_do_not_duplicate(store):
    scan(store)
    for _ in range(3):
        scan(store)
    store = CashbackStore(store.path)
    scan(store)
    assert store.list_entries(CASHIER, "5")["total"] == 1
    assert row(store)["verification"] == "verified"
    assert row(store)["ever_left"] == 0


@pytest.mark.parametrize(
    "source", ["nim_notification", "nim_fallback", "pending", "nickname", ""]
)
def test_provisional_sources_not_payable(store, source):
    scan(store, notices=[evidence(source=source)])
    with pytest.raises(CashbackError, match="not_payable"):
        pay(store)


def test_self_and_bot_not_payable(store):
    scan(store, notices=[evidence(inviter="2")])
    assert row(store)["verification"] == "pending"
    store.snapshot(
        "5",
        "bot-a",
        "99",
        {"1": member("1"), "2": {**member("2"), "eligible": False}},
        [evidence()],
    )
    assert row(store)["verification"] == "blocked"
    with pytest.raises(CashbackError):
        pay(store)


def test_departure_voids_and_rejoin_stays_void(store):
    scan(store)
    scan(store, ("1",), [])
    assert row(store)["state"] == "void"
    scan(store)
    assert row(store)["state"] == "void"
    assert row(store)["present"] == 1
    assert row(store)["ever_left"] == 1


def test_second_native_join_between_snapshots_voids(store):
    scan(store)
    scan(
        store,
        notices=[
            evidence(source="nim_notification"),
            evidence(source="nim_notification", event="second-join"),
        ],
    )
    assert row(store)["state"] == "void"
    assert row(store)["ever_left"] == 1


def test_business_and_native_same_join_not_repeated(store):
    scan(
        store,
        notices=[
            evidence(source="business_apply_log", event="application-1"),
            evidence(source="nim_notification"),
        ],
    )
    assert row(store)["state"] == "pending"
    assert row(store)["ever_left"] == 0


def test_paid_departure_keeps_payment_and_correction_voids(store):
    scan(store)
    pay(store)
    paid = row(store)
    scan(store, ("1",), [])
    assert row(store)["state"] == "paid"
    assert row(store)["paid_at"] == paid["paid_at"]
    pay(
        store,
        operation="correct",
        reason="人工付款登记有误",
        request="correction-request-1",
    )
    assert row(store)["state"] == "void"
    history = store.audit(OWNER, "5")
    assert any(r["action"] == "pay" for r in history)
    assert any(r["action"] == "correct" and r["reason"] for r in history)


@pytest.mark.parametrize("category", ["ordinary", "agent"])
def test_correct_present_reopens_pending(store, category):
    scan(store)
    if category == "agent":
        with store.connect(True) as db:
            db.execute(
                "UPDATE entries SET member_class='agent',amount=1000 WHERE member='2'"
            )
    pay(store)
    before = row(store)
    items = [select(store)]
    result = pay(
        store,
        items=items,
        operation="correct",
        reason="付款未成功",
        request="correction-request-1",
    )
    after = row(store)
    assert after["state"] == "pending"
    assert after["paid_by"] is None
    assert after["paid_at"] is None
    assert after["version"] == before["version"] + 1
    changed = {"state", "paid_by", "paid_at", "version"}
    assert {k: v for k, v in before.items() if k not in changed} == {
        k: v for k, v in after.items() if k not in changed
    }
    history = store.audit(OWNER, "5")
    payment = next(item for item in history if item["action"] == "pay")
    correction = next(item for item in history if item["action"] == "correct")
    assert json.loads(payment["after_json"])["paid_at"] == before["paid_at"]
    with store.connect() as db:
        original_audit = dict(
            db.execute("SELECT * FROM audit WHERE action='correct'").fetchone()
        )
    assert json.loads(original_audit["before_json"]) == {
        **{k: before[k] for k in ("state", "paid_at", "amount", "version")},
        "paid_by": json.loads(payment["after_json"])["paid_by"],
    }
    assert original_audit["actor"] == CASHIER
    assert CASHIER not in correction["before_json"]
    assert correction["reason"] == "付款未成功"
    assert json.loads(correction["after_json"])["state"] == "pending"
    assert (
        pay(
            store,
            items=items,
            operation="correct",
            reason="付款未成功",
            request="correction-request-1",
        )
        == result
    )
    assert row(store) == after
    assert store.audit(OWNER, "5") == history
    assert store.list_entries(CASHIER, "5", state="paid")["total"] == 0
    assert store.list_entries(CASHIER, "5")["amount"] == before["amount"]


def test_correct_admin_can_withdraw_another_cashiers_registration(store):
    scan(store)
    pay(store)
    pay(
        store,
        actor=OWNER,
        operation="correct",
        reason="管理员确认登记错误",
        request="admin-correction-request",
    )
    assert row(store)["state"] == "pending"
    assert row(store)["paid_by"] is None
    assert row(store)["amount"] == 1800


def test_correct_requires_reason_and_paid_state_without_partial_writes(store):
    scan(store)
    before = row(store)
    with pytest.raises(CashbackError, match="correction_forbidden"):
        pay(
            store,
            operation="correct",
            reason="尚未结账不能撤回",
            request="unpaid-correction-request",
        )
    assert row(store) == before
    pay(store)
    before, history = row(store), store.audit(OWNER, "5")
    with pytest.raises(CashbackError, match="reason_required"):
        pay(store, operation="correct", request="no-reason-correction")
    assert row(store) == before
    assert store.audit(OWNER, "5") == history


def test_correct_batch_conflict_rolls_back_every_selected_record(store):
    scan(store, ("1", "2", "3"), [evidence(), evidence("3", event="event-3")])
    pay(store, items=[select(store), select(store, "3")])
    before = [row(store), row(store, "3")]
    history = store.audit(OWNER, "5")
    stale = {**select(store, "3"), "version": row(store, "3")["version"] - 1}
    with pytest.raises(CashbackError, match="conflict"):
        pay(
            store,
            items=[select(store), stale],
            operation="correct",
            reason="批量撤回登记",
            request="batch-correction-conflict",
        )
    assert [row(store), row(store, "3")] == before
    assert store.audit(OWNER, "5") == history


def test_correct_batch_preserves_departure_ineligibility(store):
    scan(store, ("1", "2", "3"), [evidence(), evidence("3", event="event-3")])
    pay(store, items=[select(store), select(store, "3")])
    scan(store, ("1", "2"), [])
    result = pay(
        store,
        items=[select(store), select(store, "3")],
        operation="correct",
        reason="批量撤回错误登记",
        request="batch-correction-success",
    )
    assert result["count"] == 2 and result["amount"] == 3600
    assert row(store)["state"] == "pending"
    assert row(store, "3")["state"] == "void"
    scan(store, ("1", "2", "3"), [])
    assert row(store, "3")["state"] == "void"
    with pytest.raises(CashbackError):
        pay(store, items=[select(store, "3")], request="departed-repayment-request")


def test_correct_other_cashier_denied(store):
    scan(store)
    pay(store)
    config = {**store.get_group("5"), "cashier": OTHER, "enabled": True}
    store.configure(OWNER, config, {"1": member("1"), "2": member("2")}, set())
    with pytest.raises(CashbackError, match="correction_forbidden"):
        pay(
            store,
            actor=OTHER,
            operation="correct",
            reason="付款未成功",
            request="correction-request-2",
        )


def test_cashier_revocation_scope_and_identity_binding(store):
    assert store.identify(CASHIER, "subject-1")["role"] == "cashier"
    with pytest.raises(CashbackError, match="identity_changed"):
        store.identify(CASHIER, "subject-2")
    assert store.list_entries(OTHER, "5")["total"] == 0
    store.save_user(OWNER, CASHIER, "结账员", False, 1)
    with pytest.raises(CashbackError, match="forbidden"):
        store.identify(CASHIER, "subject-1")


def test_admin_seed_removal_revokes_admin(store):
    store.bootstrap([{"email": "new@example.com", "name": "新管理员"}])
    with pytest.raises(CashbackError):
        store.users(OWNER)


def test_price_frozen_and_switch_collector_dedupes(store):
    scan(store)
    config = {
        **store.get_group("5"),
        "rate": 5000,
        "enabled": True,
        "bot_id": "bot-b",
        "account": "88",
    }
    store.configure(OWNER, config, {"1": member("1"), "2": member("2")}, set())
    scan(
        store,
        ("1", "2", "3"),
        [evidence(), evidence("3", event="event-3")],
        "bot-b",
        "88",
    )
    assert row(store)["amount"] == 1800
    assert row(store, "3")["amount"] == 5000
    with pytest.raises(CashbackError, match="collector_changed"):
        scan(store)


def test_pause_before_snapshot_commit_rolls_back_entries_and_audit(store):
    calls = 0

    def guard():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise CashbackError("business_service_inactive", "paused", 409)

    before = store.audit(OWNER, "5")
    with pytest.raises(CashbackError, match="business_service_inactive"):
        store.snapshot(
            "5",
            "bot-a",
            "99",
            {"1": member("1"), "2": member("2")},
            [evidence()],
            guard=guard,
        )
    with store.connect() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM entries WHERE member='2'").fetchone()[0]
            == 0
        )
    assert store.audit(OWNER, "5") == before


def test_enabling_requires_complete_baseline(store):
    config = {**store.get_group("5"), "enabled": True, "bot_id": "new"}
    with pytest.raises(CashbackError, match="baseline_required"):
        store.configure(OWNER, config, None, set())


def test_pause_then_enable_baselines_unseen_members(store):
    config = {**store.get_group("5"), "enabled": False}
    store.configure(OWNER, config, {"1": member("1")}, set())
    config = {**store.get_group("5"), "enabled": True}
    store.configure(OWNER, config, {"1": member("1"), "2": member("2")}, set())
    scan(store)
    assert row(store)["eligible"] == 0


def test_conflicting_attribution_blocks_and_keeps_original(store):
    scan(store, ("1", "2", "3"))
    scan(
        store,
        ("1", "2", "3"),
        [evidence(inviter="3", source="business_apply_log", event="conflict")],
    )
    assert row(store)["inviter"] == "1"
    assert row(store)["verification"] == "conflict"
    with pytest.raises(CashbackError, match="not_payable"):
        pay(store)


@pytest.mark.parametrize("order", ["native_first", "business_first"])
def test_join_approver_is_not_recruiter_and_order_does_not_matter(store, order):
    notices = [
        evidence(inviter="1", source="nim_notification", event="approved-by-owner"),
        evidence(inviter="3", event="invited-by-member"),
    ]
    if order == "business_first":
        notices.reverse()
    scan(store, ("1", "2", "3"), notices)
    assert row(store)["inviter"] == "3"
    assert row(store)["verification"] == "verified"
    assert row(store)["ever_left"] == 0
    assert pay(store)["count"] == 1


def test_native_only_join_has_no_recipient_and_cannot_exception_pay(store):
    scan(store, notices=[evidence(source="nim_notification")])
    assert row(store)["inviter"] == ""
    assert row(store)["verification"] == "pending"
    assert store.list_entries(CASHIER, "5")["total"] == 0
    for operation, actor, reason in [
        ("pay", CASHIER, ""),
        ("exception", OWNER, "Do not use the approving owner"),
    ]:
        with pytest.raises(CashbackError, match="not_payable"):
            pay(store, operation=operation, actor=actor, reason=reason)


@pytest.fixture
def self_join_store(store):
    """Prepare an unassigned member who has separately recruited another member."""
    store.configure(
        OWNER,
        {**store.get_group("5"), "enabled": True, "qualification_seconds": 10800},
        {"1": member("1")},
        set(),
    )
    scan(store, ("1", "2", "3"), [evidence("3", "2", "other-member")])
    return store


def test_confirmed_self_entry_excluded_without_losing_its_own_invitees(self_join_store):
    store = self_join_store
    before, child = row(store), row(store, "3")
    assert (
        store.confirm_self_join(
            OWNER,
            "5",
            [select(store)],
            "Administrator confirms this member joined independently",
        )
        == 1
    )
    after = row(store)
    assert after["self_join"] == 1
    assert after["state"] == "void"
    assert after["verification"] == "blocked"
    for field in (
        "member",
        "peer",
        "first_seen",
        "hold_until",
        "amount",
        "present",
        "ever_left",
        "paid_at",
        "paid_by",
    ):
        assert after[field] == before[field]
    assert row(store, "3") == child
    assert store.list_entries(CASHIER, "5", "verifying")["total"] == 1
    assert store.list_entries(CASHIER, "5", "void")["total"] == 0
    assert store.invitation_summary("5", "2")["total"] == 1
    audit = next(
        r for r in store.audit(OWNER, "5") if r["action"] == "self_join_confirmed"
    )
    assert "joined independently" in audit["reason"]


def test_unknown_entry_is_not_automatically_self_joined(self_join_store):
    store = self_join_store
    scan(store, ("1", "2", "3"), [evidence(source="nim_notification")])
    assert row(store)["self_join"] == 0
    assert row(store)["state"] == "verifying"
    assert row(store)["verification"] == "pending"
    result = store.list_entries(CASHIER, "5", "verifying")
    assert all(entry["member"] != "2" for entry in result["items"])
    assert result["total"] == len(result["items"])
    assert result["amount"] == sum(entry["amount"] for entry in result["items"])
    with store.connect(True) as db:
        db.execute(
            "UPDATE entries SET inviter='1',inviter_name='成员1',inviter_peer='1001',"
            "state='void',verification='pending',present=0,ever_left=1,amount=1800 "
            "WHERE group_id='5' AND member='2'"
        )
    void = store.list_entries(CASHIER, "5", "void")
    assert void["items"] == []
    assert void["total"] == 0
    assert void["amount"] == 0


def test_paid_history_without_current_inviter_stays_visible(store):
    scan(store)
    pay(store)
    with store.connect(True) as db:
        db.execute(
            "UPDATE entries SET inviter='',inviter_name='' "
            "WHERE group_id='5' AND member='2'"
        )
    paid = store.list_entries(CASHIER, "5", "paid")
    assert [entry["member"] for entry in paid["items"]] == ["2"]
    assert paid["total"] == 1
    assert paid["amount"] == 1800


def test_self_entry_confirmation_survives_restart_and_is_idempotent(self_join_store):
    store = self_join_store
    store.confirm_self_join(OWNER, "5", [select(store)], "Explicit self entry")
    store = CashbackStore(store.path)
    scan(store, ("1", "2", "3"), [])
    assert (
        store.confirm_self_join(OWNER, "5", [select(store)], "Explicit self entry") == 0
    )
    assert row(store)["self_join"] == 1
    assert row(store)["state"] == "void"
    assert row(store)["ever_left"] == 0
    assert (
        sum(r["action"] == "self_join_confirmed" for r in store.audit(OWNER, "5")) == 1
    )


def test_later_invitation_proof_does_not_restore_self_entry_reward(self_join_store):
    store = self_join_store
    store.confirm_self_join(OWNER, "5", [select(store)], "Explicit self entry")
    for _ in range(2):
        scan(store, ("1", "2", "3"), [evidence(event="late-proof")])
    assert row(store)["self_join"] == 1
    assert row(store)["state"] == "void"
    assert row(store)["verification"] == "conflict"
    assert row(store)["inviter"] == ""
    audits = [
        r
        for r in store.audit(OWNER, "5")
        if r["action"] == "self_join_attribution_conflict"
    ]
    assert len(audits) == 1 and audits[0]["level"] == "warning"


@pytest.mark.parametrize("operation", ["pay", "exception"])
def test_self_entry_flag_independently_blocks_registration(self_join_store, operation):
    store = self_join_store
    store.confirm_self_join(OWNER, "5", [select(store)], "Explicit self entry")
    with store.connect(True) as db:
        db.execute(
            "UPDATE entries SET state='pending',verification='verified',inviter='1',"
            "inviter_class='legacy' WHERE member='2'"
        )
    with pytest.raises(CashbackError, match="not_payable"):
        pay(
            store,
            actor=OWNER,
            operation=operation,
            reason="Attempt to bypass self entry",
        )


def test_cashier_cannot_confirm_self_entry(self_join_store):
    store = self_join_store
    before = row(store)
    with pytest.raises(CashbackError, match="forbidden"):
        store.confirm_self_join(CASHIER, "5", [select(store)], "Explicit self entry")
    assert row(store) == before


def test_self_entry_batch_with_attributed_member_has_no_partial_changes(
    self_join_store,
):
    store = self_join_store
    before = row(store)
    with pytest.raises(CashbackError, match="self_join_unavailable"):
        store.confirm_self_join(
            OWNER, "5", [select(store), select(store, "3")], "Explicit self entry"
        )
    assert row(store) == before
    assert not any(
        r["action"] == "self_join_confirmed" for r in store.audit(OWNER, "5")
    )


def test_self_entry_confirmation_requires_exact_current_version(self_join_store):
    store = self_join_store
    stale = {**select(store), "version": row(store)["version"] - 1}
    with pytest.raises(CashbackError, match="conflict"):
        store.confirm_self_join(OWNER, "5", [stale], "Explicit self entry")
    assert row(store)["self_join"] == 0


def test_departed_unknown_source_can_be_confirmed_and_is_excluded_from_void_totals(
    store,
):
    scan(store, notices=[evidence(source="nim_notification")])
    store.snapshot("5", "bot-a", "99", {"1": member("1")}, [])
    before = row(store)
    assert before["state"] == "void" and before["ever_left"] == 1
    assert before["amount"] == 1800
    assert (
        store.confirm_self_join(
            OWNER, "5", [select(store)], "Administrator confirms organic group entry"
        )
        == 1
    )
    after = row(store)
    assert after["self_join"] == 1 and after["state"] == "void"
    assert after["amount"] == before["amount"]
    assert store.list_entries(CASHIER, "5", "void")["total"] == 0
    assert store.list_entries(CASHIER, "5", "void")["amount"] == 0


def test_rejoined_unattributed_void_record_can_be_confirmed_and_hidden(store):
    scan(store, notices=[evidence(source="nim_notification")])
    store.snapshot("5", "bot-a", "99", {"1": member("1")}, [])
    store.snapshot(
        "5",
        "bot-a",
        "99",
        {"1": member("1"), "2": member("2")},
        [evidence(source="nim_notification", event="second-join")],
    )
    before = row(store)
    assert before["state"] == "void" and before["present"] and before["ever_left"]
    assert (
        store.confirm_self_join(
            OWNER, "5", [select(store)], "Administrator confirms organic group entry"
        )
        == 1
    )
    assert row(store)["self_join"] == 1
    assert store.list_entries(CASHIER, "5", "void")["total"] == 0


def test_self_entry_cannot_rewrite_registered_payment(store):
    scan(store)
    pay(store)
    before = row(store)
    with pytest.raises(CashbackError, match="self_join_unavailable"):
        store.confirm_self_join(OWNER, "5", [select(store)], "Explicit self entry")
    assert row(store) == before


def test_self_entry_departure_rejoin_and_deadline_do_not_create_reward(self_join_store):
    store = self_join_store
    store.confirm_self_join(OWNER, "5", [select(store)], "Explicit self entry")
    scan(store, ("1", "3"), [])
    store.snapshot(
        "5",
        "bot-a",
        "99",
        {uid: member(uid) for uid in ("1", "2", "3")},
        [],
        now=row(store)["hold_until"] + 1,
    )
    assert row(store)["self_join"] == 1
    assert row(store)["state"] == "void"
    assert row(store)["present"] == 1
    assert row(store)["ever_left"] == 1


def test_self_entry_flag_migration_preserves_existing_entries(self_join_store):
    store = self_join_store
    before = row(store)
    with store.connect(True) as db:
        db.execute("DROP INDEX entry_settlement_summary")
        db.execute("DROP INDEX entry_no_reward_summary")
        db.execute("ALTER TABLE entries DROP COLUMN self_join")
    store = CashbackStore(store.path)
    assert row(store) == before


@pytest.mark.parametrize("paid", [False, True])
def test_legacy_owner_attribution_is_repaired_without_rewriting_payment(store, paid):
    scan(store, ("1", "2", "3"))
    if paid:
        pay(store)
    before = row(store)
    with store.connect(True) as db:
        db.execute("UPDATE evidence SET source='nim_notification'")
    scan(store, ("1", "2", "3"), [evidence(inviter="3", event="true-recruiter")])
    after = row(store)
    assert after["id"] == before["id"]
    assert after["amount"] == before["amount"]
    assert after["state"] == before["state"]
    assert after["paid_at"] == before["paid_at"]
    assert after["paid_by"] == before["paid_by"]
    assert after["ever_left"] == before["ever_left"]
    if paid:
        assert after["inviter"] == "1"
        assert after["verification"] == "conflict"
        action = "paid_attribution_review_required"
    else:
        assert after["inviter"] == "3"
        assert after["verification"] == "verified"
        action = "join_operator_attribution_removed"
    with store.connect() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM audit WHERE action=?", (action,)
            ).fetchone()[0]
            == 1
        )
    scan(store, ("1", "2", "3"), [evidence(source="nim_notification")])
    assert row(store)["inviter"] == after["inviter"]


def test_old_native_claim_cannot_pay_before_first_reconciliation(store):
    scan(store)
    with store.connect(True) as db:
        db.execute("UPDATE evidence SET source='nim_notification'")
    with pytest.raises(CashbackError, match="not_payable"):
        pay(store)
    scan(store, notices=[])
    assert row(store)["inviter"] == ""
    assert row(store)["verification"] == "pending"


def test_departed_native_only_assignment_is_unattributed_not_verified(store):
    scan(store)
    with store.connect(True) as db:
        db.execute("UPDATE evidence SET source='nim_notification'")
    scan(store, ("1",), [])
    assert row(store)["state"] == "void"
    assert row(store)["inviter"] == ""
    assert row(store)["verification"] == "pending"


def test_business_recruiter_evidence_survives_no_new_records_and_restart(store):
    scan(store, ("1", "2", "3"), [evidence(inviter="3")])
    store = CashbackStore(store.path)
    scan(store, ("1", "2", "3"), [evidence(source="nim_notification")])
    assert row(store)["inviter"] == "3"
    assert row(store)["verification"] == "verified"


def test_legacy_native_repair_preserves_audited_amount_correction(store):
    scan(store, ("1", "2", "3"))
    old = row(store)
    with store.connect(True) as db:
        db.execute("UPDATE evidence SET source='nim_notification'")
        db.execute(
            "UPDATE entries SET amount=300,version=version+1 WHERE id=?", (old["id"],)
        )
        store._audit(
            db,
            "5",
            old["id"],
            "system",
            "amount_corrected",
            {"amount": old["amount"]},
            {"amount": 300},
            "Explicit amount correction.",
        )
    store.configure(
        OWNER,
        {**store.get_group("5"), "enabled": True, "rate": 2500},
        {uid: member(uid) for uid in ("1", "2", "3")},
        set(),
        now=time.time() + 1,
    )
    store.snapshot(
        "5",
        "bot-a",
        "99",
        {uid: member(uid) for uid in ("1", "2", "3")},
        [
            {
                **evidence(inviter="3", event="approved-application"),
                "event_at": old["first_seen"],
            }
        ],
        now=time.time() + 2,
    )
    assert row(store)["inviter"] == "3"
    assert row(store)["amount"] == 300
    assert row(store)["verification"] == "verified"


def test_inviter_identity_change_is_conflict_not_automatic_reassignment(store):
    scan(store, ("1", "2", "3"))
    members = {uid: member(uid) for uid in ("1", "2", "3")}
    members["1"]["peer"] = "replacement"
    store.snapshot("5", "bot-a", "99", members, [])
    assert row(store)["verification"] == "conflict"
    with pytest.raises(CashbackError, match="identity_conflict"):
        pay(store)


@pytest.mark.parametrize(
    "field,value", [("account", "88"), ("member_peer", "999"), ("inviter_peer", "999")]
)
def test_wrong_account_or_identity_in_business_evidence_cannot_pay(store, field, value):
    scan(store)
    with store.connect(True) as db:
        db.execute(f"UPDATE evidence SET {field}=?", (value,))
    with pytest.raises(CashbackError, match="not_payable"):
        pay(store)


def test_exception_payment_requires_admin_reason_and_preserves_void_history(store):
    scan(store)
    scan(store, ("1",), [])
    with pytest.raises(CashbackError):
        pay(store, operation="exception", reason="转账后发现退群")
    with pytest.raises(CashbackError, match="reason_required"):
        pay(store, actor=OWNER, operation="exception")
    pay(store, actor=OWNER, operation="exception", reason="转账后发现退群")
    assert row(store)["state"] == "paid"
    assert row(store)["ever_left"]
    assert store.audit(OWNER, "5")[0]["action"] == "exception"


def test_idempotent_retry_and_payload_conflict(store):
    scan(store)
    items = [select(store)]
    result = pay(store, items=items)
    assert pay(store, items=items) == result
    with pytest.raises(CashbackError, match="idempotency_conflict"):
        pay(store, items=items, reason="changed")
    assert sum(r["action"] == "pay" for r in store.audit(OWNER, "5")) == 1


def test_concurrent_old_version_only_one_payment(store):
    scan(store)
    items = [select(store)]

    def submit(i):
        try:
            pay(store, items=items, request=f"concurrent-request-{i}")
            return "ok"
        except CashbackError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(submit, [1, 2])) == ["conflict", "ok"]


def test_batch_all_or_nothing_and_same_recipient(store):
    scan(store, ("1", "2", "3", "4"), [evidence(), evidence("3", "4", "event-3")])
    with pytest.raises(CashbackError, match="mixed_recipients"):
        pay(store, items=[select(store), select(store, "3")])
    assert row(store)["state"] == "pending"
    with pytest.raises(CashbackError, match="conflict"):
        pay(store, items=[select(store), {**select(store, "3"), "version": 0}])
    assert row(store)["state"] == "pending"


@pytest.mark.parametrize("unqualified", ["deadline", "price"])
def test_withdrawal_does_not_skip_remaining_qualification(store, unqualified):
    scan(store)
    pay(store)
    before = row(store)
    with store.connect(True) as db:
        if unqualified == "deadline":
            db.execute(
                "UPDATE entries SET hold_until=? WHERE id=?",
                (time.time() + 3600, before["id"]),
            )
        else:
            db.execute(
                "UPDATE entries SET pricing_status='unconfigured' WHERE id=?",
                (before["id"],),
            )
    current = row(store)
    pay(
        store,
        operation="correct",
        reason="付款未完成，撤回登记",
        request="unqualified-withdrawal",
    )
    after = row(store)
    assert after["state"] == "verifying"
    assert after["paid_at"] is None and after["paid_by"] is None
    for field in ("amount", "inviter", "member_class", "hold_until", "pricing_status"):
        assert after[field] == current[field]
    assert store.list_entries(CASHIER, "5")["payable_amount"] == 0
    assert store.invitation_summary("5", "1")["pending"] == 0
    assert (
        store.private_invitation_summaries("1", "bot-a", "99", ["5"])[0]["pending"] == 0
    )
    with pytest.raises(
        CashbackError,
        match="qualification_required|not_payable|historical_price_unavailable",
    ):
        pay(store, request="unqualified-withdrawal-repay")


def test_unavailable_and_stale_roster_block_payment(store):
    scan(store)
    store.scan_error("5", "incomplete")
    with pytest.raises(CashbackError, match="roster_unavailable"):
        pay(store)
    scan(store)
    with store.connect(True) as db:
        db.execute("UPDATE groups SET scanned=0 WHERE id='5'")
    with pytest.raises(CashbackError, match="roster_unavailable"):
        pay(store)


def test_search_by_name_no_uid_guess_and_names_refresh(store):
    scan(store)
    assert store.list_entries(CASHIER, "5", search="成员2")["total"] == 1
    assert store.list_entries(CASHIER, "5", search="not a name")["total"] == 0
    changed = {"1": {**member("1"), "name": "新邀请人昵称"}, "2": member("2")}
    store.snapshot("5", "bot-a", "99", changed, [evidence()])
    assert row(store)["inviter_name"] == "新邀请人昵称"


def pagination_records(store, counts, state="pending"):
    """Create isolated ledger records for account and member page boundaries.

    Args:
        store: Temporary test ledger.
        counts: Mapping of inviter account to invited member count.
        state: Settlement state, or unassigned attribution view.

    Returns:
        The generated invitee account list.
    """
    members = {uid: member(uid) for uid in counts}
    notices, invitees = [], []
    for inviter, count in counts.items():
        for _ in range(count):
            uid = str(100000 + len(invitees))
            invitees.append(uid)
            members[uid] = member(uid)
            notices.append(evidence(uid, inviter, f"pagination-{uid}"))
    store.snapshot("5", "bot-a", "99", members, notices)
    with store.connect(True) as db:
        db.executemany(
            "UPDATE entries SET eligible=0 WHERE member=?", [(uid,) for uid in counts]
        )
        db.execute(
            "UPDATE entries SET amount=500,state=?,paid_by=?,paid_at=?,hold_until=? "
            "WHERE eligible=1",
            (
                "verifying" if state == "unassigned" else state,
                CASHIER if state == "paid" else None,
                time.time() if state == "paid" else None,
                time.time() + 10800 if state == "verifying" else 0,
            ),
        )
        if state == "unassigned":
            db.execute(
                "UPDATE entries SET inviter='',inviter_name='',inviter_peer='',"
                "verification='conflict' WHERE eligible=1"
            )
    return invitees


@pytest.mark.parametrize("state", ["verifying", "pending", "paid", "void"])
def test_inviter_pagination_does_not_split_pp_at_old_100_entry_boundary(store, state):
    pagination_records(store, {"1": 84, "38406086": 19, "9": 82}, state)
    legacy = [store.list_entries(CASHIER, "5", state, page=page) for page in (1, 2)]
    assert [
        sum(r["amount"] for r in result["items"] if r["inviter"] == "38406086")
        for result in legacy
    ] == [8000, 1500]
    with store.connect() as db:
        before = list(db.iterdump())
    result = store.list_entries(CASHIER, "5", state, pagination="inviters")
    pp = next(s for s in result["sections"] if s["inviter"] == "38406086")
    assert (pp["count"], pp["amount"], pp["page_count"]) == (19, 9500, 1)
    assert len([r for r in result["items"] if r["inviter"] == "38406086"]) == 19
    assert (result["total"], result["amount"], result["section_total"]) == (
        185,
        92500,
        3,
    )
    assert result["page_count"] == 1
    with store.connect() as db:
        assert list(db.iterdump()) == before


@pytest.mark.parametrize(
    "state", ["verifying", "pending", "paid", "void", "unassigned"]
)
def test_member_pages_keep_complete_section_totals_and_clamp_disappeared_pages(
    store, state
):
    invitees = pagination_records(store, {"1": 205}, state)
    inviter = "" if state == "unassigned" else "1"
    ids = []
    for page, expected in ((1, 100), (2, 100), (3, 5), (999, 5)):
        result = store.list_entries(
            CASHIER,
            "5",
            state,
            pagination="inviters",
            inviter=inviter,
            member_page=page,
        )
        summary = result["sections"][0]
        assert summary["count"] == 205 and summary["amount"] == 102500
        assert summary["page_count"] == 3 and summary["page"] == min(page, 3)
        assert len(result["items"]) == expected
        if page != 999:
            ids.extend(r["member"] for r in result["items"])
    assert len(ids) == len(set(ids)) == 205 and set(ids) == set(invitees)
    with store.connect(True) as db:
        db.execute(
            "UPDATE entries SET eligible=0 WHERE member IN (?, ?, ?, ?, ?)",
            tuple(ids[-5:]),
        )
    result = store.list_entries(
        CASHIER,
        "5",
        state,
        pagination="inviters",
        inviter=inviter,
        member_page=3,
    )
    assert result["sections"][0]["page"] == 2
    assert result["sections"][0]["count"] == 200
    assert len(result["items"]) == 100


def test_inviter_pages_cover_accounts_once_and_bound_larger_sections(store):
    counts = {str(n): 1 for n in range(1, 13)}
    counts["1"] = 205
    pagination_records(store, counts)
    accounts = []
    for page in (1, 2, 3):
        result = store.list_entries(CASHIER, "5", pagination="inviters", page=page)
        assert (result["page"], result["page_count"], result["page_size"]) == (
            page,
            3,
            5,
        )
        assert result["total"] == 216 and result["amount"] == 108000
        assert len(result["items"]) <= 500
        accounts.extend(s["inviter"] for s in result["sections"])
    assert len(accounts) == len(set(accounts)) == 12
    assert set(accounts) == set(counts)
    with store.connect(True) as db:
        db.execute(
            "UPDATE entries SET eligible=0 WHERE inviter IN (?, ?)",
            tuple(accounts[-2:]),
        )
    result = store.list_entries(CASHIER, "5", pagination="inviters", page=3)
    assert result["page"] == result["page_count"] == 2


def test_grouped_filters_use_same_scope_and_aliases_do_not_split_or_merge_accounts(
    store,
):
    pagination_records(store, {"1": 103, "3": 2})
    hour_start, hour_end = entry_time_window("hour", time.time())
    with store.connect(True) as db:
        db.execute(
            "UPDATE entries SET member_name='同名邀请人' WHERE member IN ('1','3')"
        )
        db.execute(
            "UPDATE entries SET inviter_name=CASE WHEN member='100000' THEN '旧称' "
            "ELSE '同名邀请人' END WHERE eligible=1"
        )
        db.execute(
            "UPDATE entries SET first_seen=? WHERE eligible=1", (hour_start + 30,)
        )
        db.execute(
            "UPDATE entries SET first_seen=?,amount=1000,member_class='agent' "
            "WHERE member='100000'",
            (hour_start - 1,),
        )
    all_rows = store.list_entries(CASHIER, "5", pagination="inviters")
    assert len(all_rows["sections"]) == 2
    assert {s["inviter_name"] for s in all_rows["sections"]} == {"同名邀请人"}
    assert all_rows["sections"][0]["amount"] == 52000
    filtered = store.list_entries(
        CASHIER,
        "5",
        pagination="inviters",
        period="custom",
        start_at=hour_start,
        end_at=hour_end,
    )
    assert filtered["total"] == 104 and filtered["amount"] == 52000
    assert [(s["count"], s["amount"]) for s in filtered["sections"]] == [
        (102, 51000),
        (2, 1000),
    ]
    searched = store.list_entries(
        CASHIER, "5", pagination="inviters", search="成员100000"
    )
    assert searched["total"] == 1 and searched["amount"] == 1000
    assert searched["sections"][0]["count"] == 1
    assert (
        store.list_entries(CASHIER, "5", pagination="inviters", search="38406086")[
            "total"
        ]
        == 0
    )
    absent = store.list_entries(CASHIER, "5", pagination="inviters", inviter="999999")
    assert absent["sections"] == absent["items"] == []


def test_grouped_reads_keep_one_snapshot_during_concurrent_amount_change(
    store, monkeypatch
):
    pagination_records(store, {"1": 1})
    original_scope = store._scope

    def concurrent_scope(db, actor, group):
        result = original_scope(db, actor, group)
        with store.connect(True) as writer:
            writer.execute("UPDATE entries SET amount=1000 WHERE eligible=1")
        return result

    monkeypatch.setattr(store, "_scope", concurrent_scope)
    result = store.list_entries(CASHIER, "5", pagination="inviters")
    assert (
        result["amount"]
        == result["sections"][0]["amount"]
        == result["items"][0]["amount"]
        == 500
    )
    assert row(store, "100000")["amount"] == 1000


@pytest.mark.parametrize("count", [2, 80])
@pytest.mark.parametrize("state", ["pending", "paid"])
def test_listing_batches_integrity_reads_and_preserves_metadata(
    store, monkeypatch, count, state
):
    pagination_records(store, {"1": count}, state)
    statements = []
    original_connect = store.connect

    @contextmanager
    def traced_connect(*args, **kwargs):
        with original_connect(*args, **kwargs) as db:
            db.set_trace_callback(statements.append)
            yield db

    monkeypatch.setattr(store, "connect", traced_connect)
    result = store.list_entries(CASHIER, "5", state, pagination="inviters")
    assert result["total"] == len(result["items"]) == count
    reads = [query for query in statements if query.startswith(("SELECT", "WITH"))]
    assert len(reads) <= 15
    details = [
        query
        for query in reads
        if "AS entry_id FROM current_invitation_evidence p" in query
        or "AS entry_id FROM cashback_membership_events m" in query
        or "AS entry_id FROM cashback_source_quarantine q" in query
    ]
    assert len(details) == 3
    with original_connect() as db:
        db.execute("BEGIN")
        rule, _ = store._scope(db, CASHIER, "5")
        for item in result["items"]:
            original = db.execute(
                "SELECT * FROM entries WHERE id=?", (item["id"],)
            ).fetchone()
            for key, value in store._entry_integrity(db, rule, original).items():
                assert item[key] == value
        for query in details:
            plan = [step[3] for step in db.execute("EXPLAIN QUERY PLAN " + query)]
            assert not any(
                step.startswith(("SCAN e ", "SCAN m ", "SCAN q ", "SCAN audit "))
                for step in plan
            )


@pytest.mark.parametrize(
    "state", ["verifying", "paid", "void", "unassigned", "no_reward"]
)
def test_nonpending_reads_do_not_scan_payment_eligibility(store, monkeypatch, state):
    scan(store)
    statements = []
    original_connect = store.connect

    @contextmanager
    def traced_connect(*args, **kwargs):
        with original_connect(*args, **kwargs) as db:
            db.set_trace_callback(statements.append)
            yield db

    monkeypatch.setattr(store, "connect", traced_connect)
    result = store.list_entries(CASHIER, "5", state, pagination="inviters")
    assert (result["payable_count"], result["payable_amount"]) == (0, 0)
    assert not any("hold_until<=" in statement for statement in statements)


@pytest.mark.parametrize(
    "state", ["verifying", "pending", "paid", "void", "unassigned", "no_reward"]
)
@pytest.mark.parametrize("pagination", ["entries", "inviters"])
def test_empty_reads_authorize_before_skipping_detail_work(
    store, monkeypatch, state, pagination
):
    statements = []
    original_connect = store.connect

    @contextmanager
    def traced_connect(*args, **kwargs):
        with original_connect(*args, **kwargs) as db:
            db.set_trace_callback(statements.append)
            yield db

    monkeypatch.setattr(store, "connect", traced_connect)
    result = store.list_entries(CASHIER, "5", state, pagination=pagination, page=999)
    assert result["items"] == result["sections"] == []
    assert result["total"] == result["amount"] == result["section_total"] == 0
    assert result["payable_count"] == result["payable_amount"] == 0
    assert result["risk_blocked_count"] == result["risk_blocked_amount"] == 0
    assert result["page"] == result["page_count"] == 1
    assert result["page_size"] == (5 if pagination == "inviters" else 100)
    assert any("FROM users WHERE" in statement for statement in statements)
    assert not any(
        fragment in statement
        for statement in statements
        for fragment in (
            "instr(",
            "SELECT entry_id,action,after_json FROM audit",
            "SELECT inviter FROM cashback_inviter_risk",
        )
    )
    assert not any(
        statement.startswith("SELECT inviter,COUNT") for statement in statements
    )
    with pytest.raises(CashbackError, match="forbidden"):
        store.list_entries("missing@example.com", "5", state, pagination=pagination)
    with pytest.raises(CashbackError, match="group_forbidden"):
        store.list_entries(CASHIER, "missing", state, pagination=pagination)
    with store.connect(True) as db:
        db.execute("UPDATE users SET active=0 WHERE email=?", (CASHIER,))
    with pytest.raises(CashbackError, match="forbidden"):
        store.list_entries(CASHIER, "5", state, pagination=pagination)


def test_paid_reads_skip_attribution_scan_without_hiding_registered_history(
    store, monkeypatch
):
    pagination_records(store, {"1": 3}, "paid")
    with store.connect(True) as db:
        db.execute("DELETE FROM evidence")
    statements = []
    original_connect = store.connect

    @contextmanager
    def traced_connect(*args, **kwargs):
        with original_connect(*args, **kwargs) as db:
            db.set_trace_callback(statements.append)
            yield db

    monkeypatch.setattr(store, "connect", traced_connect)
    result = store.list_entries(CASHIER, "5", "paid", pagination="inviters")
    assert result["total"] == 3 and result["amount"] == 1500
    assert len(result["items"]) == 3
    assert not any(
        "EXISTS (SELECT 1 FROM current_invitation_evidence" in statement
        for statement in statements
    )
    assert store.list_entries(CASHIER, "5")["total"] == 0


def test_inviter_totals_and_outer_page_share_one_aggregation(store, monkeypatch):
    pagination_records(store, {str(n): 2 for n in range(1, 13)})
    statements = []
    original_connect = store.connect

    @contextmanager
    def traced_connect(*args, **kwargs):
        with original_connect(*args, **kwargs) as db:
            db.set_trace_callback(statements.append)
            yield db

    monkeypatch.setattr(store, "connect", traced_connect)
    result = store.list_entries(CASHIER, "5", pagination="inviters", page=999)
    assert result["total"] == 24 and result["amount"] == 12000
    assert result["page"] == result["page_count"] == 3
    assert len(result["sections"]) == 2
    aggregation = [
        statement for statement in statements if statement.startswith("WITH grouped")
    ]
    assert len(aggregation) == 1
    assert not any("COUNT(DISTINCT inviter)" in statement for statement in statements)
    with original_connect() as db:
        plan = [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + aggregation[0])]
    assert sum("SEARCH entries " in step for step in plan) == 1


def test_grouped_name_uses_latest_filtered_record_with_same_time_tie(store):
    pagination_records(store, {"1": 3})
    with store.connect(True) as db:
        db.execute("UPDATE entries SET peer='missing' WHERE member='1'")
        db.execute("UPDATE entries SET first_seen=100 WHERE eligible=1")
        db.execute(
            "UPDATE entries SET id=CASE member WHEN '100000' THEN 'z-oldest' "
            "WHEN '100001' THEN 'y-earlier' WHEN '100002' THEN 'a-latest' ELSE id END "
            "WHERE eligible=1"
        )
        db.execute("UPDATE entries SET inviter_name='Earlier' WHERE member='100001'")
        db.execute("UPDATE entries SET inviter_name='Latest' WHERE member='100002'")
    all_rows = store.list_entries(CASHIER, "5", pagination="inviters")
    filtered = store.list_entries(CASHIER, "5", pagination="inviters", search="Earlier")
    assert all_rows["sections"][0]["inviter_name"] == "Latest"
    assert filtered["sections"][0]["inviter_name"] == "Earlier"


@pytest.mark.parametrize(
    "query",
    [
        {"pagination": "unknown"},
        {"pagination": "inviters", "member_page": 0},
        {"pagination": "inviters", "member_page": True},
        {"pagination": "inviters", "inviter": "1' OR 1=1--"},
        {"pagination": "inviters", "inviter": ["1"]},
        {"pagination": "entries", "inviter": "1"},
        {"pagination": "inviters", "member_page": 2},
    ],
)
def test_invalid_grouped_queries_fail_closed(store, query):
    with pytest.raises(CashbackError, match="invalid_query"):
        store.list_entries(CASHIER, "5", **query)


def test_five_yuan_correction_backs_up_and_preserves_ledger_state(store, tmp_path):
    from scripts.correct_wsl_cashback_amounts import correct_amounts

    scan(
        store,
        ("1", "2", "3", "4", "5", "6", "8"),
        [evidence(uid, event=f"event-{uid}") for uid in ("2", "3", "4", "5", "6", "8")],
    )
    with store.connect(True) as db:
        db.execute(
            "UPDATE entries SET state='paid',paid_by=?,paid_at=?,amount=600 WHERE member='2'",
            (CASHIER, time.time()),
        )
        db.execute(
            "UPDATE entries SET amount=0,member_class='unclassified',"
            "pricing_status='unclassified' WHERE member='3'"
        )
        db.execute("UPDATE entries SET amount=500 WHERE member='4'")
        db.execute("UPDATE entries SET amount=300,self_join=1 WHERE member='5'")
        db.execute("UPDATE entries SET amount=600,eligible=0 WHERE member='6'")
        db.execute("UPDATE entries SET amount=300,state='void' WHERE member='8'")
        db.execute("UPDATE groups SET agent_rate=1000 WHERE id='5'")
        original = {row["id"]: dict(row) for row in db.execute("SELECT * FROM entries")}
        audit_count = db.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
    preview = correct_amounts(store.path, ["5"])
    assert preview["entries_changed"] == 2
    assert preview["previous_amounts"] == {600: 1, 300: 1}
    assert not preview["applied"]
    assert row(store)["amount"] == 600
    result = correct_amounts(
        store.path, ["5"], apply=True, backup_root=tmp_path / "backups"
    )
    assert result["applied"]
    assert result["states"] == {"paid": 1, "void": 1}
    with sqlite3.connect(result["backup"]) as backup:
        backup.row_factory = sqlite3.Row
        assert {
            item["id"]: dict(item) for item in backup.execute("SELECT * FROM entries")
        } == original
        assert backup.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == audit_count
    with store.connect() as db:
        for item in db.execute("SELECT * FROM entries"):
            before = original[item["id"]]
            if item["member"] in {"2", "8"}:
                before = {**before, "amount": 500, "version": before["version"] + 1}
            assert dict(item) == before
        assert {
            r["rate"] for r in db.execute("SELECT rate FROM reward_rule_history")
        } == {500}
        assert db.execute("SELECT agent_rate FROM groups").fetchone()[0] == 1000
        corrections = db.execute(
            "SELECT * FROM audit WHERE action='amount_corrected'"
        ).fetchall()
        assert len(corrections) == 2
        assert all(json.loads(r["after_json"])["amount"] == 500 for r in corrections)
    rerun = correct_amounts(
        store.path, ["5"], apply=True, backup_root=tmp_path / "backups"
    )
    assert rerun["entries_changed"] == 0
    assert not rerun["applied"]
    assert rerun["backup"] is None
    scan(store, ("1", "2", "3", "4", "5", "6", "8", "9"), [evidence("9", event="late")])
    assert row(store, "9")["amount"] == 500
    assert row(store)["amount"] == 500
    assert row(store)["state"] == "paid"


def test_five_yuan_correction_rolls_back_on_audit_failure(store, tmp_path, monkeypatch):
    from scripts.correct_wsl_cashback_amounts import correct_amounts

    scan(store)
    original = row(store)
    original_rate = store.get_group("5")["rate"]

    def fail_audit(*args, **kwargs):
        raise RuntimeError("audit_unavailable")

    monkeypatch.setattr(CashbackStore, "_audit", fail_audit)
    with pytest.raises(RuntimeError, match="audit_unavailable"):
        correct_amounts(store.path, ["5"], apply=True, backup_root=tmp_path / "backups")
    assert row(store) == original
    assert store.get_group("5")["rate"] == original_rate


def test_five_yuan_correction_requires_backup_and_known_scope(store, tmp_path):
    from scripts.correct_wsl_cashback_amounts import correct_amounts

    scan(store)
    original = row(store)
    with pytest.raises(ValueError, match="backup"):
        correct_amounts(store.path, ["5"], apply=True)
    with pytest.raises(ValueError, match="Unknown group"):
        correct_amounts(
            store.path, ["5", "6"], apply=True, backup_root=tmp_path / "backups"
        )
    assert row(store) == original


def test_ledger_period_filters_use_beijing_first_seen_boundaries(store):
    scan(
        store,
        ("1", "2", "3", "4"),
        [
            evidence("2", event="event-2"),
            evidence("3", event="event-3"),
            evidence("4", event="event-4"),
        ],
    )
    now = time.time()
    month_start, _ = entry_time_window("month", now)
    day_start, _ = entry_time_window("day", now)
    hour_start, hour_end = entry_time_window("hour", now)
    with store.connect(True) as db:
        db.execute(
            "UPDATE entries SET first_seen=? WHERE member='2'",
            (day_start if hour_start != day_start else day_start + 3600,),
        )
        db.execute(
            "UPDATE entries SET first_seen=? WHERE member='3'",
            (hour_start + 30,),  # current Beijing hour
        )
        db.execute(
            "UPDATE entries SET first_seen=? WHERE member='4'",
            (month_start - 1,),  # previous month
        )
    assert store.list_entries(CASHIER, "5", period="all")["total"] == 3
    assert store.list_entries(CASHIER, "5", period="month")["total"] == 2
    assert store.list_entries(CASHIER, "5", period="day")["total"] == 2
    assert store.list_entries(CASHIER, "5", period="hour")["total"] == 1
    assert (
        store.list_entries(
            CASHIER,
            "5",
            period="custom",
            start_at=hour_start,
            end_at=hour_end,
        )["total"]
        == 1
    )
    assert hour_end - hour_start == 3600
    with pytest.raises(CashbackError, match="invalid_query"):
        store.list_entries(CASHIER, "5", period="week")
    with pytest.raises(CashbackError, match="invalid_query"):
        store.list_entries(CASHIER, "5", period="custom", start_at=hour_start)
    with pytest.raises(CashbackError, match="invalid_query"):
        store.list_entries(
            CASHIER,
            "5",
            period="custom",
            start_at=hour_end,
            end_at=hour_start,
        )
    with pytest.raises(CashbackError, match="invalid_query"):
        store.list_entries(
            CASHIER, "5", period="all", start_at=hour_start, end_at=hour_end
        )


@pytest.fixture
def assignment_store(store):
    store.configure(
        OWNER,
        {**store.get_group("5"), "enabled": True, "qualification_seconds": 10800},
        {"1": member("1")},
        set(),
    )
    scan(store, ("1", "2", "3"), [evidence("3", "1", "known-inviter")])
    return store


def assign(store, **changes):
    args = {
        "actor": CASHIER,
        "group": "5",
        "operation": "assign",
        "items": [select(store)],
        "request_id": "manual-attribution-001",
        "reason": "Cashier confirmed the recruiter privately",
        "inviter": "1",
        "members": {uid: member(uid) for uid in ("1", "2", "3")},
    }
    return store.mutate(**{**args, **changes})


def test_unassigned_view_and_exact_existing_inviter(assignment_store):
    store = assignment_store
    before = row(store)
    assert store.list_entries(CASHIER, "5", "unassigned")["total"] == 1
    assert store.list_entries(CASHIER, "5", "unassigned")["payable_amount"] == 0
    assert store.inviter_options(CASHIER, "5") == [{"uid": "1", "name": "成员1"}]
    result = assign(store)
    assert result["state"] == "verifying"
    after = row(store)
    assert after["inviter"] == "1" and after["verification"] == "verified"
    assert after["hold_until"] == before["hold_until"]
    assert after["first_seen"] == before["first_seen"]
    assert store.list_entries(CASHIER, "5", "unassigned")["total"] == 0
    assert store.list_entries(CASHIER, "5", "verifying")["total"] == 2
    scan(store, ("1", "2", "3"), [evidence(source="nim_notification")])
    assert row(store)["verification"] == "verified"
    assert row(store)["inviter"] == "1"
    assert (
        store.private_invitation_summaries("1", "bot-a", "99", ["5"])[0]["total"] == 2
    )
    assert any(a["action"] == "inviter_assigned" for a in store.audit(OWNER, "5"))
    assert (
        assign(store, items=[{"id": before["id"], "version": before["version"]}])
        == result
    )
    with pytest.raises(CashbackError, match="idempotency_conflict"):
        assign(
            store, request_id="manual-attribution-001", reason="Different confirmation"
        )


def test_qualified_manual_assignment_transfers_pending_and_can_pay(assignment_store):
    store = assignment_store
    with store.connect(True) as db:
        db.execute(
            "UPDATE entries SET hold_until=? WHERE member='2'", (time.time() - 1,)
        )
    assert assign(store)["state"] == "pending"
    assert pay(store)["count"] == 1
    assert row(store)["state"] == "paid"
    assert store.invitation_summary("5", "1")["paid"] == 1800
    assert (
        store.private_invitation_summaries("1", "bot-a", "99", ["5"])[0]["paid"] == 1800
    )
    restored = CashbackStore(store.path)
    assert row(restored)["state"] == "paid"
    scan(restored, ("1", "2", "3"), [evidence("3", "1", "known-inviter")])
    assert row(restored)["inviter"] == "1"


def test_manual_confirmation_does_not_skip_qualification(assignment_store):
    store = assignment_store
    assign(store)
    with pytest.raises(CashbackError, match="qualification_required"):
        pay(store)
    before = row(store)
    deadline = before["hold_until"]
    with store.connect(True) as db:
        db.execute("UPDATE entries SET last_seen=? WHERE member='2'", (deadline - 5,))
    store.snapshot(
        "5",
        "bot-a",
        "99",
        {uid: member(uid) for uid in ("1", "2", "3")},
        [],
        now=deadline + 1,
    )
    assert row(store)["state"] == "pending"


def test_legacy_unknown_assignment_restarts_observation(assignment_store):
    store = assignment_store
    with store.connect(True) as db:
        db.execute(
            "UPDATE entries SET member_class='legacy',hold_until=? WHERE member='2'",
            (time.time() - 100,),
        )
    assert assign(store)["state"] == "verifying"
    assert row(store)["hold_until"] >= time.time() + 10799
    entry = next(
        r
        for r in store.list_entries(CASHIER, "5", "verifying")["items"]
        if r["member"] == "2"
    )
    assert entry["qualification_reason"] == "manual_confirmation"


def test_manual_paid_attribution_keeps_original_payment(assignment_store):
    store = assignment_store
    with store.connect(True) as db:
        db.execute(
            "UPDATE entries SET state='paid',verification='conflict',paid_at=?,paid_by=?,"
            "amount=700,hold_until=0 WHERE member='2'",
            (time.time(), CASHIER),
        )
    before = row(store)
    assert assign(store)["state"] == "paid"
    scan(store, ("1", "2", "3"), [evidence("3", "1", "known-inviter")])
    after = row(store)
    for key in (
        "state",
        "amount",
        "paid_at",
        "paid_by",
        "first_seen",
        "ever_left",
        "hold_until",
    ):
        assert after[key] == before[key]
    assert store.list_entries(CASHIER, "5", "pending")["total"] == 0
    assert (
        store.private_invitation_summaries("1", "bot-a", "99", ["5"])[0]["paid"] == 700
    )
    with pytest.raises(CashbackError, match="qualification_required|not_payable"):
        pay(store)


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"actor": "unregistered@example.com"}, "forbidden"),
        ({"inviter": "2"}, "invalid_assignment"),
        ({"inviter": "99"}, "inviter_unavailable"),
        ({"inviter": "7"}, "inviter_unavailable"),
        ({"inviter": "' OR 1=1"}, "invalid_assignment"),
        ({"inviter": None}, "invalid_assignment"),
        ({"members": None}, "inviter_unavailable"),
        ({"members": {"1": {**member("1"), "eligible": False}}}, "inviter_unavailable"),
        (
            {"members": {"1": {**member("1"), "peer": "9999"}, "2": member("2")}},
            "identity_conflict",
        ),
        (
            {"members": {"1": member("1"), "2": {**member("2"), "peer": "9999"}}},
            "identity_conflict",
        ),
        ({"members": {"1": member("1")}}, "assignment_forbidden"),
        ({"reason": ""}, "reason_required"),
        ({"items": [{"id": "bad", "version": 1}]}, "conflict"),
        ({"items": []}, "invalid_assignment"),
    ],
)
def test_manual_assignment_rejects_unsafe_inputs(assignment_store, changes, code):
    store = assignment_store
    before = row(store)
    with pytest.raises(CashbackError, match=code):
        assign(store, **changes)
    assert row(store) == before


@pytest.mark.parametrize(
    "sql,code",
    [
        (
            "UPDATE entries SET state='void',ever_left=1 WHERE member='2'",
            "assignment_forbidden",
        ),
        ("UPDATE entries SET present=0 WHERE member='2'", "assignment_forbidden"),
        ("UPDATE entries SET eligible=0 WHERE member='2'", "conflict"),
        ("UPDATE entries SET self_join=1 WHERE member='2'", "not_payable"),
        (
            "UPDATE entries SET verification='blocked' WHERE member='2'",
            "assignment_forbidden",
        ),
        ("UPDATE entries SET inviter='1' WHERE member='2'", "assignment_forbidden"),
        ("UPDATE groups SET error='roster_failed' WHERE id='5'", "roster_unavailable"),
        ("UPDATE groups SET scanned=0 WHERE id='5'", "roster_unavailable"),
        (
            "UPDATE groups SET source_error='decode_failed' WHERE id='5'",
            "invitation_source_unavailable",
        ),
        (
            "UPDATE entries SET state='paid',paid_by='other@example.com',paid_at=1 WHERE member='2'",
            "assignment_forbidden",
        ),
    ],
)
def test_manual_assignment_preserves_ineligible_and_paid_ownership(
    assignment_store, sql, code
):
    with assignment_store.connect(True) as db:
        db.execute(sql)
    before = row(assignment_store)
    with pytest.raises(CashbackError, match=code):
        assign(assignment_store)
    assert row(assignment_store) == before


def test_manual_assignment_does_not_override_upstream_evidence(assignment_store):
    store = assignment_store
    with store.connect(True) as db:
        db.execute(
            "INSERT INTO evidence(group_id,member,source,event_id,inviter,observed,account,"
            "member_peer,inviter_peer,event_at) VALUES('5','2','business_apply_log','other','3',?,'99','1002','1003',?)",
            (time.time(), time.time()),
        )
    with pytest.raises(CashbackError, match="attribution_conflict"):
        assign(store)


def test_manual_assignment_rejects_unmapped_decoded_inviter(assignment_store):
    store = assignment_store
    rule = store.get_group("5")
    store.remember_source_page(
        "5",
        "bot-a",
        "99",
        rule["started"],
        {
            "items": [
                {
                    "apply_id": "123",
                    "state": "MEMBER_STATE_GOOD",
                    "member_id": "2",
                    "member_nim_id": "1002",
                    "inviter_id": "7",
                    "inviter_nim_id": "1007",
                    "apply_time": datetime.now(timezone.utc).isoformat(),
                }
            ],
            "complete": False,
            "last_id": "123",
        },
    )
    with pytest.raises(CashbackError, match="attribution_conflict"):
        assign(store)


def test_later_real_invitation_conflicts_with_manual_confirmation(assignment_store):
    store = assignment_store
    assign(store)
    scan(store, ("1", "2", "3"), [evidence("2", "3", "real-conflict")])
    assert row(store)["verification"] == "conflict"
    assert row(store)["inviter"] == "1"
    with pytest.raises(CashbackError, match="not_payable"):
        pay(store)


def test_qualification_restart_reports_actual_clock(assignment_store):
    store = assignment_store
    before = row(store, "3")
    new_time = time.time() + 130
    store.snapshot(
        "5",
        "bot-a",
        "99",
        {uid: member(uid) for uid in ("1", "2", "3")},
        [],
        now=new_time,
    )
    data = store.list_entries(CASHIER, "5", "verifying")["items"][0]
    assert data["first_seen"] == before["first_seen"]
    assert data["qualification_started"] == pytest.approx(new_time)
    assert data["qualification_reason"] == "observation_gap"


def test_live_revision_does_not_expose_other_groups(store):
    store.operation_log("secret", group="6")
    assert all(group == "5" for group, _ in store.live_revision(CASHIER, ["5"]))
    with pytest.raises(CashbackError):
        store.live_revision(CASHIER, ["6"])


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["pay", "correct"])
async def test_service_payment_revalidates_and_retry_does_not_rescan(store, operation):
    scan(store)
    if operation == "correct":
        pay(store)
    service = CashbackService.__new__(CashbackService)
    service.store, service.locks = store, {}
    service.selected_groups = lambda: {"5"}
    adapter = SimpleNamespace(
        account="99",
        config={
            "id": "bot-a",
            "enable": True,
            "_business_active": True,
            "_business_epoch": 1,
        },
    )
    service.adapter = lambda _: adapter
    service._scan = AsyncMock()
    items = [select(store)]
    result = await service.mutate(
        CASHIER, "5", operation, items, "service-payment-001", "登记核对"
    )
    service._scan.assert_awaited_once()
    service._scan.side_effect = RuntimeError("offline")
    assert (
        await service.mutate(
            CASHIER, "5", operation, items, "service-payment-001", "登记核对"
        )
        == result
    )


@pytest.mark.asyncio
async def test_service_denies_out_of_scope_before_roster(store):
    service = CashbackService.__new__(CashbackService)
    service.store, service.locks = store, {}
    service.selected_groups = lambda: {"5"}
    service._scan = AsyncMock()
    scan(store)
    with pytest.raises(CashbackError):
        await service.mutate(OTHER, "6", "pay", [select(store)], "service-payment-001")
    service._scan.assert_not_awaited()


@pytest.mark.asyncio
async def test_suspended_evidence_recovery_and_collector_switch(
    store, tmp_path, monkeypatch
):
    ledger = Ledger(tmp_path / "transport.sqlite3")
    await ledger.open()
    started = time.time()
    adapter = SimpleNamespace(
        account="99",
        config={"id": "bot-a", "_business_active": False, "_business_epoch": 1},
        ledger=ledger,
        get_group_invitation_records=AsyncMock(
            return_value={
                "items": [
                    {
                        "member_id": "2",
                        "inviter_id": "1",
                        "member_nim_id": "1002",
                        "inviter_nim_id": "1001",
                        "apply_id": "100",
                        "apply_time": str(started),
                        "state": "MEMBER_STATE_GOOD",
                    }
                ]
            }
        ),
    )
    service = CashbackService.__new__(CashbackService)
    service.store, service.business_checked = store, {}
    service.check_selected = lambda group: None
    service.adapter = lambda bot: adapter
    service.roster = AsyncMock(return_value={u: member(u) for u in ("1", "2")})
    service.activities = SimpleNamespace(authorize=lambda *args: None)
    try:
        await ledger.ingest(
            "99",
            "5",
            "during-pause",
            {
                "kind": "team_invite",
                "inviter_peer": "1001",
                "member_peers": ["1002"],
                "event_at": started,
                "business_suspended": True,
            },
        )
        await ledger.mark("99", "5", "during-pause", "invite_notification")
        with pytest.raises(CashbackError, match="business_service_inactive"):
            await service._scan("5")
        with store.connect() as db:
            assert (
                db.execute("SELECT COUNT(*) FROM entries WHERE member='2'").fetchone()[
                    0
                ]
                == 0
            )
        store.configure(
            OWNER,
            {**store.get_group("5"), "enabled": True, "rate": 2500},
            {"1": member("1")},
            set(),
            now=started + 1,
        )
        monkeypatch.setattr(time, "time", lambda: started + 2)
        adapter.config.update(_business_active=True, _business_epoch=2)
        await service._scan("5")
        assert row(store)["verification"] == "verified"
        assert row(store)["amount"] == 1800
        original = row(store)["id"]
        store.configure(
            OWNER,
            {
                **store.get_group("5"),
                "enabled": True,
                "bot_id": "bot-b",
                "account": "88",
            },
            service.roster.return_value,
            set(),
        )
        with pytest.raises(CashbackError, match="collector_changed"):
            await service._scan("5")
        adapter.account = "88"
        adapter.config["id"] = "bot-b"
        await service._scan("5")
        assert row(store)["id"] == original
        assert row(store)["amount"] == 1800
        service.roster.return_value = {"1": member("1")}
        await service._scan("5")
        service.roster.return_value = {u: member(u) for u in ("1", "2")}
        await service._scan("5")
        assert row(store)["id"] == original
        assert row(store)["state"] == "void"
        assert row(store)["ever_left"] == 1
    finally:
        await ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("resumed", [False, True])
async def test_scan_rejects_pause_during_platform_request(store, resumed):
    adapter = SimpleNamespace(
        account="99",
        config={"id": "bot-a", "_business_active": True, "_business_epoch": 1},
    )
    service = CashbackService.__new__(CashbackService)
    service.store = store
    service.check_selected = lambda group: None
    service.adapter = lambda bot: adapter
    service.roster = AsyncMock(return_value={u: member(u) for u in ("1", "2")})
    service.activities = SimpleNamespace(authorize=lambda *args: None)

    async def receive_after_pause(*args):
        adapter.config.update(_business_active=resumed, _business_epoch=2)
        return [evidence()]

    service.evidence = receive_after_pause
    with pytest.raises(CashbackError, match="business_service_inactive"):
        await service._scan("5")
    with store.connect() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM entries WHERE member='2'").fetchone()[0]
            == 0
        )


@pytest.mark.asyncio
async def test_real_collector_timestamps_and_business_approval(tmp_path, store):
    ledger = Ledger(tmp_path / "messages.sqlite3")
    await ledger.open()
    now = time.time()
    started = store.get_group("5")["started"]
    try:
        for message, stamp in [
            ("fresh", now * 1000),
            ("old", (now - 500) * 1000),
            ("unknown", 0),
        ]:
            await ledger.ingest(
                "99",
                "5",
                message,
                {
                    "kind": "team_invite",
                    "inviter_peer": "1001",
                    "member_peers": ["1002"],
                    "event_at": stamp,
                },
            )
            await ledger.mark("99", "5", message, "invite_notification")
        item = {
            "member_id": "3",
            "inviter_id": "1",
            "member_nim_id": "1003",
            "inviter_nim_id": "1001",
            "apply_id": "100",
            "apply_time": str(now),
            "state": "MEMBER_STATE_GOOD",
        }
        adapter = SimpleNamespace(
            account="99",
            config={"id": "bot-a"},
            ledger=ledger,
            get_group_invitation_records=AsyncMock(
                return_value={
                    "items": [
                        item,
                        {
                            **item,
                            "member_id": "4",
                            "member_nim_id": "1004",
                            "state": "MEMBER_STATE_INVITED",
                        },
                    ]
                }
            ),
        )
        service = CashbackService.__new__(CashbackService)
        service.store, service.business_checked = store, {}
        result = await service.evidence(
            adapter, "5", {u: member(u) for u in ("1", "2", "3", "4")}, started
        )
        assert [(r["source"], r["member"]) for r in result] == [
            ("nim_notification", "2"),
            ("business_apply_log", "3"),
        ]
        adapter.get_group_invitation_records.assert_awaited_once_with(
            "5", pending_only=False, last_id=str(int(started) - 1)
        )
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_business_source_uses_enable_time_cursor_without_losing_boundary(store):
    started = store.get_group("5")["started"]
    item = {
        "member_id": "2",
        "inviter_id": "1",
        "member_nim_id": "1002",
        "inviter_nim_id": "1001",
        "apply_id": "100",
        "apply_time": str(started),
        "state": "MEMBER_STATE_GOOD",
        "member_name": "Fresh invitee",
    }
    departed = {
        **item,
        "member_id": "4",
        "member_nim_id": "1004",
        "apply_id": "101",
        "member_name": "Left before scan",
    }

    async def records(group, *, pending_only, last_id):
        assert group == "5" and not pending_only
        assert last_id == str(int(started) - 1)
        return {
            "items": [
                item,
                departed,
                {
                    **item,
                    "apply_id": "99",
                    "member_id": "3",
                    "member_nim_id": "1003",
                    "apply_time": str(started - 0.01),
                },
            ],
            "last_id": str(int(time.time())),
        }

    adapter = SimpleNamespace(
        account="99",
        config={"id": "bot-a"},
        ledger=None,
        get_group_invitation_records=AsyncMock(side_effect=records),
    )
    service = CashbackService.__new__(CashbackService)
    service.store, service.business_checked = store, {}
    result = await service.evidence(
        adapter, "5", {u: member(u) for u in ("1", "2", "3")}, started
    )
    assert len(result) == 2
    by_member = {proof["member"]: proof for proof in result}
    assert set(by_member) == {"2", "4"}
    assert by_member["2"]["event_at"] == started
    assert by_member["2"]["source"] == "business_apply_log"
    assert by_member["4"]["member_name"] == "Left before scan"
    assert (
        await service.evidence(
            adapter, "5", {u: member(u) for u in ("1", "2", "3")}, started
        )
        == result
    )
    assert adapter.get_group_invitation_records.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "reason"),
    [
        ("response_invalid", "response_invalid"),
        ("token=private-session", "source_unavailable"),
    ],
)
async def test_business_source_failure_logs_safe_reason_and_samples(
    store, message, reason
):
    adapter = SimpleNamespace(
        account="99",
        config={"id": "bot-a"},
        ledger=None,
        get_group_invitation_records=AsyncMock(side_effect=RuntimeError(message)),
    )
    service = CashbackService.__new__(CashbackService)
    service.store, service.business_checked = store, {}
    for _ in range(2):
        service.business_checked.clear()
        assert (
            await service.evidence(
                adapter, "5", {"1": member("1")}, store.get_group("5")["started"]
            )
            == []
        )
    assert adapter.get_group_invitation_records.await_count == 1
    logs = [
        row
        for row in store.audit(OWNER, "5")
        if row["action"] == "invitation_source_failed"
    ]
    assert len(logs) == 1
    details = json.loads(logs[0]["after_json"])
    assert details["reason_code"] == reason
    assert details["exception_type"] == "RuntimeError"
    assert "private-session" not in json.dumps(logs)


def test_historical_baseline_unions_invites_messages_and_cards(tmp_path, monkeypatch):
    from astrbot.builtin_stars.wangshangliao_moderation import cashback

    monkeypatch.setattr(cashback, "instance_dir", lambda _: tmp_path / "locator")
    root = tmp_path / "retired-bot"
    root.mkdir()
    with sqlite3.connect(root / "activities.sqlite3") as db:
        db.execute("CREATE TABLE invite_seen(group_id TEXT,member TEXT)")
        db.execute("INSERT INTO invite_seen VALUES('5','11')")
    with sqlite3.connect(root / "messages.sqlite3") as db:
        db.execute("CREATE TABLE inbox(team TEXT,payload TEXT)")
        db.execute("INSERT INTO inbox VALUES('5',?)", (json.dumps({"sender": "12"}),))
    with sqlite3.connect(root / "cards.sqlite3") as db:
        db.execute("CREATE TABLE card_seen(identity TEXT)")
        db.execute(
            "INSERT INTO card_seen VALUES(?)", (json.dumps(["99", "5", "13", "1013"]),)
        )
    assert CashbackService.historical_members("5") == {"11", "12", "13"}


@pytest.mark.parametrize("history_available", [False, True])
def test_delayed_attribution_uses_only_recorded_historical_price(
    store, history_available
):
    before_change = time.time()
    if not history_available:
        with store.connect(True) as db:
            db.execute("DELETE FROM reward_rule_history")
    config = {**store.get_group("5"), "enabled": True, "rate": 2500}
    store.configure(OWNER, config, {"1": member("1")}, set(), now=before_change + 1)
    store.snapshot(
        "5",
        "bot-a",
        "99",
        {"1": member("1"), "2": member("2")},
        [{**evidence(), "event_at": before_change}],
        now=before_change + 2,
    )
    if history_available:
        assert row(store)["verification"] == "verified"
        assert row(store)["amount"] == 1800
        reopened = CashbackStore(store.path)
        scan(reopened, notices=[{**evidence(), "event_at": before_change}])
        assert row(reopened)["amount"] == 1800
    else:
        assert row(store)["verification"] == "pending"
        with pytest.raises(CashbackError):
            pay(store)


def test_historical_evidence_never_rewrites_registered_payment(store):
    joined_at = time.time()
    config = {**store.get_group("5"), "enabled": True, "rate": 2500}
    store.configure(OWNER, config, {"1": member("1")}, set(), now=joined_at + 1)
    store.snapshot(
        "5",
        "bot-a",
        "99",
        {"1": member("1"), "2": member("2")},
        [evidence()],
        now=joined_at + 2,
    )
    pay(store)
    paid = row(store)
    store.snapshot(
        "5",
        "bot-a",
        "99",
        {"1": member("1"), "2": member("2")},
        [{**evidence(), "event_at": joined_at}],
        now=joined_at + 3,
    )
    after = row(store)
    assert after["state"] == "paid"
    assert after["amount"] == paid["amount"] == 2500
    assert after["paid_at"] == paid["paid_at"]
    assert after["verification"] == "conflict"


@pytest.mark.asyncio
async def test_emergency_pause_works_without_collector(store):
    service = CashbackService.__new__(CashbackService)
    service.store, service.locks = store, {}
    service.selected_groups = lambda: {"5"}
    service.collector_id = "bot-a"
    service.adapter = lambda _: pytest.fail("Do not require a collector to pause")
    config = {
        key: store.get_group("5")[key]
        for key in ("id", "bot_id", "cashier", "rate", "version")
    }
    await service.configure(OWNER, {**config, "enabled": False})
    assert not store.get_group("5")["enabled"]


def test_contact_stale_version_does_not_overwrite_current_profile(store):
    store.save_cashier(OWNER, "123", "原联系人", True, 0)
    store.save_cashier(OWNER, "123", "最新联系人", False, 1)
    with pytest.raises(CashbackError, match="conflict") as exc:
        store.save_cashier(OWNER, "123", "旧页面", True, 1)
    assert exc.value.status == 409
    assert "群内联系人" in exc.value.text
    current = store.cashier_profiles(OWNER)[0]
    assert (current["name"], current["active"], current["version"]) == (
        "最新联系人",
        0,
        2,
    )


def test_sender_cash_summary_uses_fixed_amounts_excludes_void(store):
    scan(store)
    result = store.invitation_summary("5", "1")
    assert result["pending"] == 1800 and result["paid"] == 0
    assert result["total"] == 1 and result["present"] == 1
    assert store.invitation_summary("5", "2")["total"] == 0
    assert store.invitation_summary("6", "1") is None
    config = {**store.get_group("5"), "enabled": True, "rate": 500}
    store.configure(OWNER, config, {"1": member("1"), "2": member("2")}, set())
    result = store.invitation_summary("5", "1")
    assert result["rate"] == 500 and result["pending"] == 1800
    scan(store, members=("1",))
    result = store.invitation_summary("5", "1")
    assert result["pending"] == 0 and result["void"] == 1
    scan(store)
    result = store.invitation_summary("5", "1")
    assert result["pending"] == 0 and result["void"] == 1
    assert result["present"] == 1


def test_sender_cash_summary_flags_conflict_without_counting_payable(store):
    scan(store, members=("1", "2", "3"))
    scan(
        store,
        members=("1", "2", "3"),
        notices=[evidence(inviter="3", event="disputed")],
    )
    result = store.invitation_summary("5", "1")
    assert result["pending"] == 0
    assert result["review"] + result["void"] == 1
    assert result["items"][0]["verification"] == "conflict"
    assert store.invitation_summary("5", "3")["total"] == 0


@pytest.mark.parametrize("unqualified", ["deadline", "price", "member_class"])
def test_pending_payable_amount_agrees_across_page_group_and_private(
    store, unqualified
):
    scan(store)
    with store.connect(True) as db:
        if unqualified == "deadline":
            db.execute(
                "UPDATE entries SET hold_until=? WHERE member='2'",
                (time.time() + 3600,),
            )
        elif unqualified == "price":
            db.execute(
                "UPDATE entries SET pricing_status='unconfigured' WHERE member='2'"
            )
        else:
            db.execute("UPDATE entries SET member_class='legacy' WHERE member='2'")
    listed = store.list_entries(CASHIER, "5")
    assert listed["total"] == 1
    assert listed["amount"] == 1800
    assert listed["payable_count"] == 0 and listed["payable_amount"] == 0
    summary = store.invitation_summary("5", "1")
    assert summary["total"] == 1
    assert summary["pending"] == 0 and summary["review"] == 1
    assert (
        store.private_invitation_summaries("1", "bot-a", "99", ["5"])[0]["pending"] == 0
    )


@pytest.mark.parametrize("invalid_proof", ["operator", "account", "self_join"])
def test_chat_summaries_do_not_count_unproven_or_self_entered_members(
    store, invalid_proof
):
    scan(store)
    with store.connect(True) as db:
        if invalid_proof == "operator":
            db.execute("UPDATE evidence SET source='nim_notification'")
        elif invalid_proof == "account":
            db.execute("UPDATE evidence SET account='100'")
        else:
            db.execute("UPDATE entries SET self_join=1 WHERE member='2'")
    summary = store.invitation_summary("5", "1")
    assert summary["total"] == 0
    assert summary["pending"] == 0 and summary["items"] == []
    assert (
        store.private_invitation_summaries("1", "bot-a", "99", ["5"])[0]["total"] == 0
    )
    assert store.list_entries(CASHIER, "5")["payable_count"] == 0


def test_disabled_contact_does_not_block_stopping_rewards(store):
    store.save_cashier(OWNER, "123", "联系人", True, 0)
    config = {**store.get_group("5"), "cashier": "wsl:123", "enabled": True}
    store.configure(OWNER, config, {"1": member("1")}, set())
    store.save_cashier(OWNER, "123", "联系人", False, 1)
    config = {**store.get_group("5"), "enabled": False}
    store.configure(OWNER, config, None, set())
    assert store.get_group("5")["enabled"] == 0
    config = {**store.get_group("5"), "enabled": True}
    with pytest.raises(CashbackError, match="contact_unavailable"):
        store.configure(OWNER, config, {"1": member("1")}, set())


def test_contacts_do_not_grant_login_or_restrict_global_cashiers(store):
    store.save_cashier(OWNER, "123", "同名成员", True, 0)
    store.save_cashier(OWNER, "456", "同名成员", True, 0)
    profiles = store.cashier_profiles(OWNER)
    assert {p["uid"] for p in profiles} == {"123", "456"}
    assert all("email" not in p for p in profiles)
    identity = store.identify("new-cashier@example.com", "verified-subject")
    assert identity["uid"] == ""
    assert identity["email"] == "new-cashier@example.com"
    assert identity["role"] == "cashier"
    assert store.groups(identity["email"])[0]["id"] == "5"
    with pytest.raises(CashbackError):
        store.identify("new-cashier@example.com", "another-subject")
    store.save_cashier(OWNER, "123", "同名成员", False, 1)
    assert store.identify("new-cashier@example.com", "verified-subject") == identity
    with pytest.raises(CashbackError):
        store.list_entries("wsl:123", "5")


def test_legacy_binding_preserves_paid_by_and_restart_contacts(store):
    with store.connect(True) as db:
        db.execute(
            "INSERT INTO users(email,uid,name,role,subject) VALUES('wsl:123','123','老结账员','cashier','legacy-sub')"
        )
        db.execute(
            "INSERT INTO login_bindings VALUES('legacy-sub','legacy@example.com','wsl:123',?)",
            (time.time(),),
        )
    scan(store)
    pay(store, actor="wsl:123")
    restarted = CashbackStore(store.path)
    assert restarted.identify("legacy@example.com", "legacy-sub")["email"] == "wsl:123"
    assert restarted.cashier_profiles(OWNER)[0]["uid"] == "123"
    restarted.save_cashier(OWNER, "123", "新联系人昵称", False, 1)
    assert restarted.identify("legacy@example.com", "legacy-sub")["role"] == "cashier"
    pay(
        restarted,
        actor="wsl:123",
        operation="correct",
        reason="测试更正",
        request="legacy-correction-request",
    )
    assert row(restarted)["state"] == "pending"
    assert any(r["action"] == "pay" for r in restarted.audit(OWNER, "5"))


def test_log_scope_redaction_and_filters(store):
    store.operation_log(
        "read_failed",
        group="5",
        level="error",
        code="roster_unavailable",
        trace="a" * 32,
        detail={
            "exception_type": "TimeoutError",
            "token": "secret",
            "password": "private",
            "body": "payment account",
        },
    )
    logs = store.audit(CASHIER, "5", level="error", trace="a" * 32)
    assert len(logs) == 1
    assert "secret" not in json.dumps(logs)
    assert "payment account" not in json.dumps(logs)
    assert logs[0]["code"] == "roster_unavailable"
    assert store.audit(OTHER, "5")
    with pytest.raises(CashbackError):
        store.audit(CASHIER, "")


def test_all_configured_groups_available_but_admin_actions_remain_restricted(store):
    config = {
        **store.get_group("5"),
        "id": "6",
        "cashier": OTHER,
        "enabled": True,
        "version": 0,
    }
    store.configure(OWNER, config, {"1": member("1")}, set())
    store.snapshot(
        "6", "bot-a", "99", {"1": member("1"), "2": member("2")}, [evidence()]
    )
    identity = store.identify("all-groups@example.com", "all-groups-sub")
    actor = identity["email"]
    assert {g["id"] for g in store.groups(actor)} == {"5", "6"}
    store.mutate(
        actor, "6", "pay", [select(store, group="6")], "multi-group-cashier-pay"
    )
    assert row(store, group="6")["paid_by"] == actor
    assert store.audit(actor, "6")
    for call in (
        lambda: store.configure(actor, config, {"1": member("1")}, set()),
        lambda: store.save_cashier(actor, "123", "联系人", True, 0),
        lambda: store.users(actor),
        lambda: store.mutate(
            actor,
            "6",
            "exception",
            [select(store, group="6")],
            "not-admin-exception",
            "测试",
        ),
    ):
        with pytest.raises(CashbackError):
            call()


def test_collector_groups_follow_only_selected_bot(store):
    adapter = SimpleNamespace(
        config={"type": "wangshangliao", "id": "chosen", "enabled_groups": ["5", "6"]},
        group_names={"5": "测试群", "6": "第二群"},
    )
    other = SimpleNamespace(
        config={"type": "wangshangliao", "id": "other", "enabled_groups": ["7"]}
    )
    service = CashbackService.__new__(CashbackService)
    service.store, service.collector_id = store, "chosen"
    service.context = SimpleNamespace(
        platform_manager=SimpleNamespace(platform_insts=[adapter, other])
    )
    assert {r["id"] for r in service.catalog(OWNER)} == {"5", "6"}
    assert {r["id"] for r in service.visible_groups(OWNER)} == {"5", "6"}
    assert {r["id"] for r in service.visible_groups(CASHIER)} == {"5"}
    with pytest.raises(CashbackError):
        service.check_selected("7")
    adapter.config["enabled_groups"] = ["6"]
    with pytest.raises(CashbackError):
        service.check_selected("5")


@pytest.mark.asyncio
async def test_configure_text_contact_requires_no_member_or_login_binding(store):
    adapter = SimpleNamespace(
        config={"type": "wangshangliao", "id": "chosen", "enabled_groups": ["5", "6"]},
        account="99",
        group_names={"5": "测试群", "6": "第二群"},
    )
    service = CashbackService.__new__(CashbackService)
    service.store = store
    service.collector_id = "chosen"
    service.context = SimpleNamespace(
        platform_manager=SimpleNamespace(platform_insts=[adapter])
    )
    service.locks = {}
    service.roster = AsyncMock(return_value={"1": member("1")})
    service.evidence = AsyncMock(return_value=[])
    service.historical_members = lambda _group: set()
    config = {
        **store.get_group("5"),
        "id": "6",
        "bot_id": "chosen",
        "account": "99",
        "contact": "微信：outside_group_cashier",
        "enabled": True,
        "version": 0,
    }
    config.pop("cashier")
    before = store.users(OWNER)
    await service.configure(OWNER, config)
    assert store.get_group("6")["contact"] == "微信：outside_group_cashier"
    assert store.get_group("5")["contact"] == ""
    assert store.users(OWNER) == before
    assert store.cashier_profiles(OWNER) == []
    assert store.invitation_summary("6", "1")["contact_name"] == config["contact"]


def test_text_contact_migration_preserves_money_and_does_not_rebind(store):
    store.save_cashier(OWNER, "123", "旧联系人", True, 0)
    store.configure(
        OWNER,
        {**store.get_group("5"), "enabled": True, "cashier": "wsl:123"},
        {"1": member("1")},
        set(),
    )
    scan(store)
    store.mutate(CASHIER, "5", "pay", [select(store)], "before-contact-migration")
    original = row(store)
    with store.connect(True) as db:
        db.execute("ALTER TABLE groups DROP COLUMN contact")
    restarted = CashbackStore(store.path)
    assert restarted.get_group("5")["contact"] == "旧联系人"
    assert row(restarted) == original
    config = {
        **restarted.get_group("5"),
        "enabled": True,
        "contact": "微信：new_contact",
    }
    restarted.configure(OWNER, config, None, set())
    assert row(restarted) == original
    assert CashbackStore(store.path).get_group("5")["contact"] == "微信：new_contact"


def test_multi_group_prices_payments_and_departures_are_independent(store):
    config = {**store.get_group("5"), "enabled": True, "rate": 500}
    store.configure(OWNER, config, {"1": member("1")}, set())
    store.configure(
        OWNER,
        {**config, "id": "6", "rate": 800, "version": 0},
        {"1": member("1")},
        set(),
    )
    scan(store)
    store.snapshot(
        "6", "bot-a", "99", {"1": member("1"), "2": member("2")}, [evidence()]
    )
    assert store.invitation_summary("5", "1")["pending"] == 500
    assert store.invitation_summary("6", "1")["pending"] == 800
    with pytest.raises(CashbackError, match="conflict"):
        store.mutate(CASHIER, "6", "pay", [select(store)], "wrong-group-payment")
    store.mutate(CASHIER, "5", "pay", [select(store)], "first-group-payment")
    store.snapshot("6", "bot-a", "99", {"1": member("1")}, [])
    assert store.invitation_summary("5", "1")["paid"] == 500
    assert store.invitation_summary("5", "1")["present"] == 1
    assert store.invitation_summary("6", "1")["void"] == 1
    assert store.invitation_summary("6", "1")["paid"] == 0
    config = {**store.get_group("6"), "enabled": False}
    store.configure(OWNER, config, None, set())
    assert store.get_group("5")["enabled"] == 1
    assert store.get_group("5")["rate"] == 500


@pytest.mark.asyncio
async def test_disabled_scan_has_no_platform_calls_or_ledger_changes(store):
    """Keep disabled collection offline without changing historical entries.

    Args:
        store: Isolated cashback ledger.
    """
    scan(store)
    store.configure(OWNER, {**store.get_group("5"), "enabled": False}, None, set())
    before, audit = row(store), store.audit(OWNER, "5")
    service = CashbackService.__new__(CashbackService)
    service.store = store
    service.adapter = lambda _: pytest.fail("Disabled collection must stay offline")
    service.check_selected = lambda _: pytest.fail("Do not scan a disabled group")
    assert await service._scan("5") == {}
    assert row(store) == before
    assert store.audit(OWNER, "5") == audit


def test_cashback_runtime_scope_excludes_only_paused_integrated_groups():
    """Keep legacy scope intact while excluding paused integrated tenants."""
    service = CashbackService.__new__(CashbackService)
    config = {
        "enabled_groups": ["5", "6"],
        "_open_integrated": True,
        "_open_groups": {"6": {}},
        "_open_paused": False,
    }
    service.adapters = lambda: [SimpleNamespace(config=config)]
    assert service.selected_groups() == {"6"}
    config["_open_paused"] = True
    assert service.selected_groups() == set()
    config.pop("_open_integrated")
    assert service.selected_groups() == {"5", "6"}
    assert config["enabled_groups"] == ["5", "6"]


@pytest.mark.asyncio
async def test_poll_skips_disabled_groups_and_keeps_enabled_group_running(store):
    """Run only enabled collectors without stopping an unrelated healthy group.

    Args:
        store: Isolated cashback ledger.
    """
    store.configure(
        OWNER,
        {**store.get_group("5"), "id": "6", "enabled": True, "version": 0},
        {"1": member("1")},
        set(),
    )
    store.configure(OWNER, {**store.get_group("5"), "enabled": False}, None, set())
    service = CashbackService.__new__(CashbackService)
    service.store, service.locks = store, {}
    service.selected_groups = lambda: {"5", "6"}
    reached = asyncio.Event()

    async def scan_group(group):
        """Signal the enabled group worker.

        Args:
            group: Scheduled group identifier.
        """
        assert group == "6"
        reached.set()

    service._scan = AsyncMock(side_effect=scan_group)
    task = asyncio.create_task(service.poll())
    try:
        await asyncio.wait_for(reached.wait(), 2)
        service._scan.assert_awaited_once_with("6")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_poll_skips_unselected_groups_and_continues_after_failure(
    store, monkeypatch
):
    store.configure(
        OWNER,
        {**store.get_group("5"), "id": "6", "enabled": True, "version": 0},
        {"1": member("1")},
        set(),
    )
    service = CashbackService.__new__(CashbackService)
    service.store, service.locks = store, {}
    service.selected_groups = lambda: {"6"}
    service._scan = AsyncMock()

    async def one_round(expected):
        reached = asyncio.Event()

        async def call(group):
            if service._scan.await_count == expected:
                reached.set()
            if group == "5":
                raise CashbackError("offline", "offline")

        service._scan.side_effect = call
        task = asyncio.create_task(service.poll())
        try:
            await asyncio.wait_for(reached.wait(), 2)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    await one_round(1)
    service._scan.assert_awaited_once_with("6")
    service.selected_groups = lambda: {"5", "6"}
    service._scan.reset_mock()
    await one_round(2)
    assert service._scan.await_count == 2


@pytest.mark.asyncio
async def test_poll_slow_group_does_not_block_healthy_group(store):
    store.configure(
        OWNER,
        {**store.get_group("5"), "id": "6", "enabled": True, "version": 0},
        {"1": member("1")},
        set(),
    )
    service = CashbackService.__new__(CashbackService)
    service.store, service.locks = store, {}
    service.selected_groups = lambda: {"5", "6"}
    slow, healthy = asyncio.Event(), asyncio.Event()

    async def scan_group(group):
        if group == "5":
            await slow.wait()
        else:
            healthy.set()

    service._scan = scan_group
    task = asyncio.create_task(service.poll())
    try:
        await asyncio.wait_for(healthy.wait(), 2)
        assert not slow.is_set()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_poll_concurrency_is_bounded_and_workers_are_drained(store):
    for group in range(6, 11):
        store.configure(
            OWNER,
            {**store.get_group("5"), "id": str(group), "enabled": True, "version": 0},
            {"1": member("1")},
            set(),
        )
    service = CashbackService.__new__(CashbackService)
    service.store, service.locks = store, {}
    service.selected_groups = lambda: {str(g) for g in range(5, 11)}
    ready, hold = asyncio.Event(), asyncio.Event()
    active = set()

    async def scan_group(group):
        active.add(group)
        if len(active) == 3:
            ready.set()
        try:
            await hold.wait()
        finally:
            active.remove(group)

    service._scan = scan_group
    task = asyncio.create_task(service.poll())
    try:
        await asyncio.wait_for(ready.wait(), 2)
        assert len(active) == 3
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not active


@pytest.mark.asyncio
async def test_scan_failure_sampling_and_recovery_have_traces_and_duration(store):
    service = CashbackService.__new__(CashbackService)
    service.store = store
    adapter = SimpleNamespace(account="99", config={"id": "bot-a"})
    service.adapter = lambda _: adapter
    service.check_selected = lambda _: None
    service.activities = SimpleNamespace(authorize=lambda *_: None)
    service.roster = AsyncMock(return_value={"1": member("1")})
    service.evidence = AsyncMock(return_value=[])
    await service._scan("5")
    await service._scan("5")
    service.roster.side_effect = CashbackError("empty_roster", "Unavailable")
    for _ in range(2):
        with pytest.raises(CashbackError, match="empty_roster"):
            await service._scan("5")
    service.roster.side_effect = None
    await service._scan("5")
    logs = [r for r in store.audit(OWNER, "5") if r["action"] == "invitation_scan"]
    assert len(logs) == 3
    assert [r["level"] for r in logs] == ["info", "warning", "info"]
    assert json.loads(logs[0]["after_json"])["outcome"] == "recovered"
    assert all(json.loads(r["after_json"])["duration_ms"] >= 0 for r in logs)
    assert all(re.fullmatch("[a-f0-9]{32}", r["trace"]) for r in logs)


@pytest.mark.asyncio
async def test_scheduler_recovers_after_transient_storage_failure(store, monkeypatch):
    calls = 0

    def groups():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("Fixture busy")
        return store.groups()

    sleep = asyncio.sleep

    async def tick(_seconds):
        await sleep(0.01)

    monkeypatch.setattr(asyncio, "sleep", tick)
    service = CashbackService.__new__(CashbackService)
    service.store = SimpleNamespace(groups=groups, operation_log=store.operation_log)
    service.locks = {}
    service.selected_groups = lambda: {"5"}
    reached = asyncio.Event()

    async def scan_group(_group):
        reached.set()

    service._scan = scan_group
    task = asyncio.create_task(service.poll())
    try:
        await asyncio.wait_for(reached.wait(), 2)
        assert not task.done()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert any(r["code"] == "scheduler_unavailable" for r in store.audit(OWNER, ""))


def test_health_log_sampling_is_shared_but_errors_are_not_lost(store):
    assert store.operation_log("invitation_scan", group="5", sample_seconds=300)
    reopened = CashbackStore(store.path)
    assert (
        reopened.operation_log("invitation_scan", group="5", sample_seconds=300)
        is False
    )
    assert reopened.operation_log(
        "invitation_scan", group="5", level="warning", code="offline"
    )
    assert reopened.operation_log(
        "invitation_scan", group="5", level="warning", code="offline"
    )
    logs = [r for r in store.audit(OWNER, "5") if r["action"] == "invitation_scan"]
    assert len(logs) == 3
    assert all(re.fullmatch("[a-f0-9]{32}", row["trace"]) for row in logs)


def test_bound_identity_rechecks_revocation_without_taking_write_lock(
    store, monkeypatch
):
    store.identify(CASHIER, CASHIER)
    connect = store.connect
    calls = []

    def checked_connect(write=False):
        calls.append(write)
        return connect(write)

    monkeypatch.setattr(store, "connect", checked_connect)
    store.identify(CASHIER, CASHIER)
    assert calls == [False]
    store.save_user(OWNER, CASHIER, "Cashier", False, 1)
    with pytest.raises(CashbackError, match="forbidden"):
        store.identify(CASHIER, CASHIER)


@pytest.mark.asyncio
async def test_cashier_name_from_roster_not_request(store):
    service = CashbackService.__new__(CashbackService)
    service.store = store
    service.directory = AsyncMock(return_value=[{"uid": "123", "name": "平台昵称"}])
    await service.save_cashier(OWNER, "123", "5", True, 0)
    assert store.cashier_profiles(OWNER)[0]["name"] == "平台昵称"
    with pytest.raises(CashbackError, match="cashier_not_member"):
        await service.save_cashier(OWNER, "999", "5", True, 0)
