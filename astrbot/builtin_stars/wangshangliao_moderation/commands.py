"""Administrator commands using native platform capabilities."""

import hashlib
import json
import re
import secrets
import sqlite3
import time
from contextlib import closing

from astrbot.api.event import MessageChain
from astrbot.core.platform.sources.wangshangliao.storage import instance_dir

from .activities import ADMIN_COMMANDS, PUBLIC_COMMANDS, GroupActivities
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

【身份与权限】
sid  仅管理员可获取自己的 UID
我的权限  查看管理员身份
管理员由 AstrBot 配置，不支持聊天自授权。
首次主管由 WebUI 对应配置的管理员列表设置；维护人员可从已有会话记录核实 UID。
非管理员私聊命令统一返回“无权限”，普通聊天保持原配置。

【私聊管理 · 管理员】
1. 群列表
2. 选择群 1
3. 成员列表
   成员搜索 小明
   下一页
4. 禁言 2
   禁言 2 3
   解禁 2
   踢出 2
编号绑定当前列表，十分钟有效。
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
开启邀请奖励
邀请奖励状态
邀请记录
暂停邀请奖励
本群统一每人奖励，最多两位小数，作为积分记账。
启用时现有成员作为基线；只奖励后续已入群且归属明确的新成员。
重复进群不重复计奖；待审核、历史邀请和机器人不计奖。

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
启用人格工具 wsl_private_management 后，可用自然语言请求批量预览。
先选择群，核对预览，再发新消息明确执行；可查询进度或停止。
名片动作授权与自动开关仍需到 Dashboard 逐群保存。

【说明】
群内开放排名、抽奖报名、活动及自身邀请查询和上述管理动作。
其他管理查询仅限管理员私聊。
踢出仅限管理员私聊预览与确认。
动作仍需机器人群授权及平台权限。
排名等群内功能回复20秒后自动撤回；抽奖通知、报名和抽奖状态反馈保留，成员命令不删除。
开发门禁仅在 Dashboard 开启。
禁言和解禁无需二次确认；平台受理后直接返回结果。
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
        if event.is_private_chat() and not event.is_admin():
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
            if action not in actions:
                return None
        if action in {"", "帮助"}:
            return HELP
        if action == "我的权限":
            return f"【我的权限】\nUID: {event.get_sender_id()}\nAstrBot 管理员: {'是' if event.is_admin() else '否'}"
        if not event.is_admin():
            return "无权限"
        adapter = event.platform
        manual_kick_only = adapter.config.get("moderation", {}).get(
            "manual_kick_only", False
        )
        if manual_kick_only and ai and action in {"踢出", "确认踢出"}:
            return {"status": "rejected", "reason": "manual_kick_only"}
        content_mode = bool(
            adapter.config.get("moderation", {}).get("content_rules_since")
            or manual_kick_only
        )
        if content_mode and action == "踢出" and not event.is_private_chat():
            return None
        if content_mode and action == "确认踢出":
            if not event.is_private_chat() or ai:
                return PRIVATE_ONLY
            path = instance_dir(adapter.config["id"]) / "moderation.sqlite3"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS kick_confirmations(token TEXT PRIMARY KEY,account TEXT,owner TEXT,session TEXT,group_id TEXT,member TEXT,peer TEXT,expires REAL,message TEXT,used INTEGER DEFAULT 0)"
                )
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT account,owner,session,group_id,member,peer,expires,message,used FROM kick_confirmations WHERE token=?",
                    (argument,),
                ).fetchone()
                if (
                    not row
                    or row[0] != adapter.account
                    or row[1] != event.get_sender_id()
                    or row[2] != event.unified_msg_origin
                    or row[6] <= time.time()
                    or row[8]
                    or row[7] == str(event.message_obj.message_id)
                ):
                    return "确认码无效、过期或已使用；请重新私聊预览。"
                db.execute(
                    "UPDATE kick_confirmations SET used=1 WHERE token=?", (argument,)
                )
            roster = await adapter.get_moderation_members(row[3])
            matches = [
                m
                for m in roster.get("groupMemberInfo", [])
                if str(m.get("userId")) == row[4]
                and str(m.get("nimId")) == row[5]
                and m.get("groupRole") == "GROUP_ROLE_MEMBER"
            ]
            if not roster.get("complete") or len(matches) != 1 or not event.is_admin():
                return "拒绝：权限或成员身份已变化，请重新核验。"
            result = await adapter.execute_moderation(
                "confirmed-kick/" + argument, "kick", int(row[3]), int(row[4])
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
        if action == "群列表":
            groups = sorted(adapter.groups)
            if not groups:
                return "没有可管理的已启用群。请先在机器人设置中启用目标群。"
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
            return "已启用群（编号十分钟有效）：\n" + "\n".join(
                f"{i}. {labels[g]}" for i, g in enumerate(groups, 1)
            )
        if action == "选择群":
            if not event.is_private_chat():
                return "群内自动使用当前群。"
            index = int(argument)
            groups = selection.get("groups", [])
            if not 1 <= index <= len(groups):
                return "编号无效或过期，请重新发送“群列表”。"
            selection.update(group=groups[index - 1], members=[])
            name = selection.get("group_names", {}).get(
                selection["group"], selection["group"]
            )
            return f"当前已选群：{name}"
        if not group or group not in adapter.config.get("enabled_groups", []):
            return "目标群未启用或尚未选择，请先发送“群列表”。"
        if action == "排名":
            return await self.rankings.read(
                adapter, group, event.get_sender_id(), page=int(argument or "1")
            )
        if action in PUBLIC_COMMANDS | ADMIN_COMMANDS:
            if ai:
                return "活动设置仅支持管理员私聊固定命令。"
            return await self.activities.command(event, group, action, argument)
        if action in {
            "定时禁言",
            "确认定时",
            "定时状态",
            "暂停定时",
            "恢复定时",
            "删除定时",
        }:
            if not event.is_private_chat() or ai:
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
                "可在管理员私聊预览开关或指定成员改名；开启需已授权修改名片，关闭不恢复历史名片。"
                "\n每日定时独立配置，默认不启用；私聊发送“定时状态”查询。人工全员禁言/解除先暂停定时，恢复需重新私聊确认。"
            )
            escalation = (
                "\nAI及自动规则踢出已停用；后续违规仍撤回并最多禁言60分钟。只有管理员手动私聊预览并确认才能踢出。"
                if p.get("manual_kick_only")
                else "\n本群累计三次自动禁言受理后，第四次确认违规才撤回并踢出；第三次只禁言，不在到期时自动踢出。按账号/群/成员累计，重复、失败、未知不计数；确认移出后结束本轮计数。"
                if p.get("auto_kick", {}).get(group) is True
                else "\n自动踢出未开启；手动踢出须私聊预览后确认。"
            )
            semantic = (
                "\nAI 结合当前消息和同群近期上下文审核；正常或证据不足不处罚，模型故障时高置信规则兜底。"
                if p.get("semantic", {}).get("enabled")
                else "\n当前使用确定性文本规则审核。"
            )
            if p.get("progressive_mute"):
                return (
                    "【递进处罚规则】\n"
                    "前三次确认违规均撤回原消息并自动禁言：首次5分钟、第二次15分钟、第三次1小时。"
                    "只累计平台受理的自动禁言；重复、失败和未知不计数，未知结果需核验。\n"
                    f"禁言关键词：{'、'.join(p.get('mute_keywords') or []) or '未设置'}\n"
                    f"踢出关键词：{'、'.join(p.get('kick_keywords') or []) or '未设置'}\n"
                    "启用AI时，关键词结合语境审核；AI关闭或故障时规则兜底，举报、引用不直接处罚。"
                    + (
                        "原踢出关键词仅参与违规判断，不自动踢出。"
                        if p.get("manual_kick_only")
                        else "踢出关键词也遵守先三次禁言、第四次违规才踢出的门槛，不直接跳级。"
                    )
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
                f"踢出关键词：{'、'.join(p.get('kick_keywords') or []) or '未设置'}"
                + timing
            )[:3000]
        if action in {"成员列表", "成员搜索"}:
            roster = await adapter.get_moderation_members(group)
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
                "offset": 0,
            }
            self.selections[key] = selection
        if action in {"成员列表", "成员搜索", "下一页"}:
            if "all_members" not in selection:
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
                "成员快照（当前页编号，十分钟有效）：\n"
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
            with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
                if content_mode:
                    if not db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='content_violations'"
                    ).fetchone():
                        return "暂无业务规则记录。"
                    rows = db.execute(
                        "SELECT sender,category,status,COUNT(*) FROM content_violations WHERE account=? AND group_id=? AND observed>? GROUP BY sender,category,status LIMIT 30",
                        (adapter.account, group, time.time() - 86400),
                    ).fetchall()
                    escalation_counts = ""
                    if db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='automatic_mutes'"
                    ).fetchone():
                        counts = db.execute(
                            "SELECT member,SUM(CASE WHEN status IN ('accepted','verified') THEN 1 ELSE 0 END),"
                            "SUM(CASE WHEN status='unknown' THEN 1 ELSE 0 END) FROM automatic_mutes "
                            "WHERE account=? AND group_id=? AND closed=0 GROUP BY member LIMIT 30",
                            (adapter.account, group),
                        ).fetchall()
                        escalation_counts = "\n\n【自动禁言累计】\n" + (
                            "\n".join(
                                f"账号 {member}：受理 {count}/3 次，未知 {unknown} 次"
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
                        + "\n人工踢出须私聊确认；已开启自动升级的群先自动禁言三次，第四次确认违规才踢出。旧消息仅审计。"
                    )
                if not db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='violations'"
                ).fetchone():
                    return "暂无记录。"
                rows = db.execute(
                    "SELECT sender,count,started FROM violations WHERE account=? AND group_id=? AND started>? LIMIT 30",
                    (adapter.account, group, time.time() - 86400),
                ).fetchall()
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
            if event.is_private_chat():
                if action == "禁言":
                    fields = argument.split()
                    if not 1 <= len(fields) <= 2:
                        return "格式：禁言 <成员编号> [分钟]，默认30分钟。"
                    argument = fields[0]
                    duration = fields[1] if len(fields) == 2 else ""
                index = int(argument)
                members = selection.get("members", [])
                if not 1 <= index <= len(members):
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
                with (
                    closing(
                        sqlite3.connect(
                            instance_dir(adapter.config["id"]) / "moderation.sqlite3"
                        )
                    ) as db,
                    db,
                ):
                    db.execute(
                        "CREATE TABLE IF NOT EXISTS kick_confirmations(token TEXT PRIMARY KEY,account TEXT,owner TEXT,session TEXT,group_id TEXT,member TEXT,peer TEXT,expires REAL,message TEXT,used INTEGER DEFAULT 0)"
                    )
                    db.execute(
                        "INSERT INTO kick_confirmations VALUES(?,?,?,?,?,?,?,?,?,0)",
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
                        ),
                    )
                return f"尚未执行。目标群：{group}；成员：{member}。十分钟内在当前私聊发新消息：确认踢出 {token}"
        operation = (
            "command/"
            + hashlib.sha256(
                f"{adapter.account}/{event.unified_msg_origin}/{event.message_obj.message_id}".encode()
            ).hexdigest()
        )
        if ai:
            operation = (
                "ai/"
                + hashlib.sha256(
                    json.dumps(
                        [
                            adapter.account,
                            event.unified_msg_origin,
                            event.message_obj.message_id,
                            actions[action],
                            group,
                            member,
                            argument if action == "公告" else "",
                        ]
                        + ([minutes] if action == "禁言" else []),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
            )
        if not event.is_admin():
            return "拒绝：管理员授权已撤销。"
        result = await adapter.execute_moderation(
            operation,
            actions[action],
            int(group),
            member,
            argument if action == "公告" else "",
            **({"minutes": minutes} if action == "禁言" else {}),
        )
        if ai:
            return {
                "status": result.get("status", "unknown"),
                "operation_id": operation,
            }
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
