"""Instance-isolated transactions, migrations, authority and immutable ledgers."""

import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCOPES = {"ads", "points", "game", "moderation", "manager"}
MAX_POINTS = 10**12


class Rejected(ValueError):
    """An expected user-visible business rejection."""


def encode(value):
    """Encode stable snapshots for comparison and audit.

    Args:
        value: JSON-compatible data.
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class Store:
    """Own a single instance database; never share another bot's balances."""

    def __init__(self, path: Path, owner: str, clock=time.time):
        if not owner.isascii() or not owner.isdigit() or int(owner) <= 0:
            raise Rejected("请先核实首位超管数字 UID")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path, self.owner, self.clock = path, owner, clock
        self.db = sqlite3.connect(path, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        path.chmod(0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS user_labels(uid TEXT PRIMARY KEY,username TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ad_channels(chat TEXT PRIMARY KEY,title TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL,version INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS roles(uid TEXT PRIMARY KEY,scopes TEXT NOT NULL,expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS grants(token TEXT PRIMARY KEY,target TEXT NOT NULL,scopes TEXT NOT NULL,expires REAL NOT NULL,role_expires REAL NOT NULL,used INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS ledger(op TEXT PRIMARY KEY,uid TEXT NOT NULL,delta INTEGER NOT NULL,reason TEXT NOT NULL,at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS wallets(uid TEXT PRIMARY KEY,balance INTEGER NOT NULL CHECK(balance>=0 AND balance<=1000000000000));
            CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,at REAL NOT NULL,actor TEXT NOT NULL,action TEXT NOT NULL,data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS callbacks(token TEXT PRIMARY KEY,uid TEXT NOT NULL,chat TEXT NOT NULL,payload TEXT NOT NULL,expires REAL NOT NULL,used INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS callbacks_expiry ON callbacks(expires);
            CREATE TABLE IF NOT EXISTS dialogs(uid TEXT PRIMARY KEY,payload TEXT NOT NULL,expires REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS dialogs_expiry ON dialogs(expires);
            CREATE TABLE IF NOT EXISTS rewards(uid TEXT NOT NULL,day TEXT NOT NULL,last REAL NOT NULL,earned INTEGER NOT NULL,PRIMARY KEY(uid,day));
            CREATE TABLE IF NOT EXISTS chat_seen(uid TEXT NOT NULL,day TEXT NOT NULL,digest TEXT NOT NULL,PRIMARY KEY(uid,day,digest));
            CREATE TABLE IF NOT EXISTS ads(id TEXT PRIMARY KEY,uid TEXT NOT NULL,kind TEXT NOT NULL,body TEXT NOT NULL,contact TEXT NOT NULL,package TEXT NOT NULL,approved INTEGER NOT NULL DEFAULT 0,paid INTEGER NOT NULL DEFAULT 0,approved_by TEXT NOT NULL DEFAULT '',paid_by TEXT NOT NULL DEFAULT '',proof TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'pending',message INTEGER,expires REAL,at REAL NOT NULL,error TEXT NOT NULL DEFAULT '',renewal_of TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS draws(issue INTEGER PRIMARY KEY,at REAL NOT NULL,raw TEXT NOT NULL,balls TEXT NOT NULL,evidence TEXT NOT NULL,received REAL NOT NULL,conflict INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS bets(id TEXT PRIMARY KEY,uid TEXT NOT NULL,room TEXT NOT NULL,issue INTEGER NOT NULL,play TEXT NOT NULL,amount INTEGER NOT NULL,snapshot TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',payout INTEGER NOT NULL DEFAULT 0,at REAL NOT NULL,settled REAL);
            CREATE INDEX IF NOT EXISTS bets_pending ON bets(status,issue);
            CREATE INDEX IF NOT EXISTS bets_user_period ON bets(uid,issue,room);
            CREATE TABLE IF NOT EXISTS chases(id TEXT PRIMARY KEY,uid TEXT NOT NULL,room TEXT NOT NULL,plays TEXT NOT NULL,amount INTEGER NOT NULL,periods INTEGER NOT NULL,mode TEXT NOT NULL,stop_win INTEGER NOT NULL,next_issue INTEGER NOT NULL,step INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'active',error TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS notices(id TEXT PRIMARY KEY,chat TEXT NOT NULL,text TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending');
            DROP INDEX IF EXISTS notices_status;
            CREATE INDEX IF NOT EXISTS notices_pending ON notices(status) WHERE status='pending';
            INSERT OR IGNORE INTO meta(key,value) VALUES('schema','1');
            COMMIT;
        """)
        with self.tx() as db:
            for table in ("bets", "chases"):
                if "points_chat" not in {
                    row["name"] for row in db.execute(f"PRAGMA table_info({table})")
                }:
                    db.execute(
                        f"ALTER TABLE {table} ADD COLUMN points_chat TEXT NOT NULL DEFAULT ''"
                    )
            if "receipt_op" not in {
                row["name"] for row in db.execute("PRAGMA table_info(bets)")
            }:
                db.execute("ALTER TABLE bets ADD COLUMN receipt_op TEXT")
            db.execute(
                "CREATE INDEX IF NOT EXISTS bets_group_history ON bets(uid,points_chat,at)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS bets_group_period ON bets(uid,points_chat,issue)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS bets_receipt_status ON bets(receipt_op,status)"
            )
            existing = db.execute("SELECT value FROM meta WHERE key='owner'").fetchone()
            if existing and existing[0] != owner:
                raise Rejected("首位超管已绑定；禁止通过配置静默替换，请按恢复文档处理")
            db.execute(
                "INSERT OR IGNORE INTO meta(key,value) VALUES('owner',?)", (owner,)
            )
            # Sending may have reached Telegram before a process crash.
            db.execute(
                "UPDATE ads SET status='review',error='restart_unknown' WHERE status IN ('sending','pinning','unpinning')"
            )
            db.execute("UPDATE notices SET status='unknown' WHERE status='sending'")
        from .ad_state import migrate

        migrate(self)
        from .tenants import migrate as migrate_tenants

        migrate_tenants(self)
        from .merchant_ads import migrate as migrate_merchant_ads

        migrate_merchant_ads(self)
        self.ad_locks = {}

    @contextmanager
    def tx(self):
        """Commit bounded synchronous work atomically; never await inside.

        Yields:
            The transaction connection.
        """
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def get(self, key, default=None, db=None):
        row = (
            (db or self.db)
            .execute("SELECT value FROM settings WHERE key=?", (key,))
            .fetchone()
        )
        return json.loads(row[0]) if row else default

    def put(self, db, key, value):
        db.execute(
            "INSERT INTO settings(key,value,version) VALUES(?,?,1) ON CONFLICT(key) DO UPDATE SET value=excluded.value,version=settings.version+1",
            (key, encode(value)),
        )

    def audit(self, db, actor, action, data):
        db.execute(
            "INSERT INTO audit(at,actor,action,data) VALUES(?,?,?,?)",
            (self.clock(), str(actor), action, encode(data)),
        )

    def allowed(self, uid, scope=None, db=None):
        if str(uid) == self.owner:
            return True
        row = (
            (db or self.db)
            .execute("SELECT scopes,expires FROM roles WHERE uid=?", (str(uid),))
            .fetchone()
        )
        return bool(
            row
            and row[1] > self.clock()
            and (
                scope is None
                or scope in json.loads(row[0])
                or (scope in SCOPES and "manager" in json.loads(row[0]))
            )
        )

    def require(self, uid, scope=None, db=None, owner=False, *, chat=None):
        if chat is not None and not owner:
            from .tenants import LOCAL_SCOPES, PLATFORM, binding

            row = binding(self, chat, db)
            if row and row["tenant"] != PLATFORM:
                if str(uid) == self.owner:
                    return
                if (
                    row["owner"] == str(uid)
                    and row["status"] == row["tenant_status"] == "active"
                    and (scope is None or scope in LOCAL_SCOPES)
                ):
                    return
                raise Rejected("没有此群经营主体的权限")
        if (owner and str(uid) != self.owner) or not self.allowed(uid, scope, db):
            raise Rejected("无权限")

    def grant(self, uid, target, scopes, days):
        self.require(uid, owner=True)
        if (
            not str(target).isdigit()
            or int(target) <= 0
            or str(target) == self.owner
            or not set(scopes) <= SCOPES
            or not scopes
            or not 1 <= days <= 365
        ):
            raise Rejected("请填写有效目标 UID、权限和 1—365 天授权期限")
        token = secrets.token_urlsafe(24)
        with self.tx() as db:
            db.execute(
                "INSERT INTO grants(token,target,scopes,expires,role_expires) VALUES(?,?,?,?,?)",
                (
                    hashlib.sha256(token.encode()).hexdigest(),
                    str(target),
                    encode(sorted(set(scopes))),
                    self.clock() + 600,
                    self.clock() + days * 86400,
                ),
            )
            self.audit(
                db,
                uid,
                "grant_created",
                {"target": str(target), "scopes": scopes, "days": days},
            )
        return token

    def accept_grant(self, uid, token):
        with self.tx() as db:
            key = hashlib.sha256(token.encode()).hexdigest()
            row = db.execute("SELECT * FROM grants WHERE token=?", (key,)).fetchone()
            if (
                not row
                or row["target"] != str(uid)
                or row["used"]
                or row["expires"] <= self.clock()
            ):
                raise Rejected("授权链接无效、过期或不属于你")
            db.execute("UPDATE grants SET used=1 WHERE token=?", (key,))
            scopes = json.loads(row["scopes"])
            if "moderation" in scopes and "manager" not in scopes:
                groups = db.execute(
                    "SELECT groups_json FROM mod_grants WHERE token=?", (key,)
                ).fetchone()
                if not groups:
                    raise Rejected("群管授权缺少群范围，请重新签发")
                db.execute("DELETE FROM mod_acl WHERE uid=?", (str(uid),))
                db.executemany(
                    "INSERT INTO mod_acl(uid,chat) VALUES(?,?)",
                    [(str(uid), chat) for chat in json.loads(groups[0])],
                )
            db.execute(
                "INSERT INTO roles(uid,scopes,expires) VALUES(?,?,?) ON CONFLICT(uid) DO UPDATE SET scopes=excluded.scopes,expires=excluded.expires",
                (str(uid), row["scopes"], row["role_expires"]),
            )
            self.audit(db, uid, "grant_accepted", {"scopes": json.loads(row["scopes"])})

    def revoke(self, uid, target):
        with self.tx() as db:
            self.require(uid, owner=True, db=db)
            db.execute("DELETE FROM roles WHERE uid=?", (str(target),))
            db.execute("UPDATE grants SET used=1 WHERE target=?", (str(target),))
            self.audit(db, uid, "access_revoked", {"target": str(target)})

    def credit(self, db, op, uid, delta, reason, chat=None):
        """Apply an exactly-once integer movement in the caller's transaction.

        Args:
            db: Active transaction.
            op: Unique business operation.
            uid: Telegram numeric user identifier.
            delta: Signed whole points.
            reason: Fixed business reason or administrator explanation.
            chat: Explicit group wallet; mandatory after the one-time migration.

        Returns:
            Whether a new movement was committed.
        """
        uid = str(uid)
        if type(delta) is not int or abs(delta) > MAX_POINTS:
            raise Rejected("积分数值无效")
        if self.get("group_points_enabled", False, db):
            if not chat or not str(chat).startswith("-") or not str(chat)[1:].isdigit():
                raise Rejected("积分按群独立，请指定群后操作")
            chat = str(chat)
            prior = db.execute(
                "SELECT uid,delta,reason FROM group_ledger WHERE chat=? AND op=?",
                (chat, op),
            ).fetchone()
            if prior:
                if tuple(prior) != (uid, delta, reason):
                    raise Rejected("操作冲突，请核查流水")
                return False
            db.execute(
                "INSERT OR IGNORE INTO group_wallets(chat,uid,balance) VALUES(?,?,0)",
                (chat, uid),
            )
            balance = db.execute(
                "SELECT balance FROM group_wallets WHERE chat=? AND uid=?", (chat, uid)
            ).fetchone()[0]
            if not 0 <= balance + delta <= MAX_POINTS:
                raise Rejected("本群积分不足或超出余额上限")
            db.execute(
                "UPDATE group_wallets SET balance=balance+? WHERE chat=? AND uid=?",
                (delta, chat, uid),
            )
            db.execute(
                "INSERT INTO group_ledger(chat,op,uid,delta,reason,at) VALUES(?,?,?,?,?,?)",
                (chat, op, uid, delta, reason, self.clock()),
            )
            return True
        prior = db.execute(
            "SELECT uid,delta,reason FROM ledger WHERE op=?", (op,)
        ).fetchone()
        if prior:
            if tuple(prior) != (uid, delta, reason):
                raise Rejected("操作冲突，请核查流水")
            return False
        db.execute("INSERT OR IGNORE INTO wallets(uid,balance) VALUES(?,0)", (uid,))
        balance = db.execute(
            "SELECT balance FROM wallets WHERE uid=?", (uid,)
        ).fetchone()[0]
        if not 0 <= balance + delta <= MAX_POINTS:
            raise Rejected("积分不足或超出余额上限")
        db.execute("UPDATE wallets SET balance=balance+? WHERE uid=?", (delta, uid))
        db.execute(
            "INSERT INTO ledger(op,uid,delta,reason,at) VALUES(?,?,?,?,?)",
            (op, uid, delta, reason, self.clock()),
        )
        return True

    def balance(self, uid, chat=None):
        if self.get("group_points_enabled", False):
            if not chat:
                raise Rejected("积分按群独立，请选择群后查询")
            row = self.db.execute(
                "SELECT balance FROM group_wallets WHERE chat=? AND uid=?",
                (str(chat), str(uid)),
            ).fetchone()
            return row[0] if row else 0
        row = self.db.execute(
            "SELECT balance FROM wallets WHERE uid=?", (str(uid),)
        ).fetchone()
        return row[0] if row else 0

    def callback(self, uid, chat, payload):
        token = secrets.token_urlsafe(16)
        self.db.execute(
            "INSERT INTO callbacks(token,uid,chat,payload,expires) VALUES(?,?,?,?,?)",
            (token, str(uid), str(chat), encode(payload), self.clock() + 600),
        )
        return "sb:" + token

    def resolve(self, token, uid, chat):
        row = self.db.execute(
            "SELECT * FROM callbacks WHERE token=?", (token,)
        ).fetchone()
        if (
            not row
            or row["uid"] != str(uid)
            or row["chat"] != str(chat)
            or row["expires"] <= self.clock()
        ):
            raise Rejected("按钮已过期或不属于当前会话，请重新打开菜单")
        return json.loads(row["payload"])

    def consume(self, token):
        with self.tx() as db:
            changed = db.execute(
                "UPDATE callbacks SET used=1 WHERE token=? AND used=0 AND expires>?",
                (token, self.clock()),
            ).rowcount
            if not changed:
                raise Rejected("此确认已使用，请查询订单或流水；需要新操作请重新预览")

    def dialog(self, uid, payload=None):
        if payload is not None:
            self.db.execute(
                "INSERT INTO dialogs(uid,payload,expires) VALUES(?,?,?) ON CONFLICT(uid) DO UPDATE SET payload=excluded.payload,expires=excluded.expires",
                (str(uid), encode(payload), self.clock() + 600),
            )
            return None
        row = self.db.execute(
            "SELECT payload,expires FROM dialogs WHERE uid=?", (str(uid),)
        ).fetchone()
        return json.loads(row[0]) if row and row[1] > self.clock() else None

    def clear_dialog(self, uid):
        self.db.execute("DELETE FROM dialogs WHERE uid=?", (str(uid),))

    def close(self):
        self.db.close()
