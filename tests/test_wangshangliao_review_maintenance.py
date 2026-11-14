"""Background review retention and stale-envelope replay protections."""

import asyncio
import sqlite3
import threading
import time
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import content_rules
from astrbot.builtin_stars.wangshangliao_moderation import review_maintenance as module
from astrbot.builtin_stars.wangshangliao_moderation.review_store import ReviewStore
from astrbot.core.platform.sources.wangshangliao import automatic
from astrbot.core.platform.sources.wangshangliao.diagnostics import Diagnostics
from tests.test_wangshangliao_content_rules import fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("stamp", ["old", "missing", "invalid", "nan", "future"])
async def test_stale_envelopes_do_not_recreate_retired_dedup_rows(
    tmp_path, monkeypatch, stamp
):
    adapter, _ = fixture(tmp_path, monkeypatch)
    store = Mock(side_effect=AssertionError("historical_review_write"))
    monkeypatch.setattr(content_rules, "review_store", store)
    payload = {
        "sender": "2",
        "text": "加我vx:abc123456",
        "recall_route": {
            "time": {
                "old": (time.time() - 301) * 1000,
                "missing": None,
                "invalid": "invalid",
                "nan": "nan",
                "future": (time.time() + 61) * 1000,
            }[stamp]
        },
    }
    for _ in range(2):
        assert await content_rules.enforce(adapter, "5", "retired", payload)
    store.assert_not_called()
    adapter.get_moderation_members.assert_not_awaited()
    adapter.recall_violation.assert_not_awaited()
    adapter.execute_moderation.assert_not_awaited()
    assert adapter.diagnostics.emit.call_args.args[1] == "historical_or_unknown_time"


@pytest.fixture
def maintenance(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "instance_dir", lambda _: tmp_path)
    store = ReviewStore(tmp_path / "moderation.sqlite3")
    adapter = SimpleNamespace(
        config={
            "id": "fixture",
            "type": "wangshangliao",
            "_business_epoch": 1,
        },
        account="1",
        _review_store=store,
        diagnostics=Diagnostics("fixture"),
    )
    context = SimpleNamespace(
        platform_manager=SimpleNamespace(platform_insts=[adapter])
    )
    return module.ReviewMaintenance(context), adapter, store


@pytest.mark.asyncio
async def test_existing_database_indexes_are_prepared_before_maintenance(
    maintenance, monkeypatch
):
    runtime, _, store = maintenance
    store.path.touch()
    thread = threading.get_ident()
    prepared = []

    def initialize():
        assert threading.get_ident() != thread
        prepared.append(True)

    monkeypatch.setattr(store, "initialize", initialize)
    await runtime.initialize()
    assert prepared == [True]
    assert runtime.inflight is None
    await runtime.close()


@pytest.mark.asyncio
async def test_unused_database_is_not_created_during_startup(maintenance):
    runtime, _, store = maintenance
    await runtime.initialize()
    assert not store.path.exists()
    assert not store.initialized
    await runtime.close()


@pytest.mark.asyncio
async def test_startup_prepares_old_schema_before_platform_instances_exist(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(module, "instance_dir", lambda _: tmp_path)
    path = tmp_path / "moderation.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript(
            "CREATE TABLE progressive_actions(operation TEXT PRIMARY KEY,"
            "account TEXT,group_id TEXT,member TEXT,message TEXT,"
            "recall_status TEXT,result TEXT,created REAL);"
            "INSERT INTO progressive_actions VALUES("
            "'original','1','5','2','m','unknown','{}',1);"
        )
    context = SimpleNamespace(
        platform_manager=SimpleNamespace(platform_insts=[]),
        get_config=lambda: {
            "platform": [{"id": "fixture", "type": "wangshangliao", "account_id": "1"}]
        },
    )
    runtime = module.ReviewMaintenance(context)
    try:
        await runtime.initialize()
        with closing(sqlite3.connect(path)) as db:
            assert db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE name='review_ref_progressive_actions'"
            ).fetchone()
            assert db.execute(
                "SELECT operation,recall_status FROM progressive_actions"
            ).fetchall() == [("original", "unknown")]
        store = ReviewStore(path)
        store.initialize()
        assert sum(store.prune("1").values()) == 0
        assert store.retention_stats["skipped"] == ""
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_startup_index_preparation_drains_on_cancellation(
    maintenance, monkeypatch
):
    runtime, _, store = maintenance
    store.path.touch()
    entered, release = threading.Event(), threading.Event()

    def initialize():
        entered.set()
        if not release.wait(3):
            raise TimeoutError("test_worker_not_released")

    monkeypatch.setattr(store, "initialize", initialize)
    work = asyncio.create_task(runtime.initialize())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        work.cancel()
        await asyncio.sleep(0.01)
        assert not work.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await work
        assert runtime.inflight is None
        assert not runtime.active
    finally:
        release.set()
        await asyncio.gather(work, return_exceptions=True)
        await runtime.close()


def test_later_automatic_schema_is_immediately_ready_and_unknown_is_retained(
    tmp_path,
):
    store = ReviewStore(tmp_path / "moderation.sqlite3")
    store.initialize()
    now = time.time()
    old = now - 91 * 86400
    adapter = SimpleNamespace(config={"id": "fixture"})
    with closing(automatic.database(adapter, path=store.path)) as db, db:
        db.execute(
            "INSERT INTO automatic_mutes VALUES(?,?,?,?,?,?,?,?,?)",
            ("unknown", "1", "5", "2", "22", "uncertain", "unknown", old, 0),
        )
    for mid in ("uncertain", "resolved"):
        assert store.observe("1", "5", mid, "2", "external_promotion", old, 0)
        store.finish("1", "5", mid, "muted", "accepted")
    assert store.prune("1", now=now)["content_violations"] == 1
    assert store.retention_stats["skipped"] == ""
    assert store.content_outcome("1", "5", "uncertain") == "muted"
    assert store.content_outcome("1", "5", "resolved") == "not_applied"
    with store.connect() as db:
        assert db.execute("SELECT status FROM automatic_mutes").fetchall() == [
            ("unknown",)
        ]


@pytest.mark.asyncio
async def test_maintenance_is_account_scoped_bounded_and_off_loop(
    maintenance, monkeypatch
):
    runtime, adapter, store = maintenance
    main_thread = threading.get_ident()

    def prune(*, account, limit, guard):
        assert account == "1"
        assert limit == 128
        assert threading.get_ident() != main_thread
        guard()
        return {"content": 2, "semantic": 1}

    monkeypatch.setattr(store, "prune", prune)
    try:
        await runtime.step()
        assert adapter.diagnostics.gauges["review_cleanup_deleted"] == 3
        assert runtime.inflight is None
        assert not store.path.exists()
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["absent", "path", "account", "type"])
async def test_maintenance_does_not_create_or_touch_unowned_stores(
    maintenance, monkeypatch, change, tmp_path
):
    runtime, adapter, store = maintenance
    prune = Mock(side_effect=AssertionError("unowned_store_prune"))
    monkeypatch.setattr(store, "prune", prune)
    if change == "absent":
        del adapter._review_store
    elif change == "path":
        store.path = tmp_path / "other.sqlite3"
    elif change == "account":
        adapter.account = ""
    else:
        adapter.config["type"] = "other"
    await runtime.step()
    prune.assert_not_called()
    assert not store.path.exists()
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", [True, False])
async def test_writer_contention_defers_but_unexpected_errors_fail(
    maintenance, monkeypatch, busy
):
    runtime, adapter, store = maintenance
    emit = Mock()
    adapter.diagnostics.emit = emit
    error = sqlite3.OperationalError("fixture")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY if busy else sqlite3.SQLITE_ERROR
    monkeypatch.setattr(store, "prune", Mock(side_effect=error))
    await runtime.step()
    if busy:
        emit.assert_called_once_with("review_maintenance", "deferred")
    else:
        emit.assert_called_once_with(
            "review_maintenance", "failed", failed=True, error="operation_failed"
        )
    assert runtime.inflight is None
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["close", "cancel", "account", "generation"])
async def test_cleanup_drains_thread_and_revokes_late_commit(
    maintenance, monkeypatch, change
):
    runtime, adapter, store = maintenance
    entered, release = threading.Event(), threading.Event()
    committed = []

    def prune(*, account, limit, guard):
        guard()
        entered.set()
        if not release.wait(3):
            raise TimeoutError("test_worker_not_released")
        guard()
        committed.append(account)
        return {"content": 1}

    monkeypatch.setattr(store, "prune", prune)
    work = asyncio.create_task(runtime.step())
    closing = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        if change == "close":
            closing = asyncio.create_task(runtime.close())
        elif change == "cancel":
            work.cancel()
        elif change == "account":
            adapter.account = "2"
        else:
            adapter.config["_business_epoch"] += 1
        await asyncio.sleep(0.01)
        assert not work.done()
        if closing is not None:
            assert not closing.done()
        release.set()
        if change == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await work
        else:
            await work
        if closing is not None:
            await closing
        assert not committed
        assert runtime.inflight is None
    finally:
        release.set()
        await asyncio.gather(work, return_exceptions=True)
        await runtime.close()
