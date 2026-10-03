import importlib
import sqlite3
import time

import pytest

mod = importlib.import_module("data.plugins.astrbot_plugin_telethon_ai.tenants")


@pytest.fixture
def store(tmp_path):
    s = mod.Tenants(tmp_path / "tenants.db")
    yield s
    s.close()


def ready(s, suffix="1", account="one", chat="-1001", budget=2):
    t = s.create_trial("admin", suffix, suffix, "AIClient_" + suffix, budget=budget)
    s.assign("admin", t, account)
    s.authorize_group("admin", t, chat)
    s.set_enabled("admin", t, True)
    return t


def test_exclusive_accounts_groups_and_bot(store):
    ready(store)
    b = store.create_trial("admin", "2", "2", "AIClient_2")
    with pytest.raises(sqlite3.IntegrityError):
        store.assign("admin", b, "one")
    with pytest.raises(sqlite3.IntegrityError):
        store.authorize_group("admin", b, "-1001")
    with pytest.raises(sqlite3.IntegrityError):
        store.create_trial("admin", "3", "1", "AIClient_3")


def test_ownership(store):
    t = ready(store)
    assert store.owned("AIClient_1", "1", "1")["id"] == t
    for values in [
        ("AIClient_1", "1", "2"),
        ("AIClient_1", "2", "1"),
        ("AIClient_other", "1", "1"),
    ]:
        with pytest.raises(mod.Denied):
            store.owned(*values)


def test_reservation_limits_and_no_double_settlement(store):
    t = ready(store)
    first, _ = store.reserve("one", "-1001", "1")
    with pytest.raises(mod.Denied):
        store.reserve("one", "-1001", "2")
    assert store.can_send(first)
    store.finish(first, "sent")
    store.finish(first, "released")
    assert store.summary(t)["used"] == 1
    with pytest.raises(mod.Denied):
        store.reserve("one", "-1001", "1")
    second, _ = store.reserve("one", "-1001", "2")
    store.finish(second, "uncertain")
    with pytest.raises(mod.Denied):
        store.reserve("one", "-1001", "3")


def test_suspend_expiry_and_group_recheck(store):
    t = ready(store)
    r, _ = store.reserve("one", "-1001", "1")
    store.set_enabled("admin", t, False)
    assert not store.can_send(r)
    with pytest.raises(mod.Denied):
        store.reserve("one", "-1001", "2")
    store.set_enabled("admin", t, True)
    with store.db:
        store.db.execute(
            "UPDATE tenants SET expires=? WHERE id=?", (time.time() - 1, t)
        )
    assert not store.can_send(r)
    with pytest.raises(mod.Denied):
        store.set_enabled("admin", t, True)


def test_two_tenants_do_not_share_quota_or_groups(store):
    a = ready(store)
    b = ready(store, "2", "two", "-1002")
    store.reserve("one", "-1001", "1")
    store.reserve("two", "-1002", "1")
    for account, chat in [("one", "-1002"), ("two", "-1001")]:
        with pytest.raises(mod.Denied):
            store.reserve(account, chat, "2")
    assert store.summary(a)["used"] == store.summary(b)["used"] == 1


def test_atomic_budget_across_connections(store):
    t = ready(store, budget=1)
    path = store.db.execute("PRAGMA database_list").fetchone()[2]
    other = mod.Tenants(path)
    try:
        r, _ = store.reserve("one", "-1001", "1")
        store.finish(r, "sent")
        with pytest.raises(mod.Denied):
            other.reserve("one", "-1001", "2")
        assert other.summary(t)["used"] == 1
    finally:
        other.close()


def test_expiry_blocks_new_admission_and_pending_send(store):
    t = ready(store)
    reservation, _ = store.reserve("one", "-1001", "1")
    with store.db:
        store.db.execute(
            "UPDATE tenants SET expires=? WHERE id=?", (time.time() - 1, t)
        )
    assert not store.can_send(reservation)
    with pytest.raises(mod.Denied):
        store.reserve("one", "-1001", "2")


def test_resume_preserves_usage_and_exhaustion(store):
    t = ready(store, budget=1)
    reservation, _ = store.reserve("one", "-1001", "1")
    store.finish(reservation, "sent")
    store.set_enabled("admin", t, False)
    with pytest.raises(mod.Denied):
        store.reserve("one", "-1001", "2")
    store.set_enabled("admin", t, True)
    assert store.summary(t)["used"] == 1
    with pytest.raises(mod.Denied):
        store.reserve("one", "-1001", "3")
