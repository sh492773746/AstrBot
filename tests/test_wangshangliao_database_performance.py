"""Additive indexes retain ledger semantics and bounded query plans."""

import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import activity_store
from astrbot.core.platform.sources.wangshangliao import approval, schedule_store
from astrbot.core.platform.sources.wangshangliao.storage import Ledger
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError


@pytest.mark.asyncio
async def test_legacy_ledger_upgrade_preserves_data_and_uses_range_indexes(tmp_path):
    path = tmp_path / "messages.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "CREATE TABLE inbox(account TEXT,team TEXT,message TEXT,payload TEXT,state TEXT,PRIMARY KEY(account,team,message))"
        )
        db.execute(
            "INSERT INTO inbox VALUES('a','g','original','{\"text\":\"original\"}','processed')"
        )
    ledger = Ledger(path)
    for _ in range(2):
        await ledger.open()
        try:
            async with ledger.db.execute(
                "SELECT message,payload,state FROM inbox"
            ) as cursor:
                assert await cursor.fetchall() == [
                    ("original", '{"text":"original"}', "processed")
                ]
            for query, params, expected in [
                (
                    "SELECT message,payload,state FROM inbox WHERE account=? AND team=? AND received_at>=?",
                    ("a", "g", 0),
                    "inbox_ranking_window",
                ),
                (
                    "SELECT message,payload FROM inbox WHERE account=? AND team=? AND rowid<? ORDER BY rowid DESC LIMIT ?",
                    ("a", "g", 99, 20),
                    "inbox_conversation",
                ),
            ]:
                async with ledger.db.execute(
                    "EXPLAIN QUERY PLAN " + query, params
                ) as cursor:
                    plan = " ".join(row[3] for row in await cursor.fetchall())
                    assert expected in plan
                    assert "TEMP B-TREE" not in plan
            async with ledger.db.execute("PRAGMA synchronous") as cursor:
                assert (await cursor.fetchone())[0] == 2
            async with ledger.db.execute("PRAGMA journal_mode") as cursor:
                assert (await cursor.fetchone())[0] == "wal"
        finally:
            await ledger.close()


@pytest.mark.parametrize(
    "query,params,index",
    [
        (
            "SELECT * FROM lotteries WHERE account=? AND group_id=? ORDER BY rowid DESC LIMIT 20",
            ("a", "g"),
            "lottery_group_history",
        ),
        (
            "SELECT * FROM lotteries WHERE account=? AND group_id=? ORDER BY created DESC LIMIT 1",
            ("a", "g"),
            "lottery_group_created",
        ),
        (
            "SELECT * FROM lotteries WHERE account=? AND status IN ('announcing','open','drawing')",
            ("a",),
            "lottery_pending_tick",
        ),
        (
            "SELECT * FROM invite_seen WHERE account=? AND group_id=? AND status='pending' ORDER BY checked_at,first_seen,member LIMIT 200",
            ("a", "g"),
            "invite_pending_scan",
        ),
        (
            "SELECT COUNT(*),COALESCE(SUM(amount),0) FROM invite_credits WHERE account=? AND group_id=? AND inviter=?",
            ("a", "g", "u"),
            "invite_credit_totals",
        ),
        (
            "SELECT inviter,member,amount FROM invite_credits WHERE account=? AND group_id=? ORDER BY recorded DESC LIMIT 20",
            ("a", "g"),
            "invite_credit_history",
        ),
    ],
)
def test_activity_hot_queries_use_indexes(tmp_path, monkeypatch, query, params, index):
    monkeypatch.setattr(activity_store, "instance_dir", lambda _: tmp_path)
    adapter = SimpleNamespace(config={"id": "fixture"})
    with closing(activity_store.database(adapter)) as db:
        plan = " ".join(
            row[3] for row in db.execute("EXPLAIN QUERY PLAN " + query, params)
        )
        assert index in plan
        assert "TEMP B-TREE" not in plan


@pytest.mark.parametrize(
    "table,order,index",
    [
        ("executions", "planned", "schedule_execution_history"),
        ("rule_audit", "id", "schedule_rule_history"),
    ],
)
def test_schedule_history_indexes(tmp_path, monkeypatch, table, order, index):
    monkeypatch.setattr(schedule_store, "instance_dir", lambda _: tmp_path)
    with closing(
        schedule_store.database(SimpleNamespace(config={"id": "fixture"}))
    ) as db:
        plan = " ".join(
            row[3]
            for row in db.execute(
                f"EXPLAIN QUERY PLAN SELECT * FROM {table} WHERE account=? AND group_id=? ORDER BY {order} DESC LIMIT 20",
                ("a", "g"),
            )
        )
        assert index in plan and "TEMP B-TREE" not in plan


def test_reward_totals_and_unique_credits_survive_reopening(tmp_path, monkeypatch):
    monkeypatch.setattr(activity_store, "instance_dir", lambda _: tmp_path)
    adapter = SimpleNamespace(config={"id": "fixture"})
    with closing(activity_store.database(adapter)) as db, db:
        db.executemany(
            "INSERT OR IGNORE INTO invite_credits(account,group_id,member,inviter,amount) VALUES(?,?,?,?,?)",
            [
                ("a", "g", "1", "u", 120),
                ("a", "g", "2", "u", 150),
                ("a", "g", "1", "u", 120),
                ("other", "g", "1", "u", 999),
            ],
        )
    with closing(activity_store.database(adapter)) as db:
        assert tuple(
            db.execute(
                "SELECT COUNT(*),SUM(amount) FROM invite_credits WHERE account='a' AND group_id='g' AND inviter='u'"
            ).fetchone()
        ) == (2, 270)


def test_approval_connections_close_on_success_and_rejection(tmp_path, monkeypatch):
    opened = []
    connect = sqlite3.connect

    class TrackingConnection(sqlite3.Connection):
        closed = False

        def close(self):
            self.closed = True
            super().close()

    def tracking_connect(*args, **kwargs):
        db = connect(*args, **kwargs, factory=TrackingConnection)
        opened.append(db)
        return db

    monkeypatch.setattr(approval, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(approval.sqlite3, "connect", tracking_connect)
    session = {"business": {"uid": "a"}}
    preview = approval.approval(
        "fixture",
        "owner",
        session,
        {"action": "moderation_preview", "operation_action": "mute_all", "group": 1},
    )
    consume = {
        "action": "moderation_execute",
        "instance_id": "fixture",
        "approval_token": preview["approval_token"],
    }
    with pytest.raises(ProtocolError):
        approval.approval("fixture", "other", session, consume)
    assert (
        approval.approval("fixture", "owner", session, consume)["action"] == "mute_all"
    )
    assert len(opened) == 3 and all(db.closed for db in opened)
