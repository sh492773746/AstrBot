"""Worker affinity, cancellation and authority boundaries for durable writes."""

import asyncio
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from astrbot.core.platform.sources.wangshangliao.database import (
    AsyncDatabase,
    account_guard,
)
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError


@pytest.mark.asyncio
async def test_connection_and_helpers_stay_on_one_worker(tmp_path):
    path = tmp_path / "worker.sqlite3"
    threads = []

    def connect():
        threads.append(threading.get_ident())
        return sqlite3.connect(path)

    async with AsyncDatabase(connect, write=True) as db:
        await db.execute("CREATE TABLE values_test(value)")
        await db.execute("INSERT INTO values_test VALUES(?)", (7,))
        assert await db.fetchone("SELECT value FROM values_test") == (7,)
        assert await db.fetchall("SELECT value FROM values_test") == [(7,)]
        threads.append(await db.call(lambda connection: threading.get_ident()))
    assert len(set(threads)) == 1
    assert threads[0] != threading.get_ident()
    with sqlite3.connect(path) as check:
        assert check.execute("SELECT value FROM values_test").fetchone() == (7,)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_waiting_for_writer_does_not_block_loop_or_ignore_revocation(
    tmp_path, cancel
):
    path = tmp_path / "busy.sqlite3"
    blocker = sqlite3.connect(path)
    blocker.execute("CREATE TABLE values_test(value)")
    blocker.commit()
    blocker.execute("BEGIN IMMEDIATE")
    opened = threading.Event()
    authorized = True

    def connect():
        db = sqlite3.connect(path, timeout=2)
        opened.set()
        return db

    def guard():
        if not authorized:
            raise ProtocolError("revoked")

    async def write():
        async with AsyncDatabase(connect, write=True, guard=guard) as db:
            await db.execute("INSERT INTO values_test VALUES(1)")

    work = asyncio.create_task(write())
    try:
        assert await asyncio.to_thread(opened.wait, 1)
        await asyncio.sleep(0.02)
        assert not work.done()
        if cancel:
            work.cancel()
        else:
            authorized = False
        blocker.rollback()
        with pytest.raises(asyncio.CancelledError if cancel else ProtocolError):
            await asyncio.wait_for(work, 3)
        assert blocker.execute("SELECT COUNT(*) FROM values_test").fetchone() == (0,)
    finally:
        blocker.rollback()
        blocker.close()
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_inflight_query_is_drained_and_rolled_back(tmp_path):
    path = tmp_path / "cancel.sqlite3"
    with sqlite3.connect(path) as setup:
        setup.execute("CREATE TABLE values_test(value)")
    entered, release = threading.Event(), threading.Event()

    def hold(connection):
        entered.set()
        if not release.wait(3):
            raise TimeoutError("fixture_not_released")
        return connection.execute("SELECT 1").fetchone()

    async def write():
        async with AsyncDatabase(lambda: sqlite3.connect(path), write=True) as db:
            await db.execute("INSERT INTO values_test VALUES(1)")
            await db.call(hold)

    work = asyncio.create_task(write())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        work.cancel()
        await asyncio.sleep(0)
        assert not work.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(work, 3)
        with sqlite3.connect(path) as check:
            check.execute("BEGIN IMMEDIATE")
            assert check.execute("SELECT COUNT(*) FROM values_test").fetchone() == (0,)
    finally:
        release.set()
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["account", "instance", "generation", "revision", "disabled", "plugin_disabled"],
)
async def test_mid_transaction_identity_change_rolls_back(tmp_path, change):
    path = tmp_path / "identity.sqlite3"
    with sqlite3.connect(path) as setup:
        setup.execute("CREATE TABLE values_test(value)")
    adapter = SimpleNamespace(
        account="1",
        config={
            "id": "bot",
            "_business_epoch": 1,
            "_business_revision": "v1",
            "_business_active": True,
            "enable": True,
        },
    )
    with pytest.raises(ProtocolError, match="database_authority_changed"):
        async with AsyncDatabase(
            lambda: sqlite3.connect(path), write=True, guard=account_guard(adapter)
        ) as db:
            await db.execute("INSERT INTO values_test VALUES(1)")
            if change == "account":
                adapter.account = "2"
            else:
                key, value = {
                    "instance": ("id", "other"),
                    "generation": ("_business_epoch", 2),
                    "revision": ("_business_revision", "v2"),
                    "disabled": ("enable", False),
                    "plugin_disabled": ("_business_active", False),
                }[change]
                adapter.config[key] = value
    with sqlite3.connect(path) as check:
        assert check.execute("SELECT COUNT(*) FROM values_test").fetchone() == (0,)
