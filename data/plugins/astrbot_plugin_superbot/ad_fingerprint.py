"""Local, approved advertisement samples; no model or external classifier."""

import asyncio
import hashlib
import json
import re
from collections import OrderedDict, deque
from difflib import SequenceMatcher
from urllib.parse import unquote, urlsplit

from telegram import ChatPermissions

from .ad_killer import normalize
from .ad_link_rules import LINKS
from .store import Rejected

VERSION = "fingerprint-1"
PROMOTION = re.compile(r"招代理|返佣|赚钱|收益|优惠|五折|下单|承接|课程|稳定项目")
DIVERSION = re.compile(r"私[信聊]|主页|个人[简介绍]|联系|加[我群]|微信|扫码|进群")
AMBIGUOUS = re.compile(
    r"举报|反诈|骗子|别信|不要相信|广告吗|广告吧|有人发|有人说|[“”「」『』]"
)
CONTACT = re.compile(
    r"https?://\S+|@\w+|[\w.+-]+@[\w.-]+\.\w+|(?<!\d)\+?\d[\d -]{6,}\d"
)


def canonical(text):
    """Keep meaningful punctuation and numbers while normalizing spacing."""
    return re.sub(r"\s+", " ", normalize(text)).strip()


def masked(text):
    """Store text with common contact identifiers replaced."""
    return CONTACT.sub("[联系信息]", LINKS.sub("[链接]", text))


def grams(text):
    """Return a bounded set of ordered text fragments for indexed lookup."""
    return sorted({text[i : i + 3] for i in range(max(0, len(text) - 2))})[:256]


class Fingerprints:
    """Manage samples, per-group policy and a single deterministic executor."""

    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        self.history = OrderedDict()
        self.generations = OrderedDict()
        self.stopping = False
        self.rollout_cursor = ""
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS fp_samples(
                id INTEGER PRIMARY KEY,source_chat TEXT NOT NULL,source_message INTEGER NOT NULL,
                body TEXT NOT NULL,digest TEXT NOT NULL,template TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',version INTEGER NOT NULL DEFAULT 1,
                submitter TEXT NOT NULL,approver TEXT,created REAL NOT NULL,expires REAL NOT NULL,
                UNIQUE(source_chat,source_message,digest));
            CREATE INDEX IF NOT EXISTS fp_sample_digest ON fp_samples(digest,status);
            CREATE INDEX IF NOT EXISTS fp_sample_expiry ON fp_samples(status,expires);
            CREATE TABLE IF NOT EXISTS fp_grams(
                gram TEXT NOT NULL,sample INTEGER NOT NULL,PRIMARY KEY(gram,sample));
            CREATE TABLE IF NOT EXISTS fp_groups(
                chat TEXT PRIMARY KEY,mode TEXT NOT NULL DEFAULT 'off',version INTEGER NOT NULL DEFAULT 1,
                actor TEXT NOT NULL,since REAL NOT NULL,rollout INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS fp_rollout(
                chat TEXT PRIMARY KEY,group_version INTEGER NOT NULL,
                policy_version INTEGER NOT NULL,status TEXT NOT NULL,error TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS fp_exceptions(
                chat TEXT NOT NULL,sample INTEGER NOT NULL,actor TEXT NOT NULL,at REAL NOT NULL,
                PRIMARY KEY(chat,sample));
            CREATE TABLE IF NOT EXISTS fp_events(
                chat TEXT NOT NULL,message INTEGER NOT NULL,uid TEXT NOT NULL,digest TEXT NOT NULL,
                sample INTEGER,reason TEXT NOT NULL,score REAL NOT NULL,body TEXT,
                status TEXT NOT NULL,at REAL NOT NULL,reviewed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(chat,message));
            CREATE INDEX IF NOT EXISTS fp_user_time ON fp_events(chat,uid,at);
            CREATE INDEX IF NOT EXISTS fp_event_time ON fp_events(chat,at);
            CREATE TABLE IF NOT EXISTS fp_seen(
                chat TEXT NOT NULL,message INTEGER NOT NULL,at REAL NOT NULL,
                PRIMARY KEY(chat,message));
            INSERT OR IGNORE INTO fp_seen SELECT chat,message,at FROM fp_events;
            CREATE TABLE IF NOT EXISTS fp_overflow(
                chat TEXT NOT NULL,day INTEGER NOT NULL,count INTEGER NOT NULL,
                PRIMARY KEY(chat,day));
            CREATE TABLE IF NOT EXISTS fp_restrictions(
                chat TEXT NOT NULL,message INTEGER NOT NULL,uid TEXT NOT NULL,
                expected TEXT NOT NULL,status TEXT NOT NULL,at REAL NOT NULL,
                PRIMARY KEY(chat,message));
            UPDATE fp_restrictions SET status='unknown' WHERE status='restoring';
        """)

    def invalidate(self, update):
        """Invalidate prior decisions before any network permission checks."""
        msg = getattr(update, "edited_message", None) or getattr(
            update, "message", None
        )
        chat = getattr(update, "effective_chat", None)
        if msg and chat and (str(chat.id), msg.message_id) in self.generations:
            key = (str(chat.id), msg.message_id)
            self.generations[key] += 1

    async def submit(self, actor, chat, message, body):
        """Submit trusted message content; approving remains a separate operation.

        Args:
            actor: Submitting administrator.
            chat: Source group.
            message: Source message identifier.
            body: Original text obtained from Telegram or an existing hit.
        """
        await self.runtime.moderation.check(str(actor), str(chat), "delete")
        if not self.store.db.execute(
            "SELECT 1 FROM mod_groups WHERE chat=? AND enabled=1", (str(chat),)
        ).fetchone():
            raise Rejected("群未登记或未启用")
        if not isinstance(body, str) or not body.strip() or len(body) > 4096:
            raise Rejected("样本文本为空或超过4096字符")
        clean = canonical(body)
        digest = hashlib.sha256(clean.encode()).hexdigest()
        with self.store.tx() as db:
            if (
                db.execute(
                    "SELECT count(*) FROM fp_samples WHERE status IN ('active','pending','paused')"
                ).fetchone()[0]
                >= 5000
            ):
                raise Rejected("样本容量已满，请先复核或删除")
            db.execute(
                """INSERT OR IGNORE INTO fp_samples
                (source_chat,source_message,body,digest,template,submitter,created,expires)
                VALUES(?,?,?,?,?,?,?,?)""",
                (
                    str(chat),
                    message,
                    masked(body),
                    digest,
                    masked(clean),
                    str(actor),
                    self.store.clock(),
                    self.store.clock() + 90 * 86400,
                ),
            )
            row = db.execute(
                "SELECT id FROM fp_samples WHERE source_chat=? AND source_message=? AND digest=?",
                (str(chat), message, digest),
            ).fetchone()
            self.store.audit(
                db, str(actor), "fp_submit", {"sample": row[0], "chat": str(chat)}
            )
        return row[0]

    def decide(self, actor, identity, version, target):
        """Approve or retire a versioned global sample under manager authority."""
        if target not in {"active", "paused", "revoked", "deleted"}:
            raise Rejected("无效样本操作")
        with self.store.tx() as db:
            self.store.require(str(actor), "manager", db=db)
            row = db.execute(
                "SELECT * FROM fp_samples WHERE id=?", (identity,)
            ).fetchone()
            if (
                not row
                or row["version"] != version
                or row["status"] in {"revoked", "deleted", "expired"}
                or row["expires"] <= self.store.clock()
            ):
                raise Rejected("样本已变化、撤销或到期")
            db.execute(
                "UPDATE fp_samples SET status=?,version=version+1,approver=? WHERE id=?",
                (target, str(actor), identity),
            )
            db.execute("DELETE FROM fp_grams WHERE sample=?", (identity,))
            if target == "active":
                db.executemany(
                    "INSERT INTO fp_grams VALUES(?,?)",
                    [(g, identity) for g in grams(row["template"])],
                )
            if target == "deleted":
                db.execute(
                    "UPDATE fp_samples SET body='',template='' WHERE id=?", (identity,)
                )
            self.store.audit(
                db, str(actor), "fp_decide", {"sample": identity, "target": target}
            )

    def match(self, chat, text, reply=False):
        """Bound candidate lookup and return evidence, never a punishment.

        Args:
            chat: Group whose exceptions apply.
            text: Current message text.
            reply: Whether the message quotes another message.
        """
        clean = canonical(text)
        digest = hashlib.sha256(clean.encode()).hexdigest()
        common = """status='active' AND expires>? AND NOT EXISTS
        (SELECT 1 FROM fp_exceptions e WHERE e.chat=? AND e.sample=fp_samples.id)"""
        exact = self.store.db.execute(
            f"SELECT * FROM fp_samples WHERE digest=? AND {common} ORDER BY id LIMIT 1",
            (digest, self.store.clock(), str(chat)),
        ).fetchone()
        template = masked(clean)
        features = grams(template)
        candidates = []
        if not exact and features:
            candidates = self.store.db.execute(
                f"""SELECT * FROM fp_samples WHERE id IN
                (SELECT DISTINCT sample FROM fp_grams WHERE gram IN
                ({",".join("?" for _ in features)}) LIMIT 30) AND {common} ORDER BY id""",
                (*features, self.store.clock(), str(chat)),
            ).fetchall()
        if len(candidates) >= 30:
            return {
                "sample": None,
                "reason": "candidate_limit",
                "score": 0,
                "automatic": False,
            }
        score, sample = (1.0, exact) if exact else (0.0, None)
        for row in candidates:
            value = SequenceMatcher(
                None, template, row["template"], autojunk=False
            ).ratio()
            if value > score:
                score, sample = value, row
        if not sample or score < 0.80:
            return None
        ambiguous = reply or bool(AMBIGUOUS.search(clean))
        enough = sum(ch.isalnum() for ch in clean) >= 20
        automatic = (
            enough
            and not ambiguous
            and bool(
                exact
                or (
                    score >= 0.92
                    and PROMOTION.search(clean)
                    and DIVERSION.search(clean)
                )
            )
        )
        return {
            "sample": sample["id"],
            "sample_version": sample["version"],
            "reason": "exact" if exact else "similar",
            "score": score,
            "automatic": automatic,
        }

    def ready(self, chat):
        """Check verified release and a group's distinct human-reviewed observations."""
        release = self.store.get("fp_release", {})
        if (
            release.get("version") != VERSION
            or not release.get("offline_pass")
            or not release.get("telethon_pass")
        ):
            return False
        row = self.store.db.execute(
            "SELECT * FROM fp_groups WHERE chat=?", (str(chat),)
        ).fetchone()
        if not row or self.store.clock() - row["since"] < 86400:
            return False
        counts = self.store.db.execute(
            """SELECT count(CASE WHEN reviewed=1 THEN 1 END),
            count(CASE WHEN reviewed=-1 THEN 1 END) FROM fp_events WHERE chat=? AND at>=?""",
            (str(chat), row["since"]),
        ).fetchone()
        return counts[0] >= 30 and counts[1] == 0

    def test_allowed(self, chat, uid, message, sample):
        """Recognize a short-lived, exact-message acceptance permit, never a group bypass."""
        release = self.store.get("fp_release", {})
        permit = self.store.get("fp_test_permit", {})
        now = self.store.clock()
        return bool(
            (
                (release.get("version") == VERSION and release.get("offline_pass"))
                or (
                    permit.get("version") == VERSION
                    and permit.get("authorization") == "owner_scoped_test_only"
                )
            )
            and str(chat) == "-1001000000003"
            and permit.get("chat") == str(chat)
            and permit.get("uid") == str(uid)
            and permit.get("started", now + 1) <= now < permit.get("expires", 0)
            and 0 < permit.get("expires", 0) - permit.get("started", 0) <= 600
            and 0 < len(permit.get("messages", [])) <= 40
            and message in permit.get("messages", [])
            and sample in permit.get("samples", [])
            and self.store.allowed(permit.get("actor", ""), "manager")
        )

    async def inspect(self, update):
        """Inspect after old rules finish, preserving one operation per message."""
        msg = getattr(update, "edited_message", None) or getattr(
            update, "message", None
        )
        chat = getattr(update, "effective_chat", None)
        user = getattr(msg, "from_user", None)
        if (
            self.stopping
            or not msg
            or not chat
            or chat.type != "supergroup"
            or not user
            or user.is_bot
            or getattr(msg, "sender_chat", None)
        ):
            return False
        text = getattr(msg, "text", None) or getattr(msg, "caption", None) or ""
        if not text.strip() or text.startswith("/") or len(text) > 4096:
            return False
        group_id, uid = str(chat.id), str(user.id)
        modules = self.store.get("modules", {})
        group = self.store.db.execute(
            "SELECT * FROM fp_groups WHERE chat=?", (group_id,)
        ).fetchone()
        policy = self.runtime.ad_killer.policy(group_id)
        config = json.loads(policy["config"])
        if (
            not modules.get("fingerprint")
            or not modules.get("moderation")
            or not group
            or group["mode"] == "off"
            or not policy["enabled"]
            or policy["error"]
            or uid in config["users"]
            or not self.store.db.execute(
                "SELECT 1 FROM mod_groups WHERE chat=? AND enabled=1", (group_id,)
            ).fetchone()
        ):
            return False
        if self.store.db.execute(
            "SELECT 1 FROM fp_seen WHERE chat=? AND message=?",
            (group_id, msg.message_id),
        ).fetchone():
            return False
        clean = canonical(text)
        decision_text = text
        removed_allowed = False
        unresolved_allowed = False
        for raw in LINKS.findall(text):
            try:
                url = urlsplit(raw if "://" in raw else "https://" + raw)
                host = (url.hostname or "").lower()
                path = unquote(url.path).strip("/")
                first = path.removeprefix("s/").split("/")[0].lower()
                is_tg = host in {"t.me", "telegram.me", "telegram.dog"}
                permitted = any(
                    host == d or host.endswith("." + d) for d in config["domains"]
                )
                permitted |= is_tg and (
                    "@" + first in config["telegram_allow"]
                    or "https://t.me/" + path in config["telegram_allow"]
                    or (
                        "-100" + path.split("/")[1] in config["telegram_allow"]
                        if path.startswith("c/") and len(path.split("/")) > 1
                        else False
                    )
                )
                if permitted:
                    decision_text = decision_text.replace(raw, "")
                    removed_allowed = True
                elif is_tg and any(x.startswith("-") for x in config["telegram_allow"]):
                    unresolved_allowed = True
            except ValueError:
                unresolved_allowed = True
        for handle in re.findall(r"(?<!\w)@[a-zA-Z]\w{3,31}", text):
            if handle.lower() in config["telegram_allow"]:
                decision_text = decision_text.replace(handle, "")
                removed_allowed = True
        result = self.match(
            group_id, decision_text, bool(getattr(msg, "reply_to_message", None))
        )
        if (
            result
            and removed_allowed
            and not (
                PROMOTION.search(canonical(decision_text))
                and DIVERSION.search(canonical(decision_text))
            )
        ):
            result["automatic"] = False
            result["reason"] = "allowed_target_only"
        now = self.store.clock()
        history_key = (group_id, uid)
        window = self.history.setdefault(history_key, deque(maxlen=3))
        self.history.move_to_end(history_key)
        while len(self.history) > 2000:
            self.history.popitem(last=False)
        while window and window[0][0] < now - 120:
            window.popleft()
        prior = [x for x in window if x[1] != msg.message_id]
        window.clear()
        window.extend(prior)
        window.append((now, msg.message_id, masked(clean)[:1000]))
        if not result and len(window) > 1:
            body = " ".join(x[2] for x in window)
            if PROMOTION.search(body) and DIVERSION.search(clean):
                result = {
                    "sample": None,
                    "score": 0,
                    "reason": "sequence",
                    "automatic": False,
                }
        if not result:
            return False
        key = (group_id, msg.message_id)
        self.generations[key] = generation = self.generations.get(key, 0) + 1
        self.generations.move_to_end(key)
        while len(self.generations) > 2000:
            self.generations.popitem(last=False)
        try:
            async with self.runtime.moderation.locks.setdefault(
                group_id, asyncio.Lock()
            ):
                member = await self.runtime.bot.get_chat_member(group_id, int(uid))
                if member.status not in {"member", "restricted"}:
                    return False
                day = int(now // 86400)
                count = self.store.db.execute(
                    "SELECT count(*) FROM fp_events WHERE chat=? AND at>=?",
                    (group_id, day * 86400),
                ).fetchone()[0]
                if count >= 100:
                    self.store.db.execute(
                        """INSERT INTO fp_overflow VALUES(?,?,1)
                        ON CONFLICT(chat,day) DO UPDATE SET count=count+1""",
                        (group_id, day),
                    )
                    return False

                def valid():
                    current = self.store.db.execute(
                        "SELECT * FROM fp_groups WHERE chat=?", (group_id,)
                    ).fetchone()
                    p = self.runtime.ad_killer.policy(group_id)
                    sample = self.store.db.execute(
                        "SELECT * FROM fp_samples WHERE id=?", (result["sample"],)
                    ).fetchone()
                    return bool(
                        not self.stopping
                        and self.generations.get(key) == generation
                        and self.store.get("modules", {}).get("fingerprint")
                        and self.store.get("modules", {}).get("moderation")
                        and current
                        and current["mode"] in {"observe", "auto"}
                        and current["version"] == group["version"]
                        and p["enabled"]
                        and not p["error"]
                        and p["version"] == policy["version"]
                        and uid not in json.loads(p["config"])["users"]
                        and self.store.db.execute(
                            "SELECT 1 FROM mod_groups WHERE chat=? AND enabled=1",
                            (group_id,),
                        ).fetchone()
                        and sample
                        and sample["status"] == "active"
                        and sample["expires"] > self.store.clock()
                        and sample["version"] == result.get("sample_version")
                        and not self.store.db.execute(
                            "SELECT 1 FROM fp_exceptions WHERE chat=? AND sample=?",
                            (group_id, result["sample"]),
                        ).fetchone()
                        and (
                            (current["mode"] == "auto" and self.ready(group_id))
                            or self.test_allowed(
                                group_id, uid, msg.message_id, result["sample"]
                            )
                        )
                    )

                # Unresolved Telegram identities never override group exceptions.
                if unresolved_allowed:
                    result["automatic"] = False
                    result["reason"] = "allowlist_review"
                self.store.db.execute(
                    "INSERT OR IGNORE INTO fp_events VALUES(?,?,?,?,?,?,?,?,?,?,0)",
                    (
                        group_id,
                        msg.message_id,
                        uid,
                        hashlib.sha256(clean.encode()).hexdigest(),
                        result["sample"],
                        result["reason"],
                        result["score"],
                        masked(text),
                        "candidate" if result["automatic"] else "suspect",
                        now,
                    ),
                )
                self.store.db.execute(
                    "INSERT OR IGNORE INTO fp_seen VALUES(?,?,?)",
                    (group_id, msg.message_id, now),
                )
                if not result["automatic"] or not valid():
                    return False
                prior_hits = self.store.db.execute(
                    """SELECT count(*) FROM ak_hits WHERE chat=? AND uid=? AND at>=?
                    AND status='accepted' AND false_positive=0 AND rules='["fingerprint"]'""",
                    (group_id, uid, now - 86400),
                ).fetchone()[0]
                action = (
                    "mute"
                    if prior_hits >= 2 and member.status == "member"
                    else "delete"
                )
                claimed = self.store.db.execute(
                    """INSERT OR IGNORE INTO ak_hits(chat,message,uid,rules,action,version,status,step,at)
                    VALUES(?,?,?,'["fingerprint"]',?,?,'executing','claimed',?)""",
                    (group_id, msg.message_id, uid, action, policy["version"], now),
                ).rowcount
                if not claimed:
                    return False
                try:
                    async with asyncio.timeout(15):
                        await self.runtime.ad_killer._execute(
                            group_id,
                            msg.message_id,
                            uid,
                            action,
                            policy["actor"],
                            policy["version"],
                            ai_guard=valid,
                        )
                except BaseException as exc:
                    self.store.db.execute(
                        "UPDATE ak_hits SET status='review',error='fp_unknown' WHERE chat=? AND message=?",
                        key,
                    )
                    self.store.db.execute(
                        "UPDATE fp_groups SET mode='observe',version=version+1 WHERE chat=?",
                        (group_id,),
                    )
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                status = self.store.db.execute(
                    "SELECT status FROM ak_hits WHERE chat=? AND message=?", key
                ).fetchone()[0]
                if status == "accepted" and action == "mute":
                    restriction = self.store.db.execute(
                        "SELECT payload FROM mod_restrictions WHERE chat=? AND target=?",
                        (group_id, uid),
                    ).fetchone()
                    if restriction:
                        self.store.db.execute(
                            "INSERT OR IGNORE INTO fp_restrictions VALUES(?,?,?,?,?,?)",
                            (
                                group_id,
                                msg.message_id,
                                uid,
                                restriction[0],
                                "active",
                                now,
                            ),
                        )
                self.store.db.execute(
                    "UPDATE fp_events SET status=? WHERE chat=? AND message=?",
                    (status, *key),
                )
                return True
        finally:
            if self.generations.get(key) == generation:
                self.generations.pop(key, None)

    def false_positive(self, actor, chat, message):
        """Immediately exclude a sample locally and retain a global review request."""
        with self.store.tx() as db:
            row = db.execute(
                "SELECT * FROM fp_events WHERE chat=? AND message=?",
                (str(chat), message),
            ).fetchone()
            if not row:
                raise Rejected("样本命中不存在")
            db.execute(
                "UPDATE fp_events SET reviewed=-1 WHERE chat=? AND message=?",
                (str(chat), message),
            )
            if row["sample"]:
                db.execute(
                    "INSERT OR IGNORE INTO fp_exceptions VALUES(?,?,?,?)",
                    (str(chat), row["sample"], str(actor), self.store.clock()),
                )
            db.execute(
                "UPDATE ak_hits SET false_positive=1 WHERE chat=? AND message=?",
                (str(chat), message),
            )
            self.store.audit(
                db,
                str(actor),
                "fp_false_positive",
                {"chat": str(chat), "sample": row["sample"], "message": message},
            )

    async def restore(self, actor, chat, message):
        """Restore only a matching, unchanged restriction created by this module.

        Args:
            actor: Authorized group administrator.
            chat: Group identifier.
            message: Message whose accepted punishment created the restriction.
        """
        chat = str(chat)
        async with self.runtime.moderation.locks.setdefault(chat, asyncio.Lock()):
            await self.runtime.moderation.check(str(actor), chat, "mute")
            row = self.store.db.execute(
                "SELECT * FROM fp_restrictions WHERE chat=? AND message=?",
                (chat, message),
            ).fetchone()
            if not row or row["status"] != "active":
                raise Rejected("没有可恢复的本次禁言，或结果待核查")
            current = await self.runtime.bot.get_chat_member(chat, int(row["uid"]))
            from .store import encode

            if encode({"member": encode(current.to_dict())}) != row["expected"]:
                raise Rejected("当前限制已变化或已到期，请人工核查")
            if self.store.db.execute(
                """SELECT 1 FROM mod_ops WHERE chat=? AND at>=?
                AND action IN ('mute','mute_forever','unmute','ban','kick','unban')
                AND CAST(json_extract(payload,'$.target') AS TEXT)=? LIMIT 1""",
                (chat, row["at"], row["uid"]),
            ).fetchone():
                raise Rejected("存在后续管理员操作，请人工核查")
            if self.store.db.execute(
                """SELECT 1 FROM ak_hits WHERE chat=? AND uid=? AND message!=?
                AND at>=? AND action!='delete' AND status IN ('accepted','executing','review') LIMIT 1""",
                (chat, row["uid"], message, row["at"]),
            ).fetchone():
                raise Rejected("存在后续处罚，不可恢复旧禁言")
            saved = self.store.db.execute(
                "SELECT payload FROM mod_restrictions WHERE chat=? AND target=?",
                (chat, row["uid"]),
            ).fetchone()
            if not saved or saved[0] != row["expected"]:
                raise Rejected("限制记录已变化，请人工核查")
            self.store.db.execute(
                "UPDATE fp_restrictions SET status='restoring' WHERE chat=? AND message=?",
                (chat, message),
            )
            try:
                await self.runtime.bot.restrict_chat_member(
                    chat,
                    int(row["uid"]),
                    ChatPermissions.all_permissions(),
                    use_independent_chat_permissions=True,
                )
            except BaseException:
                self.store.db.execute(
                    "UPDATE fp_restrictions SET status='unknown' WHERE chat=? AND message=?",
                    (chat, message),
                )
                raise
            with self.store.tx() as db:
                db.execute(
                    "UPDATE fp_restrictions SET status='restored' WHERE chat=? AND message=?",
                    (chat, message),
                )
                db.execute(
                    "DELETE FROM mod_restrictions WHERE chat=? AND target=? AND payload=?",
                    (chat, row["uid"], row["expected"]),
                )
                self.store.audit(
                    db,
                    str(actor),
                    "fp_restore",
                    {"chat": chat, "message": message, "uid": row["uid"]},
                )

    async def maintenance(self):
        """Expire samples and bounded diagnostics even when no messages arrive."""
        while not self.stopping:
            now = self.store.clock()
            with self.store.tx() as db:
                ids = [
                    r[0]
                    for r in db.execute(
                        "SELECT id FROM fp_samples WHERE expires<=? AND body!='' LIMIT 200",
                        (now,),
                    )
                ]
                for identity in ids:
                    db.execute(
                        "UPDATE fp_samples SET status='expired',body='',template='',version=version+1 WHERE id=?",
                        (identity,),
                    )
                    db.execute("DELETE FROM fp_grams WHERE sample=?", (identity,))
                db.execute(
                    """UPDATE fp_events SET body=NULL WHERE rowid IN
                    (SELECT rowid FROM fp_events WHERE at<? AND body IS NOT NULL LIMIT 200)""",
                    (now - 7 * 86400,),
                )
                db.execute(
                    """DELETE FROM fp_events WHERE rowid IN
                    (SELECT rowid FROM fp_events WHERE at<? AND reviewed=0
                    AND status NOT IN ('accepted','review') LIMIT 200)""",
                    (now - 7 * 86400,),
                )
                db.execute(
                    "DELETE FROM fp_overflow WHERE day<?", (int(now // 86400) - 7,)
                )
            for key in list(self.history):
                if not self.history[key] or self.history[key][-1][0] < now - 120:
                    self.history.pop(key, None)
            rows = self.store.db.execute(
                """SELECT g.*,r.group_version,r.policy_version FROM fp_groups g
                JOIN fp_rollout r ON r.chat=g.chat WHERE g.mode='observe' AND
                g.rollout=1 AND r.status='observing' AND g.chat>?
                ORDER BY g.chat LIMIT 20""",
                (self.rollout_cursor,),
            ).fetchall()
            self.rollout_cursor = rows[-1]["chat"] if rows else ""
            for group in rows:
                if not self.store.get("modules", {}).get(
                    "fingerprint"
                ) or not self.ready(group["chat"]):
                    continue
                try:
                    await self.runtime.moderation.check(
                        group["actor"], group["chat"], "delete"
                    )
                    await self.runtime.moderation.check(
                        group["actor"], group["chat"], "mute"
                    )
                    async with self.runtime.moderation.locks.setdefault(
                        group["chat"], asyncio.Lock()
                    ):
                        fresh = self.store.db.execute(
                            "SELECT * FROM fp_groups WHERE chat=?", (group["chat"],)
                        ).fetchone()
                        policy = self.runtime.ad_killer.policy(group["chat"])
                        if (
                            self.stopping
                            or not self.ready(group["chat"])
                            or not self.store.get("modules", {}).get("fingerprint")
                            or not self.store.get("modules", {}).get("moderation")
                            or fresh["version"] != group["group_version"]
                            or not fresh["rollout"]
                            or fresh["mode"] != "observe"
                            or not policy["enabled"]
                            or policy["error"]
                            or policy["version"] != group["policy_version"]
                        ):
                            continue
                        with self.store.tx() as db:
                            db.execute(
                                "UPDATE fp_groups SET mode='auto',version=version+1 WHERE chat=?",
                                (group["chat"],),
                            )
                            db.execute(
                                "UPDATE fp_rollout SET status='promoted' WHERE chat=?",
                                (group["chat"],),
                            )
                            self.store.audit(
                                db,
                                group["actor"],
                                "fp_auto_promote",
                                {"chat": group["chat"]},
                            )
                except Exception as exc:
                    self.store.db.execute(
                        "UPDATE fp_rollout SET error=? WHERE chat=?",
                        (type(exc).__name__, group["chat"]),
                    )
            await asyncio.sleep(60)

    async def begin_rollout(self, actor):
        """Snapshot currently eligible groups after genuine acceptance evidence.

        Args:
            actor: Full business manager requesting observation deployment.
        """
        self.store.require(str(actor), "manager")
        release = self.store.get("fp_release", {})
        if (
            release.get("version") != VERSION
            or not release.get("offline_pass")
            or not release.get("telethon_pass")
        ):
            raise Rejected("离线或测试群验收尚未完成，不能推广")
        rows = self.store.db.execute(
            """SELECT p.chat,p.version FROM ak_policies p JOIN mod_groups g ON g.chat=p.chat
            WHERE p.enabled=1 AND p.error='' AND g.enabled=1 ORDER BY p.chat"""
        ).fetchall()
        result = {"observing": [], "skipped": []}
        for row in rows:
            try:
                await self.runtime.moderation.check(str(actor), row["chat"], "delete")
                await self.runtime.moderation.check(str(actor), row["chat"], "mute")
                with self.store.tx() as db:
                    self.store.require(str(actor), "manager", db=db)
                    policy = self.runtime.ad_killer.policy(row["chat"])
                    if (
                        policy["version"] != row["version"]
                        or not policy["enabled"]
                        or policy["error"]
                        or not db.execute(
                            "SELECT 1 FROM mod_groups WHERE chat=? AND enabled=1",
                            (row["chat"],),
                        ).fetchone()
                        or db.execute(
                            "SELECT 1 FROM fp_groups WHERE chat=?", (row["chat"],)
                        ).fetchone()
                    ):
                        result["skipped"].append(row["chat"])
                        continue
                    db.execute(
                        "INSERT INTO fp_groups(chat,mode,actor,since,rollout) VALUES(?,'observe',?,?,1)",
                        (row["chat"], str(actor), self.store.clock()),
                    )
                    db.execute(
                        "INSERT INTO fp_rollout VALUES(?,1,?,'observing','')",
                        (row["chat"], row["version"]),
                    )
                    self.store.audit(
                        db,
                        str(actor),
                        "fp_observe_rollout",
                        {"chat": row["chat"], "policy_version": row["version"]},
                    )
                result["observing"].append(row["chat"])
            except Rejected:
                result["skipped"].append(row["chat"])
        return result
