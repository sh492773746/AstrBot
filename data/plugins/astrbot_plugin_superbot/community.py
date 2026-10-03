"""Per-group public content, reports and a shared violation ledger."""

import asyncio
import json
import secrets
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

from telegram import InlineKeyboardButton as Button
from telegram import InlineKeyboardMarkup

from .moderation import TZ
from .store import Rejected, encode

REASONS = {
    "ads": "广告引流",
    "fraud": "诈骗嫌疑",
    "harass": "骚扰刷屏",
    "other": "其他",
}
FEATURES = {
    "reports": "举报",
    "welcome": "欢迎",
    "rules": "群规",
    "notes": "常用说明",
    "warnings": "人工警告",
}


class Community:
    """Own additive data without granting any Telegram or business authority."""

    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS cm_config(
                chat TEXT PRIMARY KEY,actor TEXT NOT NULL,version INTEGER NOT NULL,
                config TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cm_private(uid TEXT PRIMARY KEY,at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS cm_tickets(
                token TEXT PRIMARY KEY,chat TEXT NOT NULL,message INTEGER NOT NULL,
                target TEXT NOT NULL,uid TEXT NOT NULL,body TEXT,created REAL NOT NULL,
                expires REAL NOT NULL,used INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS cm_ticket_user ON cm_tickets(chat,uid,created);
            CREATE INDEX IF NOT EXISTS cm_ticket_expiry ON cm_tickets(expires);
            CREATE TABLE IF NOT EXISTS cm_cases(
                id INTEGER PRIMARY KEY,chat TEXT NOT NULL,message INTEGER NOT NULL,
                target TEXT NOT NULL,body TEXT,status TEXT NOT NULL DEFAULT 'open',
                version INTEGER NOT NULL DEFAULT 1,created REAL NOT NULL,closed REAL,
                actor TEXT NOT NULL DEFAULT '',outcome TEXT NOT NULL DEFAULT '',
                op TEXT NOT NULL DEFAULT '',UNIQUE(chat,message));
            CREATE TABLE IF NOT EXISTS cm_reports(
                case_id INTEGER NOT NULL,uid TEXT NOT NULL,reason TEXT NOT NULL,
                detail TEXT,created REAL NOT NULL,PRIMARY KEY(case_id,uid));
            CREATE INDEX IF NOT EXISTS cm_report_limits ON cm_reports(uid,created);
            CREATE INDEX IF NOT EXISTS cm_case_queue ON cm_cases(chat,status,created);
            CREATE INDEX IF NOT EXISTS cm_case_cleanup ON cm_cases(status,closed);
            CREATE TABLE IF NOT EXISTS cm_violations(
                id TEXT PRIMARY KEY,chat TEXT NOT NULL,uid TEXT NOT NULL,
                message INTEGER,source TEXT NOT NULL,reason TEXT NOT NULL,
                actor TEXT NOT NULL,at REAL NOT NULL,revoked INTEGER NOT NULL DEFAULT 0,
                version INTEGER NOT NULL DEFAULT 1,escalation TEXT NOT NULL DEFAULT '',
                UNIQUE(chat,uid,message));
            CREATE INDEX IF NOT EXISTS cm_violation_count ON cm_violations(chat,uid,revoked,at);
            CREATE INDEX IF NOT EXISTS cm_violation_history ON cm_violations(chat,at);
            CREATE TABLE IF NOT EXISTS cm_welcome(
                chat TEXT NOT NULL,uid TEXT NOT NULL,event INTEGER NOT NULL,
                status TEXT NOT NULL,PRIMARY KEY(chat,uid,event));
            UPDATE cm_cases SET status='review',outcome='restart_unknown' WHERE status='processing';
            UPDATE cm_violations SET escalation='review' WHERE escalation='claimed';
            UPDATE cm_welcome SET status='unknown' WHERE status='sending';
        """)
        self.last_cleanup = 0

    def policy(self, chat):
        """Return an isolated default or saved policy.

        Args:
            chat: Managed group ID.

        Returns:
            A versioned configuration dictionary.
        """
        row = self.store.db.execute(
            "SELECT * FROM cm_config WHERE chat=?", (str(chat),)
        ).fetchone()
        default = {
            "enabled": dict.fromkeys(FEATURES, False),
            "welcome": "欢迎 {name} 加入 {group}！",
            "rules": "",
            "notes": [],
            "log": {"enabled": False, "channel": "", "title": ""},
        }
        return (
            {**dict(row), "config": json.loads(row["config"])}
            if row
            else {"chat": str(chat), "actor": "", "version": 0, "config": default}
        )

    async def member(self, uid, chat, *, require_moderation=True):
        """Require current group membership before disclosing group content.

        Args:
            uid: User requesting public content.
            chat: Target group.
            require_moderation: Require moderation for community-only content.

        Returns:
            Verified Telegram member.
        """
        group = self.store.db.execute(
            "SELECT enabled FROM mod_groups WHERE chat=?", (str(chat),)
        ).fetchone()
        if (
            (require_moderation and not self.store.get("modules", {}).get("moderation"))
            or not group
            or not group[0]
        ):
            raise Rejected("此群功能未启用")
        member = await self.runtime.bot.get_chat_member(str(chat), int(uid))
        group = self.store.db.execute(
            "SELECT enabled FROM mod_groups WHERE chat=?", (str(chat),)
        ).fetchone()
        if (
            (require_moderation and not self.store.get("modules", {}).get("moderation"))
            or not group
            or not group[0]
        ):
            raise Rejected("此群功能已停用")
        if member.status not in {"member", "administrator", "creator"} and not (
            member.status == "restricted" and getattr(member, "is_member", False)
        ):
            raise Rejected("仅此群当前成员可查看或举报")
        return member

    @staticmethod
    def validate(config):
        """Validate shared content used by individual and template configuration.

        Args:
            config: Complete community configuration.

        Raises:
            Rejected: Content or enabled features are invalid.
        """
        if set(config) != {"enabled", "welcome", "rules", "notes", "log"}:
            raise Rejected("配置无效")
        if set(config["enabled"]) != set(FEATURES) or any(
            type(v) is not bool for v in config["enabled"].values()
        ):
            raise Rejected("开关无效")
        if any(
            not isinstance(config[k], str) or len(config[k]) > 2500
            for k in ("welcome", "rules")
        ):
            raise Rejected("正文最多2500字")
        if not isinstance(config["notes"], list) or len(config["notes"]) > 10:
            raise Rejected("常用说明最多10条")
        for note in config["notes"]:
            if set(note) != {"title", "body", "url"} or not all(
                isinstance(v, str) for v in note.values()
            ):
                raise Rejected("说明格式无效")
            if not 1 <= len(note["title"]) <= 40 or not 1 <= len(note["body"]) <= 2500:
                raise Rejected("说明标题最多40字，正文最多2500字")
            if note["url"]:
                parsed = urlsplit(note["url"])
                if (
                    parsed.scheme not in {"https", "http"}
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                    or len(note["url"]) > 500
                ):
                    raise Rejected("按钮仅支持普通 http/https 网址")
                if parsed.hostname.lower() in {
                    "t.me",
                    "telegram.me",
                    "telegram.dog",
                } and set(parse_qs(parsed.query, keep_blank_values=True)) & {
                    "start",
                    "startgroup",
                    "startchannel",
                    "startapp",
                }:
                    raise Rejected("说明网址不能包含机器人指令或授权入口")
        if config["enabled"]["rules"] and not config["rules"].strip():
            raise Rejected("先填写群规正文")
        if config["enabled"]["notes"] and not config["notes"]:
            raise Rejected("先添加常用说明")
        if (
            not isinstance(config["log"], dict)
            or set(config["log"]) != {"enabled", "channel", "title"}
            or type(config["log"]["enabled"]) is not bool
            or not all(isinstance(config["log"][k], str) for k in ("channel", "title"))
        ):
            raise Rejected("日志配置无效")

    async def save(self, actor, chat, version, config):
        """Save a private confirmed policy after validating rights and limits.

        Args:
            actor: Verified administrator ID.
            chat: Group being configured.
            version: Expected policy version.
            config: Complete replacement from a server-stored confirmation.
        """
        self.validate(config)
        async with self.runtime.moderation.locks.setdefault(str(chat), asyncio.Lock()):
            await self.runtime.moderation.check(actor, str(chat), "view")
            if config["log"]["enabled"]:
                await self.runtime.community_logs.check_channel(
                    actor, config["log"]["channel"]
                )
            if self.policy(chat)["version"] != version:
                raise Rejected("配置已变化，请重新预览")
            with self.store.tx() as db:
                db.execute(
                    "INSERT INTO cm_config(chat,actor,version,config) VALUES(?,?,1,?) ON CONFLICT(chat) DO UPDATE SET actor=excluded.actor,version=cm_config.version+1,config=excluded.config",
                    (str(chat), str(actor), encode(config)),
                )
                self.store.audit(db, actor, "community_config", {"chat": str(chat)})

    async def ticket(self, update):
        """Create an expiring private-report entry from a native reply.

        Args:
            update: Native group update containing a reply to the reported message.

        Returns:
            Opaque deep-link token.
        """
        chat, uid = str(update.effective_chat.id), str(update.effective_user.id)
        await self.member(uid, chat)
        if not self.policy(chat)["config"]["enabled"]["reports"]:
            raise Rejected("此群尚未开启举报")
        message = update.message.reply_to_message
        user = getattr(message, "from_user", None)
        if user and user.is_bot:
            raise Rejected("不能举报机器人。请回复目标群员的原消息，再发送 /report。")
        if (
            not message
            or not user
            or user.is_bot
            or getattr(message, "sender_chat", None)
        ):
            raise Rejected("请回复可确认发送者的普通成员消息，再发送 /report")
        if str(user.id) == uid:
            raise Rejected("不能举报自己的消息")
        now = self.store.clock()
        start = (
            datetime.fromtimestamp(now, TZ)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )
        with self.store.tx() as db:
            recent, today = db.execute(
                "SELECT COALESCE(SUM(created>?),0),COALESCE(SUM(created>=?),0) FROM cm_tickets WHERE chat=? AND uid=? AND created>=?",
                (now - 600, start, chat, uid, min(start, now - 600)),
            ).fetchone()
            if recent >= 3 or today >= 10:
                raise Rejected("举报入口申请过于频繁，请稍后再试")
            token = secrets.token_urlsafe(18)
            db.execute(
                "INSERT INTO cm_tickets(token,chat,message,target,uid,body,created,expires) VALUES(?,?,?,?,?,?,?,?)",
                (
                    token,
                    chat,
                    message.message_id,
                    str(user.id),
                    uid,
                    (message.text or message.caption or "")[:4096],
                    now,
                    now + 600,
                ),
            )
            db.execute(
                "INSERT OR IGNORE INTO mod_messages(chat,message,sender) VALUES(?,?,?)",
                (chat, message.message_id, str(user.id)),
            )
        return token

    async def submit(self, uid, token, reason, detail=""):
        """Merge authenticated reports without counting them as violations.

        Args:
            uid: Reporter.
            token: Expiring group-origin token.
            reason: Fixed reason identifier.
            detail: Optional bounded private explanation.

        Returns:
            Case ID.
        """
        row = self.store.db.execute(
            "SELECT * FROM cm_tickets WHERE token=? AND uid=?", (token, str(uid))
        ).fetchone()
        if not row or row["expires"] <= self.store.clock():
            raise Rejected("举报入口过期或不属于你，请回群重新发起")
        await self.member(uid, row["chat"])
        if reason not in REASONS or len(detail) > 1000:
            raise Rejected("举报内容无效")
        if not self.policy(row["chat"])["config"]["enabled"]["reports"]:
            raise Rejected("此群举报已停用")
        now = self.store.clock()
        start = (
            datetime.fromtimestamp(now, TZ)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )
        with self.store.tx() as db:
            if not db.execute(
                "UPDATE cm_tickets SET used=1 WHERE token=? AND used=0 AND expires>?",
                (token, now),
            ).rowcount:
                raise Rejected("已提交，请到我的举报查看结果")
            case = db.execute(
                "SELECT * FROM cm_cases WHERE chat=? AND message=?",
                (row["chat"], row["message"]),
            ).fetchone()
            if case and case["status"] != "open":
                raise Rejected("该消息已有处理记录")
            if (
                case
                and db.execute(
                    "SELECT 1 FROM cm_reports WHERE case_id=? AND uid=?",
                    (case["id"], str(uid)),
                ).fetchone()
            ):
                raise Rejected("你已经举报过该消息")
            counts = db.execute(
                "SELECT COALESCE(SUM(r.created>?),0),COALESCE(SUM(r.created>=?),0) FROM cm_reports r JOIN cm_cases c ON c.id=r.case_id WHERE c.chat=? AND r.uid=?",
                (now - 600, start, row["chat"], str(uid)),
            ).fetchone()
            if counts[0] >= 3 or counts[1] >= 10:
                raise Rejected("举报次数达到上限，请稍后再试")
            if not case:
                cursor = db.execute(
                    "INSERT INTO cm_cases(chat,message,target,body,created) VALUES(?,?,?,?,?)",
                    (row["chat"], row["message"], row["target"], row["body"], now),
                )
                case_id = cursor.lastrowid
            else:
                case_id = case["id"]
            db.execute(
                "INSERT INTO cm_reports(case_id,uid,reason,detail,created) VALUES(?,?,?,?,?)",
                (case_id, str(uid), reason, detail, now),
            )
            self.store.audit(
                db, uid, "community_report", {"chat": row["chat"], "case": case_id}
            )
        try:
            await self.notify_admins(row["chat"], case_id)
        except Exception as exc:
            self.runtime.report("community_notice", exc)
        return case_id

    async def notify_admins(self, chat, case_id):
        """Queue only minimal private notices for currently entitled recipients.

        Args:
            chat: Reported group.
            case_id: Case used for notification deduplication.
        """
        users = self.store.db.execute(
            "SELECT uid FROM cm_private WHERE uid=? OR uid IN (SELECT uid FROM mod_acl WHERE chat=?) "
            "OR uid IN (SELECT r.uid FROM roles r,json_each(r.scopes) s WHERE r.expires>? AND s.value='manager')",
            (self.store.owner, str(chat), self.store.clock()),
        ).fetchall()
        for user in users:
            try:
                await self.runtime.moderation.check(user["uid"], str(chat), "view")
            except Exception:
                continue
            # This queue rechecks authority at delivery and does not expose evidence.
            self.runtime.community_logs.enqueue(
                f"report:{case_id}:{user['uid']}",
                str(chat),
                user["uid"],
                "report_notice",
                f"有待处理举报 #{case_id}，请在群管理的「举报处理」查看。",
                user["uid"],
                private=True,
            )

    def count(self, chat, uid, hours=24):
        """Count unrevoked facts in the current window.

        Args:
            chat: Managed group.
            uid: Subject.
            hours: Confirmed count window.

        Returns:
            Number of effective violations.
        """
        return self.store.db.execute(
            "SELECT COUNT(*) FROM cm_violations WHERE chat=? AND uid=? AND revoked=0 AND at>=?",
            (str(chat), str(uid), self.store.clock() - hours * 3600),
        ).fetchone()[0]

    def record(self, db, key, chat, uid, message, source, reason, actor):
        """Insert an idempotent fact within its caller's transaction.

        Args:
            db: Existing transaction.
            key: Immutable operation ID.
            chat: Managed group.
            uid: Subject.
            message: Source message ID or None for a standalone warning.
            source: Manual or advertisement source.
            reason: Public reason label, never private report details.
            actor: Responsible actor.

        Returns:
            Whether a new fact was inserted.
        """
        changed = db.execute(
            "INSERT OR IGNORE INTO cm_violations(id,chat,uid,message,source,reason,actor,at) VALUES(?,?,?,?,?,?,?,?)",
            (
                key,
                str(chat),
                str(uid),
                message,
                source,
                reason,
                str(actor),
                self.store.clock(),
            ),
        ).rowcount
        if changed:
            self.store.audit(
                db,
                actor,
                "community_violation",
                {"chat": str(chat), "uid": str(uid), "id": key},
            )
        return bool(changed)

    async def warn(self, actor, chat, uid, reason, key, message=None, expected=None):
        """Record and optionally escalate one confirmed manual warning.

        Args:
            actor: Entitled moderator.
            chat: Managed group.
            uid: Protected-identity checked subject.
            reason: Fixed reason label.
            key: Single-use confirmation operation.
            message: Optional case message for shared deduplication.
            expected: Optional configuration versions bound to the preview.

        Returns:
            Short result description.
        """
        async with self.runtime.moderation.locks.setdefault(str(chat), asyncio.Lock()):
            await self.runtime.moderation.check(actor, str(chat), "view")
            policy = self.runtime.ad_killer.policy(chat)
            if expected and (
                expected["version"] != self.policy(chat)["version"]
                or expected["ak_version"] != policy["version"]
            ):
                raise Rejected("警告或升级规则已变化，请重新预览")
            if not self.policy(chat)["config"]["enabled"]["warnings"]:
                raise Rejected("此群人工警告尚未开启")
            target = await self.runtime.bot.get_chat_member(str(chat), int(uid))
            if (
                target.status not in {"member", "restricted"}
                or int(uid) == self.runtime.bot.id
                or getattr(getattr(target, "user", None), "is_bot", False)
            ):
                raise Rejected("不能警告群主、管理员、机器人或已离群成员")
            await self.runtime.moderation.check(actor, str(chat), "view")
            with self.store.tx() as db:
                inserted = self.record(
                    db, key, chat, uid, message, "manual", REASONS[reason], actor
                )
            if not inserted:
                return "这条消息已有违规记录，不重复计数。"
            config = json.loads(policy["config"])
            escalation = config["escalation"]
            if (
                not policy["enabled"]
                or policy["error"]
                or not escalation["enabled"]
                or self.count(chat, uid, config["escalation_hours"])
                < escalation["count"]
            ):
                return "警告已记录。"
            op = escalation["action"]
            if op == "delete" and message is None:
                return "警告已记录；没有指定消息，不自动撤回。"
            self.store.db.execute(
                "UPDATE cm_violations SET escalation='claimed' WHERE id=?", (key,)
            )
            try:
                payload = await self.runtime.moderation.preview(
                    actor,
                    str(chat),
                    op,
                    str(message if op == "delete" else uid),
                    config["mute_minutes"] if op == "mute" else 0,
                )
                # Claims are made before any Telegram write, while holding the same group lock.
                fresh = self.runtime.ad_killer.policy(chat)
                if (
                    fresh["version"] != policy["version"]
                    or not fresh["enabled"]
                    or fresh["error"]
                ):
                    raise Rejected("升级规则已变化")
                await self.runtime.moderation.execute(
                    actor, payload, "cm:" + key, locked=True
                )
                self.store.db.execute(
                    "UPDATE cm_violations SET escalation='accepted' WHERE id=?", (key,)
                )
                return "警告已记录，累计处罚已受理。"
            except Exception as exc:
                attempted = self.store.db.execute(
                    "SELECT status FROM mod_ops WHERE id=?", ("cm:" + key,)
                ).fetchone()
                self.store.db.execute(
                    "UPDATE cm_violations SET escalation=? WHERE id=?",
                    ("review" if attempted else "skipped", key),
                )
                if attempted:
                    self.runtime.ad_killer.pause(chat, "待核查：累计处罚结果不明")
                self.runtime.report("community_escalation", exc)
                return "警告已记录；累计处罚未确认，请到违规记录核查。"

    async def decide(
        self, actor, case_id, version, decision, reason, token, prepared=None
    ):
        """Claim a case before handling it and preserve uncertain remote results.

        Args:
            actor: Authorized administrator.
            case_id: Case number.
            version: Previewed case version.
            decision: Ignore, warn, or an existing moderation operation.
            reason: Fixed public reason for warning.
            token: Single-use operation identifier.
            prepared: Immutable moderation preview, or warning policy versions.
        """
        row = self.store.db.execute(
            "SELECT * FROM cm_cases WHERE id=?", (case_id,)
        ).fetchone()
        if not row:
            raise Rejected("案件不存在")
        await self.runtime.moderation.check(actor, row["chat"], "view")
        if decision == "warn" and (
            not prepared
            or prepared["version"] != self.policy(row["chat"])["version"]
            or prepared["ak_version"]
            != self.runtime.ad_killer.policy(row["chat"])["version"]
        ):
            raise Rejected("警告或升级规则已变化，请重新预览")
        with self.store.tx() as db:
            if not db.execute(
                "UPDATE cm_cases SET status='processing',version=version+1,actor=?,op=? WHERE id=? AND version=? AND status='open'",
                (str(actor), token, case_id, version),
            ).rowcount:
                raise Rejected("案件已被处理或正在执行")
        try:
            if decision == "warn":
                result = await self.warn(
                    actor,
                    row["chat"],
                    row["target"],
                    reason,
                    "case:" + str(case_id),
                    row["message"],
                    expected=prepared,
                )
                violation = self.store.db.execute(
                    "SELECT escalation FROM cm_violations WHERE id=?",
                    ("case:" + str(case_id),),
                ).fetchone()
                status = (
                    "review" if violation and violation[0] == "review" else "closed"
                )
            elif decision == "ignore":
                result, status = "已忽略", "closed"
            elif decision in {"delete", "mute", "kick", "ban"}:
                if (
                    not prepared
                    or prepared["chat"] != row["chat"]
                    or prepared["op"] != decision
                    or str(prepared["target"])
                    != str(row["message"] if decision == "delete" else row["target"])
                ):
                    raise Rejected("请重新预览处罚")
                await self.runtime.moderation.execute(actor, prepared, "case:" + token)
                result, status = "处罚已受理", "closed"
            else:
                raise Rejected("处理方式无效")
        except BaseException:
            attempted = self.store.db.execute(
                "SELECT 1 FROM mod_ops WHERE id=?", ("case:" + token,)
            ).fetchone()
            warned = self.store.db.execute(
                "SELECT 1 FROM cm_violations WHERE id=?", ("case:" + str(case_id),)
            ).fetchone()
            self.store.db.execute(
                "UPDATE cm_cases SET status=?,outcome=? WHERE id=?",
                (
                    "review" if attempted or warned else "open",
                    "结果待核查" if attempted or warned else "未提交处罚，可重新预览",
                    case_id,
                ),
            )
            raise
        with self.store.tx() as db:
            db.execute(
                "UPDATE cm_cases SET status=?,outcome=?,closed=? WHERE id=?",
                (
                    status,
                    result,
                    self.store.clock() if status == "closed" else None,
                    case_id,
                ),
            )
            self.store.audit(
                db,
                actor,
                "community_case_closed",
                {"chat": row["chat"], "case": case_id},
            )

    def welcome_parts(self, chat, user, title):
        """Build plain-text welcome content and public deep-link buttons.

        Args:
            chat: Managed group.
            user: New member.
            title: Telegram group title.

        Returns:
            Plain text and inline keyboard rows.
        """
        config = self.policy(chat)["config"]
        text = ""
        if config["enabled"]["welcome"]:
            text = (
                config["welcome"]
                .replace("{name}", user.full_name[:60])
                .replace("{group}", title[:100])[:3000]
            )
        rows = []
        for kind, label in (("rules", "查看群规"), ("notes", "常用说明")):
            if config["enabled"][kind]:
                rows.append(
                    [
                        Button(
                            label,
                            url=f"https://t.me/{self.runtime.bot.username}?start=cm{kind}_{chat}",
                        )
                    ]
                )
        return text, rows

    async def welcome(self, chat, user, event, title):
        """Send at most one new-member greeting when verification did not do so.

        Args:
            chat: Receiving group.
            user: New member.
            event: Native service message ID.
            title: Group display title.
        """
        if user.is_bot:
            return
        policy = self.policy(chat)
        if not policy["config"]["enabled"]["welcome"]:
            return
        # An enabled verification flow owns its greeting, including failures.
        if self.runtime.join_verify.policy(chat)["enabled"]:
            return
        await self.runtime.moderation.check(policy["actor"], str(chat), "view")
        text, rows = self.welcome_parts(chat, user, title)
        with self.store.tx() as db:
            claimed = db.execute(
                "INSERT OR IGNORE INTO cm_welcome(chat,uid,event,status) VALUES(?,?,?,'sending')",
                (str(chat), str(user.id), event),
            ).rowcount
        if not claimed:
            return
        try:
            await self.runtime.bot.send_message(
                chat_id=chat,
                text=text or "欢迎加入！",
                parse_mode=None,
                reply_markup=InlineKeyboardMarkup(rows) if rows else None,
            )
            status = "sent"
        except BaseException:
            self.store.db.execute(
                "UPDATE cm_welcome SET status='unknown' WHERE chat=? AND uid=? AND event=?",
                (str(chat), str(user.id), event),
            )
            raise
        self.store.db.execute(
            "UPDATE cm_welcome SET status=? WHERE chat=? AND uid=? AND event=?",
            (status, str(chat), str(user.id), event),
        )

    def cleanup(self):
        """Clean closed private evidence in bounded transactions.

        Returns:
            None.
        """
        now = self.store.clock()
        if now - self.last_cleanup < 3600:
            return
        with self.store.tx() as db:
            ids = [
                row[0]
                for row in db.execute(
                    "SELECT id FROM cm_cases WHERE status='closed' AND closed<? AND (body IS NOT NULL OR EXISTS(SELECT 1 FROM cm_reports r WHERE r.case_id=cm_cases.id AND detail IS NOT NULL)) LIMIT 100",
                    (now - 30 * 86400,),
                )
            ]
            for case in ids:
                db.execute("UPDATE cm_cases SET body=NULL WHERE id=?", (case,))
                db.execute("UPDATE cm_reports SET detail=NULL WHERE case_id=?", (case,))
            count = db.execute(
                "DELETE FROM cm_tickets WHERE rowid IN (SELECT rowid FROM cm_tickets WHERE expires<? LIMIT 500)",
                (now - 86400,),
            ).rowcount
        self.last_cleanup = now if len(ids) < 100 and count < 500 else now - 3595
