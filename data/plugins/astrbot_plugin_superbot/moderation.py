"""Private, dual-authority moderation with durable conservative execution."""

import asyncio
import json
import re
from contextlib import nullcontext
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import ChatPermissions

from .store import Rejected, encode

TZ = ZoneInfo("Asia/Shanghai")
SEND = tuple(
    k
    for k in ChatPermissions.__slots__
    if k.startswith("can_send_")
    or k in {"can_add_web_page_previews", "can_react_to_messages"}
)
ACTIONS = {
    "mute": "限时禁言",
    "mute_forever": "永久禁言",
    "unmute": "解禁",
    "kick": "踢出（可重进）",
    "ban": "封禁",
    "unban": "解除封禁",
    "delete": "删除消息",
    "mute_all": "全群禁言",
    "unmute_all": "恢复全群发言",
}


def boundary(start, end, now):
    """Return the next strictly future daily boundary.

    Args:
        start: Shanghai HH:MM mute time.
        end: Shanghai HH:MM restore time.
        now: Unix timestamp.

    Returns:
        Timestamp and action pair.
    """
    if start == end or not all(
        re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", x) for x in (start, end)
    ):
        raise Rejected("时段需为不同的 HH:MM，北京时间")
    local = datetime.fromtimestamp(now, TZ)
    choices = []
    for value, action in ((start, "mute_all"), (end, "unmute_all")):
        hour, minute = map(int, value.split(":"))
        dt = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if dt.timestamp() <= now:
            dt += timedelta(days=1)
        choices.append((dt.timestamp(), action))
    return min(choices)


class Moderation:
    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        self.locks = {}
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS mod_groups(chat TEXT PRIMARY KEY,title TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 0,version INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS mod_grants(token TEXT PRIMARY KEY,groups_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS mod_acl(uid TEXT NOT NULL,chat TEXT NOT NULL,PRIMARY KEY(uid,chat));
            CREATE TABLE IF NOT EXISTS mod_messages(chat TEXT NOT NULL,message INTEGER NOT NULL,sender TEXT NOT NULL,PRIMARY KEY(chat,message));
            CREATE TABLE IF NOT EXISTS mod_members(chat TEXT NOT NULL,uid TEXT NOT NULL,name TEXT NOT NULL,username TEXT NOT NULL,seen REAL NOT NULL,PRIMARY KEY(chat,uid));
            CREATE INDEX IF NOT EXISTS mod_members_username ON mod_members(chat,username);
            INSERT OR IGNORE INTO mod_members(chat,uid,name,username,seen) SELECT chat,sender,sender,'',0 FROM mod_messages WHERE sender<>'';
            CREATE TABLE IF NOT EXISTS mod_ops(id TEXT PRIMARY KEY,chat TEXT NOT NULL,actor TEXT NOT NULL,action TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL,step TEXT NOT NULL,at REAL NOT NULL,error TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS mod_restrictions(chat TEXT NOT NULL,target TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(chat,target));
            CREATE TABLE IF NOT EXISTS mod_schedules(chat TEXT PRIMARY KEY,actor TEXT NOT NULL,start TEXT NOT NULL,end TEXT NOT NULL,enabled INTEGER NOT NULL,version INTEGER NOT NULL,next REAL NOT NULL,action TEXT NOT NULL,error TEXT NOT NULL DEFAULT '');
            UPDATE mod_ops SET status='unknown',error='restart_unknown' WHERE status='executing';
            UPDATE mod_schedules SET enabled=0,error='restart_review',version=version+1 WHERE enabled=1 AND chat IN (SELECT chat FROM mod_ops WHERE status='unknown');
        """)
        self.store.db.execute(
            "UPDATE mod_schedules SET enabled=0,version=version+1,error='restart_missed_boundary' WHERE enabled=1 AND next<=?",
            (self.store.clock(),),
        )

    def row(self, chat):
        row = self.store.db.execute(
            "SELECT * FROM mod_groups WHERE chat=?", (str(chat),)
        ).fetchone()
        if not row:
            raise Rejected("群未登记")
        return dict(row)

    def remember(self, chat, user):
        if not user or user.is_bot:
            return
        name = " ".join((getattr(user, "full_name", "") or str(user.id)).split())[:80]
        username = (getattr(user, "username", None) or "").lower()
        self.store.db.execute(
            "INSERT INTO mod_members(chat,uid,name,username,seen) VALUES(?,?,?,?,?) ON CONFLICT(chat,uid) DO UPDATE SET name=excluded.name,username=excluded.username,seen=excluded.seen",
            (str(chat), str(user.id), name, username, self.store.clock()),
        )

    async def check(self, uid, chat, action="mute", enabled=True):
        from .tenants import platform_group

        if not platform_group(self.store, chat):
            if not self.store.get("modules", {}).get("moderation"):
                raise Rejected("群管模块尚未开启")
            permission = (
                None
                if action == "view"
                else (
                    "can_delete_messages"
                    if action == "delete"
                    else "can_restrict_members"
                )
            )
            await self.runtime.tenants.verify(uid, chat, "moderation", permission)
            if not self.store.get("modules", {}).get("moderation"):
                raise Rejected("群管模块已关闭")
            if enabled and not self.row(chat)["enabled"]:
                raise Rejected("此群未启用")
            info = await self.runtime.bot.get_chat(chat)
            return info, []
        self.store.require(uid, "moderation", chat=chat)
        if not self.store.get("modules", {}).get("moderation"):
            raise Rejected("群管模块尚未开启")
        group = self.row(chat)
        if enabled and not group["enabled"]:
            raise Rejected("此群未启用")
        if (
            not self.store.allowed(uid, "manager")
            and not self.store.db.execute(
                "SELECT 1 FROM mod_acl WHERE uid=? AND chat=?", (str(uid), str(chat))
            ).fetchone()
        ):
            raise Rejected("没有此群的授权")
        if not self.runtime.application or not self.runtime.application.running:
            raise Rejected("机器人离线")
        bot = self.runtime.bot
        info = await bot.get_chat(chat)
        if info.type != "supergroup":
            raise Rejected("首期仅支持 Telegram 超级群")
        members = [
            await bot.get_chat_member(chat, int(uid)),
            await bot.get_chat_member(chat, bot.id),
        ]
        permission = (
            "can_delete_messages" if action == "delete" else "can_restrict_members"
        )
        for index, member in enumerate(members):
            if member.status != "creator" and (
                member.status != "administrator"
                or (action != "view" and not getattr(member, permission, False))
            ):
                if index == 1:
                    label = (
                        "删除消息" if action == "delete" else "封禁成员（含禁言、踢出）"
                    )
                    raise Rejected("请把我设置为管理员，并赋予相关权限：" + label)
                raise Rejected("你在目标群缺少对应 Telegram 管理权限")
        # Recheck local revocation after network awaits.
        self.store.require(uid, "moderation")
        latest = self.row(chat)
        if latest["version"] != group["version"] or not self.store.get(
            "modules", {}
        ).get("moderation"):
            raise Rejected("群配置已变化，请重新预览")
        if (
            not self.store.allowed(uid, "manager")
            and not self.store.db.execute(
                "SELECT 1 FROM mod_acl WHERE uid=? AND chat=?", (str(uid), str(chat))
            ).fetchone()
        ):
            raise Rejected("群授权已撤销")
        return info, [m.to_dict() for m in members]

    async def preview(self, uid, chat, action, target="", minutes=0):
        info, rights = await self.check(uid, chat, action)
        payload = {
            "chat": str(chat),
            "op": action,
            "target": str(target),
            "minutes": minutes,
            "version": self.row(chat)["version"],
            "rights": rights,
        }
        if action in {"mute", "mute_forever", "kick", "ban", "unmute", "unban"}:
            if (
                not str(target).isascii()
                or not str(target).isdigit()
                or int(target) <= 0
                or int(target) == self.runtime.bot.id
            ):
                raise Rejected("请输入有效成员数字 UID")
            member = await self.runtime.bot.get_chat_member(chat, int(target))
            if member.status in {"creator", "administrator"}:
                raise Rejected("禁止处罚群主或管理员")
            payload["member"] = member.to_dict()
            if action in {"mute", "mute_forever"} and member.status != "member":
                raise Rejected("只允许禁言当前普通成员；不能覆盖已有个人限制")
            if action == "mute" and not 1 <= minutes <= 365 * 1440:
                raise Rejected("禁言期限为 1 分钟至 365 天")
            if action in {"unmute", "unban"}:
                saved = self.store.db.execute(
                    "SELECT payload FROM mod_restrictions WHERE chat=? AND target=?",
                    (str(chat), str(target)),
                ).fetchone()
                if not saved:
                    raise Rejected("没有本插件可追溯的处罚，拒绝解除")
                old = json.loads(saved[0])
                expected = "restricted" if action == "unmute" else "kicked"
                if member.status != expected or old.get("member") != encode(
                    member.to_dict()
                ):
                    raise Rejected("当前限制与记录不符，请人工核查")
        if action == "delete" and (not str(target).isdigit() or int(target) <= 0):
            raise Rejected("消息编号无效")
        if action == "delete":
            sender = self.store.db.execute(
                "SELECT sender FROM mod_messages WHERE chat=? AND message=?",
                (str(chat), int(target)),
            ).fetchone()
            if not sender or not sender[0] or int(sender[0]) == self.runtime.bot.id:
                raise Rejected(
                    "无法核实发送者；仅支持启用群管后机器人实际收到的普通成员消息"
                )
            member = await self.runtime.bot.get_chat_member(chat, int(sender[0]))
            if member.status in {"creator", "administrator"}:
                raise Rejected("不删除群主或管理员的消息")
            payload["member"] = member.to_dict()
        if action in {"mute_all", "unmute_all"}:
            permissions = info.permissions.to_dict() if info.permissions else {}
            payload["permissions"] = permissions
            saved = self.store.db.execute(
                "SELECT payload FROM mod_restrictions WHERE chat=? AND target='all'",
                (str(chat),),
            ).fetchone()
            if action == "mute_all" and (
                saved or not permissions.get("can_send_messages")
            ):
                raise Rejected("群已有禁言或恢复记录，请先核查")
            if action == "unmute_all":
                if not saved:
                    raise Rejected("没有本插件全群禁言记录")
                old = json.loads(saved[0])
                if any(
                    permissions.get(k, False) != old["expected"].get(k, False)
                    for k in SEND
                ):
                    raise Rejected("群权限已被修改，暂停恢复，请人工核查")
                payload["restore"] = old["before"]
        return payload

    async def execute(self, uid, payload, op_id, scheduled=False, locked=False):
        chat, action = payload["chat"], payload["op"]
        async with (
            nullcontext() if locked else self.locks.setdefault(chat, asyncio.Lock())
        ):
            if self.store.db.execute(
                "SELECT 1 FROM mod_ops WHERE id=?", (op_id,)
            ).fetchone():
                raise Rejected("操作已提交，请查看审计")
            if payload["version"] != self.row(chat)["version"]:
                raise Rejected("群配置已变化，请重新预览")
            if not scheduled and action in {"mute_all", "unmute_all"}:
                self.pause(chat, "manual_override")
            current = await self.preview(
                uid, chat, action, payload.get("target", ""), payload.get("minutes", 0)
            )
            self.store.require(uid, "moderation")
            if (
                not self.store.get("modules", {}).get("moderation")
                or self.row(chat)["version"] != payload["version"]
            ):
                raise Rejected("群配置已变化")
            if (
                not self.store.allowed(uid, "manager")
                and not self.store.db.execute(
                    "SELECT 1 FROM mod_acl WHERE uid=? AND chat=?", (str(uid), chat)
                ).fetchone()
            ):
                raise Rejected("群授权已撤销")
            if scheduled:
                rule = self.store.db.execute(
                    "SELECT * FROM mod_schedules WHERE chat=?", (chat,)
                ).fetchone()
                if (
                    not rule
                    or not rule["enabled"]
                    or rule["version"] != payload["schedule_version"]
                    or self.store.clock() - rule["next"] > 15
                ):
                    raise Rejected("定时计划已暂停、变更或错过边界")
            if any(current.get(k) != payload.get(k) for k in ("member", "permissions")):
                raise Rejected("目标状态已变化，请重新预览")
            if self.store.db.execute(
                "SELECT 1 FROM mod_ops WHERE chat=? AND status='unknown'", (chat,)
            ).fetchone():
                raise Rejected("该群有未知结果，需先人工核查")
            if payload.get("case_guard"):
                guard = payload["case_guard"]
                case = self.store.db.execute(
                    "SELECT status,version FROM cm_cases WHERE id=?", (guard["id"],)
                ).fetchone()
                if (
                    not case
                    or case["status"] != "processing"
                    or case["version"] != guard["version"]
                ):
                    raise Rejected("举报消息或案件状态已变化，停止自动处罚")
                ai_policy = self.store.db.execute(
                    "SELECT enabled,version FROM cm_ai_policy WHERE chat=?", (chat,)
                ).fetchone()
                content = self.store.db.execute(
                    "SELECT version,config FROM cm_config WHERE chat=?", (chat,)
                ).fetchone()
                if (
                    not ai_policy
                    or not ai_policy["enabled"]
                    or ai_policy["version"] != guard["policy_version"]
                    or not content
                    or content["version"] != guard["content_version"]
                    or not json.loads(content["config"])["enabled"]["reports"]
                ):
                    raise Rejected("自动审理或群规已变化，停止处罚")
            self.store.db.execute(
                "INSERT INTO mod_ops(id,chat,actor,action,payload,status,step,at) VALUES(?,?,?,?,?,'executing','submitted',?)",
                (op_id, chat, str(uid), action, encode(current), self.store.clock()),
            )
            bot, target = self.runtime.bot, int(payload.get("target") or 0)
            try:
                if action in {"mute", "mute_forever"}:
                    await bot.restrict_chat_member(
                        chat,
                        target,
                        ChatPermissions(**dict.fromkeys(SEND, False)),
                        until_date=int(self.store.clock() + payload["minutes"] * 60)
                        if action == "mute"
                        else 0,
                        use_independent_chat_permissions=True,
                    )
                elif action == "unmute":
                    await bot.restrict_chat_member(
                        chat,
                        target,
                        ChatPermissions.all_permissions(),
                        use_independent_chat_permissions=True,
                    )
                elif action in {"ban", "kick"}:
                    await bot.ban_chat_member(chat, target, revoke_messages=True)
                    self.store.db.execute(
                        "UPDATE mod_ops SET step='banned' WHERE id=?", (op_id,)
                    )
                    if action == "kick":
                        await self.check(uid, chat)
                        await bot.unban_chat_member(chat, target, only_if_banned=True)
                elif action == "unban":
                    await bot.unban_chat_member(chat, target, only_if_banned=True)
                elif action == "delete":
                    await bot.delete_message(chat, target)
                else:
                    permissions = dict(current["permissions"])
                    for key in SEND:
                        permissions[key] = (
                            current.get("restore", {}).get(key, False)
                            if action == "unmute_all"
                            else False
                        )
                    await bot.set_chat_permissions(
                        chat,
                        ChatPermissions.de_json(permissions, bot),
                        use_independent_chat_permissions=True,
                    )
                    if action == "mute_all":
                        info = await bot.get_chat(chat)
                        state = {
                            "before": current["permissions"],
                            "expected": info.permissions.to_dict(),
                        }
                        self.save_restriction(chat, "all", state)
                if action in {"mute", "mute_forever", "ban"}:
                    member = await bot.get_chat_member(chat, target)
                    self.save_restriction(
                        chat, str(target), {"member": encode(member.to_dict())}
                    )
                if action in {"unmute", "unban", "unmute_all"}:
                    self.store.db.execute(
                        "DELETE FROM mod_restrictions WHERE chat=? AND target=?",
                        (chat, "all" if action == "unmute_all" else str(target)),
                    )
                self.store.db.execute(
                    "UPDATE mod_ops SET status='accepted',step='done' WHERE id=?",
                    (op_id,),
                )
            except BaseException as exc:
                self.store.db.execute(
                    "UPDATE mod_ops SET status='unknown',error=? WHERE id=?",
                    (type(exc).__name__, op_id),
                )
                self.pause(chat, "unknown_result")
                raise

    def save_restriction(self, chat, target, payload):
        self.store.db.execute(
            "INSERT INTO mod_restrictions(chat,target,payload) VALUES(?,?,?) ON CONFLICT(chat,target) DO UPDATE SET payload=excluded.payload",
            (chat, target, encode(payload)),
        )

    def pause(self, chat, reason):
        self.store.db.execute(
            "UPDATE mod_schedules SET enabled=0,version=version+1,error=? WHERE chat=?",
            (reason, str(chat)),
        )

    async def tick(self):
        rows = self.store.db.execute(
            "SELECT * FROM mod_schedules WHERE enabled=1"
        ).fetchall()
        for row in rows:
            chat = row["chat"]
            try:
                if not self.store.get("modules", {}).get("moderation"):
                    self.pause(chat, "module_disabled")
                    continue
                if self.store.clock() < row["next"]:
                    continue
                if self.store.clock() - row["next"] > 15:
                    raise Rejected("missed_boundary")
                payload = await self.preview(row["actor"], chat, row["action"])
                payload["schedule_version"] = row["version"]
                fresh = self.store.db.execute(
                    "SELECT * FROM mod_schedules WHERE chat=?", (chat,)
                ).fetchone()
                if not fresh["enabled"] or fresh["version"] != row["version"]:
                    continue
                await self.execute(
                    row["actor"],
                    payload,
                    f"schedule:{chat}:{row['version']}:{row['next']}:{row['action']}",
                    scheduled=True,
                )
                stamp, action = boundary(row["start"], row["end"], self.store.clock())
                self.store.db.execute(
                    "UPDATE mod_schedules SET next=?,action=?,error='' WHERE chat=? AND version=? AND enabled=1",
                    (stamp, action, chat, row["version"]),
                )
            except Exception as exc:
                self.store.db.execute(
                    "UPDATE mod_schedules SET enabled=0,version=version+1,error=? WHERE chat=? AND version=?",
                    (
                        type(exc).__name__
                        + ":"
                        + (
                            str(exc)
                            if isinstance(exc, Rejected)
                            else "operation_failed"
                        ),
                        chat,
                        row["version"],
                    ),
                )
