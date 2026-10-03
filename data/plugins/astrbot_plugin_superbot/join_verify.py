"""Persistent post-join membership verification using the existing bot client."""

import asyncio
import json
import secrets

from telegram import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup

from .moderation import SEND
from .store import Rejected, encode


class JoinVerify:
    """Keep verification restrictions separate from moderation punishments."""

    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS jv_policies(
                chat TEXT PRIMARY KEY, actor TEXT NOT NULL, targets TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS jv_entries(
                token TEXT PRIMARY KEY,chat TEXT NOT NULL,uid TEXT NOT NULL,
                actor TEXT NOT NULL,targets TEXT NOT NULL,version INTEGER NOT NULL,
                status TEXT NOT NULL,expected TEXT,created REAL NOT NULL,
                error TEXT NOT NULL DEFAULT '',last_check REAL NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS jv_member ON jv_entries(chat,uid,created);
            UPDATE jv_entries SET status='review',error='restart_unknown'
                WHERE status IN ('restricting','releasing');
        """)

    def policy(self, chat):
        row = self.store.db.execute(
            "SELECT * FROM jv_policies WHERE chat=?", (str(chat),)
        ).fetchone()
        return (
            dict(row)
            if row
            else {
                "chat": str(chat),
                "actor": "",
                "targets": "[]",
                "enabled": 0,
                "version": 0,
            }
        )

    async def targets(self, targets):
        """Validate pinned identities and bot rights without joining any target.

        Args:
            targets: Target dictionaries containing stable IDs and join URLs.
        """
        for target in targets:
            info = await self.runtime.bot.get_chat(target["id"])
            if str(info.id) != target["id"] or info.type not in {
                "supergroup",
                "channel",
            }:
                raise Rejected("验证目标已变化，请重新设置")
            if target.get("username"):
                resolved = await self.runtime.bot.get_chat("@" + target["username"])
                if str(resolved.id) != target["id"]:
                    raise Rejected("验证链接已变化，请联系管理员")
            bot_member = await self.runtime.bot.get_chat_member(
                info.id, self.runtime.bot.id
            )
            if bot_member.status not in {"administrator", "creator"}:
                raise Rejected("机器人必须在每个验证目标中担任管理员")

    async def join(self, chat, user, event_id):
        """Restrict one verified new member after recording an operation claim.

        Args:
            chat: Receiving supergroup ID.
            user: Telegram user from the native new-members service event.
            event_id: Unique service message ID.
        """
        chat, uid = str(chat), str(user.id)
        if user.is_bot:
            return
        async with self.runtime.moderation.locks.setdefault(chat, asyncio.Lock()):
            policy = self.policy(chat)
            if not policy["enabled"] or not self.store.get("modules", {}).get(
                "moderation"
            ):
                return
            group = self.store.db.execute(
                "SELECT enabled FROM mod_groups WHERE chat=?", (chat,)
            ).fetchone()
            if not group or not group[0]:
                return
            # An event claim is permanent, including after successful verification.
            claim = f"jv:{chat}:{uid}:{event_id}"
            if self.store.db.execute(
                "SELECT 1 FROM audit WHERE action='join_verify_event' AND data=?",
                (encode({"event": claim}),),
            ).fetchone():
                return
            if self.store.db.execute(
                "SELECT 1 FROM jv_entries WHERE chat=? AND uid=? AND status IN ('pending','review','restricting','releasing')",
                (chat, uid),
            ).fetchone():
                return
            await self.runtime.moderation.check(policy["actor"], chat)
            targets = json.loads(policy["targets"])
            await self.targets(targets)
            member = await self.runtime.bot.get_chat_member(chat, user.id)
            if member.status != "member":
                return
            if self.policy(chat) != policy:
                return
            token = secrets.token_urlsafe(16)
            with self.store.tx() as db:
                db.execute(
                    "INSERT INTO jv_entries(token,chat,uid,actor,targets,version,status,created) VALUES(?,?,?,?,?,?,'restricting',?)",
                    (
                        token,
                        chat,
                        uid,
                        policy["actor"],
                        policy["targets"],
                        policy["version"],
                        self.store.clock(),
                    ),
                )
                self.store.audit(
                    db, policy["actor"], "join_verify_event", {"event": claim}
                )
            try:
                await self.runtime.bot.restrict_chat_member(
                    chat,
                    user.id,
                    ChatPermissions(**dict.fromkeys(SEND, False)),
                    until_date=0,
                    use_independent_chat_permissions=True,
                )
                current = await self.runtime.bot.get_chat_member(chat, user.id)
                if current.status != "restricted" or getattr(
                    current, "can_send_messages", True
                ):
                    raise Rejected("verification_restriction_unconfirmed")
                self.store.db.execute(
                    "UPDATE jv_entries SET status='pending',expected=? WHERE token=?",
                    (encode(current.to_dict()), token),
                )
            except BaseException as exc:
                self.store.db.execute(
                    "UPDATE jv_entries SET status='review',error=? WHERE token=?",
                    (type(exc).__name__, token),
                )
                raise
            buttons = [
                [
                    InlineKeyboardButton(
                        "加入 " + target["title"][:30], url=target["url"]
                    )
                ]
                for target in targets
            ]
            buttons.append(
                [InlineKeyboardButton("我已加入，验证", callback_data="jv:" + token)]
            )
            greeting = ""
            if hasattr(self.runtime, "community"):
                content_policy = self.runtime.community.policy(chat)
                if content_policy["actor"]:
                    try:
                        await self.runtime.moderation.check(
                            content_policy["actor"], chat, "view"
                        )
                        group = self.runtime.moderation.row(chat)
                        greeting, extra = self.runtime.community.welcome_parts(
                            chat, user, group["title"]
                        )
                        buttons.extend(extra)
                    except Exception as exc:
                        self.runtime.report("community_welcome_permissions", exc)
            try:
                await self.runtime.bot.send_message(
                    chat_id=chat,
                    text=(greeting or f"{user.full_name[:60]}，欢迎加入！")
                    + "\n先加入下方频道或群，再点击验证即可发言。\n未验证暂时不能发言；遇到问题请联系群管理员。",
                    parse_mode=None,
                    reply_markup=InlineKeyboardMarkup(buttons),
                )
            except Exception as exc:
                self.store.db.execute(
                    "UPDATE jv_entries SET error=? WHERE token=?",
                    ("notice_" + type(exc).__name__, token),
                )
                self.runtime.report("join_verify_notice", exc)

    async def verify(self, token, uid, chat, actor=None):
        """Release only a matching verification restriction, never other penalties.

        Args:
            token: Opaque persistent verification token.
            uid: Member who owns the restriction.
            chat: Bound receiving group ID.
            actor: Optional authorized administrator performing manual release.

        Returns:
            A short user-facing result.
        """
        chat, uid = str(chat), str(uid)
        async with self.runtime.moderation.locks.setdefault(chat, asyncio.Lock()):
            row = self.store.db.execute(
                "SELECT * FROM jv_entries WHERE token=?", (token,)
            ).fetchone()
            if not row or row["chat"] != chat or row["uid"] != uid:
                raise Rejected("这个验证按钮不属于你")
            if row["status"] == "complete":
                return "已验证，可以正常发言。"
            if row["status"] != "pending":
                raise Rejected("操作结果待核查，请联系群管理员，不要重复提交")
            now = self.store.clock()
            if not actor and now - row["last_check"] < 5:
                raise Rejected("稍等几秒再验证")
            self.store.db.execute(
                "UPDATE jv_entries SET last_check=? WHERE token=?", (now, token)
            )
            await self.runtime.moderation.check(actor or row["actor"], chat)
            if not actor:
                targets = json.loads(row["targets"])
                await self.targets(targets)
                for target in targets:
                    member = await self.runtime.bot.get_chat_member(
                        target["id"], int(uid)
                    )
                    if member.status not in {
                        "creator",
                        "administrator",
                        "member",
                    } and not (
                        member.status == "restricted"
                        and getattr(member, "is_member", False)
                    ):
                        raise Rejected(
                            "还没有加入全部指定频道或群；审批中也需要等待通过"
                        )
            current = await self.runtime.bot.get_chat_member(chat, int(uid))
            if (
                current.status != "restricted"
                or encode(current.to_dict()) != row["expected"]
            ):
                raise Rejected("当前限制已变化，请管理员核查，不能自动解除")
            if (
                self.store.db.execute(
                    "SELECT 1 FROM mod_restrictions WHERE chat=? AND target=?",
                    (chat, uid),
                ).fetchone()
                or self.store.db.execute(
                    "SELECT 1 FROM mod_ops WHERE chat=? AND status IN ('unknown','executing')",
                    (chat,),
                ).fetchone()
            ):
                raise Rejected("有其他处罚或待核查群管操作，不能自动解除")
            await self.runtime.moderation.check(actor or row["actor"], chat)
            with self.store.tx() as db:
                db.execute(
                    "UPDATE jv_entries SET status='releasing' WHERE token=?", (token,)
                )
                self.store.audit(
                    db,
                    actor or uid,
                    "join_verify_release",
                    {"chat": chat, "uid": uid, "manual": bool(actor)},
                )
            try:
                await self.runtime.bot.restrict_chat_member(
                    chat,
                    int(uid),
                    ChatPermissions.all_permissions(),
                    use_independent_chat_permissions=True,
                )
                current = await self.runtime.bot.get_chat_member(chat, int(uid))
                if current.status not in {"member", "administrator", "creator"}:
                    raise Rejected("verification_release_unconfirmed")
                self.store.db.execute(
                    "UPDATE jv_entries SET status='complete',error='' WHERE token=?",
                    (token,),
                )
            except BaseException as exc:
                self.store.db.execute(
                    "UPDATE jv_entries SET status='review',error=? WHERE token=?",
                    (type(exc).__name__, token),
                )
                raise
            return "验证通过，可以发言了。"
