import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from data.plugins.astrbot_plugin_lajiaoliao.storage import (
    SessionError,
    SessionStore,
    atomic_json,
)


def test_private_storage_and_ownership(tmp_path):
    store = SessionStore(tmp_path / "private")
    row = store.create("alice")
    ref = row["session_ref"]
    assert (store.root.stat().st_mode & 0o777) == 0o700
    assert ((store.directory(ref) / "session.json").stat().st_mode & 0o777) == 0o600
    with pytest.raises(SessionError, match="access_denied"):
        store.get(ref, "bob")
    assert store.list_owned("bob") == []
    with pytest.raises(SessionError, match="invalid_session_ref"):
        store.directory("../token")


def test_single_draft_and_expiry(tmp_path):
    store = SessionStore(tmp_path)
    row = store.create("alice")
    with pytest.raises(SessionError, match="already_exists"):
        store.create("alice")
    row["expires_at"] = time.time() - 1
    store._write(row)
    path = store.directory(row["session_ref"])
    atomic_json(path / "token.json", {"synthetic": True})
    with pytest.raises(SessionError, match="expired"):
        store.get(row["session_ref"], "alice")
    assert store.expire() == [row["session_ref"]]
    assert not (path / "token.json").exists()
    store.create("alice")


def test_unique_account_and_instance_binding(tmp_path):
    store = SessionStore(tmp_path)
    a = store.create("alice")["session_ref"]
    b = store.create("bob")["session_ref"]
    store.complete(a, "alice", "user-1")
    with pytest.raises(SessionError, match="already_registered"):
        store.complete(b, "bob", "user-1")
    store.complete(b, "bob", "user-2")
    store.bind(a, "alice", "bot")
    with pytest.raises(SessionError, match="instance_already_bound"):
        store.bind(b, "bob", "bot")
    with pytest.raises(SessionError, match="session_already_bound"):
        store.bind(a, "alice", "another")
    assert store.for_instance(a, "bot")["account_id"] == "user-1"
    with pytest.raises(SessionError, match="binding_mismatch"):
        store.for_instance(a, "another")


def test_concurrent_binding(tmp_path):
    store = SessionStore(tmp_path)
    ref = store.create("alice")["session_ref"]
    store.complete(ref, "alice", "u")

    def bind(instance):
        try:
            return store.bind(ref, "alice", instance)["instance_id"]
        except SessionError:
            return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(bind, ["one", "two"]))
    assert len([item for item in results if item]) == 1


def test_consumer_lock_and_revocation(tmp_path):
    store = SessionStore(tmp_path)
    ref = store.create("alice")["session_ref"]
    fd = store.acquire_consumer(ref)
    try:
        with pytest.raises(SessionError, match="consumer_already_connected"):
            store.acquire_consumer(ref)
    finally:
        os.close(fd)
    fd = store.acquire_consumer(ref)
    os.close(fd)
    atomic_json(store.directory(ref) / "token.json", {"synthetic": True})
    store.revoke(ref, "alice")
    assert not (store.directory(ref) / "token.json").exists()
    assert json.loads((store.directory(ref) / "session.json").read_text())["state"] == "revoked"
