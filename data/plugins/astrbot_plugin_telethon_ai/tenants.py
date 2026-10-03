"""Tenant entitlements and conservative, transactional request reservations."""

import json
import secrets
import sqlite3
import time
from pathlib import Path


class Denied(ValueError):
    pass


class Tenants:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        path.chmod(0o600)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS tenants (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, bot TEXT UNIQUE NOT NULL,
                platform TEXT UNIQUE NOT NULL, expires REAL NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 0,
                budget INTEGER NOT NULL CHECK(budget>0),
                group_limit INTEGER NOT NULL CHECK(group_limit>0)
            );
            CREATE TABLE IF NOT EXISTS leases (
                account TEXT PRIMARY KEY, tenant TEXT NOT NULL REFERENCES tenants(id)
            );
            CREATE TABLE IF NOT EXISTS groups (
                chat TEXT PRIMARY KEY, tenant TEXT NOT NULL REFERENCES tenants(id),
                authorized INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS reservations (
                id TEXT PRIMARY KEY, tenant TEXT NOT NULL REFERENCES tenants(id),
                account TEXT NOT NULL, chat TEXT NOT NULL, message TEXT NOT NULL,
                created REAL NOT NULL, state TEXT NOT NULL,
                UNIQUE(account,chat,message)
            );
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY, at REAL NOT NULL, actor TEXT NOT NULL,
                action TEXT NOT NULL, details TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS grants (
                owner TEXT PRIMARY KEY, expires REAL NOT NULL,
                budget INTEGER NOT NULL, group_limit INTEGER NOT NULL,
                bot TEXT UNIQUE
            );
            CREATE TABLE IF NOT EXISTS enrollments (
                bot TEXT PRIMARY KEY, owner TEXT NOT NULL, username TEXT NOT NULL,
                token_cipher BLOB NOT NULL, tenant TEXT UNIQUE REFERENCES tenants(id),
                state TEXT NOT NULL DEFAULT 'pending', error_code TEXT
            );
            CREATE TABLE IF NOT EXISTS requests (
                id TEXT PRIMARY KEY, tenant TEXT NOT NULL REFERENCES tenants(id),
                actor TEXT NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY, tenant TEXT NOT NULL REFERENCES tenants(id),
                owner TEXT NOT NULL, bot TEXT NOT NULL, kind TEXT NOT NULL,
                cycle TEXT NOT NULL, account TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'pending', created REAL NOT NULL,
                next_attempt REAL NOT NULL DEFAULT 0, delivered REAL,
                message_id INTEGER, error_code TEXT,
                UNIQUE(tenant,kind,cycle,account)
            );
            CREATE INDEX IF NOT EXISTS notifications_delivery
                ON notifications(state,next_attempt);
        """)

    def grant(self, actor, owner, days=2):
        owner = self.positive(owner)
        if type(days) is not int or not 1 <= days <= 7:
            raise Denied("Trial duration must be 1-7 days")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.db.execute(
                "SELECT 1 FROM grants WHERE owner=?", (owner,)
            ).fetchone():
                raise Denied("Trial already issued; operator review required")
            self.db.execute(
                "INSERT INTO grants VALUES (?,?,30,1,NULL)",
                (owner, time.time() + days * 86400),
            )
            self.audit(actor, "grant_issued", owner=owner, days=days)

    def pending_grant(self, owner):
        return self.db.execute(
            "SELECT * FROM grants WHERE owner=? AND bot IS NULL AND expires>?",
            (str(owner), time.time()),
        ).fetchone()

    def enroll(self, owner, bot, username, cipher):
        owner, bot = self.positive(owner), self.positive(bot)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            existing = self.db.execute(
                "SELECT * FROM enrollments WHERE bot=?", (bot,)
            ).fetchone()
            if existing:
                if existing["owner"] != owner:
                    raise Denied("Bot belongs to another owner")
                return existing["tenant"]
            grant = self.pending_grant(owner)
            if grant is None:
                raise Denied("No pending grant")
            tenant = secrets.token_hex(12)
            self.db.execute(
                "INSERT INTO tenants VALUES (?,?,?,?,?,0,?,?)",
                (
                    tenant,
                    owner,
                    bot,
                    "AIClient_" + bot,
                    grant["expires"],
                    grant["budget"],
                    grant["group_limit"],
                ),
            )
            self.db.execute(
                "INSERT INTO enrollments(bot,owner,username,token_cipher,tenant) VALUES (?,?,?,?,?)",
                (bot, owner, username, cipher, tenant),
            )
            self.db.execute("UPDATE grants SET bot=? WHERE owner=?", (bot, owner))
            self.audit(owner, "bot_enrolled", tenant=tenant, bot=bot)
            return tenant

    def request(self, platform, bot, actor, kind, value=""):
        owner = self.owned(platform, bot, actor)
        if kind not in {"group", "resume", "renew"} or (
            kind != "renew" and owner["expires"] <= time.time()
        ):
            raise Denied("Request not permitted")
        if kind == "group" and (
            not str(value).startswith("-100") or not str(value)[1:].isdigit()
        ):
            raise Denied("Expected numeric group ID")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if (
                self.db.execute(
                    "SELECT COUNT(*) FROM requests WHERE tenant=? AND state='pending'",
                    (owner["id"],),
                ).fetchone()[0]
                >= 5
            ):
                raise Denied("Too many pending requests")
            request_id = secrets.token_hex(8)
            self.db.execute(
                "INSERT INTO requests VALUES (?,?,?,?,?,'pending',?)",
                (request_id, owner["id"], str(actor), kind, str(value), time.time()),
            )
            self.audit(
                actor,
                "request_created",
                tenant=owner["id"],
                request=request_id,
                kind=kind,
            )
            return request_id

    def review_request(self, actor, request_id, approve):
        if type(approve) is not bool:
            raise Denied("Expected boolean decision")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            item = self.db.execute(
                "SELECT * FROM requests WHERE id=? AND state='pending'", (request_id,)
            ).fetchone()
            if item is None:
                raise Denied("Request missing or already reviewed")
            tenant = self.db.execute(
                "SELECT * FROM tenants WHERE id=?", (item["tenant"],)
            ).fetchone()
            if approve:
                if tenant["expires"] <= time.time():
                    raise Denied(
                        "Tenant expired; renewal requires a separate reviewed grant"
                    )
                if item["kind"] == "renew":
                    raise Denied("Automatic renewal not enabled")
                if item["kind"] == "resume":
                    self.db.execute(
                        "UPDATE tenants SET enabled=1 WHERE id=?", (tenant["id"],)
                    )
                else:
                    count = self.db.execute(
                        "SELECT COUNT(*) FROM groups WHERE tenant=?", (tenant["id"],)
                    ).fetchone()[0]
                    if count >= tenant["group_limit"]:
                        raise Denied("Group quota exceeded")
                    self.db.execute(
                        "INSERT INTO groups VALUES (?,?,1)",
                        (item["value"], tenant["id"]),
                    )
            state = "approved" if approve else "rejected"
            self.db.execute(
                "UPDATE requests SET state=? WHERE id=?", (state, request_id)
            )
            self.audit(
                actor,
                "request_reviewed",
                request=request_id,
                state=state,
                tenant=tenant["id"],
            )

    def audit(self, actor, action, **details):
        self.db.execute(
            "INSERT INTO audit(at,actor,action,details) VALUES (?,?,?,?)",
            (time.time(), str(actor), action, json.dumps(details)),
        )

    @staticmethod
    def positive(value):
        if not str(value).isdigit() or int(value) <= 0:
            raise Denied("Expected positive numeric ID")
        return str(value)

    def create_trial(
        self, actor, owner, bot, platform, days=2, budget=30, group_limit=1
    ):
        owner, bot = self.positive(owner), self.positive(bot)
        if (
            type(days) is not int
            or not 1 <= days <= 7
            or type(budget) is not int
            or not 1 <= budget <= 1000
            or type(group_limit) is not int
            or not 1 <= group_limit <= 5
            or not platform.startswith("AIClient_")
            or ":" in platform
        ):
            raise Denied("Invalid trial limits or platform")
        tenant = secrets.token_hex(12)
        with self.db:
            self.db.execute(
                "INSERT INTO tenants VALUES (?,?,?,?,?,0,?,?)",
                (
                    tenant,
                    owner,
                    bot,
                    platform,
                    time.time() + days * 86400,
                    budget,
                    group_limit,
                ),
            )
            self.audit(actor, "trial_created", tenant=tenant, bot=bot)
        return tenant

    def owned(self, platform, bot, actor):
        row = self.db.execute(
            "SELECT * FROM tenants WHERE platform=? AND bot=? AND owner=?",
            (str(platform), str(bot), str(actor)),
        ).fetchone()
        if row is None:
            raise Denied("Not the owner of this customer Bot")
        return row

    def assign(self, actor, tenant, account):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT * FROM tenants WHERE id=?", (tenant,)
            ).fetchone()
            if row is None or row["expires"] <= time.time():
                raise Denied("Trial missing or expired")
            # No automatic reallocation: history and in-flight requests require review.
            self.db.execute("INSERT INTO leases VALUES (?,?)", (account, tenant))
            self.audit(actor, "account_assigned", tenant=tenant, account=account)

    def authorize_group(self, actor, tenant, chat):
        chat = str(chat)
        if not chat.startswith("-100") or not chat[1:].isdigit():
            raise Denied("Expected Telegram supergroup ID")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT * FROM tenants WHERE id=?", (tenant,)
            ).fetchone()
            if row is None or row["expires"] <= time.time():
                raise Denied("Trial missing or expired")
            count = self.db.execute(
                "SELECT COUNT(*) FROM groups WHERE tenant=?", (tenant,)
            ).fetchone()[0]
            if count >= row["group_limit"]:
                raise Denied("Group quota exceeded")
            self.db.execute("INSERT INTO groups VALUES (?,?,1)", (chat, tenant))
            self.audit(actor, "group_authorized", tenant=tenant, chat=chat)

    def set_enabled(self, actor, tenant, enabled):
        if type(enabled) is not bool:
            raise Denied("Expected boolean")
        with self.db:
            row = self.db.execute(
                "SELECT * FROM tenants WHERE id=?", (tenant,)
            ).fetchone()
            if row is None or (enabled and row["expires"] <= time.time()):
                raise Denied("Trial missing or expired")
            self.db.execute(
                "UPDATE tenants SET enabled=? WHERE id=?", (int(enabled), tenant)
            )
            self.audit(actor, "enabled_changed", tenant=tenant, enabled=enabled)

    def reserve(self, account, chat, message):
        now = time.time()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT t.* FROM tenants t JOIN leases l ON l.tenant=t.id "
                "JOIN groups g ON g.tenant=t.id "
                "WHERE l.account=? AND g.chat=? AND g.authorized=1",
                (account, str(chat)),
            ).fetchone()
            if row is None or not row["enabled"] or row["expires"] <= now:
                raise Denied("No active tenant entitlement")
            used = self.db.execute(
                "SELECT COUNT(*) FROM reservations WHERE tenant=? AND state!='released'",
                (row["id"],),
            ).fetchone()[0]
            inflight = self.db.execute(
                "SELECT COUNT(*) FROM reservations WHERE tenant=? AND state='reserved' AND created>?",
                (row["id"], now - 120),
            ).fetchone()[0]
            if used >= row["budget"] or inflight:
                raise Denied("Quota exhausted or request already in flight")
            reservation = secrets.token_hex(16)
            try:
                self.db.execute(
                    "INSERT INTO reservations VALUES (?,?,?,?,?,?,'reserved')",
                    (reservation, row["id"], account, str(chat), str(message), now),
                )
            except sqlite3.IntegrityError as exc:
                raise Denied("Duplicate message") from exc
            self.audit("gateway", "reserved", tenant=row["id"], reservation=reservation)
            return reservation, row["id"]

    def can_send(self, reservation):
        row = self.db.execute(
            "SELECT r.created,r.state,t.enabled,t.expires FROM reservations r "
            "JOIN tenants t ON t.id=r.tenant JOIN leases l ON l.tenant=t.id AND l.account=r.account "
            "JOIN groups g ON g.tenant=t.id AND g.chat=r.chat AND g.authorized=1 WHERE r.id=?",
            (reservation,),
        ).fetchone()
        now = time.time()
        return bool(
            row
            and row["state"] == "reserved"
            and row["enabled"]
            and row["expires"] > now
            and now - row["created"] <= 120
        )

    def finish(self, reservation, state):
        if state not in {"sent", "uncertain", "released"}:
            raise Denied("Invalid settlement")
        with self.db:
            cursor = self.db.execute(
                "UPDATE reservations SET state=? WHERE id=? AND state='reserved'",
                (state, reservation),
            )
            if cursor.rowcount:
                self.audit("gateway", "settled", reservation=reservation, state=state)

    def summary(self, tenant):
        row = self.db.execute("SELECT * FROM tenants WHERE id=?", (tenant,)).fetchone()
        if row is None:
            raise Denied("Unknown tenant")
        result = dict(row)
        result["used"] = self.db.execute(
            "SELECT COUNT(*) FROM reservations WHERE tenant=? AND state!='released'",
            (tenant,),
        ).fetchone()[0]
        result["accounts"] = [
            r[0]
            for r in self.db.execute(
                "SELECT account FROM leases WHERE tenant=?", (tenant,)
            )
        ]
        result["groups"] = [
            r[0]
            for r in self.db.execute(
                "SELECT chat FROM groups WHERE tenant=?", (tenant,)
            )
        ]
        return result

    def close(self):
        self.db.close()
