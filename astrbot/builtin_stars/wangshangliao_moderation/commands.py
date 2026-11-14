"""Administrator commands using native platform capabilities."""

import asyncio
import hashlib
import re
import secrets
import sqlite3
import time

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.core.platform.sources.wangshangliao.actions import ActionRequest
from astrbot.core.platform.sources.wangshangliao.blacklist import BlacklistStore
from astrbot.core.platform.sources.wangshangliao.database import (
    AsyncDatabase,
    account_guard,
)
from astrbot.core.platform.sources.wangshangliao.directory import muted_member_ids
from astrbot.core.platform.sources.wangshangliao.policy import authorize_action
from astrbot.core.platform.sources.wangshangliao.storage import instance_dir
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

from .activities import ADMIN_COMMANDS, PUBLIC_COMMANDS, GroupActivities
from .group_management import MANAGER_COMMANDS
from .member_mutes import release_member
from .policy import GROUP_ADMIN_REQUIRED, require_group_admin
from .rankings import DailyRankings
from .syntax import ALIASES, is_removed_group_command

PRIVATE_ONLY = "请由已有后台权限的负责人在旺商聊插件管理页面操作。"


class Commands:
    """Keep short-lived directory selections isolated by login and caller."""

    def __init__(self, schedules=None, activities=None):
        self.selections = {}
        self.schedules = schedules
        self.rankings = DailyRankings()
        self.activities = activities or GroupActivities()

    async def run(self, event, command: str) -> str | MessageChain | None:
        """Execute one command with authoritative identity and capability checks.

        Args:
            event: Native authenticated message event.
            command: Subcommand and arguments, excluding the command prefix.

        Returns:
            Bounded text, a native ranking chain, or None for private-only group commands.
        """
        parts = command.strip().split(maxsplit=1)
        action = parts[0] if parts else ""
        argument = parts[1].strip() if len(parts) == 2 else ""
        return await self.execute(event, action, argument)

    async def execute(self, event, action: str, argument: str, *, ai: bool = False):
        """Dispatch validated arguments through the shared command service.

        Args:
            event: Authenticated native event.
            action: Fixed command action.
            argument: Action-specific value, never reparsed as a command.
            ai: Whether to return structured mutation results and stable AI IDs.

        Returns:
            Query text, a structured AI mutation result, or None for a silent group rejection.
        """
        if ai:
            return {"status": "rejected", "reason": "ai_management_retired"}
        if event.is_private_chat():
            return None
        if not action or is_removed_group_command(action):
            return None
        if ALIASES.get(action, action) == "排名":
            return await self.rankings.read(
                event.platform,
                event.get_group_id(),
                event.get_sender_id(),
                page=1,
                native_mentions=True,
            )
        management = getattr(self, "management", None)
        bind_reply = getattr(event, "bind_management_reply_guard", None)
        public_mode = "_open_groups" in getattr(
            getattr(event, "platform", None), "config", {}
        )
        if public_mode and management is None:
            return "无权限"
        public_group = (
            not event.is_private_chat() and management is not None and public_mode
        )

        def allowed(group=""):
            return (
                management.allowed(event, group)
                if management is not None
                else event.is_admin()
            )

        if event.is_private_chat() and not allowed():
            return "无权限"
        actions = {
            "禁言": "mute",
            "解禁": "unmute",
            "踢出": "kick",
            "公告": "announce",
            "全员禁言": "mute_all",
            "解除全员禁言": "unmute_all",
        }
        if not event.is_private_chat():
            if public_mode:
                require_group_admin(event.platform.config, event.get_group_id())
            if action in PUBLIC_COMMANDS:
                return await self.activities.command(
                    event, event.get_group_id(), action, argument
                )
            if action not in actions and not public_group:
                return None
            if public_group and not allowed(event.get_group_id()):
                return "无权限"
        if not allowed():
            return "无权限"
        if (
            management is not None
            and not management.superadmin(event)
            and action
            not in (
                MANAGER_COMMANDS
                if "_open_groups" not in event.platform.config
                else MANAGER_COMMANDS
                | {
                    "管理员列表",
                    "添加管理员",
                    "移除管理员",
                    "确认授权",
                    "开启广告审核",
                    "关闭广告审核",
                    "开启防刷屏",
                    "关闭防刷屏",
                    "添加关键词",
                    "删除关键词",
                }
                | (
                    ADMIN_COMMANDS
                    if event.platform.config.get("_open_integrated")
                    else set()
                )
            )
        ):
            return "无权限"
        adapter = event.platform
        key = (
            id(adapter),
            adapter.account,
            event.get_sender_id(),
            event.unified_msg_origin,
        )
        now = time.monotonic()
        self.selections = {
            k: v for k, v in self.selections.items() if now < v["expires"]
        }
        selection = self.selections.get(key, {})
        group = event.get_group_id() or selection.get("group", "")
        versions = dict(management.versions) if management else {}
        if (
            management is not None
            and callable(bind_reply)
            and group
            and allowed(group)
            and action not in {"群列表", "选择群"}
        ):
            bind_reply(management, (group,))

        def authorize():
            if not allowed(group):
                raise ProtocolError("command_admin_revoked")
            require_group_admin(adapter.config, group)
            if management is not None:
                if management.versions.get(
                    management.key(adapter, group), 0
                ) != versions.get(management.key(adapter, group), 0):
                    raise ProtocolError("command_admin_revoked")
                return
            context = getattr(self.activities, "context", None)
            if context is not None and str(event.get_sender_id()) not in {
                str(uid)
                for uid in context.get_config(event.unified_msg_origin).get(
                    "admins_id", []
                )
            }:
                raise ProtocolError("command_admin_revoked")

        manual_kick_only = adapter.config.get("moderation", {}).get(
            "manual_kick_only", False
        )
        if manual_kick_only and ai and action in {"踢出", "确认踢出"}:
            return {"status": "rejected", "reason": "manual_kick_only"}
        content_mode = bool(
            adapter.config.get("moderation", {}).get("content_rules_since")
            or manual_kick_only
            or "_open_groups" in adapter.config
        )
        guard = (
            account_guard(adapter, authorize)
            if management is not None
            or action == "违规计数"
            or (content_mode and action in {"踢出", "确认踢出"})
            else None
        )
        if (
            content_mode
            and action == "踢出"
            and not event.is_private_chat()
            and not public_group
        ):
            return None
        if content_mode and action == "确认踢出":
            if (not event.is_private_chat() and not public_group) or ai:
                return PRIVATE_ONLY
            path = instance_dir(adapter.config["id"]) / "moderation.sqlite3"
            async with AsyncDatabase(
                lambda: sqlite3.connect(path), write=True, guard=guard
            ) as db:
                await db.execute(
                    "CREATE TABLE IF NOT EXISTS kick_confirmations(token TEXT PRIMARY KEY,account TEXT,owner TEXT,session TEXT,group_id TEXT,member TEXT,peer TEXT,expires REAL,message TEXT,used INTEGER DEFAULT 0)"
                )
                if "grant_version" not in {
                    column[1]
                    for column in await db.fetchall(
                        "PRAGMA table_info(kick_confirmations)"
                    )
                }:
                    await db.execute(
                        "ALTER TABLE kick_confirmations ADD COLUMN grant_version INTEGER NOT NULL DEFAULT -1"
                    )
                row = await db.fetchone(
                    "SELECT account,owner,session,group_id,member,peer,expires,message,used,grant_version FROM kick_confirmations WHERE token=?",
                    (argument,),
                )
                if (
                    not row
                    or row[0] != adapter.account
                    or row[1] != event.get_sender_id()
                    or row[2] != event.unified_msg_origin
                    or row[3] != group
                    or row[6] <= time.time()
                    or row[8]
                    or row[7] == str(event.message_obj.message_id)
                    or "_open_groups" in adapter.config
                    and row[9] != versions.get(management.key(adapter, group), 0)
                ):
                    return "确认码无效、过期或已使用；请在当前对话重新预览。"
                group = row[3]
                if management is not None:
                    authorize()
                await db.execute(
                    "UPDATE kick_confirmations SET used=1 WHERE token=?", (argument,)
                )
            roster = await adapter.get_moderation_members(row[3])
            group = row[3]
            if guard is not None:
                guard()
            matches = [
                m
                for m in roster.get("groupMemberInfo", [])
                if str(m.get("userId")) == row[4]
                and str(m.get("nimId")) == row[5]
                and m.get("groupRole") == "GROUP_ROLE_MEMBER"
            ]
            if not roster.get("complete") or len(matches) != 1 or not allowed(group):
                return "拒绝：权限或成员身份已变化，请重新核验。"
            if (
                management is not None
                and not management.superadmin(event)
                and str(event.get_sender_id())
                not in {str(m["userId"]) for m in roster.get("groupMemberInfo", [])}
            ):
                return "无权限"
            result = await adapter.execute_action(
                ActionRequest.bind(
                    adapter,
                    "confirmed-kick/" + argument,
                    "kick",
                    int(row[3]),
                    member=int(row[4]),
                ),
                authorize=guard,
            )
            return f"踢出操作状态：{result.get('status', 'unknown')}；accepted 表示已受理，unknown 不得重复提交。"
        if action == "开发门禁":
            window = getattr(adapter, "test_window", None)
            state = window.status() if window else {}
            return (
                f"【开发门禁】\n状态：{'开启' if state.get('active') else '关闭'}\n"
                f"剩余时间：{state.get('seconds', 0)} 秒\n"
                f"剩余入站次数：{state.get('remaining', 0)}\n"
                f"发送实例：{state.get('sender_instance', '未设置')}"
            )
        if action == "群列表":
            groups = sorted(
                set(adapter.groups) & set(management.groups(event))
                if management is not None
                else adapter.groups
            )
            if public_group:
                roster = await adapter.get_moderation_members(group)
                guard()
                if not roster.get("complete") or str(event.get_sender_id()) not in {
                    str(m["userId"]) for m in roster.get("groupMemberInfo", [])
                }:
                    return "无权限"
                groups = [group] if group in groups else []
            if not groups:
                return "没有可管理的已启用群。请先在机器人设置中启用目标群。"
            if management is not None and callable(bind_reply):
                bind_reply(management, tuple(groups))
            source_names = getattr(adapter, "group_names", {})
            names = {
                group: " ".join(str(source_names.get(group) or "").split())[:128]
                or group
                for group in groups
            }
            counts = {}
            for name in names.values():
                counts[name] = counts.get(name, 0) + 1
            labels = {
                group: (f"{name}（群号 {group}）" if counts[name] > 1 else name)
                for group, name in names.items()
            }
            self.selections[key] = {
                "expires": now + 600,
                "groups": groups,
                "group_names": labels,
            }
            return (
                "已启用群（编号十分钟有效）：\n"
                + "\n".join(f"{i}. {labels[g]}" for i, g in enumerate(groups, 1))
                + "\n发送「选择群 1」选择对应群，再直接发送管理命令。"
            )
        if action == "选择群":
            if not event.is_private_chat():
                return "群内自动使用当前群。"
            index = int(argument)
            groups = selection.get("groups", [])
            if not 1 <= index <= len(groups):
                return "编号无效或过期，请重新发送“群列表”。"
            if not allowed(groups[index - 1]):
                return "无权限"
            if management is not None and callable(bind_reply):
                bind_reply(management, (groups[index - 1],))
            selection.update(
                group=groups[index - 1], members=[], all_members=[], offset=0
            )
            name = selection.get("group_names", {}).get(
                selection["group"], selection["group"]
            )
            return (
                f"当前已选群：{name}\n可直接发送「规则」「禁言名单」「关键词回复」「定时发言」；不用先选菜单。"
                + (
                    "\n" + GROUP_ADMIN_REQUIRED
                    if public_mode
                    and adapter.config.get("_open_native_roles", {}).get(
                        selection["group"]
                    )
                    not in {"GROUP_ROLE_OWNER", "GROUP_ROLE_ADMIN"}
                    else ""
                )
            )
        if not group or group not in adapter.config.get("enabled_groups", []):
            return "尚未选择已启用群。先发「群列表」，再发「选择群 序号」；选好后直接发送命令即可。"
        if not allowed(group):
            return "无权限"
        require_group_admin(adapter.config, group)
        from astrbot.core.platform.sources.wangshangliao.group_scope import scoped

        adapter = scoped(adapter, group)
        if management is not None and not management.superadmin(event):
            roster = await adapter.get_moderation_members(group)
            guard()
            if not roster.get("complete") or str(event.get_sender_id()) not in {
                str(m["userId"]) for m in roster.get("groupMemberInfo", [])
            }:
                return "无权限"
            if public_mode:
                require_group_admin(
                    {
                        **adapter.config,
                        "_open_native_roles": {
                            group: next(
                                (
                                    m.get("groupRole")
                                    for m in roster.get("groupMemberInfo", [])
                                    if str(m["userId"]) == str(adapter.account)
                                ),
                                "",
                            )
                        },
                    },
                    group,
                )
        if action == "排名":
            return await self.rankings.read(
                adapter, group, event.get_sender_id(), page=int(argument or "1")
            )
        if action in PUBLIC_COMMANDS | ADMIN_COMMANDS:
            return await self.activities.command(event, group, action, argument)
        if action in {
            "定时禁言",
            "确认定时",
            "定时状态",
            "暂停定时",
            "恢复定时",
            "删除定时",
        }:
            if not event.is_private_chat() and not public_group:
                return PRIVATE_ONLY
            return await self.schedules.command(event, group, action, argument)
        if action == "能力":
            granted = (
                adapter.config.get("moderation", {})
                .get("permissions", {})
                .get(group, [])
            )
            labels = {value: name for name, value in actions.items()}
            labels["recall"] = "违规撤回"
            return (
                f"【当前群动作授权】\n群：{group}\n"
                + (
                    "\n".join(f"- {labels.get(a, a)}" for a in granted)
                    or "未授权任何动作"
                )
                + "\n\n实际执行仍需平台权限。"
            )
        if action == "规则":
            p = adapter.config.get("moderation", {})
            timing = (
                f"\n本群自动规范名片：{'开启' if p.get('card_auto', {}).get(group) else '关闭'}。"
                f"\n本群名片后缀：{p.get('card_suffix', {}).get(group) or '无'}。"
                "由已有后台权限的负责人在插件页面管理；开启需已授权修改名片，关闭不恢复历史名片。"
                "\n每日定时独立配置，默认不启用；请在插件页面查看，或在已开通群发送“定时状态”。人工全员禁言/解除先暂停定时。"
            )
            escalation = (
                "\n自动踢出已停用；旧配置不恢复自动踢出。违规按三次警告、1/3/10分钟禁言循环；"
                "人工踢出请使用旺商聊App。按账号/群/成员累计受理的自动禁言，"
                "重复、失败、未知不计数。"
            )
            semantic = (
                "\nAI 结合当前消息和同群近期上下文审核；正常或证据不足不处罚，模型故障时高置信规则兜底。"
                if p.get("semantic", {}).get("enabled")
                else "\n当前使用确定性文本规则审核。"
            )
            if p.get("progressive_mute"):
                return (
                    "【递进处罚规则】\n"
                    "前三次确认违规仅撤回并警告；其后每次确认违规尝试禁言，"
                    "时长依次为1分钟、3分钟、10分钟，10分钟处罚结束后重新三次警告。"
                    "只累计平台受理的自动禁言；重复、失败和未知不计数，未知结果需核验。\n"
                    f"禁言关键词：{'、'.join(p.get('mute_keywords') or []) or '未设置'}\n"
                    f"原踢出关键词：{'、'.join(p.get('kick_keywords') or []) or '未设置'}\n"
                    "启用AI时，关键词结合语境审核；AI关闭或故障时规则兜底，举报、引用不直接处罚。"
                    + "原踢出关键词仅参与违规判断，不自动踢出。"
                    + "群主、平台管理员和机器人免罚；权限不足不执行。"
                    "撤回与禁言分别记结果，上游失败不假报成功。"
                    + semantic
                    + escalation
                    + timing
                )[:3000]
            if p.get("content_rules_since"):
                return (
                    "【业务内容规则】\n群主和管理员免罚。联系方式/外链与引流组合、成人推广：首次撤回警告，24小时再犯禁言（当前接口 min=1）。旧消息、时间不明、单独链接数字和举报引用不自动处罚。"
                    + semantic
                    + escalation
                    + timing
                )
            return (
                f"【当前规则】\n群：{group}\n"
                f"群管：{'开启' if p.get('enabled') else '关闭'}\n"
                f"自动规则：{'开启' if p.get('automation_enabled') else '关闭'}\n"
                f"违规撤回：{'开启' if p.get('recall_enabled') else '关闭'}\n"
                f"冷却：{p.get('cooldown_seconds', 0)} 秒\n\n"
                f"禁言关键词：{'、'.join(p.get('mute_keywords') or []) or '未设置'}\n"
                f"原踢出关键词：{'、'.join(p.get('kick_keywords') or []) or '未设置'}"
                + (
                    "\n手动踢出模式下，旧踢出关键词不触发自动处罚。"
                    if p.get("manual_kick_only")
                    else "\n旧踢出关键词只触发自动禁言。"
                )
                + escalation
                + timing
            )[:3000]
        muted_page = action == "禁言名单" or (
            action == "下一页" and selection.get("kind") == "muted"
        )
        if action in {"成员列表", "成员搜索"} or muted_page:
            offset = selection.get("offset", 0) if action == "下一页" else 0
            if muted_page:
                # A failed refresh must not leave a formerly actionable snapshot.
                self.selections[key] = selection = {
                    "expires": now + 600,
                    "group": group,
                    "kind": "muted",
                }
                authorize_action(adapter.config, group, "unmute")
            try:
                roster = await adapter.get_moderation_members(group)
                if guard is not None:
                    guard()
                if muted_page and not roster.get("complete"):
                    raise ProtocolError("member_pagination_incomplete")
                if muted_page:
                    muted = await muted_member_ids(adapter, group, authorize=guard)
                    if guard is not None:
                        guard()
            except ProtocolError as exc:
                if not muted_page:
                    raise
                logger.warning("WSL mute list read failed: %s", str(exc))
                return "暂时无法获取当前禁言名单，请稍后重新查询；本次未解禁任何人。"
            members = [
                {
                    **m,
                    "display_name": str(
                        m.get("groupMemberNick")
                        or m.get("userNick")
                        or m.get("nickname")
                        or m.get("name")
                        or m["userId"]
                    ),
                }
                for m in roster["groupMemberInfo"]
                if not muted_page
                or str(m["userId"]) in muted
                and m.get("groupRole") == "GROUP_ROLE_MEMBER"
                and str(m["userId"]) != str(adapter.account)
            ]
            if action == "成员搜索":
                if not argument:
                    return "用法：成员搜索 <名称>"
                members = [
                    m
                    for m in members
                    if argument.casefold() in m["display_name"].casefold()
                ]
            selection = {
                "expires": now + 600,
                "group": group,
                "all_members": members,
                "offset": offset,
                "kind": "muted" if muted_page else "members",
            }
            self.selections[key] = selection
            if muted_page and not members:
                return "本群目前没有可解禁的被禁言成员。"
        if action in {"成员列表", "成员搜索", "禁言名单", "下一页"}:
            if "all_members" not in selection or selection.get("group") != group:
                return "列表已过期，请重新查询成员。"
            members = selection["all_members"]
            offset = selection["offset"]
            page = []
            lines = []
            size = 0
            for member in members[offset:]:
                name = (
                    member["display_name"]
                    .encode("utf-8")[:240]
                    .decode("utf-8", errors="ignore")
                )
                line = f"{len(page) + 1}. {name} ({member['userId']})"
                length = len(line.encode("utf-8")) + 1
                if page and (size + length > 2800 or len(page) >= 30):
                    break
                page.append(member)
                lines.append(line)
                size += length
            selection.update(members=page, offset=offset + len(page))
            more = selection["offset"] < len(members)
            return (
                (
                    "【当前禁言名单】（当前页编号，十分钟有效；确认时再次核对）\n"
                    if selection.get("kind") == "muted"
                    else "成员快照（当前页编号，十分钟有效）：\n"
                )
                + ("\n".join(lines) or "没有更多成员。")
                + ("\n下一页：发送“下一页”" if more else "\n已到末页。")
            )
        if action == "结果":
            result = await adapter.get_moderation_result(argument, group)
            if not result:
                return "当前群没有此操作。"
            labels = {value: name for name, value in actions.items()}
            labels["recall"] = "违规撤回"
            states = {
                "accepted": "服务端已接受，尚未确认",
                "verified": "已确认",
                "rejected": "拒绝",
                "unknown": "未知，不自动重试",
            }
            return (
                f"【操作结果】\n状态：{states.get(result.get('status'), '未知')}\n"
                f"群：{group}\n动作：{labels.get(result.get('action'), '未知')}\n操作ID：{argument}"
            )
        if action == "违规计数":
            path = instance_dir(adapter.config["id"]) / "moderation.sqlite3"
            if not path.exists():
                return "暂无记录。"
            async with AsyncDatabase(
                lambda: sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True),
                guard=guard,
            ) as db:
                if content_mode:
                    if not await db.fetchone(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='content_violations'"
                    ):
                        return "暂无业务规则记录。"
                    rows = await db.fetchall(
                        "SELECT sender,category,status,COUNT(*) FROM content_violations WHERE account=? AND group_id=? AND observed>? GROUP BY sender,category,status LIMIT 30",
                        (adapter.account, group, time.time() - 86400),
                    )
                    escalation_counts = ""
                    if await db.fetchone(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='automatic_mutes'"
                    ):
                        counts = await db.fetchall(
                            "SELECT member,SUM(CASE WHEN status IN ('accepted','verified') THEN 1 ELSE 0 END),"
                            "SUM(CASE WHEN status='unknown' THEN 1 ELSE 0 END) FROM automatic_mutes "
                            "WHERE account=? AND group_id=? AND closed=0 GROUP BY member LIMIT 30",
                            (adapter.account, group),
                        )
                        escalation_counts = "\n\n【自动禁言累计】\n" + (
                            "\n".join(
                                f"账号 {member}：受理 {count} 次，未知 {unknown} 次"
                                for member, count, unknown in counts
                            )
                            or "暂无记录"
                        )
                    return (
                        "【业务规则审计】\n"
                        + (
                            "\n".join(
                                f"账号 {s}：{c}，{state}，{n} 条"
                                for s, c, state, n in rows
                            )
                            or "暂无记录"
                        )
                        + escalation_counts
                        + "\n人工踢出请使用旺商聊App；自动处理不踢人。旧消息仅审计。"
                    )
                if not await db.fetchone(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='violations'"
                ):
                    return "暂无记录。"
                rows = await db.fetchall(
                    "SELECT sender,count,started FROM violations WHERE account=? AND group_id=? AND started>? LIMIT 30",
                    (adapter.account, group, time.time() - 86400),
                )
                return "【24小时内违规计数】\n" + (
                    "\n".join(
                        f"- 账号 {sender}：{count} 次" for sender, count, _ in rows
                    )
                    or "暂无记录"
                )
        if action not in actions:
            return None
        member = 0
        minutes = 30
        duration = ""
        if action in {"禁言", "解禁", "踢出"}:
            roster = await adapter.get_moderation_members(group)
            if management is not None:
                guard()
            if (
                event.is_private_chat()
                and not argument.startswith("@")
                or (public_group and argument[:1].isdigit())
            ):
                if action == "禁言":
                    fields = argument.split()
                    if not 1 <= len(fields) <= 2:
                        return "格式：禁言 <成员编号> [分钟]，默认30分钟。"
                    argument = fields[0]
                    duration = fields[1] if len(fields) == 2 else ""
                if not re.fullmatch(r"[1-9][0-9]{0,4}", argument):
                    return (
                        "请先查询成员列表，再使用当前页成员编号；也可以真实@一位成员。"
                    )
                index = int(argument)
                members = selection.get("members", [])
                if (
                    selection.get("group") != group
                    or action == "解禁"
                    and selection.get("kind") != "muted"
                    or not 1 <= index <= len(members)
                ):
                    if action == "解禁":
                        return "禁言名单编号无效或过期，请重新查询「禁言名单」。"
                    return "成员编号无效或过期，请重新查询成员。"
                selected = members[index - 1]
                matches = [
                    m
                    for m in roster["groupMemberInfo"]
                    if str(m["userId"]) == str(selected["userId"])
                    and m.get("nimId") == selected.get("nimId")
                ]
            else:
                payload = event.get_extra("wangshangliao_payload") or {}
                peers = set(payload.get("mentions", [])) - {str(adapter.nim_account)}
                matches = [
                    m for m in roster["groupMemberInfo"] if str(m.get("nimId")) in peers
                ]
                if len(peers) != 1:
                    return "请通过成员选择器真实 @ 一个目标成员。"
                if argument and event.get_extra("wsl_plain_command") is True:
                    encoded_text = str(payload.get("text", "")).encode("utf-16-le")
                    verified = []
                    for span in payload.get("mention_spans", []):
                        start, end = span.get("start"), span.get("end")
                        if (
                            span.get("uid") in peers
                            and type(start) is int
                            and type(end) is int
                            and 0 <= start < end
                            and end * 2 <= len(encoded_text)
                        ):
                            display = encoded_text[start * 2 : end * 2].decode(
                                "utf-16-le", errors="replace"
                            )
                            if display.rstrip() == "@" + span.get("nick", ""):
                                verified.append(display.strip())
                    if len(verified) != 1 or not argument.startswith(verified[0]):
                        return "请只发送管理动作和一个真实 @ 成员，不附加问题或说明。"
                    suffix = argument[len(verified[0]) :]
                    if suffix and (action != "禁言" or not suffix[0].isspace()):
                        return "请只发送管理动作和一个真实 @ 成员，不附加问题或说明。"
                    duration = suffix.strip()
            if len(matches) != 1:
                return "拒绝：目标身份映射不明确，请重新选择。"
            member = int(matches[0]["userId"])
            if action == "解禁":
                authorize_action(adapter.config, group, "unmute")
                if (
                    not roster.get("complete")
                    or matches[0].get("groupRole") != "GROUP_ROLE_MEMBER"
                    or str(member) == str(adapter.account)
                ):
                    return "成员身份已变化，请重新查询「禁言名单」。"
            if action == "禁言" and duration:
                if (
                    not re.fullmatch(r"[0-9]{1,5}", duration)
                    or not 1 <= int(duration) <= 1440
                ):
                    return "禁言时长须为1至1440的整数分钟；不填默认30分钟。"
                minutes = int(duration)
            if content_mode and action == "踢出":
                if matches[0].get("groupRole") != "GROUP_ROLE_MEMBER" or not roster.get(
                    "complete"
                ):
                    return "拒绝：只能对身份已核实的普通成员生成踢出预览。"
                token = secrets.token_hex(12)
                path = instance_dir(adapter.config["id"]) / "moderation.sqlite3"
                async with AsyncDatabase(
                    lambda: sqlite3.connect(path), write=True, guard=guard
                ) as db:
                    await db.execute(
                        "CREATE TABLE IF NOT EXISTS kick_confirmations(token TEXT PRIMARY KEY,account TEXT,owner TEXT,session TEXT,group_id TEXT,member TEXT,peer TEXT,expires REAL,message TEXT,used INTEGER DEFAULT 0)"
                    )
                    if "grant_version" not in {
                        column[1]
                        for column in await db.fetchall(
                            "PRAGMA table_info(kick_confirmations)"
                        )
                    }:
                        await db.execute(
                            "ALTER TABLE kick_confirmations ADD COLUMN grant_version INTEGER NOT NULL DEFAULT -1"
                        )
                    await db.execute(
                        "INSERT INTO kick_confirmations(token,account,owner,session,group_id,member,peer,expires,message,used,grant_version) VALUES(?,?,?,?,?,?,?,?,?,0,?)",
                        (
                            token,
                            adapter.account,
                            event.get_sender_id(),
                            event.unified_msg_origin,
                            group,
                            str(member),
                            str(matches[0]["nimId"]),
                            time.time() + 600,
                            str(event.message_obj.message_id),
                            versions.get(management.key(adapter, group), 0)
                            if management is not None
                            and "_open_groups" in adapter.config
                            else -1,
                        ),
                    )
                return f"尚未执行。目标群：{group}；成员：{member}。十分钟内在当前对话发新消息：确认踢出 {token}"
        operation = (
            "command/"
            + hashlib.sha256(
                f"{adapter.account}/{event.unified_msg_origin}/{event.message_obj.message_id}".encode()
            ).hexdigest()
        )
        if not allowed(group):
            return "拒绝：管理员授权已撤销。"
        submission_guard = guard or authorize
        if action == "解禁":
            blacklist = BlacklistStore(adapter.config["id"])
            version, _ = await asyncio.to_thread(
                blacklist.read, str(adapter.account), group
            )
            try:
                result = await release_member(
                    adapter,
                    group,
                    str(member),
                    str(matches[0]["nimId"]),
                    operation=operation,
                    actor=event.actor_identity,
                    guard=submission_guard,
                    store=blacklist,
                    version=version,
                )
            except ProtocolError as exc:
                logger.warning("WSL unmute verification failed: %s", str(exc))
                return (
                    "禁言状态、名单或权限已变化，本次未完成解禁，请刷新核对后再操作。"
                )
            if result["verified"]:
                text = (
                    "该成员当前已不在禁言名单中，本次无需解禁。"
                    if result["status"] == "already_unmuted"
                    else "解禁已核验。"
                )
            else:
                text = {
                    "accepted": "解禁已受理，平台状态待核验。",
                    "verified": "解禁已受理，平台状态待核验。",
                    "unknown": "解禁结果未知，不自动重试。",
                }.get(result.get("status"), "解禁未受理。")
            text += {
                "removed": "\n已同时移出本群黑名单。",
                "retained": "\n黑名单未移除；请先核对解禁结果。",
                "changed": "\n黑名单或权限已变化，未移除新的记录；请刷新核对。",
                "failed": "\n黑名单移除失败，仍需处理；请在后台核对。",
            }.get(result["blacklist_status"], "")
            return text + f"\n操作ID：{operation}"

        result = await adapter.execute_action(
            ActionRequest.bind(
                adapter,
                operation,
                actions[action],
                int(group),
                member=member,
                text=argument if action == "公告" else "",
                minutes=minutes if action == "禁言" else 1,
            ),
            authorize=submission_guard,
        )
        if result.get("status") in {"accepted", "verified"}:
            if action == "禁言":
                return f"禁言已受理：{minutes} 分钟。"
        status = {
            "accepted": "服务端已接受，尚未确认",
            "verified": "已确认",
            "unknown": "未知，不自动重试",
            "rejected": "拒绝",
        }.get(result.get("status"), "未知")
        return f"{status}\n操作ID：{operation}"
