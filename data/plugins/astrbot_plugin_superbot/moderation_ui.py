"""Explicit private moderation forms and bound confirmations."""

import asyncio
import hashlib
import re
from datetime import datetime

from telegram.error import BadRequest, ChatMigrated, Forbidden

from .moderation import ACTIONS, TZ, boundary
from .store import Rejected, encode

MEMBER_ACTIONS = {"mute", "mute_forever", "unmute", "kick", "ban", "unban"}


class ModerationUI:
    def __init__(self, ui):
        self.ui, self.runtime, self.store = ui, ui.runtime, ui.store

    async def action(self, update, payload, token=""):
        uid = str(update.effective_user.id)
        self.store.require(uid, "moderation", chat=payload.get("chat"))
        self.store.clear_dialog(uid)
        mod = self.runtime.moderation
        action = payload["action"]
        chat = str(payload.get("chat", ""))
        if action.startswith("mod_multiplier"):
            from .group_multiplier import action as multiplier_action

            return await multiplier_action(self.ui, update, payload, token)
        if action.startswith("mod_tpl_"):
            from .group_template import action as template_action

            return await template_action(self.ui, update, payload, token)
        if action.startswith("mod_gb_"):
            return await self.runtime.group_game.broadcast.configure(
                self.ui, update, payload, token
            )
        if action.startswith("mod_jv_"):
            from .join_verify_ui import action as join_verify_action

            return await join_verify_action(self, update, payload, token)
        if action.startswith("mod_ak_"):
            from .ad_killer_ui import action as ad_killer_action

            return await ad_killer_action(self, update, payload, token)
        back = [("换个群", {"action": "mod_home"})]
        if action in {"mod_members", "mod_member", "mod_duration"}:
            kind = payload["kind"]
            if kind not in MEMBER_ACTIONS:
                raise Rejected("无效成员操作")
            await mod.check(uid, chat)
            self.store.clear_dialog(uid)
            page = max(0, int(payload.get("page", 0)))
            return_members = (
                "返回成员列表",
                {"action": "mod_members", "kind": kind, "chat": chat, "page": page},
            )
            if action == "mod_members":
                count = self.store.db.execute(
                    "SELECT COUNT(*) FROM mod_members WHERE chat=?", (chat,)
                ).fetchone()[0]
                page = min(page, max(0, (count - 1) // 8))
                rows = self.store.db.execute(
                    "SELECT * FROM mod_members WHERE chat=? ORDER BY uid LIMIT 8 OFFSET ?",
                    (chat, page * 8),
                ).fetchall()
                for row in rows:
                    try:
                        member = await self.runtime.bot.get_chat_member(
                            chat, int(row["uid"])
                        )
                        mod.remember(chat, getattr(member, "user", None))
                    except Exception:
                        pass
                rows = self.store.db.execute(
                    "SELECT * FROM mod_members WHERE chat=? ORDER BY uid LIMIT 8 OFFSET ?",
                    (chat, page * 8),
                ).fetchall()
                buttons = [
                    (
                        r["name"][:24]
                        + (" @" + r["username"] if r["username"] else "")
                        + " ·"
                        + r["uid"][-4:],
                        {
                            "action": "mod_member",
                            "kind": kind,
                            "chat": chat,
                            "target": r["uid"],
                            "page": page,
                        },
                    )
                    for r in rows
                ]
                if page:
                    buttons.append(
                        (
                            "上一页",
                            {
                                "action": "mod_members",
                                "kind": kind,
                                "chat": chat,
                                "page": page - 1,
                            },
                        )
                    )
                if count > (page + 1) * 8:
                    buttons.append(
                        (
                            "下一页",
                            {
                                "action": "mod_members",
                                "kind": kind,
                                "chat": chat,
                                "page": page + 1,
                            },
                        )
                    )
                buttons += [
                    (
                        "刷新",
                        {
                            "action": "mod_members",
                            "kind": kind,
                            "chat": chat,
                            "page": page,
                        },
                    ),
                    ("返回上一层", {"action": "mod_group", "chat": chat}),
                ]
                self.store.dialog(
                    uid,
                    {"moderation": True, "kind": "member_lookup:" + kind, "chat": chat},
                )
                return await self.ui.render(
                    update,
                    f"{ACTIONS[kind]} · 选择成员 · 第{page + 1}/{max(1, (count + 7) // 8)}页\n也可发送 @用户名 或 t.me/用户名。\n这里是机器人已识别的成员，不是完整群名单；找不到时请让对方在群里发消息，或回复对方消息并 @机器人。",
                    buttons,
                )
            target = str(payload["target"])
            if not self.store.db.execute(
                "SELECT 1 FROM mod_members WHERE chat=? AND uid=?", (chat, target)
            ).fetchone():
                raise Rejected("未找到此群成员，请刷新列表")
            member = await self.runtime.bot.get_chat_member(chat, int(target))
            if member.status in {"creator", "administrator"}:
                raise Rejected("不能对群主或管理员执行处罚")
            if action == "mod_duration":
                return await self.preview(
                    update, kind, chat, target + " " + str(payload["minutes"])
                )
            if kind == "mute":
                self.store.dialog(
                    uid,
                    {
                        "moderation": True,
                        "kind": "member_minutes:" + target,
                        "chat": chat,
                    },
                )
                buttons = [
                    (
                        label,
                        {
                            "action": "mod_duration",
                            "kind": kind,
                            "chat": chat,
                            "target": target,
                            "minutes": minutes,
                            "page": page,
                        },
                    )
                    for label, minutes in [
                        ("10分钟", 10),
                        ("1小时", 60),
                        ("1天", 1440),
                        ("7天", 10080),
                    ]
                ]
                return await self.ui.render(
                    update,
                    f"已选成员 {target}，禁言多久？\n也可直接输入分钟数。",
                    buttons + [return_members],
                )
            return await self.preview(update, kind, chat, target)
        if action == "mod_home":
            pending = bool(payload.get("pending", False))
            rows = self.store.db.execute(
                "SELECT * FROM platform_mod_groups WHERE enabled=? ORDER BY chat",
                (0 if pending else 1,),
            ).fetchall()
            rows = [
                r
                for r in rows
                if self.store.allowed(uid, "manager")
                or self.store.db.execute(
                    "SELECT 1 FROM mod_acl WHERE uid=? AND chat=?", (uid, r["chat"])
                ).fetchone()
            ]
            visible = []
            unavailable = 0
            for row in rows:
                try:
                    actor = await self.runtime.bot.get_chat_member(
                        row["chat"], int(uid)
                    )
                    bot_member = await self.runtime.bot.get_chat_member(
                        row["chat"], self.runtime.bot.id
                    )
                    if actor.status in {
                        "creator",
                        "administrator",
                    } and bot_member.status in {"member", "administrator", "creator"}:
                        visible.append(row)
                except ChatMigrated:
                    # The new supergroup is separately discovered and authorized.
                    continue
                except Forbidden:
                    continue
                except BadRequest as exc:
                    if any(
                        value in str(exc).lower()
                        for value in (
                            "chat not found",
                            "user not found",
                            "member not found",
                        )
                    ):
                        continue
                    unavailable += 1
                except Exception:
                    unavailable += 1
            rows = visible
            page = max(0, int(payload.get("page", 0)))
            buttons = [
                (
                    r["title"] + ("（已启用）" if r["enabled"] else "（待启用）"),
                    {"action": "mod_group", "chat": r["chat"]},
                )
                for r in rows[page * 10 : (page + 1) * 10]
            ]
            if page:
                buttons.append(
                    (
                        "上一页",
                        {"action": "mod_home", "page": page - 1, "pending": pending},
                    )
                )
            if len(rows) > (page + 1) * 10:
                buttons.append(
                    (
                        "下一页",
                        {"action": "mod_home", "page": page + 1, "pending": pending},
                    )
                )
            if self.store.allowed(uid, "manager"):
                buttons.append(("添加群", {"action": "mod_form", "kind": "add"}))
            if uid == self.store.owner:
                buttons.append(("授权指定群", {"action": "mod_form", "kind": "grant"}))
            return await self.ui.render(
                update,
                (
                    (
                        "待启用群：仅自动发现，不代表由你邀请，也没有自动获得管理授权。"
                        if pending
                        else "已启用的管理群："
                    )
                    if rows
                    else (
                        "暂无你可管理的待启用群。"
                        if pending
                        else "暂无你可管理的已启用群，可查看待启用群或手动添加。"
                    )
                )
                + ("\n部分群查询失败，稍后刷新再试。" if unavailable else ""),
                buttons
                + [
                    ("刷新群列表", {"action": "mod_home", "pending": pending}),
                    (
                        "返回已启用群" if pending else "查看待启用群",
                        {"action": "mod_home", "pending": not pending},
                    ),
                ]
                + (
                    [
                        (
                            "开启群管",
                            {
                                "action": "module_preview",
                                "key": "moderation",
                                "enabled": True,
                            },
                        )
                    ]
                    if self.store.allowed(uid, "manager")
                    and not self.store.get("modules", {}).get("moderation")
                    else []
                ),
            )
        if action in {"mod_group", "mod_schedule", "mod_records"}:
            info = await self.runtime.bot.get_chat(chat)
            if info.type == "group":
                actor = await self.runtime.bot.get_chat_member(chat, int(uid))
                scoped = (
                    self.store.allowed(uid, "manager")
                    or self.store.db.execute(
                        "SELECT 1 FROM mod_acl WHERE uid=? AND chat=?", (uid, chat)
                    ).fetchone()
                )
                if not scoped or actor.status not in {"creator", "administrator"}:
                    raise Rejected("没有此群管理权限")
                return await self.ui.render(
                    update,
                    f"已识别：{info.title}\n这是普通群组。个人禁言等功能需要 Telegram 超级群，转换后重新启用群管即可。",
                    back,
                )
            await mod.check(
                uid, chat, "mute" if action == "mod_schedule" else "view", enabled=False
            )
            group = mod.row(chat)
            schedule = self.store.db.execute(
                "SELECT * FROM mod_schedules WHERE chat=?", (chat,)
            ).fetchone()
            recent = self.store.db.execute(
                "SELECT action,status,step,error FROM mod_ops WHERE chat=? ORDER BY at DESC LIMIT 5",
                (chat,),
            ).fetchall()
            text = f"{group['title']} · {'已启用' if group['enabled'] else '已停用'}"
            if action == "mod_records":
                statuses = {
                    "accepted": "已受理",
                    "unknown": "待核查",
                    "executing": "执行中",
                }
                text += "\n" + (
                    "\n".join(
                        f"{ACTIONS.get(r['action'], r['action'])}：{statuses.get(r['status'], r['status'])}"
                        + ("（需维护人员核查）" if r["error"] else "")
                        for r in recent
                    )
                    or "还没有操作记录。"
                )
                return await self.ui.render(
                    update, text, [("返回", {"action": "mod_group", "chat": chat})]
                )
            if action == "mod_schedule":
                text += "\n" + (
                    f"每天 {schedule['start']}—{schedule['end']}（北京时间）\n{'运行中' if schedule['enabled'] else '已暂停'}"
                    if schedule and schedule["error"] != "deleted"
                    else "还没设置定时禁言。"
                )
                if schedule and schedule["error"]:
                    text += "\n最近状态：" + {
                        "paused": "手动暂停",
                        "deleted": "计划已删除",
                        "manual_override": "人工操作后暂停",
                        "group_disabled": "群已停用",
                        "module_disabled": "群管已关闭",
                    }.get(schedule["error"], "执行中断，请先核查群状态")
                buttons = [
                    (
                        "设置时段",
                        {"action": "mod_form", "kind": "schedule", "chat": chat},
                    )
                ]
                if schedule and schedule["error"] != "deleted":
                    kind = "pause" if schedule["enabled"] else "resume"
                    buttons += [
                        (
                            "暂停" if schedule["enabled"] else "恢复",
                            {"action": "mod_form", "kind": kind, "chat": chat},
                        ),
                        (
                            "删除计划",
                            {"action": "mod_form", "kind": "remove", "chat": chat},
                        ),
                    ]
                return await self.ui.render(
                    update,
                    text,
                    buttons + [("返回", {"action": "mod_group", "chat": chat})],
                )
            buttons = [
                (label, {"action": "mod_form", "kind": op, "chat": chat})
                for op, label in ACTIONS.items()
            ]
            buttons += [
                ("定时禁言", {"action": "mod_schedule", "chat": chat}),
                ("操作记录", {"action": "mod_records", "chat": chat}),
            ]
            if group["enabled"]:
                buttons.append(
                    ("加拿大28当前倍率", {"action": "mod_multiplier", "chat": chat})
                )
                if self.store.allowed(uid, "manager"):
                    buttons.append(
                        ("超管群配置模板", {"action": "mod_tpl_home", "chat": chat})
                    )
                buttons.append(("广告杀手", {"action": "mod_ak_home", "chat": chat}))
                buttons.append(("入群验证", {"action": "mod_jv_home", "chat": chat}))
                if hasattr(self.runtime, "community"):
                    buttons.append(
                        ("套用基础配置", {"action": "cm_template", "chat": chat})
                    )
                    count = self.store.db.execute(
                        "SELECT COUNT(*) FROM cm_cases WHERE chat=? AND status='open'",
                        (chat,),
                    ).fetchone()[0]
                    buttons += [
                        (f"举报处理（{count}）", {"action": "cm_cases", "chat": chat}),
                        (
                            "欢迎与群规",
                            {
                                "action": "cm_settings",
                                "chat": chat,
                                "section": "content",
                            },
                        ),
                        ("违规记录", {"action": "cm_violations", "chat": chat}),
                        (
                            "常用说明",
                            {"action": "cm_settings", "chat": chat, "section": "notes"},
                        ),
                        ("日志设置", {"action": "cm_logs", "chat": chat}),
                    ]
            if self.store.allowed(uid, "manager"):
                buttons.append(
                    (
                        "停用此群" if group["enabled"] else "启用此群",
                        {
                            "action": "mod_form",
                            "kind": "disable" if group["enabled"] else "enable",
                            "chat": chat,
                        },
                    )
                )
            return await self.ui.render(update, text, buttons + back)
        if action == "mod_form":
            kind = payload["kind"]
            if kind in MEMBER_ACTIONS:
                return await self.action(
                    update, {"action": "mod_members", "kind": kind, "chat": chat}
                )
            if kind == "grant":
                self.store.require(uid, owner=True)
            elif kind in {"add", "enable", "disable"}:
                self.store.require(uid, "manager")
            if chat:
                await mod.check(
                    uid,
                    chat,
                    "delete" if kind == "delete" else "mute",
                    enabled=kind not in {"enable", "disable"},
                )
            self.store.clear_dialog(uid)
            if kind in {
                "mute_all",
                "unmute_all",
                "pause",
                "resume",
                "remove",
                "enable",
                "disable",
            }:
                return await self.preview(update, kind, chat, "")
            self.store.dialog(uid, {"moderation": True, "kind": kind, "chat": chat})
            prompts = {
                "add": "发送数字群 ID 或 https://t.me/公开群用户名",
                "grant": "发送：目标UID 有效天数 群ID,群ID\n本链接授予群管范围；领取将替换原业务权限，原权限如需保留应另行统一授权。",
                "schedule": "发送每日时段，例如 23:00 08:00（北京时间）",
                "mute": "发送：成员数字UID 禁言分钟数",
                "delete": "发送此群的 Telegram 消息链接",
            }
            return await self.ui.render(
                update, prompts.get(kind, "发送成员数字 UID；/cancel 取消"), back
            )
        if action == "mod_confirm":
            kind, data = payload["kind"], payload["data"]
            if payload["expires"] <= self.store.clock():
                raise Rejected("确认已过期")
            if kind in ACTIONS:
                await mod.execute(uid, data, "manual:" + token)
            elif kind == "grant":
                self.store.require(uid, owner=True)
                for group in data["groups"]:
                    await mod.check(uid, group)
                link = self.store.grant(uid, data["uid"], ["moderation"], data["days"])
                self.store.db.execute(
                    "INSERT INTO mod_grants(token,groups_json) VALUES(?,?)",
                    (hashlib.sha256(link.encode()).hexdigest(), encode(data["groups"])),
                )
                return await self.ui.render(
                    update,
                    f"仅目标 UID 可领取，10 分钟有效：\nhttps://t.me/{self.runtime.bot.username}?start=sbg_{link}",
                    back,
                )
            elif kind == "add":
                self.store.require(uid, "manager")
                info = await self.runtime.bot.get_chat(data["chat"])
                for who in (int(uid), self.runtime.bot.id):
                    member = await self.runtime.bot.get_chat_member(info.id, who)
                    if member.status != "creator" and (
                        member.status != "administrator"
                        or not member.can_restrict_members
                    ):
                        raise Rejected("操作者或机器人缺少群管理权限")
                if info.type != "supergroup":
                    raise Rejected("仅支持超级群")
                self.store.db.execute(
                    "INSERT OR IGNORE INTO mod_groups(chat,title) VALUES(?,?)",
                    (str(info.id), info.title),
                )
            else:
                async with mod.locks.setdefault(chat, asyncio.Lock()):
                    await mod.check(
                        uid, chat, enabled=kind not in {"enable", "disable"}
                    )
                    group = mod.row(chat)
                    schedule = self.store.db.execute(
                        "SELECT * FROM mod_schedules WHERE chat=?", (chat,)
                    ).fetchone()
                    if (
                        group["version"] != data["group_version"]
                        or (schedule["version"] if schedule else 0)
                        != data["schedule_version"]
                    ):
                        raise Rejected("配置已变化，请重新预览")
                    if kind in {"enable", "disable"}:
                        self.store.require(uid, "manager")
                        self.store.db.execute(
                            "UPDATE mod_groups SET enabled=?,version=version+1 WHERE chat=?",
                            (int(kind == "enable"), chat),
                        )
                        if kind == "disable":
                            mod.pause(chat, "group_disabled")
                            self.runtime.ad_killer.pause(chat, "group_disabled")
                    elif kind in {"pause", "remove"}:
                        mod.pause(chat, "deleted" if kind == "remove" else "paused")
                    else:
                        stamp, op = boundary(
                            data["start"], data["end"], self.store.clock()
                        )
                        self.store.db.execute(
                            "INSERT INTO mod_schedules(chat,actor,start,end,enabled,version,next,action) VALUES(?,?,?,?,1,1,?,?) ON CONFLICT(chat) DO UPDATE SET actor=excluded.actor,start=excluded.start,end=excluded.end,enabled=1,version=mod_schedules.version+1,next=excluded.next,action=excluded.action,error=''",
                            (chat, uid, data["start"], data["end"], stamp, op),
                        )
            with self.store.tx() as db:
                self.store.audit(db, uid, "moderation_" + kind, data)
            messages = {
                "enable": "已启用群管理，现在可以选择需要的操作。",
                "disable": "已停用群管理，定时计划也已暂停。",
                "add": "群已添加，再点“启用此群”就能开始管理。",
                "pause": "定时禁言已暂停，群当前的禁言状态不变。",
                "remove": "定时计划已删除，群当前的禁言状态不变。",
                "schedule": "定时禁言已设置，从下一个设定时间开始执行。",
                "resume": "定时禁言已恢复，从下一个设定时间开始执行。",
            }
            return await self.ui.render(
                update,
                messages.get(kind, "Telegram 已受理操作，可到操作记录查看详情。"),
                [("查看此群", {"action": "mod_group", "chat": chat})] + back
                if chat
                else back,
            )

    async def preview(self, update, kind, chat, text):
        uid = str(update.effective_user.id)
        mod = self.runtime.moderation
        if kind == "joinverify":
            from .join_verify_ui import input_text

            return await input_text(self, update, chat, text)
        if kind.startswith("adkiller:"):
            from .ad_killer_ui import input_text

            return await input_text(
                self, update, kind.removeprefix("adkiller:"), chat, text
            )
        if kind.startswith("member_lookup:"):
            operation = kind.split(":", 1)[1]
            await mod.check(uid, chat)
            match = re.fullmatch(
                r"(?:@|(?:https?://)?t\.me/)([A-Za-z0-9_]{1,32})/?", text.strip()
            )
            if not match:
                raise Rejected("请点击成员按钮，或发送 @用户名、t.me/用户名")
            username = match[1].lower()
            rows = self.store.db.execute(
                "SELECT uid FROM mod_members WHERE chat=? AND username=?",
                (chat, username),
            ).fetchall()
            found = []
            for row in rows:
                member = await self.runtime.bot.get_chat_member(chat, int(row["uid"]))
                user = getattr(member, "user", None)
                if user and (user.username or "").lower() == username:
                    mod.remember(chat, user)
                    found.append(row["uid"])
            if len(found) != 1:
                raise Rejected(
                    "暂时无法核实这个用户名。请让对方在群里发消息，再刷新选择；不会按旧用户名直接操作。"
                )
            return await self.action(
                update,
                {
                    "action": "mod_member",
                    "kind": operation,
                    "chat": chat,
                    "target": found[0],
                },
            )
        if kind.startswith("member_minutes:"):
            if (
                not text.isascii()
                or not text.isdigit()
                or not 1 <= int(text) <= 365 * 1440
            ):
                raise Rejected("请输入1—525600的整数分钟数")
            return await self.preview(
                update, "mute", chat, kind.split(":", 1)[1] + " " + text
            )
        data = {}
        if kind in ACTIONS:
            parts = text.split()
            target = parts[0] if parts else ""
            if kind == "delete":
                match = re.fullmatch(
                    r"https://t\.me/(?:c/(\d+)|([A-Za-z0-9_]+))/(\d+)", text
                )
                if not match:
                    raise Rejected(
                        "使用 https://t.me/c/群编号/消息编号 或公开群消息链接"
                    )
                actual = (
                    "-100" + match[1]
                    if match[1]
                    else str((await self.runtime.bot.get_chat("@" + match[2])).id)
                )
                if actual != chat:
                    raise Rejected("消息不属于所选群")
                target = match[3]
            data = await mod.preview(
                uid,
                chat,
                kind,
                target,
                int(parts[1]) if kind == "mute" and len(parts) == 2 else 0,
            )
        elif kind == "add":
            self.store.require(uid, "manager")
            value = (
                text
                if re.fullmatch(r"-\d+", text)
                else "@" + text.removeprefix("https://t.me/")
            )
            info = await self.runtime.bot.get_chat(value)
            if info.type != "supergroup":
                raise Rejected("仅支持超级群")
            data = {"chat": str(info.id), "title": info.title}
        elif kind == "grant":
            self.store.require(uid, owner=True)
            target, days, groups = text.split()
            data = {
                "uid": target,
                "days": int(days),
                "groups": sorted(set(groups.split(","))),
            }
            if (
                not target.isascii()
                or not target.isdigit()
                or not 1 <= int(days) <= 365
            ):
                raise Rejected("UID 或有效期无效")
            for group in data["groups"]:
                await mod.check(uid, group)
        else:
            await mod.check(uid, chat, enabled=kind not in {"enable", "disable"})
            schedule = self.store.db.execute(
                "SELECT * FROM mod_schedules WHERE chat=?", (chat,)
            ).fetchone()
            data = {
                "group_version": mod.row(chat)["version"],
                "schedule_version": schedule["version"] if schedule else 0,
            }
            if kind in {"schedule", "resume"}:
                if kind == "resume":
                    if not schedule or schedule["error"] == "deleted":
                        raise Rejected("无可恢复的计划")
                    start, end = schedule["start"], schedule["end"]
                else:
                    start, end = text.split()
                stamp, op = boundary(start, end, self.store.clock())
                data.update(
                    start=start,
                    end=end,
                    next=datetime.fromtimestamp(stamp, TZ).isoformat(),
                    next_action=op,
                )
        self.store.clear_dialog(uid)
        labels = {
            "add": "添加群",
            "grant": "授权群管",
            "schedule": "设置定时",
            "resume": "恢复定时",
            "pause": "暂停定时",
            "remove": "删除定时",
            "enable": "启用群",
            "disable": "停用群",
        }
        description = f"确认{ACTIONS.get(kind, labels.get(kind, kind))}？\n群：{chat or data.get('title', data.get('chat', '所选群'))}"
        for key, label in {
            "target": "目标",
            "minutes": "分钟",
            "uid": "用户",
            "days": "有效天数",
            "groups": "授权群",
            "start": "禁言时间",
            "end": "恢复时间",
            "next": "下次执行",
        }.items():
            if data.get(key):
                value = data[key]
                description += f"\n{label}：{', '.join(value) if isinstance(value, list) else value}"
        if kind in {"ban", "kick"}:
            description += "\n警告：Telegram 可能删除该成员的历史消息。踢出可重新加入，封禁不可自行重新加入。"
        if kind in {"schedule", "resume"}:
            description += "\n北京时间；从下一个未来边界开始，不立即修改群状态。外部管理员临时接管前须暂停计划。"
        return await self.ui.render(
            update,
            description,
            [
                (
                    "确认操作",
                    {
                        "action": "mod_confirm",
                        "kind": kind,
                        "chat": chat,
                        "data": data,
                        "expires": self.store.clock() + 600,
                    },
                ),
                (
                    "返回上一页",
                    {"action": "mod_members", "kind": kind, "chat": chat}
                    if kind in MEMBER_ACTIONS
                    else {"action": "mod_group", "chat": chat}
                    if chat
                    else {"action": "mod_home"},
                ),
            ],
        )
