"""Bounded account-scoped retention without reopening uncertain sanctions."""

import asyncio
import itertools
import json
import threading
import time
from unittest.mock import Mock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import review_store
from astrbot.builtin_stars.wangshangliao_moderation.review_store import (
    REVIEW_RETENTION_SECONDS,
    ReviewStore,
)
from astrbot.core.platform.sources.wangshangliao.progression_store import (
    ProgressionStore,
)
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

NOW = 1_780_000_000.0
OLD = NOW - REVIEW_RETENTION_SECONDS - 100


@pytest.fixture
def store(tmp_path):
    value = ReviewStore(tmp_path / "moderation.sqlite3")
    value.initialize()
    return value


def content(store, mid, status="muted", warning="accepted", *, account="1", stamp=OLD):
    """Seed one complete or uncertain message and associated audit context."""
    assert store.observe(account, "5", mid, "2", "external_promotion", stamp, 0)
    store.finish(account, "5", mid, status, warning, error="transport")
    store.save_review(account, "5", mid, {"decision": "violation"}, "fixture")
    with store.connect() as db:
        db.execute(
            "UPDATE semantic_reviews SET created=? WHERE account=? AND message=?",
            (stamp, account, mid),
        )


def flood(
    store, operation, messages, *, mute="accepted", recall="accepted", account="1"
):
    """Seed one batch with independent, durable recall receipts."""
    assert store.reserve_flood(
        operation, account, "5", "2", "22", operation, messages[-1], OLD, messages
    ) == (operation, None, messages)
    store.finish_flood(operation, mute)
    for mid in messages:
        store.finish_flood(operation, recall, message=mid)


def snapshot(store):
    """Read row contents, excluding additive schema/index metadata."""
    with store.connect() as db:
        tables = [
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        return {
            table: db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in tables
        }


@pytest.mark.parametrize("same_clock", [False, True])
def test_unknown_prefix_is_paged_past_without_starving_later_completed_rows(
    store, same_clock
):
    with store.connect() as db:
        db.executemany(
            "INSERT INTO content_violations VALUES('1','5',?,'2','message_flood',?,0,?,'none')",
            [
                (
                    str(n),
                    OLD if same_clock else OLD + n,
                    "unknown" if n < 2048 else "muted",
                )
                for n in range(2112)
            ],
        )
    for _ in range(80):
        store.prune("1", now=NOW + 3000, limit=128)
        assert store.retention_stats["scanned"] <= 128
    with store.connect() as db:
        assert db.execute(
            "SELECT status,COUNT(*) FROM content_violations GROUP BY status"
        ).fetchall() == [("unknown", 2048)]


@pytest.mark.parametrize(
    "status,warning",
    [
        ("observed", "none"),
        ("claimed", "none"),
        ("unknown", "accepted"),
        ("muted", "unknown"),
        ("recalled", "pending"),
        ("new_future_status", "accepted"),
        (None, "accepted"),
        ("muted", None),
    ],
)
def test_unresolved_content_keeps_all_message_references(store, status, warning):
    content(store, "uncertain", status, warning)
    before = snapshot(store)
    result = store.prune(account="1", now=NOW)
    after = snapshot(store)
    assert sum(result.values()) == 0
    for table in before:
        if table != "review_retention":
            assert after[table] == before[table]
    assert not store.observe("1", "5", "uncertain", "2", "external_promotion", NOW, 0)


@pytest.mark.parametrize(
    "status",
    [
        "warned",
        "muted",
        "kicked",
        "recalled",
        "execution_rejected",
        "preflight_failed",
        "historical_or_unknown_time",
        "identity_unverified",
        "administrator_exempt",
        "disabled",
    ],
)
def test_only_expired_complete_content_and_owned_errors_are_removed(store, status):
    content(store, "expired", status, "none")
    content(store, "boundary", stamp=NOW - REVIEW_RETENTION_SECONDS)
    content(store, "yesterday", stamp=NOW - 86400)
    content(store, "other-account", account="9")
    with store.connect() as db:
        db.execute(
            "INSERT INTO content_rule_errors VALUES('1','5','orphan','transport',NULL)"
        )
    result = store.prune("1", now=NOW)
    assert result["content_violations"] == 1
    assert result["content_rule_errors"] == 1
    assert result["semantic_reviews"] == 1
    assert all(type(count) is int for count in result.values())
    assert store.content_outcome("1", "5", "expired") == "not_applied"
    assert store.content_outcome("1", "5", "yesterday") == "muted"
    assert store.content_outcome("1", "5", "boundary") == "muted"
    assert store.content_outcome("9", "5", "other-account") == "muted"
    with store.connect() as db:
        assert db.execute(
            "SELECT 1 FROM content_rule_errors WHERE message='orphan'"
        ).fetchone()
        assert db.execute("SELECT account FROM review_retention").fetchall() == [("1",)]


@pytest.mark.parametrize("status", ["unknown", "pending", None])
def test_uncertain_action_receipt_pins_even_an_accepted_mute(store, status):
    content(store, "mute-with-uncertain-recall", "muted", "accepted")
    store.save_action(
        "recall/1/5/mute-with-uncertain-recall",
        "1",
        "5",
        "2",
        "mute-with-uncertain-recall",
        "recall",
        status,
        True,
        "transport",
        None,
    )
    with store.connect() as db:
        db.execute("UPDATE moderation_action_audit SET created=?", (OLD,))
    before = snapshot(store)
    assert sum(store.prune("1", now=NOW).values()) == 0
    after = snapshot(store)
    for table in before:
        if table != "review_retention":
            assert after[table] == before[table]


@pytest.mark.parametrize(
    "recall,result",
    [
        ("unknown", {"status": "accepted", "action": "mute"}),
        ("accepted", {"status": "unknown", "action": "mute"}),
        ("accepted", None),
    ],
)
def test_progressive_uncertainty_pins_content_without_changing_cycles(
    store, recall, result
):
    content(store, "progressive")
    progression = ProgressionStore(store.path)
    progression.reserve("progressive-fixture", "1", "5", "2", "progressive", "22")
    with progression.connect() as db:
        db.execute(
            "UPDATE progressive_actions SET recall_status=?,result=?",
            (recall, json.dumps(result) if result is not None else "invalid-json"),
        )
    store.initialize()
    before = snapshot(store)
    assert sum(store.prune("1", now=NOW).values()) == 0
    after = snapshot(store)
    for table in before:
        if table != "review_retention":
            assert after[table] == before[table]


def test_reference_index_preparation_is_explicit_and_never_retried_in_prune(store):
    content(store, "known")
    progression = ProgressionStore(store.path)
    progression.initialize()
    with store.connect() as db:
        db.execute("DROP INDEX review_ref_progressive_actions")
        db.execute("DROP INDEX review_ref_progressive_warnings")
    before = snapshot(store)
    for _ in range(2):
        assert sum(store.prune("1", now=NOW).values()) == 0
        assert store.retention_stats["skipped"] == "unindexed"
    assert snapshot(store) == before
    store.initialize()
    assert store.prune("1", now=NOW)["content_violations"] == 1
    assert store.retention_stats["skipped"] == ""


def test_reference_index_setup_has_no_maintenance_deadline(store, monkeypatch):
    progression = ProgressionStore(store.path)
    progression.initialize()
    with progression.connect() as db:
        db.executemany(
            "INSERT INTO progressive_actions VALUES(?,'1','5','2',?,'unknown','{}',?)",
            [(f"old-operation-{n}", str(n), OLD) for n in range(100_000)],
        )
        db.execute("DROP INDEX review_ref_progressive_actions")
        db.execute("DROP INDEX review_ref_progressive_warnings")
    monkeypatch.setattr(
        review_store,
        "time",
        Mock(
            monotonic=Mock(
                side_effect=AssertionError("maintenance_budget_during_setup")
            )
        ),
    )
    store.initialize()
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM progressive_actions").fetchone() == (
            100_000,
        )
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='review_ref_progressive_actions'"
        ).fetchone()


def test_later_progressive_schema_is_immediately_ready_for_retention(store):
    content(store, "completed-before-progression")
    progression = ProgressionStore(store.path)
    progression.initialize()
    assert store.prune("1", now=NOW)["content_violations"] == 1
    assert store.retention_stats["skipped"] == ""


def test_accepted_mute_audit_without_message_keeps_unknown_progressive_recall(store):
    progression = ProgressionStore(store.path)
    progression.reserve("mute-operation", "1", "5", "2", "original-message", "22")
    progression.finish(
        "mute-operation", "unknown", {"status": "accepted", "action": "mute"}
    )
    store.save_action(
        "mute-operation", "1", "5", "2", "", "mute", "accepted", True, "", None
    )
    with store.connect() as db:
        db.execute("UPDATE moderation_action_audit SET created=?", (OLD,))
    result = store.prune("1", now=NOW)
    assert result["moderation_action_audit"] == 0
    assert snapshot(store)["moderation_action_audit"][0][0] == "mute-operation"


@pytest.mark.parametrize("receipt", ['{"status":"unknown"}', "invalid-json", "{}"])
def test_native_uncertain_operation_keeps_independent_accepted_audit(store, receipt):
    store.save_action("native", "1", "5", "2", "", "mute", "accepted", True, "", None)
    with store.connect() as db:
        db.execute(
            "CREATE TABLE operations(id TEXT PRIMARY KEY,digest TEXT,result TEXT)"
        )
        db.execute("INSERT INTO operations VALUES('native','digest',?)", (receipt,))
        db.execute("UPDATE moderation_action_audit SET created=?", (OLD,))
    assert sum(store.prune("1", now=NOW).values()) == 0
    assert snapshot(store)["moderation_action_audit"][0][0] == "native"


@pytest.mark.parametrize("which", ["mute", "recall"])
def test_unknown_flood_sibling_retains_entire_batch_and_all_related_dedup(store, which):
    flood(
        store,
        "uncertain-batch",
        ["known-recall", "uncertain-recall"],
        mute="unknown" if which == "mute" else "accepted",
    )
    if which == "recall":
        store.finish_flood("uncertain-batch", "unknown", message="uncertain-recall")
    content(store, "known-recall")
    content(store, "uncertain-recall")
    store.save_action(
        "uncertain-batch",
        "1",
        "5",
        "2",
        "",
        "mute",
        "accepted",
        True,
        "",
        None,
    )
    with store.connect() as db:
        db.execute("UPDATE moderation_action_audit SET created=?", (OLD,))
    before = snapshot(store)
    assert sum(store.prune("1", now=NOW).values()) == 0
    after = snapshot(store)
    for table in before:
        if table != "review_retention":
            assert after[table] == before[table]


@pytest.mark.parametrize("mute", ["none", "accepted", "verified", "rejected"])
def test_complete_flood_batch_reclaimed_atomically_and_original_ingress_cannot_replay(
    store, mute
):
    flood(store, "complete", ["101", "102", "103"], mute=mute)
    result = store.prune("1", now=NOW)
    assert result["flood_recalls"] == 3
    assert result["flood_batches"] == result["flood_warning_evidence"] == 1
    reopened = ReviewStore(store.path)
    reopened.initialize()
    before = snapshot(reopened)
    assert reopened.reserve_flood(
        "replay", "1", "5", "2", "22", "revision", "103", OLD, ["101", "102", "103"]
    ) == ("replay", "unknown", [])
    assert snapshot(reopened) == before
    assert reopened.reserve_flood(
        "fresh", "1", "5", "2", "22", "revision", "201", NOW, ["201"]
    ) == ("fresh", None, ["201"])
    assert reopened.reserve_flood(
        "other-account", "9", "5", "2", "22", "revision", "103", OLD, ["103"]
    ) == ("other-account", None, ["103"])


def test_flood_children_budget_preserves_owner_until_all_receipts_are_reclaimed(store):
    flood(store, "large", [str(100 + n) for n in range(9)])
    flood(store, "small", ["301", "302"])
    before = snapshot(store)
    result = store.prune("1", now=NOW, limit=8)
    assert result["flood_recalls"] == 8
    after = snapshot(store)
    assert [row for row in after["flood_batches"] if row[0] == "large"] == [
        row for row in before["flood_batches"] if row[0] == "large"
    ]
    assert [row for row in after["flood_warning_evidence"] if row[0] == "large"] == [
        row for row in before["flood_warning_evidence"] if row[0] == "large"
    ]
    assert len([row for row in after["flood_recalls"] if row[3] == "large"]) == 1
    for _ in range(5):
        result = store.prune("1", now=NOW, limit=8)
        assert result["flood_recalls"] <= 8
        assert store.retention_stats["scanned"] <= 8
    after = snapshot(store)
    assert after["flood_batches"] == []
    assert after["flood_recalls"] == []
    assert after["flood_warning_evidence"] == []


def test_unknown_recall_beyond_child_budget_protects_all_its_known_siblings(store):
    messages = [str(400 + n) for n in range(33)]
    flood(store, "unknown-tail", messages)
    store.finish_flood("unknown-tail", "unknown", message=messages[-1])
    flood(store, "later-complete", ["701", "702"])
    for _ in range(4):
        assert store.prune("1", now=NOW, limit=8)["flood_recalls"] <= 8
    after = snapshot(store)
    assert len(after["flood_recalls"]) == len(messages)
    assert all(row[3] == "unknown-tail" for row in after["flood_recalls"])
    assert [row[0] for row in after["flood_batches"]] == ["unknown-tail"]


def test_retired_flood_watermark_never_moves_backwards(store):
    store.prune("1", now=NOW)
    store.prune("1", now=NOW - 86400)
    with store.connect() as db:
        assert db.execute(
            "SELECT flood_before FROM review_retention WHERE account='1'"
        ).fetchone() == (NOW - REVIEW_RETENTION_SECONDS,)


@pytest.mark.parametrize("equal_clock", [False, True])
@pytest.mark.parametrize(
    "table,clock,index",
    [
        ("content_violations", "observed", "content_retention"),
        ("semantic_reviews", "created", "semantic_retention"),
        ("moderation_action_audit", "created", "action_retention"),
        ("flood_batches", "last_seen", "flood_retention"),
    ],
)
def test_candidate_query_seeks_account_and_cursor_without_sorting(
    store, table, clock, index, equal_clock
):
    predicate = (
        f"{clock}=? AND rowid>? AND {clock}<? ORDER BY rowid"
        if equal_clock
        else f"{clock}>? AND {clock}<? ORDER BY {clock},rowid"
    )
    params = (
        ("1", OLD, 100, NOW - REVIEW_RETENTION_SECONDS, 16)
        if equal_clock
        else ("1", OLD, NOW - REVIEW_RETENTION_SECONDS, 16)
    )
    with store.connect() as db:
        plan = " ".join(
            row[3]
            for row in db.execute(
                f"EXPLAIN QUERY PLAN SELECT rowid,{clock} FROM {table} "
                f"WHERE account=? AND {predicate} LIMIT ?",
                params,
            )
        )
    assert "SEARCH" in plan and index in plan
    assert "SCAN" not in plan and "TEMP B-TREE" not in plan
    if equal_clock:
        assert "rowid>?" in plan


@pytest.mark.parametrize("revoke_at", [1, 2])
def test_guard_revocation_rolls_back_rows_watermark_and_pagination(store, revoke_at):
    content(store, "retain-on-revocation")
    before = snapshot(store)
    calls = 0

    def guard():
        nonlocal calls
        calls += 1
        if calls == revoke_at:
            raise ProtocolError("database_authority_changed")

    with pytest.raises(ProtocolError, match="database_authority_changed"):
        store.prune("1", now=NOW, guard=guard)
    assert calls == revoke_at
    assert snapshot(store) == before
    assert store.retention_cursors == {}
    assert store.prune("1", now=NOW)["content_violations"] == 1


@pytest.mark.asyncio
async def test_writer_lock_yields_quickly_without_blocking_event_loop(store):
    content(store, "locked")
    before = snapshot(store)
    with store.connect() as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        cleanup = asyncio.create_task(asyncio.to_thread(store.prune, "1", now=NOW))
        await asyncio.sleep(0.01)
        assert time.monotonic() - started < 0.5
        result = await asyncio.wait_for(cleanup, 1)
        assert sum(result.values()) == 0
        assert time.monotonic() - started < 1
        assert store.retention_stats["skipped"] == "busy"
    assert snapshot(store) == before
    assert store.prune("1", now=NOW)["content_violations"] == 1


@pytest.mark.asyncio
async def test_competing_maintenance_is_skipped_without_waiting(store):
    entered, release = threading.Event(), threading.Event()
    calls = 0

    def guard():
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
            assert release.wait(2)

    cleanup = asyncio.create_task(
        asyncio.to_thread(store.prune, "1", now=NOW, guard=guard)
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert sum(store.prune("1", now=NOW).values()) == 0
        assert store.retention_stats["skipped"] == "busy"
    finally:
        release.set()
        await cleanup


def test_sql_time_budget_rolls_back_instead_of_partially_cleaning(store, monkeypatch):
    with store.connect() as db:
        db.executemany(
            "INSERT INTO semantic_reviews VALUES('1','5',?,'{}','fixture',?)",
            [(str(n), OLD) for n in range(1000)],
        )
    before = snapshot(store)
    ticks = itertools.count()
    monkeypatch.setattr(
        review_store,
        "time",
        Mock(time=lambda: NOW, monotonic=lambda: float(next(ticks))),
    )
    assert sum(store.prune("1", now=NOW, limit=512).values()) == 0
    assert store.retention_stats["skipped"] == "budget"
    assert snapshot(store) == before
    assert store.retention_cursors == {}


def test_prune_never_creates_missing_database_or_follows_symlinks(tmp_path):
    absent = ReviewStore(tmp_path / "absent" / "moderation.sqlite3")
    assert sum(absent.prune("1", now=NOW).values()) == 0
    assert absent.retention_stats["skipped"] == "uninitialized"
    assert not absent.path.parent.exists()
    absent.initialized = True
    assert sum(absent.prune("1", now=NOW).values()) == 0
    assert absent.retention_stats["skipped"] == "missing"
    assert not absent.path.parent.exists()
    actual = ReviewStore(tmp_path / "actual.sqlite3")
    actual.initialize()
    link = tmp_path / "link.sqlite3"
    link.symlink_to(actual.path)
    linked = ReviewStore(link)
    linked.initialized = True
    before = snapshot(actual)
    assert sum(linked.prune("1", now=NOW).values()) == 0
    assert linked.retention_stats["skipped"] == "missing"
    assert snapshot(actual) == before


def test_legacy_native_progressive_and_financial_evidence_are_not_pruned(store):
    progression = ProgressionStore(store.path)
    progression.reserve("pending-progressive", "1", "5", "2", "301", "22")
    with store.connect() as db:
        db.executescript("""
            CREATE TABLE operations(id TEXT PRIMARY KEY,digest TEXT,result TEXT);
            INSERT INTO operations VALUES('native','digest','{"status":"unknown"}');
            CREATE TABLE violation_messages(account TEXT,group_id TEXT,message TEXT);
            INSERT INTO violation_messages VALUES('1','5','302');
            CREATE TABLE financial_fixture(id TEXT PRIMARY KEY,balance INTEGER);
            INSERT INTO financial_fixture VALUES('untouched',100);
        """)
    store.initialize()
    content(store, "unrelated-complete")
    before = snapshot(store)
    store.prune("1", now=NOW)
    after = snapshot(store)
    for table in (
        "operations",
        "violation_messages",
        "financial_fixture",
        "progressive_actions",
        "progressive_warnings",
        "moderation_cycles",
    ):
        assert after[table] == before[table]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"account": ""},
        {"account": 1},
        {"account": "1", "limit": 0},
        {"account": "1", "limit": 513},
        {"account": "1", "limit": True},
        {"account": "1", "now": float("nan")},
        {"account": "1", "now": float("inf")},
        {"account": "1", "now": -1},
    ],
)
def test_invalid_cleanup_scope_is_rejected_without_database_changes(store, kwargs):
    before = snapshot(store)
    with pytest.raises(ValueError, match="review_retention_scope"):
        store.prune(**kwargs)
    assert snapshot(store) == before
