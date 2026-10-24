"""Read-only connection reuse keeps leases, fresh authority and bounded idle state."""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.cashback_store import (
    CashbackError,
    CashbackStore,
)


@pytest.fixture
def store(tmp_path):
    value = CashbackStore(tmp_path / "cash.sqlite3", reuse_reads=True)
    value.bootstrap([{"email": "admin@example.com", "name": "Admin"}])
    yield value
    value.close()


def test_reader_reuses_connection_but_never_retains_transaction(store):
    with store.connect() as first:
        first.execute("BEGIN")
        assert first.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        assert first.in_transaction
    with store.connect() as second:
        assert first is second
        assert not second.in_transaction
        assert second.execute("PRAGMA query_only").fetchone()[0] == 1
        assert second.execute("PRAGMA synchronous").fetchone()[0] == 2


def test_reader_cannot_write_and_sql_error_discards_connection(store):
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        with store.connect() as db:
            db.execute("DELETE FROM users")
    assert store._readers == []
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_writer_remains_separate_and_next_read_sees_revocation(store):
    with store.connect() as reader:
        store._actor(reader, "admin@example.com")
    with store.connect(True) as writer:
        assert writer is not reader
        assert writer.execute("PRAGMA query_only").fetchone()[0] == 0
        writer.execute("UPDATE users SET active=0")
    with store.connect() as reader:
        with pytest.raises(CashbackError, match="forbidden"):
            store._actor(reader, "admin@example.com")


def test_read_snapshot_is_consistent_and_next_lease_sees_write(store):
    with store.connect() as reader:
        reader.execute("BEGIN")
        assert reader.execute("SELECT active FROM users").fetchone()[0] == 1
        with store.connect(True) as writer:
            writer.execute("UPDATE users SET active=0")
        assert reader.execute("SELECT active FROM users").fetchone()[0] == 1
    with store.connect() as reader:
        assert reader.execute("SELECT active FROM users").fetchone()[0] == 0


def test_parallel_leases_are_exclusive_and_idle_readers_are_bounded(store):
    barrier = threading.Barrier(8)

    def read(_):
        with store.connect() as db:
            barrier.wait(timeout=10)
            assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
            return id(db)

    with ThreadPoolExecutor(max_workers=8) as executor:
        identities = list(executor.map(read, range(8)))
    assert len(set(identities)) == 8
    assert len(store._readers) == 4
    assert all(not db.in_transaction for db in store._readers)


def test_close_clears_idle_readers_and_closes_active_lease_on_return(store):
    with store.connect() as idle:
        pass
    with store.connect() as active:
        assert active is idle
        store.close()
        assert active.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    assert store._readers == []
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        active.execute("SELECT 1")


def test_explicit_read_only_store_skips_migrations_and_rejects_writes(store):
    before = store.path.read_bytes()
    reader = CashbackStore(store.path, read_only=True, reuse_reads=True)
    try:
        assert reader.users("admin@example.com")
        with pytest.raises(CashbackError, match="read_only"):
            with reader.connect(True):
                pytest.fail("A read-only benchmark must not admit a writer")
        assert store.path.read_bytes() == before
    finally:
        reader.close()


def test_read_only_constructor_does_not_create_missing_file(tmp_path):
    path = tmp_path / "missing" / "cash.sqlite3"
    reader = CashbackStore(path, read_only=True, reuse_reads=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            with reader.connect():
                pytest.fail("Missing read-only ledgers must fail")
        assert not path.exists() and not path.parent.exists()
    finally:
        reader.close()
