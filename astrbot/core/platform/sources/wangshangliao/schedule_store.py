"""Durable group schedule state shared by manual and scheduled operations."""

import asyncio
import sqlite3

from .storage import instance_dir


def group_lock(adapter, group: str) -> asyncio.Lock:
    """Return the adapter-local serialization lock for one group.

    Args:
        adapter: Active adapter instance.
        group: Group identifier.
    """
    if not hasattr(adapter, "schedule_locks"):
        adapter.schedule_locks = {}
    return adapter.schedule_locks.setdefault(group, asyncio.Lock())


def database(adapter):
    """Open the per-instance schedule database with additive schema creation.

    Args:
        adapter: Adapter owning the persistent instance directory.

    Returns:
        SQLite connection; caller must close it.
    """
    root = instance_dir(adapter.config["id"])
    root.mkdir(parents=True, exist_ok=True)
    path = root / "schedules.sqlite3"
    db = sqlite3.connect(path)
    path.chmod(0o600)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS schedules(
            account TEXT, group_id TEXT, owner TEXT, session TEXT,
            start TEXT, end TEXT, zone TEXT, version INTEGER,
            status TEXT, next_at REAL, next_action TEXT, error TEXT,
            PRIMARY KEY(account,group_id));
        CREATE TABLE IF NOT EXISTS confirmations(
            token TEXT PRIMARY KEY, account TEXT, group_id TEXT, owner TEXT,
            session TEXT, version INTEGER, start TEXT, end TEXT,
            expires REAL, message TEXT, used INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS executions(
            operation TEXT PRIMARY KEY, account TEXT, group_id TEXT,
            planned REAL, action TEXT, status TEXT, version INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS rule_audit(
            id INTEGER PRIMARY KEY, recorded TEXT DEFAULT CURRENT_TIMESTAMP,
            account TEXT, group_id TEXT, owner TEXT, version INTEGER,
            start TEXT, end TEXT, status TEXT, error TEXT);
        CREATE INDEX IF NOT EXISTS schedule_execution_history
            ON executions(account,group_id,planned DESC);
        CREATE INDEX IF NOT EXISTS schedule_rule_history
            ON rule_audit(account,group_id,id DESC);
        CREATE TRIGGER IF NOT EXISTS schedule_insert_audit AFTER INSERT ON schedules
        BEGIN
            INSERT INTO rule_audit(account,group_id,owner,version,start,end,status,error)
            VALUES(new.account,new.group_id,new.owner,new.version,new.start,new.end,new.status,new.error);
        END;
        CREATE TRIGGER IF NOT EXISTS schedule_update_audit AFTER UPDATE ON schedules
        WHEN old.version<>new.version OR old.status<>new.status
        BEGIN
            INSERT INTO rule_audit(account,group_id,owner,version,start,end,status,error)
            VALUES(new.account,new.group_id,new.owner,new.version,new.start,new.end,new.status,new.error);
        END;
    """)
    if "version" not in {row[1] for row in db.execute("PRAGMA table_info(executions)")}:
        db.execute("ALTER TABLE executions ADD COLUMN version INTEGER DEFAULT 0")
        db.commit()
    return db


def pause(adapter, group: str, reason: str) -> None:
    """Pause future work without modifying the upstream mute state.

    Args:
        adapter: Active adapter.
        group: Group identifier.
        reason: Fixed audit label.
    """
    db = database(adapter)
    try:
        with db:
            db.execute(
                "UPDATE confirmations SET used=1 WHERE account=? AND group_id=?",
                (adapter.account, group),
            )
            db.execute(
                "UPDATE schedules SET status='paused',version=version+1,error=? WHERE account=? AND group_id=? AND status<>'deleted'",
                (reason, adapter.account, group),
            )
    finally:
        db.close()
