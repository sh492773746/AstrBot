"""Storage-only membership removal, name migration and cross-list exclusion."""

import errno
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import member_exemptions
from astrbot.core.platform.sources.wangshangliao import blacklist
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError


@pytest.fixture
def lists(tmp_path, monkeypatch):
    """Redirect both stores into the same temporary instance root.

    Args:
        tmp_path: Isolated test directory.
        monkeypatch: Scoped instance path overrides.

    Returns:
        Temporary root and blacklist store for one test bot.
    """
    monkeypatch.setattr(blacklist, "instance_dir", lambda instance: tmp_path / instance)
    monkeypatch.setattr(
        member_exemptions, "instance_dir", lambda instance: tmp_path / instance
    )
    return tmp_path / "bot", blacklist.BlacklistStore("bot")


def white(*, enabled=True, peer="903", guard=lambda: None, **options):
    """Write one isolated whitelist member with optional legacy-compatible fields.

    Args:
        enabled: Whether the saved binding is enabled.
        peer: Stored transport identity, not a current-roster requirement.
        guard: Synchronous transaction authority check.
        options: Additional storage arguments.

    Returns:
        Existing exemption writer result.
    """
    return member_exemptions.set_exemption(
        "bot",
        "1",
        "5",
        "3",
        peer,
        enabled=enabled,
        actor="server/test",
        reason="Explicit storage test",
        guard=guard,
        **options,
    )


def black(store, *, enabled=True, peer="903", guard=lambda: None, **options):
    """Write the same isolated member through the blacklist store.

    Args:
        store: Temporary blacklist store.
        enabled: Whether this enrollment is active.
        peer: Stored transport identity.
        guard: Synchronous transaction authority check.
        options: Additional storage arguments.

    Returns:
        Current blacklist version and entries.
    """
    return store.set_member(
        "1",
        "5",
        "3",
        peer,
        "Saved Member",
        enabled=enabled,
        actor="dashboard/test",
        guard=guard,
        **options,
    )


def legacy_whitelist(root, *, member="3", peer="903"):
    """Create the actual pre-name SQLite schema, without new storage APIs.

    Args:
        root: Temporary instance root.
        member: Persisted historical UID, retained verbatim.
        peer: Persisted historical transport identity.
    """
    root.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(root / "member_exemptions.sqlite3")) as db:
        db.execute(
            "CREATE TABLE speech_exemptions("
            "account TEXT,group_id TEXT,member TEXT,peer TEXT,enabled INTEGER,"
            "created REAL,actor TEXT,reason TEXT,PRIMARY KEY(account,group_id,member))"
        )
        db.execute(
            "CREATE TABLE exemption_audit("
            "account TEXT,group_id TEXT,member TEXT,peer TEXT,enabled INTEGER,"
            "created REAL,actor TEXT,reason TEXT)"
        )
        values = ("1", "5", member, peer, 1, 123.0, "server/legacy", "Legacy entry")
        db.execute("INSERT INTO speech_exemptions VALUES(?,?,?,?,?,?,?,?)", values)
        db.execute("INSERT INTO exemption_audit VALUES(?,?,?,?,?,?,?,?)", values)
        db.commit()


@pytest.mark.parametrize("member", ["3", "0003"])
def test_legacy_name_reads_preserve_ids_versions_and_database_bytes(lists, member):
    root, _ = lists
    legacy_whitelist(root, member=member)
    path = root / "member_exemptions.sqlite3"
    before = path.read_bytes()
    assert member_exemptions.read_exemptions("bot", "1", "5") == (1, {member: "903"})
    assert member_exemptions.read_exemptions("bot", "1", "5", include_names=True) == (
        1,
        {member: {"peer": "903", "name": ""}},
    )
    assert path.read_bytes() == before


def test_name_migration_is_additive_and_old_callers_keep_their_return_shape(lists):
    root, _ = lists
    legacy_whitelist(root)
    member_exemptions.set_exemption(
        "bot",
        "1",
        "5",
        "4",
        "904",
        enabled=True,
        actor="server/test",
        reason="New entry",
        guard=lambda: None,
        name="Saved Display Name",
        expected_version=1,
    )
    assert member_exemptions.read_exemptions("bot", "1", "5") == (
        2,
        {"3": "903", "4": "904"},
    )
    assert member_exemptions.read_exemptions("bot", "1", "5", include_names=True) == (
        2,
        {
            "3": {"peer": "903", "name": ""},
            "4": {"peer": "904", "name": "Saved Display Name"},
        },
    )
    with closing(sqlite3.connect(root / "member_exemptions.sqlite3")) as db:
        assert db.execute(
            "SELECT account,group_id,member,peer,enabled,created,actor,reason,name "
            "FROM exemption_audit ORDER BY rowid LIMIT 1"
        ).fetchone() == (
            "1",
            "5",
            "3",
            "903",
            1,
            123.0,
            "server/legacy",
            "Legacy entry",
            "",
        )
    white(enabled=False, expected_version=2)
    assert member_exemptions.read_exemptions("bot", "1", "5") == (3, {"4": "904"})


@pytest.mark.parametrize("member", ["3", "0003"])
def test_old_uid_and_peer_bindings_remain_removable_after_name_migration(lists, member):
    root, _ = lists
    legacy_whitelist(root, member=member)
    member_exemptions.set_exemption(
        "bot",
        "1",
        "5",
        member,
        "903",
        enabled=False,
        actor="server/test",
        reason="Remove stored binding",
        guard=lambda: None,
        expected_version=1,
    )
    assert member_exemptions.read_exemptions("bot", "1", "5", include_names=True) == (
        2,
        {},
    )
    with closing(sqlite3.connect(root / "member_exemptions.sqlite3")) as db:
        assert db.execute(
            "SELECT member,peer,enabled FROM speech_exemptions"
        ).fetchone() == (member, "903", 0)


def test_saved_whitelist_name_survives_old_writer_and_stored_binding_removal(lists):
    root, _ = lists
    white(name="Former Member")
    white()
    assert member_exemptions.read_exemptions("bot", "1", "5", include_names=True) == (
        2,
        {"3": {"peer": "903", "name": "Former Member"}},
    )
    assert not member_exemptions.is_exempt("bot", "1", "5", "3", "999")
    white(enabled=False, expected_version=2)
    assert member_exemptions.read_exemptions("bot", "1", "5") == (3, {})
    with closing(sqlite3.connect(root / "member_exemptions.sqlite3")) as db:
        assert db.execute(
            "SELECT peer,enabled,name FROM speech_exemptions WHERE member='3'"
        ).fetchone() == ("903", 0, "Former Member")


def test_name_update_and_stale_version_preserve_transaction_audit(lists):
    root, _ = lists
    white(name="Saved Member")
    white(name="Renamed Member", expected_version=1)
    with pytest.raises(ProtocolError, match="^member_whitelist_changed$"):
        white(name="Stale Preview", expected_version=1)
    assert member_exemptions.read_exemptions("bot", "1", "5", include_names=True) == (
        2,
        {"3": {"peer": "903", "name": "Renamed Member"}},
    )
    with closing(sqlite3.connect(root / "member_exemptions.sqlite3")) as db:
        assert db.execute(
            "SELECT name FROM exemption_audit ORDER BY rowid"
        ).fetchall() == [("Saved Member",), ("Renamed Member",)]


def test_revoked_legacy_migration_rolls_back_columns_names_and_version(lists):
    root, _ = lists
    legacy_whitelist(root)
    checks = 0

    def guard():
        nonlocal checks
        checks += 1
        if checks == 2:
            raise ProtocolError("revoked")

    with pytest.raises(ProtocolError, match="^revoked$"):
        white(name="Must Roll Back", guard=guard)
    assert member_exemptions.read_exemptions("bot", "1", "5", include_names=True) == (
        1,
        {"3": {"peer": "903", "name": ""}},
    )
    with closing(sqlite3.connect(root / "member_exemptions.sqlite3")) as db:
        for table in ("speech_exemptions", "exemption_audit"):
            assert "name" not in {
                row[1] for row in db.execute(f"PRAGMA table_info({table})")
            }


@pytest.mark.parametrize("name", [None, 17, "x" * 1025])
def test_invalid_names_do_not_create_a_store(lists, name):
    root, _ = lists
    with pytest.raises(ValueError, match="^member_exemption_arguments$"):
        white(name=name)
    assert not root.exists()


@pytest.mark.parametrize("first", ["black", "white"])
def test_enabling_opposite_list_rejects_same_uid_even_with_a_different_peer(
    lists, first
):
    root, store = lists
    store.read("1", "5")
    if first == "black":
        black(store)
        with pytest.raises(ProtocolError, match="^member_list_conflict$"):
            white(peer="999")
        assert len(store.read("1", "5")[1]) == 1
        assert member_exemptions.read_exemptions("bot", "1", "5") == (0, {})
    else:
        white()
        with pytest.raises(ProtocolError, match="^member_list_conflict$"):
            black(store, peer="999")
        assert store.read("1", "5") == (0, [])
        assert member_exemptions.read_exemptions("bot", "1", "5") == (1, {"3": "903"})
    assert root.is_dir()


@pytest.mark.parametrize("first", ["black", "white"])
def test_legacy_zero_padded_uid_does_not_bypass_opposite_conflict(lists, first):
    root, store = lists
    if first == "black":
        black(store)
        with pytest.raises(ProtocolError, match="^member_list_conflict$"):
            member_exemptions.set_exemption(
                "bot",
                "01",
                "005",
                "0003",
                "999",
                enabled=True,
                actor="server/test",
                reason="Same business identity",
                guard=lambda: None,
            )
    else:
        legacy_whitelist(root, member="0003", peer="999")
        before = (root / "member_exemptions.sqlite3").read_bytes()
        with pytest.raises(ProtocolError, match="^member_list_conflict$"):
            black(store)
        assert (root / "member_exemptions.sqlite3").read_bytes() == before


@pytest.mark.parametrize("first", ["black", "white"])
def test_removing_opposite_membership_allows_add_without_rewriting_history(
    lists, first
):
    root, store = lists
    if first == "black":
        black(store)
        black(store, enabled=False)
        before = store.path.read_bytes()
        white(peer="999")
        assert store.path.read_bytes() == before
    else:
        white()
        white(enabled=False)
        path = root / "member_exemptions.sqlite3"
        before = path.read_bytes()
        black(store, peer="999")
        assert path.read_bytes() == before


@pytest.mark.parametrize("side", ["black", "white"])
def test_historical_overlap_remains_readable_and_either_side_can_be_removed(
    lists, side
):
    root, store = lists
    black(store)
    legacy_whitelist(root, peer="999")
    before = store.read("1", "5")
    assert before[1][0]["peer"] == "903"
    assert member_exemptions.read_exemptions("bot", "1", "5") == (1, {"3": "999"})
    for write in (lambda: black(store), lambda: white(peer="999")):
        with pytest.raises(ProtocolError, match="^member_list_conflict$"):
            write()
    assert store.read("1", "5") == before
    assert member_exemptions.read_exemptions("bot", "1", "5") == (1, {"3": "999"})
    if side == "black":
        black(store, enabled=False, expected_version=1)
        assert store.read("1", "5") == (2, [])
        assert member_exemptions.read_exemptions("bot", "1", "5") == (1, {"3": "999"})
        with store._transaction() as db:
            assert tuple(
                db.execute("SELECT status,peer,name FROM blacklist_entries").fetchone()
            ) == ("removed", "903", "Saved Member")
    else:
        white(enabled=False, peer="999", expected_version=1)
        assert member_exemptions.read_exemptions("bot", "1", "5") == (2, {})
        assert store.read("1", "5") == before


@pytest.mark.parametrize("first", ["black", "white"])
@pytest.mark.parametrize("scope", ["instance", "account", "group", "member"])
def test_conflict_is_scoped_to_same_bot_group_and_uid(lists, first, scope):
    _, store = lists
    if first == "black":
        black(store)
    else:
        white()
    instance = "another-bot" if scope == "instance" else "bot"
    account = "2" if scope == "account" else "1"
    group = "6" if scope == "group" else "5"
    member = "4" if scope == "member" else "3"
    if first == "black":
        member_exemptions.set_exemption(
            instance,
            account,
            group,
            member,
            "903",
            enabled=True,
            actor="server/test",
            reason="Independent scope",
            guard=lambda: None,
        )
        assert member in member_exemptions.read_exemptions(instance, account, group)[1]
    else:
        other = blacklist.BlacklistStore(instance)
        assert (
            other.set_member(
                account,
                group,
                member,
                "903",
                "Independent",
                True,
                "dashboard/test",
                lambda: None,
            )[1][0]["member"]
            == member
        )


@pytest.mark.parametrize("first", ["black", "white"])
def test_absent_opposite_store_is_never_created(lists, first):
    root, store = lists
    if first == "black":
        black(store)
        assert not (root / "member_exemptions.sqlite3").exists()
    else:
        white()
        assert not store.path.exists()


@pytest.mark.parametrize("first", ["black", "white"])
def test_cross_store_lock_covers_check_through_commit(lists, first):
    _, store = lists
    entered, release, attempted = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    checks = 0

    def guard():
        nonlocal checks
        checks += 1
        if checks == 2:
            entered.set()
            assert release.wait(5)

    def winner():
        if first == "black":
            black(store, guard=guard)
        else:
            white(guard=guard)

    def contender():
        attempted.set()
        if first == "black":
            white(peer="999")
        else:
            black(store, peer="999")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_write = pool.submit(winner)
        try:
            assert entered.wait(3)
            second_write = pool.submit(contender)
            assert attempted.wait(3)
            time.sleep(0.05)
            assert not second_write.done()
        finally:
            release.set()
        first_write.result(timeout=5)
        with pytest.raises(ProtocolError, match="^member_list_conflict$"):
            second_write.result(timeout=5)


@pytest.mark.parametrize("round_number", range(8))
def test_simultaneous_black_and_white_enrollment_has_exactly_one_winner(
    lists, round_number
):
    _, store = lists
    barrier = threading.Barrier(2)

    def write(side):
        barrier.wait(timeout=3)
        try:
            if side == "black":
                black(store)
            else:
                white(peer="999")
        except ProtocolError as exc:
            assert str(exc) == "member_list_conflict"
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write, side) for side in ("black", "white")]
        assert sorted(future.result(timeout=5) for future in futures) == [False, True]
    assert bool(store.read("1", "5")[1]) != bool(
        member_exemptions.read_exemptions("bot", "1", "5")[1]
    )


@pytest.mark.parametrize("side", ["black", "white"])
def test_guard_rollback_releases_shared_lock_and_allows_other_store(lists, side):
    _, store = lists
    store.read("1", "5")
    checks = 0

    def guard():
        nonlocal checks
        checks += 1
        if checks == 2:
            raise ProtocolError("revoked")

    with pytest.raises(ProtocolError, match="^revoked$"):
        if side == "black":
            black(store, guard=guard)
        else:
            white(guard=guard)
    with ThreadPoolExecutor(max_workers=1) as pool:
        work = pool.submit(white if side == "black" else lambda: black(store))
        work.result(timeout=3)


def test_reads_settings_and_intents_never_wait_for_member_list_write_lock(
    lists, monkeypatch
):
    _, store = lists
    white(enabled=False)
    black(store)
    lock = threading.RLock()
    monkeypatch.setattr(blacklist, "MEMBER_LIST_WRITE_LOCK", lock)
    monkeypatch.setattr(member_exemptions, "MEMBER_LIST_WRITE_LOCK", lock)

    def frequent_work():
        assert member_exemptions.read_exemptions("bot", "1", "5") == (1, {})
        assert not member_exemptions.is_exempt("bot", "1", "5", "3", "903")
        assert store.read("1", "5")[0] == 1
        assert store.settings("1", "5")["mute_minutes"] == 41760
        store.configure("1", "5", 60, "dashboard/test", lambda: None)
        intent = store.reserve("1", "5", "3", "903", "42", time.time(), lambda: None)
        assert intent is not None
        store.done(intent, {"status": "rejected", "submitted": False})

    with ThreadPoolExecutor(max_workers=1) as pool:
        with lock:
            pool.submit(frequent_work).result(timeout=3)


@pytest.mark.parametrize("side", ["black", "white"])
@pytest.mark.parametrize("fault", ["corrupt", "missing_table", "locked", "permission"])
def test_unreadable_opposite_list_blocks_add_but_never_blocks_remove(
    lists, monkeypatch, side, fault
):
    root, store = lists
    if side == "black":
        black(store)
        path = root / "member_exemptions.sqlite3"
        legacy_whitelist(root, member="4")
    else:
        white()
        store.read("1", "5")
        path = store.path

    def write(enabled):
        if side == "black":
            return black(store, enabled=enabled)
        return white(enabled=enabled)

    lock = None
    if fault == "corrupt":
        path.write_bytes(b"not a sqlite database")
    elif fault == "missing_table":
        table = "speech_exemptions" if side == "black" else "blacklist_entries"
        with closing(sqlite3.connect(path)) as db:
            db.execute("DROP TABLE " + table)
            db.commit()
    elif fault == "locked":
        lock = sqlite3.connect(path)
        lock.execute("BEGIN EXCLUSIVE")
        connect = sqlite3.connect

        def fast_connect(database, **options):
            options["timeout"] = 0
            return connect(database, **options)

        target = blacklist if side == "black" else member_exemptions
        monkeypatch.setattr(
            target,
            "sqlite3",
            type(
                "SQLite",
                (),
                {"connect": staticmethod(fast_connect), "Row": sqlite3.Row},
            ),
        )
    else:
        stat = type(path).stat

        def denied(current, **options):
            if current == path:
                raise PermissionError(errno.EACCES, "Permission denied")
            return stat(current, **options)

        monkeypatch.setattr(type(path), "stat", denied)
    try:
        with pytest.raises((sqlite3.Error, PermissionError)):
            write(True)
        write(False)
    finally:
        if lock is not None:
            lock.rollback()
            lock.close()
    if side == "black":
        assert store.read("1", "5") == (2, [])
    else:
        assert member_exemptions.read_exemptions("bot", "1", "5") == (2, {})
