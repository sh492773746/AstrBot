"""Transactional subscriptions, payments, bots, jobs, and audit."""

import grp
import json
import os
import secrets
import sqlite3
import time
from calendar import monthrange
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

TIERS = {"first", "month", "year"}
ORDER_TTL = 900


def extend_epoch(start: int, tier: str) -> int:
    """Advance a subscription by one UTC calendar month or year.

    Args:
        start: Starting Unix timestamp.
        tier: First month, subsequent month, or year.

    Returns:
        New expiry timestamp.
    """
    date = datetime.fromtimestamp(start, timezone.utc)
    if tier == "year":
        year, month = date.year + 1, date.month
    else:
        year = date.year + (date.month == 12)
        month = date.month % 12 + 1
    return int(
        date.replace(
            year=year, month=month, day=min(date.day, monthrange(year, month)[1])
        ).timestamp()
    )


class Store:
    """Single-host control database; every state transition is idempotent."""

    def __init__(self, path: Path, group: str | None = None):
        group = group or os.getenv("CONTROL_DB_GROUP")
        if group:
            gid = grp.getgrnam(group).gr_gid
            os.umask(0o007)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o770)
            if path.parent.stat().st_gid != gid:
                os.chown(path.parent, -1, gid)
            if path.parent.stat().st_uid == os.geteuid():
                path.parent.chmod(0o2770)
            elif path.parent.stat().st_mode & 0o070 != 0o070:
                raise ValueError("Control DB directory lacks group access")
        else:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS prices(
                tier TEXT PRIMARY KEY, stars INTEGER NOT NULL CHECK(stars>0)
            );
            CREATE TABLE IF NOT EXISTS bots(
                id INTEGER PRIMARY KEY,
                owner_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                token_cipher BLOB NOT NULL,
                expires_at INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'provisioning',
                dashboard_port INTEGER UNIQUE,
                dashboard_secret BLOB,
                enabled INTEGER NOT NULL DEFAULT 1,
                provider_secret BLOB,
                provider_key_hash TEXT UNIQUE
            );
            CREATE TABLE IF NOT EXISTS orders(
                id TEXT PRIMARY KEY,
                owner_id INTEGER NOT NULL,
                bot_id INTEGER REFERENCES bots(id),
                tier TEXT NOT NULL,
                stars INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                charge_id TEXT UNIQUE,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                description TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS owner_orders ON orders(owner_id,status);
            CREATE TABLE IF NOT EXISTS jobs(
                id INTEGER PRIMARY KEY,
                bot_id INTEGER NOT NULL REFERENCES bots(id),
                kind TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                retry_at INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                UNIQUE(bot_id,kind)
            );
            CREATE TABLE IF NOT EXISTS notices(
                bot_id INTEGER NOT NULL REFERENCES bots(id),
                kind TEXT NOT NULL, sent_at INTEGER,
                PRIMARY KEY(bot_id,kind)
            );
            CREATE TABLE IF NOT EXISTS requests(
                id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL,
                bot_id INTEGER NOT NULL REFERENCES bots(id),
                body TEXT NOT NULL, created_at INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'new'
            );
            CREATE TABLE IF NOT EXISTS model_usage(
                bot_id INTEGER NOT NULL REFERENCES bots(id),
                month TEXT NOT NULL, requests INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(bot_id,month)
            );
            CREATE TABLE IF NOT EXISTS extension_reviews(
                order_id TEXT PRIMARY KEY REFERENCES orders(id),
                bot_id INTEGER NOT NULL REFERENCES bots(id),
                digest TEXT NOT NULL,
                status TEXT NOT NULL,
                reviewed_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS audit(
                id INTEGER PRIMARY KEY, at INTEGER NOT NULL,
                actor TEXT NOT NULL, action TEXT NOT NULL, ref TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS operations(
                id TEXT PRIMARY KEY, actor INTEGER NOT NULL,
                action TEXT NOT NULL, bot_id INTEGER,
                argument TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'confirm',
                created_at INTEGER NOT NULL, result TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS tenant_entitlements(
                bot_id INTEGER PRIMARY KEY REFERENCES bots(id),
                template TEXT NOT NULL, features TEXT NOT NULL,
                model_quota INTEGER NOT NULL CHECK(model_quota>=0)
            );
            CREATE TABLE IF NOT EXISTS trials(
                id TEXT PRIMARY KEY REFERENCES operations(id),
                owner_id INTEGER NOT NULL,
                bot_id INTEGER UNIQUE REFERENCES bots(id),
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS owner_trials ON trials(owner_id,expires_at);
        """)
        if "enabled" not in {
            row["name"] for row in self.db.execute("PRAGMA table_info(bots)")
        }:
            self.db.execute(
                "ALTER TABLE bots ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"
            )
        bot_columns = {
            row["name"] for row in self.db.execute("PRAGMA table_info(bots)")
        }
        if "provider_secret" not in bot_columns:
            self.db.execute("ALTER TABLE bots ADD COLUMN provider_secret BLOB")
        if "provider_key_hash" not in bot_columns:
            self.db.execute("ALTER TABLE bots ADD COLUMN provider_key_hash TEXT")
        self.db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS bot_provider_keys ON bots(provider_key_hash)"
        )
        if group:
            if path.stat().st_gid != gid:
                os.chown(path, -1, gid)
            if path.stat().st_uid == os.geteuid():
                path.chmod(0o660)
            elif path.stat().st_mode & 0o060 != 0o060:
                raise ValueError("Control DB lacks group access")
        else:
            path.chmod(0o600)

    @contextmanager
    def tx(self):
        """Lock one transaction across related records."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def price(self, tier: str) -> int | None:
        row = self.db.execute(
            "SELECT stars FROM prices WHERE tier=?", (tier,)
        ).fetchone()
        return row["stars"] if row else None

    def worker_ready(self) -> bool:
        """Require a recently validated deployment worker before checkout."""
        row = self.db.execute(
            "SELECT value FROM settings WHERE key='worker_heartbeat'"
        ).fetchone()
        return bool(row and int(row["value"]) >= int(time.time()) - 45)

    def set_price(self, tier: str, stars: int, admin: int) -> None:
        """Set a fixed Stars price; never derive prices from USDT.

        Args:
            tier: first, month, or year.
            stars: Positive integer Stars amount.
            admin: Verified control-plane administrator ID.
        """
        if tier not in TIERS or type(stars) is not int or not 1 <= stars <= 1000000:
            raise ValueError("Invalid Stars price")
        with self.tx() as db:
            db.execute(
                "INSERT INTO prices VALUES(?,?) "
                "ON CONFLICT(tier) DO UPDATE SET stars=excluded.stars",
                (tier, stars),
            )
            db.execute(
                "INSERT INTO audit(at,actor,action,ref) VALUES(?,?,?,?)",
                (int(time.time()), str(admin), "set_price", tier),
            )

    def create_order(
        self,
        owner_id: int,
        tier: str,
        bot_id: int | None = None,
        *,
        description: str = "",
    ) -> sqlite3.Row:
        """Reserve a priced order before issuing an invoice.

        Args:
            owner_id: Telegram payer ID.
            tier: first/month/year, or custom with an admin-issued quote.
            bot_id: Existing owned bot for renewal or custom work.
            description: Custom work description; empty for normal plans.

        Returns:
            The stored invoice snapshot.

        Raises:
            ValueError: Price, ownership, or first-month eligibility fails.
        """
        now = int(time.time())
        with self.tx() as db:
            if tier not in TIERS and tier != "custom":
                raise ValueError("Unknown plan")
            if tier != "custom" and self.price(tier) is None:
                raise ValueError("Stars pricing is not configured")
            if tier == "custom":
                raise ValueError("Use an approved custom quote")
            if bot_id is not None:
                bot = db.execute(
                    "SELECT 1 FROM bots WHERE id=? AND owner_id=?",
                    (bot_id, owner_id),
                ).fetchone()
                if not bot or tier == "first":
                    raise ValueError("Bot does not belong to the buyer")
            if tier == "first":
                used = db.execute(
                    "SELECT 1 FROM orders WHERE owner_id=? "
                    "AND tier IN ('first','month','year') AND "
                    "(status IN ('paid','provisioning','active') OR "
                    "(status='pending' AND bot_id IS NULL AND expires_at>?)) LIMIT 1",
                    (owner_id, now),
                ).fetchone()
                if used:
                    raise ValueError("First-month offer already used or reserved")
            order_id = secrets.token_hex(16)
            db.execute(
                "INSERT INTO orders(id,owner_id,bot_id,tier,stars,created_at,expires_at,description) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    order_id,
                    owner_id,
                    bot_id,
                    tier,
                    self.price(tier),
                    now,
                    now + ORDER_TTL,
                    description,
                ),
            )
            return db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()

    def create_quote(
        self, owner_id: int, bot_id: int, stars: int, description: str, admin: int
    ) -> sqlite3.Row:
        """Create one custom-extension invoice for an owned bot.

        Args:
            owner_id: Customer who must pay.
            bot_id: Target bot.
            stars: Admin-approved Stars amount.
            description: Short extension scope.
            admin: Operator ID for audit.

        Returns:
            Pending custom quote.
        """
        if (
            not 1 <= stars <= 1000000
            or not description.strip()
            or len(description) > 200
        ):
            raise ValueError("Invalid quote")
        with self.tx() as db:
            if not db.execute(
                "SELECT 1 FROM bots WHERE id=? AND owner_id=?", (bot_id, owner_id)
            ).fetchone():
                raise ValueError("Unknown customer bot")
            order_id = secrets.token_hex(16)
            now = int(time.time())
            db.execute(
                "INSERT INTO orders(id,owner_id,bot_id,tier,stars,created_at,expires_at,description) "
                "VALUES(?, ?, ?, 'custom', ?, ?, ?, ?)",
                (order_id, owner_id, bot_id, stars, now, now + 7 * 86400, description),
            )
            db.execute(
                "INSERT INTO audit(at,actor,action,ref) VALUES(?,?,?,?)",
                (now, str(admin), "quote", order_id),
            )
            return db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()

    def precheckout(self, order_id: str, owner_id: int, amount: int) -> bool:
        """Confirm exact payer, snapshot price and unexpired order."""
        row = self.db.execute(
            "SELECT 1 FROM orders WHERE id=? AND owner_id=? AND stars=? "
            "AND status='pending' AND expires_at>?",
            (order_id, owner_id, amount, int(time.time())),
        ).fetchone()
        return row is not None

    def settle(self, order_id: str, owner_id: int, amount: int, charge_id: str):
        """Apply a successful Telegram Stars payment exactly once."""
        now = int(time.time())
        with self.tx() as db:
            order = db.execute(
                "SELECT * FROM orders WHERE id=?", (order_id,)
            ).fetchone()
            if not order or order["owner_id"] != owner_id or order["stars"] != amount:
                raise ValueError("Payment does not match an order")
            if order["status"] != "pending":
                if order["charge_id"] == charge_id:
                    return order
                raise ValueError("Order already paid with another charge")
            if db.execute(
                "SELECT 1 FROM orders WHERE charge_id=?", (charge_id,)
            ).fetchone():
                raise ValueError("Charge belongs to another order")
            if (
                order["tier"] == "first"
                and db.execute(
                    "SELECT 1 FROM orders WHERE owner_id=? AND id<>? "
                    "AND tier IN ('first','month','year') "
                    "AND status IN ('paid','provisioning','active') LIMIT 1",
                    (owner_id, order_id),
                ).fetchone()
            ):
                raise ValueError("First-month offer already consumed")
            bot_id = order["bot_id"]
            status = "paid" if bot_id is None or order["tier"] == "custom" else "active"
            db.execute(
                "UPDATE orders SET status=?,charge_id=? WHERE id=?",
                (status, charge_id, order_id),
            )
            if bot_id is not None and order["tier"] != "custom":
                bot = db.execute(
                    "SELECT expires_at FROM bots WHERE id=? AND owner_id=?",
                    (bot_id, owner_id),
                ).fetchone()
                if not bot:
                    raise ValueError("Renewal bot no longer exists")
                expiry = extend_epoch(max(now, bot["expires_at"]), order["tier"])
                db.execute("UPDATE bots SET expires_at=? WHERE id=?", (expiry, bot_id))
                db.execute("DELETE FROM notices WHERE bot_id=?", (bot_id,))
                self._enqueue(db, bot_id, "reconcile")
            db.execute(
                "INSERT INTO audit(at,actor,action,ref) VALUES(?,?,?,?)",
                (now, str(owner_id), "stars_payment", order_id),
            )
            return db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()

    def attach_bot(
        self,
        owner_id: int,
        bot_id: int,
        username: str,
        cipher: bytes,
        defaults=None,
        allow_trial: bool = False,
    ) -> tuple[bool, int]:
        """Bind one manager-created Bot to a paid order, or rotate its token."""
        with self.tx() as db:
            existing = db.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()
            if existing:
                if existing["owner_id"] != owner_id:
                    raise ValueError("Managed bot owner mismatch")
                db.execute(
                    "UPDATE bots SET token_cipher=?, username=? WHERE id=?",
                    (cipher, username, bot_id),
                )
                self._enqueue(db, bot_id, "reconcile")
                return False, bot_id
            order = db.execute(
                "SELECT * FROM orders WHERE owner_id=? AND bot_id IS NULL "
                "AND status='paid' AND tier IN ('first','month','year') "
                "ORDER BY created_at,id LIMIT 1",
                (owner_id,),
            ).fetchone()
            trial = self.pending_trial(owner_id) if allow_trial and not order else None
            if not order and not trial:
                raise ValueError("No paid bot creation order for this owner")
            expiry = (
                extend_epoch(int(time.time()), order["tier"])
                if order
                else trial["expires_at"]
            )
            db.execute(
                "INSERT INTO bots(id,owner_id,username,token_cipher,expires_at) "
                "VALUES(?,?,?,?,?)",
                (bot_id, owner_id, username, cipher, expiry),
            )
            if order:
                db.execute(
                    "UPDATE orders SET bot_id=?,status='provisioning' WHERE id=?",
                    (bot_id, order["id"]),
                )
            else:
                db.execute(
                    "UPDATE trials SET bot_id=? WHERE id=?", (bot_id, trial["id"])
                )
                db.execute(
                    "INSERT INTO audit(at,actor,action,ref) VALUES(?,?,?,?)",
                    (int(time.time()), str(owner_id), "bind_trial", str(bot_id)),
                )
            if defaults is not None:
                db.execute(
                    "INSERT INTO tenant_entitlements VALUES(?,?,?,?)",
                    (
                        bot_id,
                        defaults["template"],
                        json.dumps(defaults["features"]),
                        defaults["model_quota"],
                    ),
                )
            self._enqueue(db, bot_id, "reconcile")
            return True, bot_id

    def _enqueue(self, db, bot_id: int, kind: str) -> None:
        db.execute(
            "INSERT INTO jobs(bot_id,kind) VALUES(?,?) ON CONFLICT(bot_id,kind) "
            "DO UPDATE SET state='pending',retry_at=0",
            (bot_id, kind),
        )

    def refund(self, order_id: str, admin: int) -> tuple[int, str]:
        """Mark an already-refunded, unfulfilled charge as refunded."""
        with self.tx() as db:
            order = db.execute(
                "SELECT * FROM orders WHERE id=?", (order_id,)
            ).fetchone()
            if not order or order["status"] not in {"paid", "provisioning"}:
                raise ValueError("Only unfulfilled orders can be refunded here")
            if order["bot_id"] is not None and order["tier"] != "custom":
                bot = db.execute(
                    "SELECT status FROM bots WHERE id=?", (order["bot_id"],)
                ).fetchone()
                if bot and bot["status"] == "active":
                    raise ValueError(
                        "Active bot refund needs manual entitlement review"
                    )
                if db.execute(
                    "SELECT 1 FROM orders WHERE bot_id=? AND id<>? "
                    "AND tier IN ('first','month','year') "
                    "AND status IN ('paid','provisioning','active') LIMIT 1",
                    (order["bot_id"], order_id),
                ).fetchone():
                    raise ValueError("Refund needs manual review of other entitlements")
                if bot:
                    db.execute(
                        "UPDATE bots SET expires_at=0 WHERE id=?", (order["bot_id"],)
                    )
                    self._enqueue(db, order["bot_id"], "reconcile")
            db.execute("UPDATE orders SET status='refunded' WHERE id=?", (order_id,))
            db.execute(
                "INSERT INTO audit(at,actor,action,ref) VALUES(?,?,?,?)",
                (int(time.time()), str(admin), "stars_refund", order_id),
            )
            return order["owner_id"], order["charge_id"]

    def bots_for(self, owner_id: int):
        return self.db.execute(
            "SELECT id,username,expires_at,status,dashboard_port FROM bots "
            "WHERE owner_id=? ORDER BY id",
            (owner_id,),
        ).fetchall()

    def propose_operation(self, actor: int, action: str, args: list[str]) -> str:
        """Persist a short-lived, actor-bound confirmation with validated inputs."""
        if action not in {
            "pause",
            "resume",
            "backup",
            "restore",
            "upgrade",
            "refund",
            "trial",
        }:
            raise ValueError("Unknown operation")
        if len(args) != (2 if action in {"restore", "trial"} else 1):
            raise ValueError(
                "用法：操作命令 机器人ID；恢复：/adminrestore 机器人ID 快照ID；退款：/adminrefund 订单ID"
            )
        if action == "trial":
            if (
                not all(value.isascii() and value.isdigit() for value in args)
                or not 0 < int(args[0]) < 2**52
                or not 1 <= int(args[1]) <= 7
            ):
                raise ValueError("用法：/admintrial 用户数字ID 天数（1–7）")
            bot_id, argument = None, json.dumps([int(value) for value in args])
        elif action == "refund":
            order = self.refundable(args[0])
            bot_id, argument = order["bot_id"], args[0]
        else:
            if (
                not args[0].isdigit()
                or not self.db.execute(
                    "SELECT 1 FROM bots WHERE id=?", (args[0],)
                ).fetchone()
            ):
                raise ValueError("Unknown bot")
            bot_id, argument = int(args[0]), args[1] if action == "restore" else ""
            if action == "restore" and not argument.isdigit():
                raise ValueError("Snapshot ID must be numeric")
        ref = secrets.token_hex(16)
        with self.tx() as db:
            db.execute(
                "INSERT INTO operations(id,actor,action,bot_id,argument,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (ref, actor, action, bot_id, argument, int(time.time())),
            )
            db.execute(
                "INSERT INTO audit(at,actor,action,ref) VALUES(?,?,?,?)",
                (int(time.time()), str(actor), "propose_" + action, ref),
            )
        return ref

    def confirm_operation(self, ref: str, actor: int):
        """Consume once; never replay uncertain refunds or worker operations."""
        with self.tx() as db:
            row = db.execute(
                "SELECT * FROM operations WHERE id=? AND actor=? AND state='confirm' "
                "AND created_at>=?",
                (ref, actor, int(time.time()) - 300),
            ).fetchone()
            if not row:
                raise ValueError("Expired or consumed confirmation")
            if row["action"] == "trial":
                owner, days = json.loads(row["argument"])
                now = int(time.time())
                if db.execute(
                    "SELECT 1 FROM trials WHERE owner_id=? AND expires_at>?",
                    (owner, now),
                ).fetchone():
                    raise ValueError("This customer already has an unexpired trial")
                db.execute(
                    "INSERT INTO trials(id,owner_id,created_at,expires_at) VALUES(?,?,?,?)",
                    (ref, owner, now, now + days * 86400),
                )
                state = "done"
            else:
                state = "needs_review" if row["action"] == "refund" else "pending"
            db.execute("UPDATE operations SET state=? WHERE id=?", (state, ref))
            db.execute(
                "INSERT INTO audit(at,actor,action,ref) VALUES(?,?,?,?)",
                (int(time.time()), str(actor), "confirm_" + row["action"], ref),
            )
        return row

    def pending_trial(self, owner_id: int):
        """Return only this customer's unbound, unexpired non-payment grant."""
        return self.db.execute(
            "SELECT * FROM trials WHERE owner_id=? AND bot_id IS NULL "
            "AND expires_at>? ORDER BY created_at,id LIMIT 1",
            (owner_id, int(time.time())),
        ).fetchone()

    def set_bot_enabled(self, bot_id: int, enabled: bool, admin: int) -> None:
        """Persist a platform-level stop switch independent of subscription."""
        with self.tx() as db:
            row = db.execute("SELECT id FROM bots WHERE id=?", (bot_id,)).fetchone()
            if not row:
                raise ValueError("Unknown bot")
            db.execute("UPDATE bots SET enabled=? WHERE id=?", (int(enabled), bot_id))
            self._enqueue(db, bot_id, "reconcile")
            db.execute(
                "INSERT INTO audit(at,actor,action,ref) VALUES(?,?,?,?)",
                (
                    int(time.time()),
                    str(admin),
                    "resume_bot" if enabled else "pause_bot",
                    str(bot_id),
                ),
            )

    def request_custom(self, owner_id: int, bot_id: int, body: str) -> int:
        """Save a tenant's scoped custom-extension request."""
        if not body.strip() or len(body) > 1000:
            raise ValueError("Request must be 1-1000 characters")
        with self.tx() as db:
            if not db.execute(
                "SELECT 1 FROM bots WHERE id=? AND owner_id=?", (bot_id, owner_id)
            ).fetchone():
                raise ValueError("Unknown customer bot")
            result = db.execute(
                "INSERT INTO requests(owner_id,bot_id,body,created_at) VALUES(?,?,?,?)",
                (owner_id, bot_id, body.strip(), int(time.time())),
            )
            return result.lastrowid

    def refundable(self, order_id: str) -> sqlite3.Row:
        """Validate fulfillment state before requesting a Telegram refund."""
        row = self.db.execute(
            "SELECT orders.*,bots.status AS bot_status FROM orders "
            "LEFT JOIN bots ON bots.id=orders.bot_id WHERE orders.id=?",
            (order_id,),
        ).fetchone()
        if (
            not row
            or row["status"] not in {"paid", "provisioning"}
            or (row["tier"] != "custom" and row["bot_status"] == "active")
            or not row["charge_id"]
        ):
            raise ValueError("Order needs manual refund review")
        return row

    def close(self):
        self.db.close()
