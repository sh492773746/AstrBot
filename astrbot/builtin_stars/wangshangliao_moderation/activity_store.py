"""Account-scoped lottery state, invitation credits and membership snapshots."""

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
            enabled INTEGER NOT NULL DEFAULT 0,
            tracking_enabled INTEGER NOT NULL DEFAULT 0,
            tracking_started_at REAL NOT NULL DEFAULT 0,
            owner TEXT, session TEXT,
            activation TEXT NOT NULL DEFAULT '', last_scan REAL NOT NULL DEFAULT 0,
            presence_checked_at REAL NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(account,group_id));
        CREATE TABLE IF NOT EXISTS invite_seen(
            account TEXT, group_id TEXT, member TEXT, peer TEXT, first_seen REAL,
            rate INTEGER, status TEXT, checked_at REAL NOT NULL DEFAULT 0,
            last_present REAL NOT NULL DEFAULT 0, left_at REAL NOT NULL DEFAULT 0,
            PRIMARY KEY(account,group_id,member));
        CREATE TABLE IF NOT EXISTS invite_credits(
            account TEXT, group_id TEXT, member TEXT, inviter TEXT,
            member_peer TEXT, inviter_peer TEXT, amount INTEGER, recorded REAL,
            PRIMARY KEY(account,group_id,member));
        CREATE TABLE IF NOT EXISTS invite_identities(
            account TEXT, group_id TEXT, member TEXT, member_name TEXT NOT NULL DEFAULT '',
            inviter TEXT, inviter_name TEXT NOT NULL DEFAULT '', updated REAL NOT NULL DEFAULT 0,
            PRIMARY KEY(account,group_id,member));
        CREATE TABLE IF NOT EXISTS invite_attributions(
            account TEXT, group_id TEXT, member TEXT, source TEXT NOT NULL,
            event_id TEXT NOT NULL DEFAULT '', observed REAL NOT NULL DEFAULT 0,
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
        CREATE INDEX IF NOT EXISTS invite_credit_inviter_history
            ON invite_credits(account,group_id,inviter,recorded DESC);
        CREATE INDEX IF NOT EXISTS invite_attribution_recheck
            ON invite_attributions(account,group_id,source,observed,member);
    """)
    db.execute("BEGIN IMMEDIATE")
    for table in ("lottery_settings", "lotteries"):
        fields = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
        for name, declaration in (
            ("total_amount", "INTEGER NOT NULL DEFAULT 0"),
            ("packet_mode", "TEXT NOT NULL DEFAULT 'prize'"),
            ("by_invites", "INTEGER NOT NULL DEFAULT 0"),
            ("remarks", "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in fields:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
                if name == "by_invites":
                    db.execute(f"UPDATE {table} SET by_invites=1 WHERE invite_gate>0")
    columns = {row["name"] for row in db.execute("PRAGMA table_info(lotteries)")}
    for name, declaration in (
        ("packet_amounts", "TEXT NOT NULL DEFAULT '[]'"),
        ("unclaimed_amount", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in columns:
            db.execute(f"ALTER TABLE lotteries ADD COLUMN {name} {declaration}")
    columns = {row["name"] for row in db.execute("PRAGMA table_info(invite_rules)")}
    if "presence_checked_at" not in columns:
        db.execute(
            "ALTER TABLE invite_rules ADD COLUMN presence_checked_at REAL NOT NULL DEFAULT 0"
        )
    if "tracking_enabled" not in columns:
        db.execute(
            "ALTER TABLE invite_rules ADD COLUMN tracking_enabled INTEGER NOT NULL DEFAULT 0"
        )
        # Existing active or previously synchronized rules were already collecting
        # invitation lifecycle data, so preserve that behavior after migration.
        db.execute(
            "UPDATE invite_rules SET tracking_enabled=1 "
            "WHERE enabled=1 OR presence_checked_at>0"
        )
    if "tracking_started_at" not in columns:
        db.execute(
            "ALTER TABLE invite_rules ADD COLUMN tracking_started_at REAL NOT NULL DEFAULT 0"
        )
        # Older databases did not persist the activation time. The first
        # baseline snapshot is the closest accurate start time available.
        db.execute(
            "UPDATE invite_rules AS rule SET tracking_started_at=COALESCE("
            "(SELECT MIN(seen.first_seen) FROM invite_seen AS seen "
            "WHERE seen.account=rule.account AND seen.group_id=rule.group_id "
            "AND seen.status='baseline'), rule.last_scan, 0) "
            "WHERE rule.tracking_enabled=1"
        )
    columns = {row["name"] for row in db.execute("PRAGMA table_info(invite_seen)")}
    if "last_present" not in columns:
        db.execute(
            "ALTER TABLE invite_seen ADD COLUMN last_present REAL NOT NULL DEFAULT 0"
        )
    if "left_at" not in columns:
        db.execute("ALTER TABLE invite_seen ADD COLUMN left_at REAL NOT NULL DEFAULT 0")
    db.execute(
        "CREATE INDEX IF NOT EXISTS invite_credit_presence "
        "ON invite_seen(account,group_id,status,left_at,member)"
    )
    db.commit()
    return db
