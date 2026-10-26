"""Indexed native identity checks retain conflict and revocation semantics."""

import sqlite3
from contextlib import closing

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenStore


def test_legacy_binding_index_migration_preserves_rows(tmp_path):
    path = tmp_path / "open-service.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "CREATE TABLE actor_bindings(account TEXT,uid TEXT,peer TEXT,"
            "checked REAL,conflicted INTEGER DEFAULT 0,PRIMARY KEY(account,uid))"
        )
        db.executemany(
            "INSERT INTO actor_bindings VALUES(?,?,?,?,?)",
            [("1", "2", "22", 10, 0), ("1", "3", "22", 20, 1)],
        )
        before = db.execute("SELECT * FROM actor_bindings ORDER BY uid").fetchall()
    for _ in range(2):
        OpenStore(tmp_path)
    with closing(sqlite3.connect(path)) as db:
        assert (
            db.execute("SELECT * FROM actor_bindings ORDER BY uid").fetchall() == before
        )
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE name='actor_bindings_peer'"
        ).fetchone()


@pytest.mark.parametrize("change", ["uid", "peer", "within_roster"])
def test_indexed_lookup_still_marks_ambiguous_bindings(tmp_path, change):
    store = OpenStore(tmp_path)
    store.remember_roster("1", {"2": {"nimId": "22"}})
    if change == "uid":
        rows = {"2": {"nimId": "23"}}
    elif change == "peer":
        rows = {"3": {"nimId": "22"}}
    else:
        rows = {"3": {"nimId": "33"}, "4": {"nimId": "33"}}
    store.remember_roster("1", rows)
    uid, peer = ("3", "33") if change == "within_roster" else ("2", "22")
    assert not store.verified_actor("1", uid, peer)
    store.remember_roster("1", {uid: {"nimId": peer}})
    assert not store.verified_actor("1", uid, peer)
    store.remember_roster("other", {uid: {"nimId": peer}})
    assert store.verified_actor("other", uid, peer)


def test_indexed_roster_rolls_back_when_authority_is_revoked(tmp_path):
    store = OpenStore(tmp_path)
    checks = 0

    def guard():
        nonlocal checks
        checks += 1
        if checks == 2:
            raise ValueError("revoked")

    with pytest.raises(ValueError, match="revoked"):
        store.remember_roster("1", {"2": {"nimId": "22"}}, guard=guard)
    assert not store.verified_actor("1", "2", "22")


def test_large_roster_conflict_checks_seek_both_identity_indexes(tmp_path, monkeypatch):
    store = OpenStore(tmp_path)
    rows = {str(i): {"nimId": str(i + 100000)} for i in range(5000)}
    store.remember_roster("1", rows)
    original = sqlite3.connect
    statements = []
    progress = []

    def tracked(*args, **kwargs):
        db = original(*args, **kwargs)
        db.set_trace_callback(statements.append)
        db.set_progress_handler(lambda: progress.append(1) and 0, 1000)
        return db

    monkeypatch.setattr(sqlite3, "connect", tracked)
    store.remember_roster("1", rows)
    lookups = [
        sql for sql in statements if sql.startswith("SELECT 1 FROM actor_bindings")
    ]
    assert len(lookups) == 5000
    # Count VM work rather than imposing a host-dependent timing threshold.
    assert len(progress) < 2000
    with closing(original(store.path)) as db:
        plans = " ".join(
            row[3] for row in db.execute("EXPLAIN QUERY PLAN " + lookups[0])
        )
        assert "actor_bindings_peer (account=? AND peer=?)" in plans
        assert "(account=? AND uid=?)" in plans
        assert "SCAN" not in plans
        assert db.execute("SELECT COUNT(*) FROM actor_bindings").fetchone() == (5000,)
