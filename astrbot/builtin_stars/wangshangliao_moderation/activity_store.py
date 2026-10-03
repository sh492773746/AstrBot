"""Account-scoped lottery state and immutable invitation reward credits."""

import sqlite3

from astrbot.core.platform.sources.wangshangliao.storage import instance_dir


def database(adapter):
    """Open the instance activity database without modifying other ledgers.

    Args:
        adapter: Native adapter owning the instance directory.

    Returns:
        SQLite connection; the caller must close it.
    """
    root = instance_dir(adapter.config["id"])
    root.mkdir(parents=True, exist_ok=True)
    path = root / "activities.sqlite3"
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    path.chmod(0o600)
    db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS lottery_settings(
            account TEXT, group_id TEXT, prize TEXT NOT NULL DEFAULT '',
            winners INTEGER NOT NULL DEFAULT 3, duration INTEGER NOT NULL DEFAULT 600,
            capacity INTEGER NOT NULL DEFAULT 15, invite_gate INTEGER NOT NULL DEFAULT 0,
            contact TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(account,group_id));
        CREATE TABLE IF NOT EXISTS lotteries(
            id TEXT PRIMARY KEY, command TEXT UNIQUE, account TEXT, group_id TEXT,
            owner TEXT, session TEXT, owner_name TEXT, prize TEXT, winners INTEGER,
            duration INTEGER, capacity INTEGER, invite_gate INTEGER, contact TEXT,
            status TEXT, ends REAL, created REAL,
            results TEXT NOT NULL DEFAULT '[]', total INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '');
        CREATE UNIQUE INDEX IF NOT EXISTS lottery_active
            ON lotteries(account,group_id)
            WHERE status IN ('announcing','open','blocked','drawing');
        CREATE TABLE IF NOT EXISTS lottery_entries(
            lottery TEXT, member TEXT, peer TEXT, name TEXT, joined REAL,
            PRIMARY KEY(lottery,member));
        CREATE TABLE IF NOT EXISTS invite_rules(
            account TEXT, group_id TEXT, rate INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 0, owner TEXT, session TEXT,
            activation TEXT NOT NULL DEFAULT '', last_scan REAL NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(account,group_id));
        CREATE TABLE IF NOT EXISTS invite_seen(
            account TEXT, group_id TEXT, member TEXT, peer TEXT, first_seen REAL,
            rate INTEGER, status TEXT, checked_at REAL NOT NULL DEFAULT 0,
            PRIMARY KEY(account,group_id,member));
        CREATE TABLE IF NOT EXISTS invite_credits(
            account TEXT, group_id TEXT, member TEXT, inviter TEXT,
            member_peer TEXT, inviter_peer TEXT, amount INTEGER, recorded REAL,
            PRIMARY KEY(account,group_id,member));
        CREATE INDEX IF NOT EXISTS lottery_group_history
            ON lotteries(account,group_id);
        CREATE INDEX IF NOT EXISTS lottery_group_created
            ON lotteries(account,group_id,created DESC);
        CREATE INDEX IF NOT EXISTS lottery_pending_tick
            ON lotteries(account,ends)
            WHERE status IN ('announcing','open','drawing');
        CREATE INDEX IF NOT EXISTS invite_pending_scan
            ON invite_seen(account,group_id,checked_at,first_seen,member)
            WHERE status='pending';
        CREATE INDEX IF NOT EXISTS invite_credit_totals
            ON invite_credits(account,group_id,inviter,amount);
        CREATE INDEX IF NOT EXISTS invite_credit_history
            ON invite_credits(account,group_id,recorded DESC);
    """)
    return db
