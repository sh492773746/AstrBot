"""Small isolated SQL and transaction regressions, not load benchmarks."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier
from types import SimpleNamespace

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import ai_plans as billing
from astrbot.builtin_stars.wangshangliao_moderation.group_ai import DEFAULT
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from tests.test_wangshangliao_ai_plans import (
    MODEL,
    NOW,
    PAYLOAD,
    charge,
    grant,
)
from tests.test_wangshangliao_ai_plans import plans as plans


@pytest.fixture(autouse=True)
def accounting_clock(monkeypatch):
    monkeypatch.setattr(billing, "time", SimpleNamespace(time=lambda: NOW))


@pytest.fixture
def sql_trace(plans, monkeypatch):
    original = plans.store.connect
    trace = SimpleNamespace(connections=[], statements=[])

    @contextmanager
    def connect(**kwargs):
        write = kwargs.get("write", False)
        trace.connections.append(write)
        with original(**kwargs) as db:
            db.set_trace_callback(
                lambda sql: trace.statements.append((write, " ".join(sql.split())))
            )
            yield db

    monkeypatch.setattr(plans.store, "connect", connect)
    return trace


def insert_charge(
    plans,
    key,
    pool="uid/1",
    *,
    period="trial/2026-10",
    state="settled",
    created=NOW - 10,
    cost=11,
):
    """Insert one historical receipt and its matching call in an isolated fixture.

    Args:
        plans: Test-only subscription service.
        key: Distinct receipt identifier.
        pool: Existing trusted or transport pool.
        period: Historical subscription period.
        state: Durable charge outcome.
        created: Accounting timestamp.
        cost: Integer micro-CNY cost or hold.
    """
    day, month = billing.dates(created)
    with plans.store.connect(write=True) as db:
        row = db.execute("SELECT * FROM ai_pools WHERE pool=?", (pool,)).fetchone()
        db.execute(
            "INSERT INTO ai_charges(request,pool,period,day,month,account,group_id,"
            "uid,principal,binding,price_version,reserved_micro,charged_micro,state,created) "
            "VALUES(?,?,?,?,?,'99','',?,?,'fixture',1,100,?,?,?)",
            (
                key,
                pool,
                period,
                day,
                month,
                row["uid"],
                row["principal"],
                cost,
                state,
                created,
            ),
        )
        db.execute(
            "INSERT INTO calls VALUES(?,?,?,0,?,?)",
            (key, row["principal"], day, created, state),
        )


def test_batched_status_matches_individual_snapshots_and_existing_lists(plans):
    grant(plans)
    grant(plans)
    plans.snapshot("nim/2002", "2")
    plans.snapshot("nim/3003", "3")
    plans.snapshot("nim/4004")
    with plans.store.connect(write=True) as db:
        db.executemany(
            "INSERT INTO ai_subscriptions VALUES(?,?,?,?,?,?,?,?,?)",
            [
                ("expired", "uid/2", "plus", NOW - 60, NOW, 134, 938, NOW - 60, "test"),
                ("future", "uid/2", "pro", NOW + 1, NOW + 60, 335, 2345, NOW, "test"),
                ("active", "uid/3", "pro", NOW - 60, NOW + 60, 335, 2345, NOW, "test"),
                ("overlap", "uid/3", "plus", NOW - 30, NOW + 30, 134, 938, NOW, "test"),
                (
                    "same-start",
                    "uid/3",
                    "pro",
                    NOW - 30,
                    NOW + 90,
                    335,
                    2345,
                    NOW,
                    "test",
                ),
            ],
        )
        db.executemany(
            "INSERT INTO calls VALUES(?,?,?,0,?,'completed')",
            [
                ("legacy-peer", "nim/1001", "2026-10-16", NOW - 10),
                ("legacy-uid", "1", "2026-10-16", NOW - 10),
                ("legacy-group", "group/99/5", "2026-10-16", NOW - 10),
                ("legacy-empty", "", "2026-10-16", NOW - 10),
            ],
        )
        db.execute("INSERT INTO ai_payers VALUES('99','5','1','nim/1001',1,1,1)")
    with plans.store.connect() as db:
        active_period = db.execute(
            "SELECT id FROM ai_subscriptions WHERE pool='uid/1' AND starts<=? AND ends>?",
            (NOW, NOW),
        ).fetchone()[0]
    insert_charge(plans, "current", period=active_period)
    insert_charge(plans, "yesterday", period=active_period, created=NOW - 86400)
    insert_charge(plans, "prior-month", period=active_period, created=NOW - 32 * 86400)
    insert_charge(plans, "old-period", period="ended-period")
    insert_charge(plans, "not-sent", period=active_period, state="not_sent", cost=9999)
    insert_charge(plans, "unknown", "uid/2", state="unknown")
    insert_charge(plans, "overlap-charge", "uid/3", period="overlap")
    insert_charge(plans, "transport", "nim/4004")
    plans.queue_notices("nim/1001", "1", "99", reason="site_budget", now=NOW)

    with plans.store.connect() as db:
        expected = [
            plans._snapshot(db, dict(row), NOW)
            for row in db.execute("SELECT * FROM ai_pools ORDER BY pool LIMIT 1000")
        ]
        subscriptions = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM ai_subscriptions ORDER BY created DESC LIMIT 200"
            )
        ]
        payers = [dict(row) for row in db.execute("SELECT * FROM ai_payers")]
        notices = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM ai_quota_notices ORDER BY created DESC LIMIT 100"
            )
        ]
    result = plans.status()
    assert result["pools"] == expected
    assert result["subscriptions"] == subscriptions
    assert result["payers"] == payers
    assert result["notices"] == notices
    assert result["unknown"][0]["request"] == "unknown"
    assert result["site"]["cost_micro"] == expected[0]["site_cost_micro"]
    assert result["prices"][0]["config"]["multiplier"] == "0.25"


def test_status_select_count_is_constant_and_month_is_summed_once(plans, sql_trace):
    plans.snapshot("nim/1001", "1")
    sql_trace.statements.clear()
    first = plans.status()
    first_selects = [
        sql for _, sql in sql_trace.statements if sql.startswith("SELECT ")
    ]
    with plans.store.connect(write=True) as db:
        db.executemany(
            "INSERT INTO ai_pools VALUES(?,?,?,0)",
            [(f"uid/{i}", str(i), f"nim/{i}") for i in range(2, 65)],
        )
    sql_trace.statements.clear()
    sql_trace.connections.clear()
    result = plans.status()
    selects = [sql for _, sql in sql_trace.statements if sql.startswith("SELECT ")]
    assert len(first["pools"]) == 1
    assert len(result["pools"]) == 64
    assert len(selects) == len(first_selects) == 12
    assert (
        sum("WHERE month=" in sql and "SUM(charged_micro)" in sql for sql in selects)
        == 1
    )
    assert sql_trace.connections == [False]
    assert sql_trace.statements[0] == (False, "BEGIN")


def test_status_empty_pools_preserves_site_holds(plans):
    with plans.store.connect(write=True) as db:
        db.execute("INSERT INTO ai_pools VALUES('nim/orphan','','nim/orphan',0)")
    insert_charge(plans, "orphan-hold", "nim/orphan", state="unknown", cost=123)
    with plans.store.connect(write=True) as db:
        db.execute("DELETE FROM ai_pools")
    result = plans.status()
    assert result["pools"] == []
    assert result["site"]["cost_micro"] == 123
    assert result["unknown"][0]["request"] == "orphan-hold"


def test_existing_snapshot_has_no_write_and_succeeds_while_writer_is_held(
    plans, sql_trace
):
    before = plans.snapshot("nim/1001", "1", now=NOW)
    sql_trace.connections.clear()
    sql_trace.statements.clear()
    with sqlite3.connect(plans.store.path) as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE ai_plan_settings SET contact_name='uncommitted'")
        assert plans.snapshot("nim/1001", "1", now=NOW) == before
        writer.rollback()
    assert sql_trace.connections == [False]
    assert sql_trace.statements[0] == (False, "BEGIN")
    assert all(
        not sql.startswith(("INSERT ", "UPDATE ", "DELETE "))
        for _, sql in sql_trace.statements
    )


def test_missing_pool_bootstrap_does_not_aggregate_under_write_lock(plans, sql_trace):
    result = plans.snapshot("nim/1001", "1", now=NOW)
    assert result["pool"] == "uid/1"
    assert sql_trace.connections == [False, True, False]
    assert any(
        write and sql.startswith("INSERT INTO ai_pools")
        for write, sql in sql_trace.statements
    )
    assert not any(
        write and ("COUNT(" in sql or "SUM(" in sql)
        for write, sql in sql_trace.statements
    )


@pytest.mark.parametrize("method", ["snapshot", "status"])
def test_read_snapshot_is_consistent_across_concurrent_commit(
    plans, monkeypatch, method
):
    plans.snapshot("nim/1001", "1")
    insert_charge(plans, "receipt", cost=100)
    before = plans.snapshot("nim/1001", "1", now=NOW)
    original = plans.store.connect
    changed = []

    @contextmanager
    def connect(**kwargs):
        with original(**kwargs) as db:

            def trace(sql):
                upper = sql.upper()
                if not changed and "COUNT(*)" in upper and "FROM AI_CHARGES" in upper:
                    changed.append(True)
                    with original(write=True) as writer:
                        writer.execute(
                            "UPDATE ai_charges SET charged_micro=200 WHERE request='receipt'"
                        )
                        writer.execute(
                            "INSERT INTO calls VALUES('later','nim/1001',?,0,?,'completed')",
                            (before["day"], NOW),
                        )

            db.set_trace_callback(trace)
            yield db

    monkeypatch.setattr(plans.store, "connect", connect)
    if method == "snapshot":
        result = plans.snapshot("nim/1001", "1", now=NOW)
    else:
        result = plans.status()["pools"][0]
    assert changed
    assert result == before
    after = plans.snapshot("nim/1001", "1", now=NOW)
    assert after["used"] == before["used"] + 1
    assert after["cost_micro"] == after["site_cost_micro"] == 200


def test_snapshot_migrates_identity_and_deduplicates_notices_atomically(
    plans, sql_trace
):
    plans.snapshot("nim/700")
    insert_charge(plans, "unverified", "nim/700", state="unknown", cost=123)
    with plans.store.connect(write=True) as db:
        db.execute("INSERT INTO ai_pools VALUES('uid/7','7','nim/700',4)")
        db.executemany(
            "INSERT INTO ai_quota_notices VALUES(?,?,?,'99','','nim/700','notice','pending',?)",
            [
                ("old-duplicate", "nim/700", "same", NOW),
                ("new-duplicate", "uid/7", "same", NOW),
                ("old-other", "nim/700", "other", NOW),
            ],
        )
    plans.store.link_principal("7", "nim/700")
    sql_trace.connections.clear()
    sql_trace.statements.clear()
    state = plans.snapshot("nim/700", "7", now=NOW)
    assert state["pool"] == "uid/7" and state["version"] == 4
    assert state["used"] == 1 and state["cost_micro"] == 123
    assert sql_trace.connections == [False, True, False]
    assert not any(write and "SUM(" in sql for write, sql in sql_trace.statements)
    with plans.store.connect() as db:
        assert not db.execute("SELECT 1 FROM ai_pools WHERE pool='nim/700'").fetchone()
        assert db.execute("SELECT pool FROM ai_charges").fetchone()[0] == "uid/7"
        assert {
            tuple(row) for row in db.execute("SELECT id,pool FROM ai_quota_notices")
        } == {("new-duplicate", "uid/7"), ("old-other", "uid/7")}


@pytest.mark.parametrize("principal,uid", [("nim/1001", "2"), ("nim/forged", "1")])
def test_snapshot_identity_conflicts_do_not_write(plans, sql_trace, principal, uid):
    plans.snapshot("nim/1001", "1")
    sql_trace.connections.clear()
    with pytest.raises(OpenError, match="private_identity_unverified"):
        plans.snapshot(principal, uid, now=NOW)
    assert sql_trace.connections == [False]


@pytest.mark.parametrize("stage", ["before_write", "after_write"])
def test_snapshot_rechecks_alias_across_bootstrap_transaction(
    plans, monkeypatch, stage
):
    original = plans.store.connect
    changed = []
    with original(write=True) as db:
        db.execute("INSERT INTO ai_pools VALUES('nim/1001','','nim/1001',0)")
    insert_charge(plans, "transport-hold", "nim/1001", state="unknown", cost=123)

    def change_alias():
        changed.append(True)
        with original(write=True) as writer:
            writer.execute(
                "UPDATE quota_aliases SET uid='2' WHERE principal='nim/1001'"
            )

    @contextmanager
    def connect(**kwargs):
        if kwargs.get("write") and stage == "before_write" and not changed:
            change_alias()
        with original(**kwargs) as db:
            yield db
        if kwargs.get("write") and stage == "after_write" and not changed:
            change_alias()

    monkeypatch.setattr(plans.store, "connect", connect)
    with pytest.raises(OpenError, match="private_identity_unverified"):
        plans.snapshot("nim/1001", "1", now=NOW)
    assert changed
    with original() as db:
        row = db.execute(
            "SELECT pool,charged_micro FROM ai_charges WHERE request='transport-hold'"
        ).fetchone()
        assert row["charged_micro"] == 123
        assert row["pool"] == ("nim/1001" if stage == "before_write" else "uid/1")
        assert db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 1


def test_concurrent_first_snapshot_rechecks_pool_after_read_transaction(
    plans, monkeypatch
):
    original = plans._pool
    barrier = Barrier(2)
    arrivals = []

    def pool(db, principal, uid="", *, create=True):
        result = original(db, principal, uid, create=create)
        if not create and result is None:
            arrivals.append(True)
            barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(plans, "_pool", pool)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: plans.snapshot("nim/1001", "1"), range(2)))
    assert len(arrivals) == 2
    assert results[0] == results[1]
    with plans.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM ai_pools").fetchone()[0] == 1


@pytest.mark.parametrize("state", ["reserved", "dispatched"])
def test_inflight_rejects_before_any_usage_aggregation(
    plans, sql_trace, monkeypatch, state
):
    quote = plans.quote(MODEL, PAYLOAD)
    plans.reserve("nim/1001", "1", "first", quote, "99", now=NOW - 60)
    if state == "dispatched":
        plans.dispatched("first", quote, PAYLOAD)
    monkeypatch.setattr(plans, "_snapshot", lambda *_: pytest.fail("late admission"))
    sql_trace.statements.clear()
    with pytest.raises(OpenError, match="chat_cooldown"):
        plans.reserve("nim/1001", "1", "second", quote, "99", now=NOW)
    assert not any("COUNT(" in sql or "SUM(" in sql for _, sql in sql_trace.statements)


def test_recent_call_rejects_before_aggregation_but_not_sent_does_not(
    plans, monkeypatch
):
    quote = charge(plans, "first", now=NOW)
    original = plans._snapshot
    monkeypatch.setattr(plans, "_snapshot", lambda *_: pytest.fail("late admission"))
    with pytest.raises(OpenError, match="chat_cooldown"):
        plans.reserve("nim/1001", "1", "second", quote, "99", now=NOW + 1)
    monkeypatch.setattr(plans, "_snapshot", original)
    plans.reserve("nim/1001", "1", "unsent", quote, "99", now=NOW + 6)
    plans.finish("unsent", "not_sent")
    plans.reserve("nim/1001", "1", "third", quote, "99", now=NOW + 7)
    assert plans.snapshot("nim/1001", "1", now=NOW + 7)["used"] == 2


def test_group_send_cooldown_precedes_usage_aggregation(plans, monkeypatch):
    tenant = plans.store.tenant("99", "5")
    plans.snapshot("nim/1001", "1")
    plans.bind(
        "99", "5", "1", "nim/1001", 0, tenant["version"], 0, "1", guard=lambda: None
    )
    with plans.store.connect(write=True) as db:
        db.execute(
            "INSERT INTO group_ai VALUES('99','5',1,?,'',?,'')",
            (json.dumps({**DEFAULT, "enabled": True}), NOW - 10),
        )
    quote = plans.quote(MODEL, PAYLOAD)
    monkeypatch.setattr(plans, "_snapshot", lambda *_: pytest.fail("late admission"))
    with pytest.raises(OpenError, match="chat_cooldown"):
        plans.reserve(
            "nim/1001",
            "1",
            "group",
            quote,
            "99",
            "5",
            payer_version=1,
            group_version=1,
            now=NOW,
        )


def test_hot_queries_use_matching_partial_indexes(plans):
    cases = [
        (
            "SELECT 1 FROM ai_charges WHERE pool=? AND state IN ('reserved','dispatched') LIMIT 1",
            ("uid/1",),
            "ai_charges_inflight_pool",
        ),
        (
            "SELECT created FROM ai_charges WHERE pool=? AND state<>'not_sent' ORDER BY created DESC LIMIT 1",
            ("uid/1",),
            "ai_charges_recent_pool",
        ),
        (
            "SELECT pool,COUNT(*) FROM ai_charges WHERE day=? AND state<>'not_sent' GROUP BY pool",
            ("2026-10-16",),
            "ai_charges_day_pool",
        ),
        (
            "SELECT SUM(charged_micro),"
            "SUM(CASE WHEN budget_kind='trial' THEN charged_micro ELSE 0 END) "
            "FROM ai_charges WHERE month=? AND state<>'not_sent'",
            ("2026-10",),
            "ai_charges_month_kind_cost",
        ),
    ]
    with plans.store.connect() as db:
        for sql, parameters, index in cases:
            explain = [
                row["detail"]
                for row in db.execute("EXPLAIN QUERY PLAN " + sql, parameters)
            ]
            assert any(index in detail for detail in explain), explain
            assert not any("TEMP B-TREE" in detail for detail in explain), explain


def test_reserve_still_rolls_back_if_authority_is_revoked_at_commit(plans):
    quote = plans.quote(MODEL, PAYLOAD)
    checks = []

    def guard():
        checks.append(True)
        if len(checks) == 2:
            raise OpenError("operation_revoked")

    with pytest.raises(OpenError, match="operation_revoked"):
        plans.reserve("nim/1001", "1", "revoked", quote, "99", guard=guard, now=NOW)
    assert len(checks) == 2
    with plans.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM ai_charges").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM ai_pools").fetchone()[0] == 0


def test_status_is_not_a_budget_or_permission_cache(plans):
    quote = charge(plans, "old", now=NOW - 60)
    assert plans.status()["pools"][0]["remaining"] == 19
    settings = plans.settings()
    plans.configure(settings["version"], "test", "contact", "0.000001", "test")
    with pytest.raises(OpenError, match="site_budget"):
        plans.reserve("nim/1001", "1", "over-budget", quote, "99", now=NOW)
    grant(plans)
    with plans.store.connect(write=True) as db:
        db.execute("UPDATE tenants SET managers='[\"2\"]'")
    plans.configure(plans.settings()["version"], "test", "contact", "1000", "test")
    with pytest.raises(OpenError, match="manager_required"):
        plans.reserve("nim/1001", "1", "revoked-manager", quote, "99", now=NOW)
