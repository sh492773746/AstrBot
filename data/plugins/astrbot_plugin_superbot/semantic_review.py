"""Opt-in, local-only group message review with bounded retention."""

import hashlib
import json
import re
import secrets
from datetime import datetime, timezone

from .ad_killer import normalize, validate
from .store import Rejected, encode

DAY = 86400
MIB = 1024 * 1024
LABELS = {"normal", "advert", "other", "uncertain"}
NOTICE_VERSION = 1
MASK = re.compile(
    r"(?<![a-z0-9_])(?:https?://[^\s<>()]+"
    r"|www\.[^\s<>()]+"
    r"|[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}"
    r"|(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s<>()]*)?"
    r"|@[a-z][a-z0-9_]{3,31})(?![a-z0-9_])"
    r"|(?<!\d)\+\d[\d ()-]{7,18}\d(?!\d)"
    r"|(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"
    r"|(?:微信|微信号|wx|wechat|qq|whatsapp|telegram|电报)"
    r"\s*[:：号]?\s*[a-z0-9_-]{5,}",
    re.I,
)


class SemanticReview:
    """Keep review samples separate from enforcement and global fingerprints."""

    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS sr_consent(
                chat TEXT PRIMARY KEY,enabled INTEGER NOT NULL DEFAULT 0,
                actor TEXT NOT NULL,notice INTEGER NOT NULL DEFAULT 1,
                started REAL NOT NULL DEFAULT 0,version INTEGER NOT NULL DEFAULT 1,
                gap TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS sr_usage(
                day INTEGER NOT NULL,chat TEXT NOT NULL,bytes INTEGER NOT NULL,
                PRIMARY KEY(day,chat));
            CREATE TABLE IF NOT EXISTS sr_messages(
                chat TEXT NOT NULL,message INTEGER NOT NULL,revision INTEGER NOT NULL,
                digest TEXT NOT NULL,body TEXT NOT NULL,actor TEXT NOT NULL,
                sender_kind TEXT NOT NULL,reply INTEGER,at REAL NOT NULL,
                saved INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(chat,message,revision));
            CREATE INDEX IF NOT EXISTS sr_messages_age ON sr_messages(at);
            CREATE INDEX IF NOT EXISTS sr_messages_chat ON sr_messages(chat,at);
            CREATE TABLE IF NOT EXISTS sr_labels(
                chat TEXT NOT NULL,message INTEGER NOT NULL,revision INTEGER NOT NULL,
                label TEXT NOT NULL,reviewer TEXT NOT NULL,reason TEXT NOT NULL,
                at REAL NOT NULL,PRIMARY KEY(chat,message,revision));
            CREATE TABLE IF NOT EXISTS sr_drafts(
                id TEXT PRIMARY KEY,chat TEXT NOT NULL,actor TEXT NOT NULL,
                base_version INTEGER NOT NULL,config TEXT NOT NULL,
                state TEXT NOT NULL,created REAL NOT NULL,adopted_version INTEGER,
                before_config TEXT,before_enabled INTEGER);
            CREATE INDEX IF NOT EXISTS sr_drafts_age ON sr_drafts(created);
            CREATE TABLE IF NOT EXISTS sr_trials(
                id TEXT PRIMARY KEY,draft TEXT NOT NULL,result TEXT NOT NULL,
                at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS sr_trials_age ON sr_trials(at);
            CREATE INDEX IF NOT EXISTS sr_trials_draft ON sr_trials(draft);
            CREATE TABLE IF NOT EXISTS sr_packages(
                id TEXT PRIMARY KEY,chat TEXT NOT NULL,path TEXT NOT NULL,
                created REAL NOT NULL,expires REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS sr_packages_expiry ON sr_packages(expires);
        """)
        columns = {
            r[1] for r in self.store.db.execute("PRAGMA table_info(sr_messages)")
        }
        for name, sql_type in (("sent_at", "REAL"), ("edited_at", "REAL")):
            if name not in columns:
                self.store.db.execute(
                    f"ALTER TABLE sr_messages ADD COLUMN {name} {sql_type}"
                )

    def consent(self, chat):
        return self.store.db.execute(
            "SELECT * FROM sr_consent WHERE chat=?", (str(chat),)
        ).fetchone()

    def owner_only(self, uid):
        if not self.store.is_superadmin(uid):
            raise Rejected("仅首位超管可跨群整理样本")

    async def authorize(self, uid, chat):
        """Verify current Telegram rights even for owner and old callbacks."""
        from .group_authority import CURRENT, GroupAuthority

        chat = str(chat)
        if not self.store.db.execute(
            "SELECT 1 FROM mod_groups WHERE chat=?", (chat,)
        ).fetchone():
            raise Rejected("请先选择已接入的群")
        async with GroupAuthority(self.runtime).session(
            uid, chat, allow_paused=True
        ) as proof:
            info = await self.runtime.bot.get_chat(chat)
        CURRENT.set(proof)
        return info

    def pause(self, chat, reason):
        with self.store.tx() as db:
            db.execute(
                "UPDATE sr_consent SET enabled=0,version=version+1,gap=? "
                "WHERE chat=? AND enabled=1",
                (reason, str(chat)),
            )

    def retire(self, chat):
        """Remove file paths and orphaned trials before legacy group purge."""
        with self.store.tx() as db:
            paths = [
                row["path"]
                for row in db.execute(
                    "SELECT path FROM sr_packages WHERE chat=?", (str(chat),)
                )
            ]
            db.execute(
                "DELETE FROM sr_trials WHERE draft IN "
                "(SELECT id FROM sr_drafts WHERE chat=?)",
                (str(chat),),
            )
            db.execute("DELETE FROM sr_packages WHERE chat=?", (str(chat),))
        for path in paths:
            self._unlink(path)

    def gap(self, chat, reason):
        self.store.db.execute(
            "UPDATE sr_consent SET gap=? WHERE chat=? AND enabled=1",
            (reason, str(chat)),
        )

    def start(self, chat, uid, version):
        """Enable only after the notice send returned successfully."""
        with self.store.tx() as db:
            if not db.execute(
                "SELECT 1 FROM mod_groups WHERE chat=? AND enabled=1", (str(chat),)
            ).fetchone():
                raise Rejected("群管已停用，不能开启采集")
            row = db.execute(
                "SELECT version FROM sr_consent WHERE chat=?", (str(chat),)
            ).fetchone()
            if (row["version"] if row else 0) != version:
                raise Rejected("授权状态已变化，请重新打开")
            db.execute(
                "INSERT INTO sr_consent(chat,enabled,actor,notice,started,version,gap) "
                "VALUES(?,1,?,?,?,1,'') ON CONFLICT(chat) DO UPDATE SET "
                "enabled=1,actor=excluded.actor,notice=excluded.notice,"
                "started=excluded.started,version=sr_consent.version+1,gap=''",
                (str(chat), str(uid), NOTICE_VERSION, self.store.clock()),
            )
            self.store.audit(db, uid, "semantic_start", {"chat": str(chat)})

    def clear(self, chat, uid, version):
        """Purge review bodies and derived data; retain metadata-only audit."""
        chat = str(chat)
        with self.store.tx() as db:
            row = db.execute(
                "SELECT version FROM sr_consent WHERE chat=?", (chat,)
            ).fetchone()
            if not row or row["version"] != version:
                raise Rejected("授权状态已变化，请重新打开")
            db.execute(
                "UPDATE sr_consent SET enabled=0,version=version+1,gap='cleared' "
                "WHERE chat=?",
                (chat,),
            )
            for table in ("sr_labels", "sr_messages", "sr_trials"):
                if table == "sr_trials":
                    db.execute(
                        "DELETE FROM sr_trials WHERE draft IN "
                        "(SELECT id FROM sr_drafts WHERE chat=?)",
                        (chat,),
                    )
                else:
                    db.execute(f"DELETE FROM {table} WHERE chat=?", (chat,))
            db.execute(
                "UPDATE sr_drafts SET state='revoked',config='{}',before_config=NULL,"
                "before_enabled=NULL WHERE chat=?",
                (chat,),
            )
            files = [
                r["path"]
                for r in db.execute(
                    "SELECT path FROM sr_packages WHERE chat=?", (chat,)
                )
            ]
            db.execute("DELETE FROM sr_packages WHERE chat=?", (chat,))
            self.store.audit(db, uid, "semantic_clear", {"chat": chat})
        for path in files:
            self._unlink(path)

    @staticmethod
    def _unlink(path):
        from pathlib import Path

        target = Path(path)
        if target.is_file() and target.name.startswith("sr-"):
            target.unlink()

    def capture(self, update):
        """Store only enabled supergroup text/captions before business routing."""
        chat = getattr(update, "effective_chat", None)
        message = getattr(update, "message", None) or getattr(
            update, "edited_message", None
        )
        if not chat or chat.type != "supergroup" or not message:
            return False
        try:
            consent = self.consent(chat.id)
            if not consent or not consent["enabled"]:
                return False
            if not self.store.db.execute(
                "SELECT 1 FROM mod_groups WHERE chat=? AND enabled=1",
                (str(chat.id),),
            ).fetchone():
                self.pause(chat.id, "group_disabled")
                return False
            body = (
                getattr(message, "text", None)
                or getattr(message, "caption", None)
                or ""
            )[:4096]
            if not body.strip():
                return False
            sender_chat = getattr(message, "sender_chat", None)
            user = getattr(message, "from_user", None)
            if user and user.is_bot and not sender_chat:
                return False
            actor = str(sender_chat.id) if sender_chat else str(user.id) if user else ""
            if not actor:
                return False
            now = self.store.clock()
            edited = getattr(update, "edited_message", None) is not None
            digest = hashlib.sha256(body.encode()).hexdigest()
            date = int(now // DAY)
            size = len(body.encode("utf-8"))
            per_group = self.store.get("semantic_group_bytes", 20 * MIB)
            global_limit = self.store.get("semantic_instance_bytes", 200 * MIB)
            with self.store.tx() as db:
                latest = db.execute(
                    "SELECT enabled,version FROM sr_consent WHERE chat=?",
                    (str(chat.id),),
                ).fetchone()
                if (
                    not latest
                    or not latest["enabled"]
                    or latest["version"] != consent["version"]
                ):
                    return False
                current = db.execute(
                    "SELECT revision,digest FROM sr_messages WHERE chat=? AND message=? "
                    "ORDER BY revision DESC LIMIT 1",
                    (str(chat.id), message.message_id),
                ).fetchone()
                if current and (not edited or current["digest"] == digest):
                    return False
                revision = current["revision"] + 1 if current else (1 if edited else 0)
                used = db.execute(
                    "SELECT bytes FROM sr_usage WHERE day=? AND chat=?",
                    (date, str(chat.id)),
                ).fetchone()
                total = db.execute(
                    "SELECT bytes FROM sr_usage WHERE day=? AND chat='*'",
                    (date,),
                ).fetchone()
                if (used[0] if used else 0) + size > per_group or (
                    total[0] if total else 0
                ) + size > global_limit:
                    db.execute(
                        "UPDATE sr_consent SET gap='capacity' WHERE chat=?",
                        (str(chat.id),),
                    )
                    return False
                reply = getattr(message, "reply_to_message", None)
                sent_at = getattr(message, "date", None)
                edited_at = getattr(message, "edit_date", None)
                db.execute(
                    "INSERT INTO sr_messages(chat,message,revision,digest,body,actor,"
                    "sender_kind,reply,at,sent_at,edited_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(chat,message,revision) DO NOTHING",
                    (
                        str(chat.id),
                        message.message_id,
                        revision,
                        digest,
                        body,
                        actor,
                        (
                            "anonymous"
                            if str(sender_chat.id) == str(chat.id)
                            else "channel"
                        )
                        if sender_chat
                        else "user",
                        getattr(reply, "message_id", None),
                        now,
                        sent_at.timestamp() if isinstance(sent_at, datetime) else now,
                        edited_at.timestamp()
                        if isinstance(edited_at, datetime)
                        else None,
                    ),
                )
                for group in (str(chat.id), "*"):
                    db.execute(
                        "INSERT INTO sr_usage(day,chat,bytes) VALUES(?,?,?) "
                        "ON CONFLICT(day,chat) DO UPDATE SET bytes=bytes+excluded.bytes",
                        (date, group, size),
                    )
            return True
        except Exception:
            try:
                self.gap(chat.id, "storage")
            except Exception:
                pass
            return False

    def label(self, chat, message, revision, label, reason, uid):
        if label not in LABELS or len(reason) > 500:
            raise Rejected("标注类型或理由无效")
        with self.store.tx() as db:
            row = db.execute(
                "SELECT 1 FROM sr_messages WHERE chat=? AND message=? AND revision=? "
                "AND at>? - CASE WHEN saved=1 THEN ? ELSE ? END",
                (str(chat), message, revision, self.store.clock(), 30 * DAY, 7 * DAY),
            ).fetchone()
            if not row:
                raise Rejected("样本已过期或被清理")
            db.execute(
                "INSERT INTO sr_labels VALUES(?,?,?,?,?,?,?) ON CONFLICT(chat,message,revision) "
                "DO UPDATE SET label=excluded.label,reviewer=excluded.reviewer,"
                "reason=excluded.reason,at=excluded.at",
                (
                    str(chat),
                    message,
                    revision,
                    label,
                    str(uid),
                    reason,
                    self.store.clock(),
                ),
            )
            db.execute(
                "UPDATE sr_messages SET saved=1 WHERE chat=? AND message=? AND revision=?",
                (str(chat), message, revision),
            )
            self.store.audit(
                db,
                uid,
                "semantic_label",
                {
                    "chat": str(chat),
                    "message": message,
                    "revision": revision,
                    "label": label,
                },
            )

    def cleanup(self):
        now = self.store.clock()
        with self.store.tx() as db:
            expired = db.execute(
                "SELECT chat,message,revision FROM sr_messages "
                "WHERE (saved=0 AND at<=?) OR (saved=1 AND at<=?) LIMIT 200",
                (now - 7 * DAY, now - 30 * DAY),
            ).fetchall()
            keys = [tuple(r) for r in expired]
            db.executemany(
                "DELETE FROM sr_labels WHERE chat=? AND message=? AND revision=?", keys
            )
            db.executemany(
                "DELETE FROM sr_messages WHERE chat=? AND message=? AND revision=?",
                keys,
            )
            db.execute("DELETE FROM sr_usage WHERE day<?", (int(now // DAY) - 31,))
            trials = [
                r["id"]
                for r in db.execute(
                    "SELECT id FROM sr_trials WHERE at<=? OR draft IN "
                    "(SELECT id FROM sr_drafts WHERE created<=?) LIMIT 200",
                    (now - 30 * DAY, now - 30 * DAY),
                )
            ]
            db.executemany(
                "DELETE FROM sr_trials WHERE id=?", [(key,) for key in trials]
            )
            drafts = [
                r["id"]
                for r in db.execute(
                    "SELECT id FROM sr_drafts WHERE created<=? AND "
                    "(state<>'revoked' OR config<>'{}' OR before_config IS NOT NULL "
                    "OR before_enabled IS NOT NULL) LIMIT 200",
                    (now - 30 * DAY,),
                )
            ]
            db.executemany(
                "UPDATE sr_drafts SET state='revoked',config='{}',before_config=NULL,"
                "before_enabled=NULL WHERE id=?",
                [(key,) for key in drafts],
            )
            packages = db.execute(
                "SELECT id,path FROM sr_packages WHERE expires<=? LIMIT 200", (now,)
            ).fetchall()
            files = [r["path"] for r in packages]
            db.executemany(
                "DELETE FROM sr_packages WHERE id=?", [(r["id"],) for r in packages]
            )
        for path in files:
            self._unlink(path)
        return all(len(items) < 200 for items in (expired, trials, drafts, packages))

    def rows(self, chat, day=None, label=None, limit=20):
        if label and label not in LABELS:
            raise Rejected("标注类型无效")
        args = [str(chat), self.store.clock(), 30 * DAY, 7 * DAY]
        query = (
            "SELECT m.*,l.label FROM sr_messages m LEFT JOIN sr_labels l "
            "ON m.chat=l.chat AND m.message=l.message AND m.revision=l.revision "
            "WHERE m.chat=? AND m.at>? - CASE WHEN m.saved=1 THEN ? ELSE ? END"
        )
        if day:
            stamp = (
                datetime.strptime(day, "%Y-%m-%d")
                .replace(tzinfo=timezone.utc)
                .timestamp()
            )
            query += " AND m.at>=? AND m.at<?"
            args.extend([stamp, stamp + DAY])
        if label:
            query += " AND l.label=?"
            args.append(label)
        return self.store.db.execute(
            query + " ORDER BY m.at DESC,m.message DESC,m.revision DESC LIMIT ?",
            (*args, min(100, limit)),
        ).fetchall()

    def package(self, chat, uid, *, day=None, label=None):
        self.owner_only(uid)
        self.cleanup()
        rows = self.rows(chat, day, label, 100)
        root = self.store.path.parent / "semantic_packages"
        root.mkdir(mode=0o700, exist_ok=True)
        key = secrets.token_hex(12)
        salt = secrets.token_bytes(16)
        path = root / f"sr-{key}.json"
        records = [
            {
                "message": r["message"],
                "revision": r["revision"],
                "body": MASK.sub("[已脱敏]", r["body"]),
                "sender": hashlib.sha256(
                    salt + (str(chat) + ":" + r["actor"]).encode()
                ).hexdigest()[:12],
                "reply": r["reply"],
                "label": r["label"],
            }
            for r in rows
        ]
        try:
            with path.open("x", encoding="utf-8") as stream:
                path.chmod(0o600)
                json.dump(records, stream, ensure_ascii=False)
            with self.store.tx() as db:
                db.execute(
                    "INSERT INTO sr_packages VALUES(?,?,?,?,?)",
                    (
                        key,
                        str(chat),
                        str(path),
                        self.store.clock(),
                        self.store.clock() + DAY,
                    ),
                )
                self.store.audit(
                    db,
                    uid,
                    "semantic_export",
                    {"chat": str(chat), "count": len(records), "package": key},
                )
        except Exception:
            self._unlink(str(path))
            raise
        return key, len(records)

    def draft(self, chat, uid, value, field="keywords"):
        """Create a validated, per-group candidate without activating detection."""
        self.owner_only(uid)
        lists = {"keywords", "users", "domains", "telegram_allow"}
        numbers = {"repeat_count", "repeat_seconds", "flood_count", "flood_seconds"}
        if field not in lists | numbers:
            raise Rejected("不支持该规则字段")
        config = json.loads(self.runtime.ad_killer.policy(chat)["config"])
        if field in lists:
            config[field] = [part.strip() for part in value.split(",") if part.strip()]
            if not config[field]:
                raise Rejected("请填写至少一项")
        elif value.isascii() and value.isdigit():
            config[field] = int(value)
        else:
            raise Rejected("请填写整数阈值")
        validate(config)
        version = self.runtime.ad_killer.policy(chat)["version"]
        key = secrets.token_hex(12)
        with self.store.tx() as db:
            db.execute(
                "INSERT INTO sr_drafts(id,chat,actor,base_version,config,state,created) "
                "VALUES(?,?,?,?,?,'draft',?)",
                (key, str(chat), str(uid), version, encode(config), self.store.clock()),
            )
            self.store.audit(db, uid, "semantic_draft", {"chat": str(chat), "id": key})
        return key

    def trial(self, key, uid):
        """Simulate deterministic text and volume predicates without business writes."""
        self.owner_only(uid)
        draft = self.store.db.execute(
            "SELECT * FROM sr_drafts WHERE id=? AND state IN ('draft','submitted')",
            (key,),
        ).fetchone()
        if not draft:
            raise Rejected("草稿已过期或撤销")
        config = json.loads(draft["config"])
        from .ad_killer import matches_keywords, matches_repeat

        result = {
            "hit": 0,
            "false_positive": 0,
            "missed": 0,
            "uncertain": 0,
            "evidence": [],
        }
        history = {}
        for row in reversed(self.rows(draft["chat"], limit=100)):
            actor = row["actor"]
            earlier = history.setdefault(actor, [])
            text = normalize(row["body"])
            digest_text = re.sub(r"\W+", "", text)[:256]
            repeat = matches_repeat(
                digest_text,
                (
                    (entry[2], entry[3])
                    for entry in earlier[-200:]
                    if row["at"] - entry[0] <= config["repeat_seconds"]
                ),
                config["repeat_count"],
            )
            flood = (
                sum(
                    row["at"] - entry[0] <= config["flood_seconds"] for entry in earlier
                )
                + 1
                >= config["flood_count"]
            )
            hit = actor not in config["users"] and (
                matches_keywords(text, config["keywords"]) or repeat or flood
            )
            result["hit"] += int(hit)
            result["false_positive"] += int(hit and row["label"] == "normal")
            result["missed"] += int(not hit and row["label"] == "advert")
            result["uncertain"] += int(
                row["label"] in (None, "uncertain")
                or row["sender_kind"] != "user"
                or row["revision"] != 0
            )
            if hit and len(result["evidence"]) < 20:
                result["evidence"].append(
                    {
                        "message": row["message"],
                        "rules": [
                            rule
                            for rule, present in (
                                (
                                    "keywords",
                                    matches_keywords(text, config["keywords"]),
                                ),
                                ("repeat", repeat),
                                ("flood", flood),
                            )
                            if present
                        ],
                    }
                )
            if row["revision"] == 0:
                earlier.append(
                    (
                        row["at"],
                        row["message"],
                        hashlib.sha256(digest_text.encode()).hexdigest(),
                        digest_text,
                    )
                )
        with self.store.tx() as db:
            db.execute(
                "INSERT INTO sr_trials VALUES(?,?,?,?)",
                (secrets.token_hex(12), key, encode(result), self.store.clock()),
            )
            self.store.audit(
                db, uid, "semantic_trial", {"chat": draft["chat"], "draft": key}
            )
        return result

    def submit(self, key, uid):
        self.owner_only(uid)
        with self.store.tx() as db:
            row = db.execute(
                "SELECT chat FROM sr_drafts WHERE id=? AND state='draft'", (key,)
            ).fetchone()
            if (
                not row
                or not db.execute(
                    "SELECT 1 FROM sr_trials WHERE draft=?", (key,)
                ).fetchone()
            ):
                raise Rejected("请先完成试跑")
            db.execute("UPDATE sr_drafts SET state='submitted' WHERE id=?", (key,))
            self.store.audit(
                db, uid, "semantic_submit", {"chat": row["chat"], "draft": key}
            )

    async def adopt(self, key, uid):
        draft = self.store.db.execute(
            "SELECT * FROM sr_drafts WHERE id=? AND state='submitted'", (key,)
        ).fetchone()
        if not draft:
            raise Rejected("草稿不可采用")
        await self.authorize(uid, draft["chat"])
        # Authorization awaits the network; a concurrent purge may revoke this draft.
        draft = self.store.db.execute(
            "SELECT * FROM sr_drafts WHERE id=? AND state='submitted'", (key,)
        ).fetchone()
        if not draft or draft["created"] <= self.store.clock() - 30 * DAY:
            raise Rejected("草稿已到期或撤销")
        policy = self.runtime.ad_killer.policy(draft["chat"])
        if policy["version"] != draft["base_version"]:
            raise Rejected("线上规则版本已变化，请重新试跑")
        proposed = json.loads(draft["config"])
        current = json.loads(policy["config"])
        current["keywords"] = proposed["keywords"]
        for field in (
            "users",
            "domains",
            "telegram_allow",
            "repeat_count",
            "repeat_seconds",
            "flood_count",
            "flood_seconds",
        ):
            current[field] = proposed[field]
        validate(current)
        # An existing enabled rule keeps its state; a newly created rule stays off.
        with self.store.tx() as db:
            if policy["version"] == 0:
                db.execute(
                    "INSERT INTO ak_policies(chat,enabled,actor,version,config) "
                    "VALUES(?,0,?,1,?)",
                    (draft["chat"], str(uid), encode(current)),
                )
            else:
                updated = db.execute(
                    "UPDATE ak_policies SET config=?,version=version+1,actor=? "
                    "WHERE chat=? AND version=?",
                    (encode(current), str(uid), draft["chat"], policy["version"]),
                )
                if updated.rowcount != 1:
                    raise Rejected("线上规则版本已变化")
            db.execute(
                "UPDATE sr_drafts SET state='adopted',adopted_version=?,"
                "before_config=?,before_enabled=? WHERE id=? AND state='submitted'",
                (policy["version"] + 1, policy["config"], policy["enabled"], key),
            )
            self.store.audit(
                db, uid, "semantic_adopt", {"chat": draft["chat"], "draft": key}
            )

    async def undo(self, key, uid):
        row = self.store.db.execute(
            "SELECT * FROM sr_drafts WHERE id=? AND state='adopted'", (key,)
        ).fetchone()
        if not row:
            raise Rejected("没有可撤回的变更")
        await self.authorize(uid, row["chat"])
        row = self.store.db.execute(
            "SELECT * FROM sr_drafts WHERE id=? AND state='adopted'", (key,)
        ).fetchone()
        if not row:
            raise Rejected("变更状态已变化")
        policy = self.runtime.ad_killer.policy(row["chat"])
        if policy["version"] != row["adopted_version"]:
            raise Rejected("已有后续修改，请重新核对，不自动覆盖")
        with self.store.tx() as db:
            if row["base_version"] == 0:
                db.execute(
                    "DELETE FROM ak_policies WHERE chat=? AND version=?",
                    (row["chat"], policy["version"]),
                )
            else:
                updated = db.execute(
                    "UPDATE ak_policies SET config=?,enabled=?,version=version+1,actor=? "
                    "WHERE chat=? AND version=?",
                    (
                        row["before_config"],
                        row["before_enabled"],
                        str(uid),
                        row["chat"],
                        policy["version"],
                    ),
                )
                if updated.rowcount != 1:
                    raise Rejected("规则版本已变化")
            db.execute("UPDATE sr_drafts SET state='undone' WHERE id=?", (key,))
            self.store.audit(
                db, uid, "semantic_undo", {"chat": row["chat"], "draft": key}
            )
