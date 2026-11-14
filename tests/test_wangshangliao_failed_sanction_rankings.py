"""Reject known violations from rankings even when provider actions fail."""

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import rankings


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        "observed",
        "preflight_failed",
        "execution_rejected",
        "review_only",
        "identity_unverified",
    ],
)
async def test_failed_violation_never_qualifies_for_yesterday_whitelist(
    tmp_path, monkeypatch, status
):
    now = datetime(2026, 10, 31, 12, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    monkeypatch.setattr(rankings.time, "time", lambda: now)
    monkeypatch.setattr(rankings, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(rankings, "is_managed_account", lambda _: False)
    monkeypatch.setattr(rankings, "flood_fixture", lambda *_: None)
    path = tmp_path / "messages.sqlite3"
    first = now - 86400 - 10000
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "CREATE TABLE inbox(account,team,message,payload,state,received_at,pruned_at)"
        )
        for index in range(101):
            stamp = first + index * 31
            payload = json.dumps(
                {
                    "sender": "10",
                    "name": "Target",
                    "text": f"Distinct message {index}",
                    "created_at": stamp * 1000,
                }
            )
            db.execute(
                "INSERT INTO inbox VALUES('1','5',?,?,'processed',?,NULL)",
                (str(index), payload, stamp),
            )
        db.execute(
            "INSERT INTO inbox VALUES('1','5','safe',?,'processed',?,NULL)",
            (
                json.dumps(
                    {
                        "sender": "11",
                        "name": "Unaffected",
                        "text": "Normal conversation",
                        "created_at": first * 1000,
                    }
                ),
                first,
            ),
        )
    adapter = SimpleNamespace(
        config={"id": "fixture", "enable": True, "enabled_groups": ["5"]},
        account="1",
        nim_account="901",
        ledger=SimpleNamespace(path=path),
    )
    runtime = rankings.DailyRankings()
    _, _, before = await runtime.snapshot(adapter, "5", previous_day=True, settled=True)
    assert before == [("10", "Target", 101), ("11", "Unaffected", 1)]
    with closing(sqlite3.connect(tmp_path / "moderation.sqlite3")) as db, db:
        db.execute(
            "CREATE TABLE content_violations(account,group_id,message,observed,status)"
        )
        db.executemany(
            "INSERT INTO content_violations VALUES('1','5',?,?,?)",
            [(str(index), first + index * 31, status) for index in range(101)],
        )
        db.executemany(
            "INSERT INTO content_violations VALUES(?,?,?,?,?)",
            [
                ("2", "5", "safe", first, status),
                ("1", "6", "safe", first, status),
            ],
        )
    runtime.cache.clear()
    _, _, after = await runtime.snapshot(adapter, "5", previous_day=True, settled=True)
    assert after == [("11", "Unaffected", 1)]
    assert runtime.settled_cache
