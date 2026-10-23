"""Selective no-reward reads retain authority, pagination and financial data."""

import json
import sqlite3
from datetime import datetime, timezone
from itertools import product

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import cashback_store
from astrbot.builtin_stars.wangshangliao_moderation.cashback_store import (
    CashbackError,
    CashbackStore,
    entry_time_window,
)

OWNER = "owner@example.com"
INDEX = "entry_no_reward_summary"
STATES = ("pending", "paid", "verifying", "no_reward", "unassigned", "void")


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    now = datetime(2026, 10, 16, 12, 30, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(cashback_store.time, "time", lambda: now)
    store = CashbackStore(tmp_path / "unindexed.sqlite3")
    store.bootstrap([{"email": OWNER, "name": "Owner"}])
    hour_start, _ = entry_time_window("hour", now)
    timestamps = (
        now - 40 * 86400,
        now - 2 * 86400,
        now - 2 * 3600,
        hour_start,
        now - 600,
        now - 60,
        now,
    )
    rows, proofs = [], []
    categories = (
        ("paid", "priced", 1000),
        ("pending", "priced", 14),
        ("verifying", "priced", 14),
        ("void", "priced", 14),
        ("verifying", "no_reward", 143),
        ("verifying", "unclassified", 5),
    )
    for state, price, count in categories:
        for offset in range(count):
            number = len(rows)
            uid = str(10000 + number)
            inviter = (
                ""
                if price == "unclassified"
                else str(
                    101
                    if price == "no_reward" and offset < 125
                    else 102 + (offset - 125) % 6
                    if price == "no_reward"
                    else 101 + offset % 7
                )
            )
            stamp = timestamps[offset % len(timestamps)]
            eid = f"{price}-{state}-{offset}"
            rows.append(
                (
                    eid,
                    "5",
                    uid,
                    f"成员{'%_' if offset % 3 == 0 else '普通'}{offset}",
                    f"peer/{uid}",
                    inviter,
                    f"邀请人{inviter}",
                    f"peer/{inviter}" if inviter else "",
                    stamp,
                    0 if price == "no_reward" else 500,
                    1,
                    "pending" if price == "no_reward" and offset % 2 else state,
                    "verified" if inviter else "pending",
                    now,
                    now - 1 if state == "paid" else None,
                    OWNER if state == "paid" else None,
                    price,
                    "ordinary",
                    stamp,
                    stamp,
                    eid,
                    stamp,
                    1,
                    0,
                )
            )
            if inviter:
                proofs.append(
                    (
                        "5",
                        uid,
                        "business_apply_log",
                        eid,
                        inviter,
                        now,
                        "99",
                        f"peer/{uid}",
                        f"peer/{inviter}",
                        stamp,
                    )
                )
    # Keep misleading candidates in the fixture so an index cannot weaken filters.
    template = next(row for row in rows if row[0] == "no_reward-verifying-0")
    for offset, reason in enumerate(
        ("ineligible", "self_join", "wrong_account", "missing_proof", "paid", "void")
    ):
        row = list(template)
        row[0], row[2], row[3] = f"excluded-{reason}", str(90000 + offset), reason
        row[4] = f"peer/{row[2]}"
        row[10] = int(reason != "ineligible")
        row[11] = reason if reason in {"paid", "void"} else "verifying"
        row[-1] = int(reason == "self_join")
        row[20] = row[0]
        rows.append(tuple(row))
        if reason != "missing_proof":
            proofs.append(
                (
                    "5",
                    row[2],
                    "business_apply_log",
                    row[0],
                    row[5],
                    now,
                    "other" if reason == "wrong_account" else "99",
                    row[4],
                    row[7],
                    row[8],
                )
            )
    foreign = list(template)
    foreign[0], foreign[1], foreign[2] = "other-group", "6", "99999"
    foreign[4], foreign[20] = "peer/99999", "other-group"
    rows.append(tuple(foreign))
    proofs.append(
        (
            "6",
            "99999",
            "business_apply_log",
            "other-group",
            foreign[5],
            now,
            "99",
            foreign[4],
            foreign[7],
            foreign[8],
        )
    )
    with store.connect(True) as db:
        db.execute(f"DROP INDEX IF EXISTS {INDEX}")
        db.executemany(
            "INSERT INTO groups(id,name,bot_id,account,cashier,enabled,rate,started,"
            "version,scanned) VALUES(?,?,'bot','99',?,1,500,?,1,?)",
            [
                (group, f"Group {group}", OWNER, now - 50 * 86400, now)
                for group in ("5", "6")
            ],
        )
        db.executemany(
            "INSERT INTO entries(id,group_id,member,member_name,peer,inviter,"
            "inviter_name,inviter_peer,first_seen,amount,eligible,state,verification,"
            "last_seen,paid_at,paid_by,pricing_status,member_class,hold_until,"
            "evidence_started,evidence_event_id,evidence_event_at,integrity_version,"
            "self_join) VALUES(" + ",".join("?" for _ in rows[0]) + ")",
            rows,
        )
        db.executemany(
            "INSERT INTO evidence(group_id,member,source,event_id,inviter,observed,"
            "account,member_peer,inviter_peer,event_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            proofs,
        )
        db.execute("ANALYZE entries")
    yield store, now
    store.close()


def test_no_reward_migration_changes_only_index_and_analyzes_once(ledger, monkeypatch):
    store, _ = ledger
    with store.connect() as db:
        tables = [
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        before = {
            name: [
                tuple(row)
                for row in db.execute(f'SELECT * FROM "{name}" ORDER BY rowid')
            ]
            for name in tables
        }
        schema = [
            tuple(row)
            for row in db.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
            )
        ]
    statements = []
    original = sqlite3.connect

    def traced(*args, **kwargs):
        db = original(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(cashback_store.sqlite3, "connect", traced)
    CashbackStore(store.path).close()
    assert statements.count("ANALYZE entries") == 1
    with store.connect() as db:
        assert before == {
            name: [
                tuple(row)
                for row in db.execute(f'SELECT * FROM "{name}" ORDER BY rowid')
            ]
            for name in tables
        }
        assert schema == [
            tuple(row)
            for row in db.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' AND name<>? ORDER BY type,name",
                (INDEX,),
            )
        ]
        assert db.execute("SELECT 1 FROM sqlite_stat1 WHERE idx=?", (INDEX,)).fetchone()
        index_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE name=?", (INDEX,)
        ).fetchone()[0]
        assert (
            "WHERE eligible=1 AND self_join=0 AND pricing_status='no_reward'"
            in index_sql
        )
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    statements.clear()
    CashbackStore(store.path).close()
    assert not any(query.startswith("ANALYZE") for query in statements)


def test_read_only_constructor_never_installs_missing_no_reward_index(ledger):
    store, _ = ledger
    before = store.path.read_bytes()
    reader = CashbackStore(store.path, read_only=True, reuse_reads=True)
    try:
        assert reader.list_entries(OWNER, "5", "no_reward")["total"] == 143
        with reader.connect() as db:
            assert not db.execute(
                "SELECT 1 FROM sqlite_master WHERE name=?", (INDEX,)
            ).fetchone()
            assert db.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(CashbackError, match="read_only"):
            with reader.connect(True):
                pytest.fail("A read-only store must not run the migration")
        assert store.path.read_bytes() == before
    finally:
        reader.close()


def test_no_reward_empty_migration_needs_no_statistics(tmp_path, monkeypatch):
    statements = []
    original = sqlite3.connect

    def traced(*args, **kwargs):
        db = original(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(cashback_store.sqlite3, "connect", traced)
    store = CashbackStore(tmp_path / "empty.sqlite3")
    with store.connect() as db:
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE name=?", (INDEX,)
        ).fetchone()
    assert not any(query.startswith("ANALYZE") for query in statements)
    store.close()


def test_no_reward_index_full_queries_and_writes_are_equivalent(ledger, tmp_path):
    store, now = ledger
    indexed_path = tmp_path / "indexed.sqlite3"
    with store.connect() as source, sqlite3.connect(indexed_path) as target:
        source.backup(target)
    CashbackStore(indexed_path).close()
    indexed = CashbackStore(indexed_path, read_only=True, reuse_reads=True)
    baseline = CashbackStore(store.path, read_only=True, reuse_reads=True)
    hour_start, _ = entry_time_window("hour", now)
    cases = [
        {"state": state, "pagination": pagination, "page": page}
        for state, pagination, page in product(
            STATES, ("entries", "inviters"), (1, 2, 100)
        )
    ]
    for search, period, pagination, page in product(
        ("", "成员%_", "邀请人101", "%_", "no matches"),
        ("all", "month", "day", "hour", "custom"),
        ("entries", "inviters"),
        (1, 2),
    ):
        cases.append(
            {
                "state": "no_reward",
                "search": search,
                "period": period,
                "pagination": pagination,
                "page": page,
                **(
                    {"start_at": hour_start, "end_at": now - 600}
                    if period == "custom"
                    else {}
                ),
            }
        )
    cases.extend(
        {
            "state": "no_reward",
            "pagination": "inviters",
            "inviter": inviter,
            "member_page": page,
        }
        for inviter, page in product(("101", "107", "999"), (1, 2, 100))
    )
    comparisons = 0
    try:
        for case in cases:
            assert indexed.list_entries(OWNER, "5", **case) == baseline.list_entries(
                OWNER, "5", **case
            ), case
            comparisons += 1
        result = indexed.list_entries(
            OWNER, "5", "no_reward", pagination="inviters", inviter="101", member_page=2
        )
        assert result["total"] == 143
        assert result["sections"][0]["count"] == 125
        assert len(result["items"]) == 25
        assert all(not row["review_reason"] for row in result["items"])
        assert indexed.list_entries(OWNER, "6", "no_reward")["total"] == 1
        custom = indexed.list_entries(
            OWNER,
            "5",
            "no_reward",
            period="custom",
            start_at=hour_start,
            end_at=now - 600,
        )
        assert custom["total"] > 0
        assert all(row["first_seen"] == hour_start for row in custom["items"])
        missing = indexed.list_entries(OWNER, "5", "no_reward", search="no matches")
        assert missing["total"] == missing["amount"] == missing["payable_amount"] == 0
        plans = {}
        for name, reader in (("before", baseline), ("after", indexed)):
            statements = []
            with reader.connect() as db:
                db.set_trace_callback(statements.append)
            reader.list_entries(OWNER, "5", "no_reward", pagination="inviters")
            query = next(q for q in statements if q.startswith("WITH grouped AS"))
            with reader.connect() as db:
                db.set_trace_callback(None)
                plans[name] = [
                    row[3] for row in db.execute("EXPLAIN QUERY PLAN " + query)
                ]
        assert not any(INDEX in detail for detail in plans["before"])
        assert any(
            INDEX in detail and "pricing_status=?" in detail
            for detail in plans["after"]
        ), plans

        actions = [
            (
                "UPDATE entries SET pricing_status='priced',amount=300,"
                "member_class='agent',state='pending',version=version+1 "
                "WHERE id='no_reward-verifying-0'",
                (),
            ),
            ("DELETE FROM evidence WHERE event_id='no_reward-verifying-1'", ()),
            ("UPDATE entries SET eligible=0 WHERE id='no_reward-verifying-2'", ()),
            ("UPDATE entries SET self_join=1 WHERE id='no_reward-verifying-3'", ()),
            ("DELETE FROM entries WHERE id='no_reward-verifying-4'", ()),
            (
                "INSERT INTO entries SELECT 'inserted',group_id,'88888','新增成员',"
                "'peer/88888',inviter,inviter_name,inviter_peer,first_seen,amount,"
                "eligible,state,verification,present,ever_left,left_at,last_seen,"
                "paid_at,paid_by,version,evidence_started,evidence_event_id,"
                "evidence_event_at,integrity_version,inviter_class,inviter_name_snapshot,"
                "name_digit_count,pricing_status,member_class,member_avatar_ref,"
                "member_avatar_status,member_avatar_digit_count,hold_until,rule_version,"
                "self_join,first_spoke_at FROM entries "
                "WHERE id='no_reward-verifying-5'",
                (),
            ),
            (
                "INSERT INTO evidence SELECT group_id,'88888',source,'inserted',inviter,"
                "observed,account,'peer/88888',inviter_peer,event_at,member_name_length "
                "FROM evidence WHERE event_id='no_reward-verifying-5'",
                (),
            ),
        ]
        counts = []
        for sql, parameters in actions:
            for path in (store.path, indexed_path):
                with sqlite3.connect(path) as db:
                    db.execute(sql, parameters)
            for case in cases:
                assert indexed.list_entries(
                    OWNER, "5", **case
                ) == baseline.list_entries(OWNER, "5", **case), (sql, case)
                comparisons += 1
            counts.append(indexed.list_entries(OWNER, "5", "no_reward")["total"])
        assert counts == [142, 141, 140, 139, 138, 138, 139]
        print(
            "NO_REWARD_INDEX_EVIDENCE "
            + json.dumps(
                {
                    "equivalent_full_queries": comparisons,
                    "initial_cases": len(cases),
                    "post_write_totals": counts,
                    "query_plans": plans,
                },
                ensure_ascii=False,
            )
        )
    finally:
        indexed.close()
        baseline.close()
