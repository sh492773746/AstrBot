"""Advertising migrations and shared transaction-bound invariants."""

import json

from .store import Rejected


def migrate(store):
    """Apply repeatable additive migrations without replacing business rows.

    Args:
        store: Instance database owner.
    """
    with store.tx() as db:
        columns = {r[1] for r in db.execute("PRAGMA table_info(ads)")}
        for name, declaration in {
            "version": "INTEGER NOT NULL DEFAULT 1",
            "payment_source": "TEXT NOT NULL DEFAULT ''",
            "refund_state": "TEXT NOT NULL DEFAULT ''",
            "first_published": "REAL",
            "ready_at": "REAL",
            "slot": "INTEGER",
            "package_key": "TEXT NOT NULL DEFAULT ''",
        }.items():
            if name not in columns:
                db.execute(f"ALTER TABLE ads ADD COLUMN {name} {declaration}")
        db.execute(
            "CREATE TABLE IF NOT EXISTS ad_targets(chat TEXT PRIMARY KEY,capacity INTEGER NOT NULL,version INTEGER NOT NULL DEFAULT 1)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS ad_operations(id TEXT PRIMARY KEY,chat TEXT NOT NULL,kind TEXT NOT NULL,status TEXT NOT NULL,payload TEXT NOT NULL,step TEXT NOT NULL DEFAULT '',message INTEGER,error TEXT NOT NULL DEFAULT '',retry_at REAL NOT NULL DEFAULT 0,at REAL NOT NULL)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS ad_operations_pending ON ad_operations(status,chat)"
        )
        db.execute("CREATE INDEX IF NOT EXISTS ads_queue ON ads(status,ready_at,at)")
        db.execute(
            "CREATE TABLE IF NOT EXISTS ad_boards(chat TEXT PRIMARY KEY,message INTEGER,state TEXT NOT NULL,body TEXT NOT NULL,error TEXT NOT NULL DEFAULT '')"
        )
        db.execute(
            "CREATE TRIGGER IF NOT EXISTS ads_versions AFTER UPDATE OF body,approved,paid,status,expires,refund_state ON ads BEGIN UPDATE ads SET version=OLD.version+1,ready_at=CASE WHEN NEW.status='pending' AND NEW.approved=1 AND NEW.paid=1 THEN COALESCE(NEW.ready_at,CAST(strftime('%s','now') AS REAL)) WHEN NEW.status='pending' THEN NULL ELSE NEW.ready_at END WHERE id=NEW.id; END"
        )
        if not db.execute(
            "SELECT 1 FROM meta WHERE key='ad_maintenance_v1'"
        ).fetchone():
            packages = store.get("packages", {}, db)
            rows = db.execute("SELECT * FROM ads ORDER BY at,id").fetchall()
            capacities = {}
            for p in packages.values():
                if p["kind"] == "pinned":
                    capacities[p["target"]] = max(
                        capacities.get(p["target"], 0), p["slots"]
                    )
            occupied = {}
            for row in rows:
                p = json.loads(row["package"])
                chat = p["target"]
                keys = [
                    k
                    for k, v in packages.items()
                    if v["target"] == chat and v["name"] == p["name"]
                ]
                source = (
                    "wallet"
                    if row["paid_by"] == "ad_wallet"
                    else "manual"
                    if row["paid"]
                    else ""
                )
                test = db.execute(
                    "SELECT 1 FROM audit WHERE action='ad_test_payment_override' AND json_extract(data,'$.id')=?",
                    (row["id"],),
                ).fetchone()
                if test:
                    source = "test"
                slot = None
                if p["kind"] == "pinned" and row["status"] in (
                    "active",
                    "review",
                    "sending",
                    "sent",
                    "pinning",
                    "editing",
                ):
                    occupied[chat] = occupied.get(chat, 0) + 1
                    slot = occupied[chat]
                db.execute(
                    "UPDATE ads SET payment_source=?,package_key=?,slot=?,first_published=CASE WHEN message IS NOT NULL THEN at ELSE NULL END,ready_at=CASE WHEN paid=1 AND approved=1 THEN at ELSE NULL END WHERE id=?",
                    (source, keys[0] if len(keys) == 1 else "", slot, row["id"]),
                )
            for chat in capacities.keys() | occupied.keys():
                db.execute(
                    "INSERT OR IGNORE INTO ad_targets(chat,capacity) VALUES(?,?)",
                    (chat, max(capacities.get(chat, 1), occupied.get(chat, 0))),
                )
            db.execute("INSERT INTO meta(key,value) VALUES('ad_maintenance_v1','1')")


def available(store, package, package_key="", db=None):
    """Check current local acceptance rules without repricing an order snapshot.

    Args:
        store: Instance database owner.
        package: Contract snapshot.
        package_key: Original catalog entry identifier.
        db: Optional existing transaction.
    """
    db = db or store.db
    if not store.get("modules", {}, db).get("ads"):
        raise Rejected("广告模块已停用，暂不接单付款")
    if package.get("_merchant"):
        from .merchant_ads import authorized

        row = db.execute(
            "SELECT * FROM ads WHERE id=?", (package.get("_order"),)
        ).fetchone()
        if (
            not row
            or not authorized(store, row, db)
            or not db.execute(
                "SELECT 1 FROM mod_groups WHERE chat=? AND enabled=1",
                (package["target"],),
            ).fetchone()
        ):
            raise Rejected("本群直付订单尚未完成付款、归属核验或启用检查")
        return
    from .tenants import platform_group

    if not platform_group(store, package["target"], db):
        raise Rejected("平台广告不能投递到独立经营者群")
    packages = store.get("packages", {}, db)
    current = (
        packages.get(package_key)
        if package_key
        else next(
            (
                p
                for p in packages.values()
                if p["name"] == package["name"] and p["target"] == package["target"]
            ),
            None,
        )
    )
    if not current or not current["enabled"] or current["target"] != package["target"]:
        raise Rejected("广告位已停用或目标已变更，可取消未发布订单")
    channel = db.execute(
        "SELECT enabled FROM ad_channels WHERE chat=?", (package["target"],)
    ).fetchone()
    if channel:
        if not channel[0]:
            raise Rejected("频道已停用，可取消未发布订单")
    elif db.execute("SELECT 1 FROM sqlite_master WHERE name='mod_groups'").fetchone():
        if not db.execute(
            "SELECT 1 FROM mod_groups WHERE chat=? AND enabled=1", (package["target"],)
        ).fetchone():
            raise Rejected("群未启用管理，可取消未发布订单")


def version(row, expected):
    """Reject a stale human confirmation.

    Args:
        row: Fresh order record.
        expected: Version bound to the confirmation, or None for internal callers.
    """
    if expected is not None and row["version"] != expected:
        raise Rejected("订单已变化，请重新打开后确认")


def payment_authorized(store, row):
    """Use the correct business authority when publishing an approved order.

    Args:
        store: Instance store.
        row: Current advertising order.

    Returns:
        Whether approval and payment remain authorized for this business.
    """
    if row["tenant"] != "platform":
        from .merchant_ads import authorized

        return authorized(store, row)
    wallet = (
        row["paid_by"] == "ad_wallet"
        and store.db.execute(
            "SELECT 1 FROM ad_payments WHERE ad=? AND uid=? AND status='paid'",
            (row["id"], row["uid"]),
        ).fetchone()
    )
    return bool(
        store.allowed(row["approved_by"], "ads")
        and (wallet or store.allowed(row["paid_by"], "ads"))
    )
