"""One-time, transactionally verified migration from shared to group balances."""

from .store import Rejected


def migrate(store, target=None):
    """Archive shared balances and bind outstanding liabilities to their wallet.

    Args:
        store: Instance store whose caller holds the runtime lock.
        target: Explicitly approved destination for all pre-migration funds.
    """
    with store.tx() as db:
        if store.get("group_points_enabled", False, db):
            db.execute(
                "CREATE INDEX IF NOT EXISTS group_chat_seen_day ON group_chat_seen(day)"
            )
            return
        has_history = any(
            db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
            for table in ("wallets", "ledger", "bets", "chases")
        )
        if has_history and (
            not target
            or not str(target).startswith("-")
            or not str(target)[1:].isdigit()
        ):
            raise Rejected("旧积分迁移必须先明确指定归属群，禁止自动复制余额")
        target = str(target or "")
        if (
            has_history
            and not db.execute(
                "SELECT 1 FROM mod_groups WHERE chat=? AND enabled=1", (target,)
            ).fetchone()
        ):
            raise Rejected("积分迁移目标必须是已登记且启用的群")
        mismatch = db.execute(
            "SELECT 1 FROM wallets w WHERE balance<>"
            "(SELECT COALESCE(SUM(delta),0) FROM ledger l WHERE l.uid=w.uid) LIMIT 1"
        ).fetchone()
        orphan = db.execute(
            "SELECT 1 FROM ledger l LEFT JOIN wallets w ON w.uid=l.uid WHERE w.uid IS NULL LIMIT 1"
        ).fetchone()
        if mismatch or orphan:
            raise Rejected("旧积分账本不平，暂停迁移")
        db.execute(
            "CREATE TABLE IF NOT EXISTS group_wallets("
            "chat TEXT NOT NULL,uid TEXT NOT NULL,balance INTEGER NOT NULL "
            "CHECK(balance>=0 AND balance<=1000000000000),PRIMARY KEY(chat,uid))"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS group_ledger("
            "chat TEXT NOT NULL,op TEXT NOT NULL,uid TEXT NOT NULL,delta INTEGER NOT NULL,"
            "reason TEXT NOT NULL,at REAL NOT NULL,PRIMARY KEY(chat,op))"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS group_ledger_user ON group_ledger(chat,uid,at)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS group_rewards("
            "chat TEXT NOT NULL,uid TEXT NOT NULL,day TEXT NOT NULL,last REAL NOT NULL,"
            "earned INTEGER NOT NULL,PRIMARY KEY(chat,uid,day))"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS group_chat_seen("
            "chat TEXT NOT NULL,uid TEXT NOT NULL,day TEXT NOT NULL,digest TEXT NOT NULL,"
            "PRIMARY KEY(chat,uid,day,digest))"
        )
        db.execute(
            "INSERT INTO group_wallets SELECT ?,uid,balance FROM wallets", (target,)
        )
        db.execute(
            "INSERT INTO group_ledger SELECT ?,op,uid,delta,reason,at FROM ledger",
            (target,),
        )
        db.execute(
            "INSERT INTO group_rewards SELECT ?,uid,day,last,earned FROM rewards",
            (target,),
        )
        db.execute(
            "INSERT INTO group_chat_seen SELECT ?,uid,day,digest FROM chat_seen",
            (target,),
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS group_chat_seen_day ON group_chat_seen(day)"
        )
        # Legacy orders were paid from the shared wallet, now owned by target.
        db.execute("UPDATE bets SET points_chat=?", (target,))
        db.execute("UPDATE chases SET points_chat=?", (target,))
        for table in ("wallets", "ledger", "rewards", "chat_seen"):
            for verb in ("INSERT", "UPDATE", "DELETE"):
                db.execute(
                    f"CREATE TRIGGER IF NOT EXISTS freeze_{table}_{verb.lower()} "
                    f"BEFORE {verb} ON {table} BEGIN "
                    "SELECT RAISE(ABORT,'legacy points archive is read-only'); END"
                )
        store.put(db, "group_points_enabled", True)
        store.put(db, "points_legacy_group", target)
        store.audit(
            db,
            "migration",
            "group_points_migrated",
            {
                "target": target,
                "wallets": db.execute("SELECT COUNT(*) FROM group_wallets").fetchone()[
                    0
                ],
                "total": db.execute(
                    "SELECT COALESCE(SUM(balance),0) FROM group_wallets"
                ).fetchone()[0],
            },
        )
