"""Per-group deterministic advertisement detection and conservative moderation."""

import asyncio
import hashlib
import html
import json
import re
import unicodedata
from difflib import SequenceMatcher

from telegram import ChatPermissions

from .moderation import SEND
from .store import Rejected, encode

RULES = {
    "links": "外链",
    "telegram": "Telegram 引流",
    "group_links": "其他群／频道链接",
    "platform_links": "外部平台链接",
    "channel_forward": "频道转发",
    "contact": "联系方式",
    "keywords": "关键词组合",
    "repeat": "重复广告",
    "flood": "短时刷屏",
}
ACTIONS = {
    "delete": "仅撤回",
    "mute": "撤回并限时禁言",
    "mute_forever": "撤回并永久禁言",
    "kick": "撤回并踢出",
    "ban": "撤回并封禁",
}
PRIORITY = tuple(ACTIONS)
PHONE_RE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
EMAIL_RE = re.compile(r"(?<![\w.@])[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b", re.I)
CONTACT_RE = re.compile(
    r"(?i)(?:微信|微[信し]|vx|v信|qq|扣扣)\s*[:：号]?\s*[a-z0-9_-]{5,}"
)
HANDLE_RE = re.compile(r"(?<![\w@])@[a-z][a-z0-9_]{4,31}\b", re.I)
CALL_TO_ACTION = (
    "加我",
    "联系",
    "私信",
    "进群",
    "推广",
    "扫码",
    "招商",
    "返佣",
    "咨询",
)


def defaults():
    """Provide an independent, disabled copy for a newly discovered group."""
    return {
        "rules": {key: {"enabled": False, "action": "delete"} for key in RULES},
        "keywords": [],
        "users": [],
        "domains": [],
        "telegram_allow": [],
        "repeat_count": 3,
        "repeat_seconds": 600,
        "flood_count": 6,
        "flood_seconds": 30,
        "mute_minutes": 10,
        "escalation_hours": 24,
        "escalation": {"enabled": False, "count": 3, "action": "mute"},
    }


def normalize(text):
    """Normalize visible user text without evaluating markup or external links."""
    return "".join(
        ch
        for ch in unicodedata.normalize("NFKC", html.unescape(text)).casefold()
        if ch not in "\u200b\u200c\u200d\u2060\ufeff"
    )


def validate(config):
    """Reject invalid or overly broad policy data before saving it."""
    if not isinstance(config, dict) or set(config) != set(defaults()):
        raise Rejected("规则配置已变化，请重新打开")
    if set(config["rules"]) != set(RULES):
        raise Rejected("检测项无效")
    for rule in config["rules"].values():
        if (
            set(rule) != {"enabled", "action"}
            or type(rule["enabled"]) is not bool
            or rule["action"] not in ACTIONS
        ):
            raise Rejected("检测项或处罚方式无效")
    bounds = {
        "repeat_count": (2, 20),
        "repeat_seconds": (30, 3600),
        "flood_count": (3, 30),
        "flood_seconds": (10, 600),
        "mute_minutes": (1, 525600),
        "escalation_hours": (1, 720),
    }
    if any(
        type(config[k]) is not int or not a <= config[k] <= b
        for k, (a, b) in bounds.items()
    ):
        raise Rejected("阈值或禁言分钟数超出范围")
    escalation = config["escalation"]
    if (
        set(escalation) != {"enabled", "count", "action"}
        or type(escalation["enabled"]) is not bool
        or type(escalation["count"]) is not int
        or not 2 <= escalation["count"] <= 20
        or escalation["action"] not in ACTIONS
    ):
        raise Rejected("累计处罚配置无效")
    if any(
        not isinstance(config[k], list) or len(config[k]) > 100
        for k in ("keywords", "users", "domains", "telegram_allow")
    ):
        raise Rejected("白名单或关键词数量超出限制")
    if any(
        not isinstance(k, str)
        or not 1 <= len(k) <= 80
        or len(k.split("+")) > 5
        or any(not term.strip() for term in k.split("+"))
        for k in config["keywords"]
    ):
        raise Rejected("关键词用 + 连接，最多五段，每项最多80字")
    if any(
        not isinstance(u, str) or not u.isascii() or not u.isdigit() or int(u) <= 0
        for u in config["users"]
    ):
        raise Rejected("账号白名单仅接受数字 UID")
    if any(
        not isinstance(d, str) or not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", d)
        for d in config["domains"]
    ):
        raise Rejected("域名白名单仅接受域名，不含协议和路径")
    if any(
        not isinstance(value, str)
        or not re.fullmatch(
            r"(?:@[a-z][a-z0-9_]{3,31}|-[1-9][0-9]*|https://t\.me/(?:\+[A-Za-z0-9_-]+|joinchat/[A-Za-z0-9_-]+))",
            value,
        )
        for value in config["telegram_allow"]
    ):
        raise Rejected("群频道白名单接受 @用户名、负数群/频道 ID 或完整邀请链接")


class AdKiller:
    """Own policy snapshots and one irreversible operation journal per message."""

    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS ak_policies(chat TEXT PRIMARY KEY,enabled INTEGER NOT NULL DEFAULT 0,actor TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,config TEXT NOT NULL,error TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS ak_seen(chat TEXT NOT NULL,message INTEGER NOT NULL,uid TEXT NOT NULL,digest TEXT NOT NULL,at REAL NOT NULL,sample TEXT NOT NULL DEFAULT '',PRIMARY KEY(chat,message));
            CREATE INDEX IF NOT EXISTS ak_seen_window ON ak_seen(chat,uid,at);
            CREATE INDEX IF NOT EXISTS ak_seen_age ON ak_seen(at);
            CREATE TABLE IF NOT EXISTS ak_hits(chat TEXT NOT NULL,message INTEGER NOT NULL,uid TEXT NOT NULL,rules TEXT NOT NULL,action TEXT NOT NULL,version INTEGER NOT NULL,body TEXT,status TEXT NOT NULL,step TEXT NOT NULL,at REAL NOT NULL,error TEXT NOT NULL DEFAULT '',false_positive INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(chat,message));
            CREATE INDEX IF NOT EXISTS ak_hits_recent ON ak_hits(chat,at);
            UPDATE ak_hits SET status='review',error='restart_unknown' WHERE status='executing';
            UPDATE ak_policies SET enabled=0,error='待核查：上次操作中断' WHERE chat IN (SELECT chat FROM ak_hits WHERE status='review');
        """)
        if "sample" not in {
            row["name"] for row in self.store.db.execute("PRAGMA table_info(ak_seen)")
        }:
            self.store.db.execute(
                "ALTER TABLE ak_seen ADD COLUMN sample TEXT NOT NULL DEFAULT ''"
            )
        with self.store.tx() as db:
            for row in db.execute("SELECT chat,config FROM ak_policies").fetchall():
                config = json.loads(row["config"])
                changed = False
                for key in RULES:
                    if key not in config["rules"]:
                        config["rules"][key] = {"enabled": False, "action": "delete"}
                        changed = True
                if "telegram_allow" not in config:
                    config["telegram_allow"] = []
                    changed = True
                if "escalation_hours" not in config:
                    config["escalation_hours"] = 24
                    changed = True
                if changed:
                    db.execute(
                        "UPDATE ak_policies SET config=?,version=version+1 WHERE chat=?",
                        (encode(config), row["chat"]),
                    )
        self.target_cache = {}
        self.last_cleanup = 0

    def policy(self, chat):
        row = self.store.db.execute(
            "SELECT * FROM ak_policies WHERE chat=?", (str(chat),)
        ).fetchone()
        return (
            dict(row)
            if row
            else {
                "chat": str(chat),
                "enabled": 0,
                "actor": "",
                "version": 0,
                "config": encode(defaults()),
                "error": "",
            }
        )

    def save(self, chat, actor, version, config, enabled):
        """Atomically replace a confirmed policy after validating its version."""
        validate(config)
        if type(enabled) is not bool:
            raise Rejected("启停值无效")
        with self.store.tx() as db:
            self.store.require(actor, "moderation", db)
            group = db.execute(
                "SELECT enabled FROM mod_groups WHERE chat=?", (str(chat),)
            ).fetchone()
            if not group or not group[0]:
                raise Rejected("请先启用此群的群管理")
            if (
                not self.store.allowed(actor, "manager", db)
                and not db.execute(
                    "SELECT 1 FROM mod_acl WHERE uid=? AND chat=?",
                    (str(actor), str(chat)),
                ).fetchone()
            ):
                raise Rejected("没有此群管理授权")
            current = db.execute(
                "SELECT version,error FROM ak_policies WHERE chat=?", (str(chat),)
            ).fetchone()
            if (current[0] if current else 0) != version:
                raise Rejected("规则已被修改，请重新打开")
            if enabled and current and current[1]:
                raise Rejected("该群有待核查操作，不能启用")
            db.execute(
                "INSERT INTO ak_policies(chat,enabled,actor,config) VALUES(?,?,?,?) ON CONFLICT(chat) DO UPDATE SET enabled=excluded.enabled,actor=excluded.actor,config=excluded.config,version=ak_policies.version+1",
                (str(chat), int(enabled), str(actor), encode(config)),
            )
            self.store.audit(
                db,
                actor,
                "ad_killer_policy",
                {
                    "chat": str(chat),
                    "enabled": enabled,
                    "version": version + 1,
                    "config": config,
                },
            )

    def pause(self, chat, reason):
        self.store.db.execute(
            "UPDATE ak_policies SET enabled=0,error=? WHERE chat=? AND enabled=1",
            (reason, str(chat)),
        )

    def acknowledge(self, chat, actor, version, count):
        """Close manually verified unknown results without replaying operations."""
        with self.store.tx() as db:
            self.store.require(actor, "moderation", db)
            if (
                not self.store.allowed(actor, "manager", db)
                and not db.execute(
                    "SELECT 1 FROM mod_acl WHERE uid=? AND chat=?",
                    (str(actor), str(chat)),
                ).fetchone()
            ):
                raise Rejected("没有此群管理授权")
            policy = db.execute(
                "SELECT version,error FROM ak_policies WHERE chat=?", (str(chat),)
            ).fetchone()
            current_count = db.execute(
                "SELECT COUNT(*) FROM ak_hits WHERE chat=? AND status='review'",
                (str(chat),),
            ).fetchone()[0]
            if (
                not policy
                or not policy["error"]
                or policy["version"] != version
                or current_count != count
            ):
                raise Rejected("待核查记录已变化，请重新打开")
            db.execute(
                "UPDATE ak_hits SET status='reviewed' WHERE chat=? AND status='review'",
                (str(chat),),
            )
            db.execute(
                "UPDATE ak_policies SET enabled=0,error='',version=version+1 WHERE chat=?",
                (str(chat),),
            )
            self.store.audit(
                db,
                actor,
                "ad_killer_manual_review",
                {"chat": str(chat), "count": current_count, "version": version + 1},
            )

    async def inspect(self, update):
        """Return whether this group message was detected or requires quarantine."""
        message = getattr(update, "message", None) or getattr(
            update, "edited_message", None
        )
        chat = getattr(update, "effective_chat", None)
        if not message or not chat or getattr(chat, "type", "") != "supergroup":
            return False
        group_id = str(chat.id)
        policy = self.policy(group_id)
        if (
            not policy["enabled"]
            or policy["error"]
            or not self.store.get("modules", {}).get("moderation")
        ):
            return False
        if not self.store.db.execute(
            "SELECT 1 FROM mod_groups WHERE chat=? AND enabled=1", (group_id,)
        ).fetchone():
            return False
        user = getattr(message, "from_user", None)
        if (
            not user
            or getattr(user, "is_bot", False)
            or getattr(message, "sender_chat", None)
        ):
            return False
        uid = str(user.id)
        config = json.loads(policy["config"])
        if uid in config["users"]:
            return False
        content = (
            getattr(message, "text", None) or getattr(message, "caption", None) or ""
        )[:4096]
        if not content.strip() and not getattr(message, "forward_origin", None):
            return False
        edited = getattr(update, "edited_message", None) is not None
        text = normalize(content)
        from .ad_link_rules import classify

        link_flags = await classify(
            message,
            chat,
            config,
            self.runtime.bot,
            self.target_cache,
            self.store.clock(),
        )
        digest_text = re.sub(r"\W+", "", text)[:256]
        digest = (
            hashlib.sha256(digest_text.encode()).hexdigest()
            if len(digest_text) >= 12
            else ""
        )
        async with self.runtime.moderation.locks.setdefault(group_id, asyncio.Lock()):
            fresh = self.policy(group_id)
            if (
                not fresh["enabled"]
                or fresh["error"]
                or fresh["version"] != policy["version"]
            ):
                return False
            if self.store.db.execute(
                "SELECT 1 FROM ak_hits WHERE chat=? AND message=?",
                (group_id, message.message_id),
            ).fetchone():
                return True
            now = self.store.clock()
            existing = self.store.db.execute(
                "SELECT 1 FROM ak_seen WHERE chat=? AND message=?",
                (group_id, message.message_id),
            ).fetchone()
            matched = []
            rules = config["rules"]
            matched.extend(
                key
                for key, detected in link_flags.items()
                if detected and rules[key]["enabled"]
            )
            if rules["contact"]["enabled"] and (
                PHONE_RE.search(text)
                or CONTACT_RE.search(text)
                or EMAIL_RE.search(text)
            ):
                matched.append("contact")
            if rules["keywords"]["enabled"] and any(
                all(normalize(word.strip()) in text for word in phrase.split("+"))
                for phrase in config["keywords"]
            ):
                matched.append("keywords")
            if not edited and not existing:
                if rules["repeat"]["enabled"] and digest:
                    prior = sum(
                        seen["digest"] == digest
                        or bool(
                            seen["sample"]
                            and SequenceMatcher(
                                None, seen["sample"], digest_text, autojunk=False
                            ).ratio()
                            >= 0.88
                        )
                        for seen in self.store.db.execute(
                            "SELECT digest,sample FROM ak_seen WHERE chat=? AND uid=? AND at>=? ORDER BY at DESC LIMIT 200",
                            (group_id, uid, now - config["repeat_seconds"]),
                        )
                    )
                    if prior + 1 >= config["repeat_count"]:
                        matched.append("repeat")
                if rules["flood"]["enabled"]:
                    prior = self.store.db.execute(
                        "SELECT COUNT(*) FROM ak_seen WHERE chat=? AND uid=? AND at>=?",
                        (group_id, uid, now - config["flood_seconds"]),
                    ).fetchone()[0]
                    if prior + 1 >= config["flood_count"]:
                        matched.append("flood")
            if not existing:
                self.store.db.execute(
                    "INSERT OR IGNORE INTO ak_seen(chat,message,uid,digest,at,sample) VALUES(?,?,?,?,?,?)",
                    (group_id, message.message_id, uid, digest, now, digest_text),
                )
            elif edited:
                self.store.db.execute(
                    "UPDATE ak_seen SET digest=?,sample=? WHERE chat=? AND message=?",
                    (digest, digest_text, group_id, message.message_id),
                )
            if not matched:
                return False
            try:
                sender = await self.runtime.bot.get_chat_member(group_id, int(uid))
            except Exception as exc:
                self.runtime.report("ad_killer_identity", exc)
                self.store.db.execute(
                    "INSERT OR IGNORE INTO ak_hits(chat,message,uid,rules,action,version,body,status,step,at,error) VALUES(?,?,?,?,?,?,?,'skipped','identity_unknown',?,'member_query_failed')",
                    (
                        group_id,
                        message.message_id,
                        uid,
                        encode(matched),
                        "delete",
                        fresh["version"],
                        content,
                        now,
                    ),
                )
                return True
            if sender.status in {"creator", "administrator"}:
                return False
            if sender.status not in {"member", "restricted"}:
                self.store.db.execute(
                    "INSERT OR IGNORE INTO ak_hits(chat,message,uid,rules,action,version,body,status,step,at,error) VALUES(?,?,?,?,?,?,?,'skipped','identity_unknown',?,'member_not_ordinary')",
                    (
                        group_id,
                        message.message_id,
                        uid,
                        encode(matched),
                        "delete",
                        fresh["version"],
                        content,
                        now,
                    ),
                )
                return True
            action = max(
                (rules[rule]["action"] for rule in matched), key=PRIORITY.index
            )
            if config["escalation"]["enabled"]:
                if hasattr(self.runtime, "community"):
                    prior = self.runtime.community.count(
                        group_id, uid, config["escalation_hours"]
                    )
                    duplicate = self.store.db.execute(
                        "SELECT 1 FROM cm_violations WHERE chat=? AND uid=? AND message=?",
                        (group_id, uid, message.message_id),
                    ).fetchone()
                else:
                    prior = self.store.db.execute(
                        """SELECT COUNT(*) FROM ak_hits WHERE chat=? AND uid=? AND at>=?
                        AND status='accepted' AND false_positive=0
                        AND rules NOT IN ('["ai"]','["fingerprint"]')""",
                        (group_id, uid, now - config["escalation_hours"] * 3600),
                    ).fetchone()[0]
                    duplicate = False
                if not duplicate and prior + 1 >= config["escalation"]["count"]:
                    action = max(
                        (action, config["escalation"]["action"]), key=PRIORITY.index
                    )
            with self.store.tx() as db:
                db.execute(
                    "INSERT OR IGNORE INTO ak_hits(chat,message,uid,rules,action,version,body,status,step,at) VALUES(?,?,?,?,?,?,?,'executing','claimed',?)",
                    (
                        group_id,
                        message.message_id,
                        uid,
                        encode(matched),
                        action,
                        fresh["version"],
                        content,
                        now,
                    ),
                )
            try:
                await self._execute(
                    group_id,
                    message.message_id,
                    uid,
                    action,
                    fresh["actor"],
                    fresh["version"],
                )
            except BaseException as exc:
                self.store.db.execute(
                    "UPDATE ak_hits SET status='review',error=? WHERE chat=? AND message=?",
                    (type(exc).__name__, group_id, message.message_id),
                )
                self.pause(group_id, "待核查：处罚结果不明或权限不足")
                if isinstance(exc, Exception):
                    self.runtime.report("ad_killer", exc)
                else:
                    raise
            return True

    async def _execute(
        self, chat, message, uid, action, actor, version, *, ai_guard=None
    ):
        """Check authority after network awaits and never replay a claimed write."""
        mod = self.runtime.moderation
        await mod.check(actor, chat, "delete")
        if action != "delete":
            await mod.check(actor, chat, "mute")
        sender = await self.runtime.bot.get_chat_member(chat, int(uid))
        if sender.status in {"creator", "administrator"}:
            self.store.db.execute(
                "UPDATE ak_hits SET status='skipped',error='protected_admin' WHERE chat=? AND message=?",
                (chat, message),
            )
            return
        if sender.status not in {"member", "restricted"}:
            self.store.db.execute(
                "UPDATE ak_hits SET status='skipped',error='member_not_ordinary' WHERE chat=? AND message=?",
                (chat, message),
            )
            return
        current = self.policy(chat)
        if ai_guard and (
            not ai_guard() or (action != "delete" and sender.status != "member")
        ):
            self.store.db.execute(
                "UPDATE ak_hits SET status='skipped',error='ai_guard_changed' WHERE chat=? AND message=?",
                (chat, message),
            )
            return
        if not current["enabled"] or current["version"] != version or current["error"]:
            self.store.db.execute(
                "UPDATE ak_hits SET status='skipped',error='policy_changed' WHERE chat=? AND message=?",
                (chat, message),
            )
            return
        if self.store.db.execute(
            "SELECT 1 FROM mod_ops WHERE chat=? AND status='unknown'", (chat,)
        ).fetchone():
            self.store.db.execute(
                "UPDATE ak_hits SET status='skipped',error='group_unknown_result' WHERE chat=? AND message=?",
                (chat, message),
            )
            self.pause(chat, "待核查：已有群管操作结果不明")
            return
        await self.runtime.bot.delete_message(chat, message)
        self.store.db.execute(
            "UPDATE ak_hits SET step='deleted' WHERE chat=? AND message=?",
            (chat, message),
        )
        if action != "delete":
            await mod.check(actor, chat, "mute")
            sender = await self.runtime.bot.get_chat_member(chat, int(uid))
            if (
                sender.status != "member"
                or (ai_guard is not None and not ai_guard())
                or self.policy(chat)["version"] != version
                or not self.policy(chat)["enabled"]
            ):
                self.store.db.execute(
                    "UPDATE ak_hits SET status='skipped',error='existing_restriction_or_policy_change' WHERE chat=? AND message=?",
                    (chat, message),
                )
                return
            if action in {"mute", "mute_forever"}:
                config = json.loads(self.policy(chat)["config"])
                await self.runtime.bot.restrict_chat_member(
                    chat,
                    int(uid),
                    ChatPermissions(**dict.fromkeys(SEND, False)),
                    until_date=int(
                        self.store.clock()
                        + (10 if ai_guard else config["mute_minutes"]) * 60
                    )
                    if action == "mute"
                    else 0,
                    use_independent_chat_permissions=True,
                )
                self.store.db.execute(
                    "UPDATE ak_hits SET step='restricted' WHERE chat=? AND message=?",
                    (chat, message),
                )
                state = await self.runtime.bot.get_chat_member(chat, int(uid))
                mod.save_restriction(chat, uid, {"member": encode(state.to_dict())})
            else:
                await self.runtime.bot.ban_chat_member(
                    chat, int(uid), revoke_messages=True
                )
                self.store.db.execute(
                    "UPDATE ak_hits SET step='banned' WHERE chat=? AND message=?",
                    (chat, message),
                )
                if action == "kick":
                    await mod.check(actor, chat, "mute")
                    if (
                        self.policy(chat)["version"] != version
                        or not self.policy(chat)["enabled"]
                    ):
                        raise Rejected("群规则在踢出第二步前已变化，封禁结果待核查")
                    await self.runtime.bot.unban_chat_member(
                        chat, int(uid), only_if_banned=True
                    )
                    self.store.db.execute(
                        "UPDATE ak_hits SET step='unbanned' WHERE chat=? AND message=?",
                        (chat, message),
                    )
                else:
                    state = await self.runtime.bot.get_chat_member(chat, int(uid))
                    mod.save_restriction(chat, uid, {"member": encode(state.to_dict())})
        with self.store.tx() as db:
            db.execute(
                "UPDATE ak_hits SET status='accepted',step='done' WHERE chat=? AND message=?",
                (chat, message),
            )
            if hasattr(self.runtime, "community") and ai_guard is None:
                self.runtime.community.record(
                    db,
                    f"ak:{chat}:{message}",
                    chat,
                    uid,
                    message,
                    "advertisement",
                    "广告违规",
                    actor,
                )

    def cleanup(self):
        """Remove aged message bodies in short batches without touching audit facts."""
        now = self.store.clock()
        if now - self.last_cleanup < 3600:
            return
        with self.store.tx() as db:
            seen = db.execute(
                "DELETE FROM ak_seen WHERE rowid IN (SELECT rowid FROM ak_seen WHERE at<? LIMIT 500)",
                (now - 7 * 86400,),
            ).rowcount
            bodies = db.execute(
                "UPDATE ak_hits SET body=NULL WHERE rowid IN (SELECT rowid FROM ak_hits WHERE body IS NOT NULL AND status IN ('accepted','skipped','failed','reviewed') AND at<? LIMIT 500)",
                (now - 30 * 86400,),
            ).rowcount
        self.last_cleanup = now if seen < 500 and bodies < 500 else now - 3590
