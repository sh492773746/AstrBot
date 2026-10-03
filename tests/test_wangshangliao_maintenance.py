"""Offline regressions for bounded dispatch, retention and stop races."""

import asyncio
import importlib.util
import json
import os
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import cards
from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
from astrbot.core.platform.sources.wangshangliao.storage import Ledger


@pytest.mark.asyncio
async def test_retention_batch_boundary_and_grace(tmp_path, monkeypatch):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    await ledger.open()
    now = time.time()
    monkeypatch.setattr(
        "astrbot.core.platform.sources.wangshangliao.storage.time.time", lambda: now
    )
    try:
        await ledger.db.executemany(
            "INSERT INTO inbox(account,team,message,payload,state,completed_at) VALUES('a','g',?,'{}','processed',?)",
            [(str(n), now - 30 * 86400) for n in range(501)]
            + [("young", now - 30 * 86400 + 1)],
        )
        await ledger.db.execute("UPDATE ledger_maintenance SET retention_approved=1")
        await ledger.db.commit()
        assert await ledger.prune() == 0
        await ledger.db.execute(
            "UPDATE ledger_maintenance SET installed_at=?", (now - 86400,)
        )
        await ledger.db.commit()
        assert await ledger.prune() == 500
        assert await ledger.prune() == 1
        assert await ledger.prune() == 0
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_rollback_ledger_accepts_migrated_database(tmp_path):
    rollback_path = os.environ.get("WSL_ROLLBACK_STORAGE")
    if not rollback_path:
        pytest.skip(
            "Set WSL_ROLLBACK_STORAGE to verify the deployment rollback artifact"
        )
    path = tmp_path / "ledger.sqlite"
    ledger = Ledger(path)
    await ledger.open()
    await ledger.ingest("a", "g", "1", {})
    await ledger.close()
    spec = importlib.util.spec_from_file_location(
        "astrbot.core.platform.sources.wangshangliao.rollback_storage",
        rollback_path,
    )
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    rollback = old.Ledger(path)
    await rollback.open()
    try:
        await rollback.ingest("a", "g", "2", {})
        await rollback.mark("a", "g", "2", "processed")
        async with rollback.db.execute("SELECT COUNT(*) FROM inbox") as cursor:
            assert (await cursor.fetchone())[0] == 2
    finally:
        await rollback.close()


@pytest.mark.asyncio
async def test_migration_retention_and_dedup(tmp_path):
    path = tmp_path / "messages.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE inbox(account,team,message,payload,state,PRIMARY KEY(account,team,message))"
        )
        db.execute(
            "INSERT INTO inbox VALUES('a','private/2/peer','old','{}','processed')"
        )
    ledger = Ledger(path)
    await ledger.open()
    try:
        for state in ("processed", "pending", "processing", "needs_review"):
            await ledger.ingest(
                "a",
                "private/2/peer",
                state,
                {
                    "text": "private body",
                    "mentions": ["2"],
                    "sender": "2",
                    "recall_route": {"peer": "peer"},
                    "created_at": 0,
                },
            )
            await ledger.mark("a", "private/2/peer", state, state)
        await ledger.db.execute(
            "UPDATE inbox SET completed_at=? WHERE message<>'old'",
            (time.time() - 31 * 86400,),
        )
        await ledger.db.commit()
        assert await ledger.prune() == 0
        await ledger.db.execute(
            "UPDATE ledger_maintenance SET retention_approved=1,installed_at=?",
            (time.time() - 90000,),
        )
        await ledger.db.commit()
        assert await ledger.prune() == 1
        assert await ledger.prune() == 0
        await ledger.ingest("a", "private/2/peer", "processed", {"text": "duplicate"})
        async with ledger.db.execute(
            "SELECT message,payload,state FROM inbox"
        ) as cursor:
            rows = {r[0]: (json.loads(r[1]), r[2]) for r in await cursor.fetchall()}
        assert rows["processed"] == (
            {"sender": "2", "recall_route": {"peer": "peer"}, "created_at": 0},
            "processed",
        )
        for state in ("pending", "processing", "needs_review"):
            assert rows[state][0]["text"] == "private body"
        await ledger.close()
        await ledger.open()
        assert await ledger.prune() == 0
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_dispatch_is_bounded_and_recovers_overflow(tmp_path):
    adapter = WangshangliaoAdapter(
        {"id": "maintenance-test", "account_id": "a"}, {}, asyncio.Queue()
    )
    adapter.ledger = Ledger(tmp_path / "messages.sqlite3")
    await adapter.ledger.open()
    adapter.connection_state = "online"
    calls = []
    active = set()
    peak = 0
    finished = asyncio.Event()

    async def process(team, *, single):
        nonlocal peak
        assert single and team not in active
        active.add(team)
        peak = max(peak, len(active))
        await asyncio.sleep(0)
        async with adapter.ledger.db.execute(
            "SELECT message FROM inbox WHERE team=? AND state='pending' ORDER BY rowid LIMIT 1",
            (team,),
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            calls.append((team, row[0]))
            await adapter.ledger.mark("a", team, row[0], "processed")
        active.remove(team)
        if len(calls) == 1102:
            finished.set()

    adapter.process_group = process
    for n in range(1100):
        await adapter.ledger.ingest("a", f"private/{n}/peer", "0", {})
    for n in (1, 2):
        await adapter.ledger.ingest("a", "private/0/peer", str(n), {})
    task = asyncio.create_task(adapter.dispatch_pending())
    try:
        await asyncio.wait_for(finished.wait(), 15)
        assert peak <= 16
        assert [mid for team, mid in calls if team == "private/0/peer"] == [
            "0",
            "1",
            "2",
        ]
        assert len(set(calls)) == 1102
    finally:
        adapter.stopping.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.gather(*adapter.workers.values(), return_exceptions=True)
        await adapter.ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("recover", [False, True])
async def test_stop_during_remote_read_is_terminal(tmp_path, monkeypatch, recover):
    monkeypatch.setattr(cards, "instance_dir", lambda _: tmp_path)
    service = cards.CardJobs()
    entered, release = asyncio.Event(), asyncio.Event()

    async def roster(_):
        entered.set()
        await release.wait()
        return {
            "complete": True,
            "groupMemberInfo": [
                {"userId": "2", "nimId": "n2", "groupMemberNick": "new"}
            ],
        }

    adapter = SimpleNamespace(
        account="1",
        config={"id": "test", "moderation": {}},
        connection_state="online",
        meta=lambda: SimpleNamespace(name="wangshangliao"),
        get_moderation_members=roster,
    )
    job = {
        "id": "job",
        "account": "1",
        "owner": "admin",
        "group": "10",
        "state": "needs_review",
        "items": [
            {
                "member": "2",
                "nim": "n2",
                "name": "new",
                "identity": "identity",
                "state": "unknown",
            }
        ],
    }
    with service.database(adapter) as db:
        db.execute("INSERT INTO card_jobs VALUES(?,?)", ("job", json.dumps(job)))
    service.run = AsyncMock()
    context = SimpleNamespace(
        platform_manager=SimpleNamespace(platform_insts=[adapter])
    )
    task = asyncio.create_task(
        service.poll(context)
        if recover
        else service.refresh_status(adapter, "job", "admin")
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        service.stop(adapter, "job", "admin")
        release.set()
        if recover:
            await asyncio.sleep(0.05)
        else:
            await task
        persisted = service.status(adapter, "job", "admin")
        assert persisted["state"] == "stopped" and persisted["stop_requested"]
        service.run.assert_not_called()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await service.close()
