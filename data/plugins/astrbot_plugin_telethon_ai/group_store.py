"""Durable group management state, independent of AI entitlements and quotas."""

import json
import secrets
import sqlite3
import time
from pathlib import Path

from .tenants import Denied


class GroupStore:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        path.chmod(0o600)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS claims(
                chat TEXT PRIMARY KEY, owner TEXT NOT NULL, automatic TEXT
            );
            CREATE TABLE IF NOT EXISTS bindings(
                platform TEXT NOT NULL, chat TEXT NOT NULL REFERENCES claims(chat),
                bot TEXT NOT NULL, owner TEXT NOT NULL, tenant TEXT,
                title TEXT NOT NULL, rules TEXT NOT NULL DEFAULT '',
                welcome TEXT NOT NULL DEFAULT '', welcome_enabled INTEGER NOT NULL DEFAULT 0,
                keywords_enabled INTEGER NOT NULL DEFAULT 0, disabled_reason TEXT NOT NULL DEFAULT '',
                generation INTEGER NOT NULL DEFAULT 0, last_reply REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(platform,chat)
            );
            CREATE TABLE IF NOT EXISTS keywords(
                id INTEGER PRIMARY KEY, platform TEXT NOT NULL, chat TEXT NOT NULL,
                mode TEXT NOT NULL CHECK(mode IN ('exact','contains')),
                phrase TEXT NOT NULL, response TEXT NOT NULL,
                UNIQUE(platform,chat,mode,phrase),
                FOREIGN KEY(platform,chat) REFERENCES bindings(platform,chat) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS confirmations(
                token TEXT PRIMARY KEY, platform TEXT NOT NULL, bot TEXT NOT NULL,
                actor TEXT NOT NULL, chat TEXT NOT NULL, payload TEXT NOT NULL,
                expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS operations(
                id TEXT PRIMARY KEY, platform TEXT NOT NULL, chat TEXT NOT NULL,
                actor TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL,
                at REAL NOT NULL, details TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mutes(
                platform TEXT NOT NULL, chat TEXT NOT NULL, target TEXT NOT NULL,
                snapshot TEXT NOT NULL, baseline TEXT NOT NULL, until REAL NOT NULL,
                PRIMARY KEY(platform,chat,target)
            );
            CREATE TABLE IF NOT EXISTS audit(
                id INTEGER PRIMARY KEY, at REAL NOT NULL, actor TEXT NOT NULL,
                platform TEXT NOT NULL, chat TEXT NOT NULL,
                action TEXT NOT NULL, details TEXT NOT NULL
            );
        """)
        if "public" not in {
            row["name"] for row in self.db.execute("PRAGMA table_info(bindings)")
        }:
            self.db.execute(
                "ALTER TABLE bindings ADD COLUMN public INTEGER NOT NULL DEFAULT 0"
            )
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS access_hints(
                platform TEXT NOT NULL, chat TEXT NOT NULL, actor TEXT NOT NULL,
                PRIMARY KEY(platform,chat,actor),
                FOREIGN KEY(platform,chat) REFERENCES bindings(platform,chat) ON DELETE CASCADE
            );
        """)
        with self.db:
            self.db.execute(
                "UPDATE operations SET state='uncertain' WHERE state='sending'"
            )

    def audit(self, actor, platform, chat, action, **details):
        self.db.execute(
            "INSERT INTO audit(at,actor,platform,chat,action,details) VALUES(?,?,?,?,?,?)",
            (time.time(), str(actor), platform, str(chat), action, json.dumps(details)),
        )

    def binding(self, platform, chat):
        row = self.db.execute(
            "SELECT b.*,c.automatic FROM bindings b JOIN claims c USING(chat) "
            "WHERE b.platform=? AND b.chat=?",
            (platform, str(chat)),
        ).fetchone()
        if row is None:
            raise Denied("Group is not bound to this Bot")
        return dict(row)

    def bind(self, platform, bot, owner, tenant, chat, title, limit, *, public=False):
        """Atomically claim a group and enforce this Bot's independent limit.

        Raises:
            Denied: Another owner holds the group, or a binding limit is exceeded.
        """
        chat, owner, bot = str(chat), str(owner), str(bot)
        if not chat.startswith("-") or not chat[1:].isdigit() or not owner.isdigit():
            raise Denied("Invalid group identity")
        if public and tenant is not None:
            raise Denied("Public group management cannot grant tenant access")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            existing = self.db.execute(
                "SELECT owner FROM claims WHERE chat=?", (chat,)
            ).fetchone()
            if not public and existing and existing["owner"] not in {"", owner}:
                raise Denied("Group belongs to another owner")
            bound = self.db.execute(
                "SELECT owner,bot,tenant,public FROM bindings WHERE platform=? AND chat=?",
                (platform, chat),
            ).fetchone()
            if bound:
                if (
                    bound["owner"],
                    bound["bot"],
                    bound["tenant"],
                    bool(bound["public"]),
                ) != (
                    owner,
                    bot,
                    tenant,
                    public,
                ):
                    raise Denied("Binding identity changed")
                return
            count = self.db.execute(
                "SELECT COUNT(*) FROM bindings WHERE platform=?", (platform,)
            ).fetchone()[0]
            if limit is not None and count >= limit:
                raise Denied("Group management limit exceeded")
            self.db.execute(
                "INSERT OR IGNORE INTO claims(chat,owner) VALUES(?,?)",
                (chat, "" if public else owner),
            )
            if not public:
                self.db.execute(
                    "UPDATE claims SET owner=? WHERE chat=? AND owner=''", (owner, chat)
                )
            self.db.execute(
                "INSERT INTO bindings(platform,chat,bot,owner,tenant,title,public) VALUES(?,?,?,?,?,?,?)",
                (platform, chat, bot, owner, tenant, title[:200], public),
            )
            self.audit(owner, platform, chat, "bound", public=public)

    def unbind(self, actor, platform, chat):
        with self.db:
            binding = self.binding(platform, chat)
            if not binding["public"] and str(actor) != binding["owner"]:
                raise Denied("Only the owner can unbind")
            self.db.execute(
                "UPDATE claims SET automatic=NULL WHERE chat=? AND automatic=?",
                (str(chat), platform),
            )
            self.db.execute(
                "DELETE FROM bindings WHERE platform=? AND chat=?",
                (platform, str(chat)),
            )
            self.db.execute(
                "DELETE FROM claims WHERE chat=? AND NOT EXISTS"
                "(SELECT 1 FROM bindings WHERE chat=?)",
                (str(chat), str(chat)),
            )
            self.db.execute(
                "UPDATE claims SET owner='' WHERE chat=? AND NOT EXISTS"
                "(SELECT 1 FROM bindings WHERE chat=? AND public=0)",
                (str(chat), str(chat)),
            )
            self.audit(actor, platform, chat, "unbound")

    def set_automatic(self, actor, platform, chat):
        with self.db:
            binding = self.binding(platform, chat)
            if not binding["public"] and str(actor) != binding["owner"]:
                raise Denied("Only the owner can select the automatic Bot")
            self.db.execute(
                "UPDATE claims SET automatic=? WHERE chat=?", (platform, str(chat))
            )
            self.db.execute(
                "UPDATE bindings SET disabled_reason='',generation=generation+1 "
                "WHERE platform=? AND chat=?",
                (platform, str(chat)),
            )
            self.audit(actor, platform, chat, "automatic_selected")

    def disable(self, platform, chat, reason):
        with self.db:
            binding = self.binding(platform, chat)
            if binding["disabled_reason"]:
                return
            self.db.execute(
                "UPDATE bindings SET disabled_reason=?,generation=generation+1 "
                "WHERE platform=? AND chat=?",
                (reason, platform, str(chat)),
            )
            self.audit("system", platform, chat, "automatic_stopped", reason=reason)

    def set_config(self, actor, platform, chat, field, value):
        if field not in {"rules", "welcome", "welcome_enabled", "keywords_enabled"}:
            raise Denied("Unknown setting")
        if field.endswith("_enabled"):
            if type(value) is not bool:
                raise Denied("Expected a switch")
        elif not isinstance(value, str) or len(value) > 3000:
            raise Denied("Text exceeds 3000 characters")
        with self.db:
            self.binding(platform, chat)
            self.db.execute(
                f"UPDATE bindings SET {field}=? WHERE platform=? AND chat=?",
                (value, platform, str(chat)),
            )
            self.audit(actor, platform, chat, "setting_saved", field=field)

    def add_keyword(self, actor, platform, chat, mode, phrase, response):
        if mode not in {"exact", "contains"} or not phrase.strip() or len(phrase) > 100:
            raise Denied("Invalid keyword")
        if not response.strip() or len(response) > 3000:
            raise Denied("Invalid reply")
        with self.db:
            self.binding(platform, chat)
            if (
                self.db.execute(
                    "SELECT COUNT(*) FROM keywords WHERE platform=? AND chat=?",
                    (platform, str(chat)),
                ).fetchone()[0]
                >= 50
            ):
                raise Denied("Keyword limit exceeded")
            try:
                self.db.execute(
                    "INSERT INTO keywords(platform,chat,mode,phrase,response) VALUES(?,?,?,?,?)",
                    (platform, str(chat), mode, phrase.strip(), response),
                )
            except sqlite3.IntegrityError as exc:
                raise Denied("Keyword already exists") from exc
            self.audit(actor, platform, chat, "keyword_added", mode=mode)

    def match(self, platform, chat, text):
        rows = self.db.execute(
            "SELECT * FROM keywords WHERE platform=? AND chat=? "
            "ORDER BY CASE mode WHEN 'exact' THEN 0 ELSE 1 END, length(phrase) DESC,id",
            (platform, str(chat)),
        )
        for row in rows:
            if (row["mode"] == "exact" and text == row["phrase"]) or (
                row["mode"] == "contains" and row["phrase"] in text
            ):
                return row["response"]
        return None

    def token(self, platform, bot, actor, chat, **payload):
        token = secrets.token_hex(12)
        with self.db:
            self.db.execute(
                "DELETE FROM confirmations WHERE expires<?", (time.time() - 86400,)
            )
            self.db.execute(
                "INSERT INTO confirmations VALUES(?,?,?,?,?,?,?,0)",
                (
                    token,
                    platform,
                    str(bot),
                    str(actor),
                    str(chat),
                    json.dumps(payload),
                    time.time() + 300,
                ),
            )
        return token

    def consume(self, token, platform, bot, actor):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT * FROM confirmations WHERE token=? AND platform=? AND bot=? "
                "AND actor=? AND used=0 AND expires>?",
                (token, platform, str(bot), str(actor), time.time()),
            ).fetchone()
            if row is None:
                raise Denied("Confirmation expired, used or belongs to another user")
            self.db.execute("UPDATE confirmations SET used=1 WHERE token=?", (token,))
            return row["chat"], json.loads(row["payload"])

    def claim_operation(self, key, platform, chat, actor, kind, **details):
        with self.db:
            return (
                self.db.execute(
                    "INSERT OR IGNORE INTO operations VALUES(?,?,?,?,?,'sending',?,?)",
                    (
                        key,
                        platform,
                        str(chat),
                        str(actor),
                        kind,
                        time.time(),
                        json.dumps(details),
                    ),
                ).rowcount
                == 1
            )

    def settle(self, key, state, **details):
        if state not in {"sent", "failed", "uncertain"}:
            raise Denied("Invalid operation state")
        with self.db:
            row = self.db.execute(
                "SELECT * FROM operations WHERE id=?", (key,)
            ).fetchone()
            if row and row["state"] == "sending":
                details = {**json.loads(row["details"]), **details}
                self.db.execute(
                    "UPDATE operations SET state=?,details=? WHERE id=?",
                    (state, json.dumps(details), key),
                )
                self.audit(
                    row["actor"], row["platform"], row["chat"], row["kind"], state=state
                )

    def admit_reply(self, platform, chat, now=None):
        now = time.time() if now is None else now
        with self.db:
            return bool(
                self.db.execute(
                    "UPDATE bindings SET last_reply=? WHERE chat=? "
                    "AND EXISTS(SELECT 1 FROM bindings WHERE platform=? AND chat=?) "
                    "AND NOT EXISTS(SELECT 1 FROM bindings WHERE chat=? AND last_reply>?)",
                    (now, str(chat), platform, str(chat), str(chat), now - 10),
                ).rowcount
            )

    def close(self):
        self.db.close()
