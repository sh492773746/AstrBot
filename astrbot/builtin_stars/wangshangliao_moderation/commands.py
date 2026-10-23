"""Administrator commands using native platform capabilities."""

import hashlib
import re
import secrets
import sqlite3
import time

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.core.platform.sources.wangshangliao.actions import ActionRequest
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
from .policy import GROUP_ADMIN_REQUIRED, require_group_admin
from .private_invitation import (
    PRIVATE_MENU,
    private_invitation_command,
    private_query_guard,
)
from .rankings import DailyRankings

HELP = """【旺商聊命令帮助】

直接发送以下命令，不需要斜杠、群管前缀或 @ 机器人。

【群内查询 · 普通群员】
排名  查看今天的聊天排行
排名 2  查看第二页（每页20条）
参加抽奖  报名当前抽奖
抽奖状态  查询当前活动
我的邀请  查询自己的有效邀请和奖励
邀请奖励  查询统一奖励值与自己的累计奖励
我拉了谁  查询自己已确认邀请的成员名称
为什么没统计  查看邀请计入条件

【身份与权限】
sid  仅管理员可获取自己的 UID
我的权限  查看管理员身份
管理员由 AstrBot 配置，不支持聊天自授权。
群管理员由后台“群授权”单独指定，只管理被授权的群，不获得总控或返现结账权限。
群管理员私聊使用“管理 群列表”“管理 选择群 1”“管理 成员列表”等固定命令。
首次主管由 WebUI 对应配置的管理员列表设置；维护人员可从已有会话记录核实 UID。
普通用户私聊发送“菜单”查看菜单，“我的邀请”仅查询本人返现汇总。
其他管理员私聊命令仍返回“无权限”，普通聊天保持原配置。

【私聊管理 · 管理员】
1. 群列表
2. 选择群 1
3. 成员列表
   成员搜索 小明
   下一页
4. 禁言 2
   禁言 2 3
   踢出 2
编号绑定当前列表，十分钟有效。
解除禁言须先查询“禁言名单”，再发送“解禁 2”；普通成员列表编号不能用于解禁。
私聊菜单4→7先选本人授权群，再选被禁言成员并确认；4→8单独查看名单。
业务内容规则启用时，手动踢出只生成预览；必须在当前私聊发新消息：
确认踢出 <确认码>
确认码十分钟有效、仅原管理员可用一次；群内不能确认。

【群内管理 · 管理员】
禁言 @成员  默认30分钟
禁言 @成员 3  禁言3分钟（1至1440分钟）
解禁 @成员
公告 公告内容
全员禁言
解除全员禁言
请使用客户端成员选择器进行真实 @。
私聊选群后也可执行公告、全员禁言及解除。

【私聊查询 · 管理员】
能力
规则
违规计数
结果 <操作ID>
开发门禁

【抽奖 · 管理员私聊先选群】
抽奖奖励 18元猪脚饭
中奖人数 3
抽奖倒计时 10
参与上限 15
抽奖邀请门槛 0
领奖联系人 秦铭
抽奖设置
开启抽奖
抽奖状态
立即开奖
取消抽奖
中奖名单
中奖名单 1
抽奖记录
倒计时为分钟；上限0为不限；邀请门槛0为无需邀请。
群员发送“参加抽奖”报名，开奖统一公布并真实 @ 中奖者。
群通知需要保存该群的主动发送授权。活动开启后设置冻结。
奖品由联系人发放，机器人不自动付款。

【邀请奖励 · 管理员私聊先选群】
设置邀请奖励 5
开启邀请统计
暂停邀请统计
开启邀请奖励
邀请奖励状态
邀请记录
邀请统计
暂停邀请奖励
本群统一每人奖励，最多两位小数，作为积分记账。
“开启邀请统计”先建立成员基线，后续已入群且归属明确的新成员会计入人数。
奖励可保持关闭；开启奖励后只对后续新入群成员积分记账。
重复进群不重复计奖；待审核、历史邀请和机器人不计奖。
邀请统计显示已记账邀请的累计、当前在群和已退群人数；退群不回收积分。

【每日全员禁言 · 私聊管理员先选群】
定时禁言 23:00 08:00
确认定时 <确认码>
定时状态
暂停定时
恢复定时
删除定时
默认北京时间，支持跨午夜；确认码十分钟有效，须在同一私聊发新消息。
保存和恢复只从下一未来边界开始，不立即改变当前状态。
人工全员禁言/解除会暂停计划；恢复须重新确认。
首次无启用时段；需逐群授权 mute_all、unmute_all。
外部管理员临时接管前请先暂停计划；故障或错过边界需核查后恢复。

【名片规范 · 私聊管理员】
名片修改、批量预览与确认仅在 AstrBot 管理页面操作。
AI不执行管理操作；动作授权与自动开关仍需到 Dashboard 逐群保存。

【说明】
群内开放排名、抽奖报名、活动及自身邀请查询和上述管理动作。
其他管理查询仅限管理员私聊。
踢出仅限管理员私聊预览与确认。
动作仍需机器人群授权及平台权限。
群内命令、本人菜单选择及对应回复统一安排12秒撤回；原命令需平台撤回权限，私聊和普通聊天不受影响。
开发门禁仅在 Dashboard 开启。
固定解禁命令复核当前禁言，菜单解禁还须预览确认；平台受理后返回结果。
未知结果不要重复提交。"""

PRIVATE_ONLY = "此功能仅限私聊，请私信机器人发送“帮助”；群内发送“排名”查询今日发言榜。"


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
        if event.is_private_chat() and private_invitation_command(
            action + (" " + argument if argument else "")
        ):
            private_query_guard(event)
            if action == "私聊菜单":
                return PRIVATE_MENU
            if argument:
                event.set_extra("_wsl_invitation_selection", int(argument))
            runtime = getattr(self.activities, "cashback", None)
            if runtime is None:
                return "邀请返现查询暂不可用，请稍后再试。"
            return await runtime.private_invitation_reply(event)
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
            if action == "排名":
                return await self.rankings.read(
                    event.platform,
                    event.get_group_id(),
                    event.get_sender_id(),
                    page=int(argument or "1"),
                    native_mentions=True,
                )
            if action not in actions and not public_group:
                return None
            if public_group and not allowed(event.get_group_id()):
                return "无权限"
        if action in {"", "帮助"}:
            return HELP
        if action == "我的权限":
            if public_group:
                return "你是本群的机器人管理员，可以使用本群管理命令；不能管理其他群或修改返现账本。"
            if management is not None and callable(bind_reply):
                bind_reply(management, tuple(management.groups(event)))
            return f"【我的权限】\nUID: {event.get_sender_id()}\nAstrBot 管理员: {'是' if event.is_admin() else '否'}\n本账号可管理群：{len(management.groups(event)) if management else len(event.platform.groups) if event.is_admin() else 0}"
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
                "可在管理员私聊预览开关或指定成员改名；开启需已授权修改名片，关闭不恢复历史名片。"
                "\n每日定时独立配置，默认不启用；私聊发送“定时状态”查询。人工全员禁言/解除先暂停定时，恢复需重新私聊确认。"
            )
            escalation = (
                "\n自动踢出已停用；旧配置不恢复自动踢出。后续违规仍撤回并最多禁言60分钟；"
                "踢出需管理员私聊预览并确认。按账号/群/成员累计受理的自动禁言，"
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
                    "时长依次为5分钟、15分钟、最多1小时。"
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
                        + "\n人工踢出须私聊确认；自动处理不踢人。旧消息仅审计。"
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
            return HELP
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
                try:
                    muted = await muted_member_ids(
                        adapter,
                        group,
                        authorize=guard,
                        expected_member=(str(member), str(matches[0]["nimId"])),
                    )
                    if guard is not None:
                        guard()
                except ProtocolError as exc:
                    logger.warning("WSL unmute verification failed: %s", str(exc))
                    return "暂时无法核对当前禁言状态，本次未解禁，请稍后重新查询「禁言名单」。"
                if str(member) not in muted:
                    return "该成员当前已不在禁言名单中，本次无需解禁。"
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
            base_guard = submission_guard

            async def submission_guard():
                base_guard()
                current = await muted_member_ids(
                    adapter,
                    group,
                    authorize=base_guard,
                    expected_member=(str(member), str(matches[0]["nimId"])),
                )
                base_guard()
                if str(member) not in current:
                    raise ProtocolError("member_no_longer_muted")

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
            if action == "解禁":
                return "解禁已受理。"
        status = {
            "accepted": "服务端已接受，尚未确认",
            "verified": "已确认",
            "unknown": "未知，不自动重试",
            "rejected": "拒绝",
        }.get(result.get("status"), "未知")
        return f"{status}\n操作ID：{operation}"
