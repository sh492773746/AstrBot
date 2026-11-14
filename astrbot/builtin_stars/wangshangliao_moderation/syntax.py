"""Exact native command syntax, independent of model interpretation."""

import re

WHITELIST_COMMANDS = {"白名单", "添加白名单", "移除白名单", "选择白名单", "确认白名单"}
BLACKLIST_COMMANDS = {
    "黑名单",
    "黑名单列表",
    "添加黑名单",
    "移除黑名单",
    "选择黑名单",
    "确认黑名单",
}

REMOVED_GROUP_COMMANDS = frozenset(
    {"帮助", "群管帮助", "help", "我的权限", "sid", "黑名单禁言时长"}
)

BUSINESS_COMMANDS = {
    "关键词回复",
    "添加回复",
    "修改回复",
    "删除回复",
    "启用回复",
    "停用回复",
    "定时广告",
    "添加广告",
    "修改广告",
    "删除广告",
    "启用广告",
    "停用广告",
    "确认回复",
    "确认广告",
}
# General chat actions remain retired; explicit mute-only grants are independent.
RETIRED_CHAT_ACTIONS = frozenset({"禁言", "全员禁言", "解除全员禁言"})
RETIRED_CHAT_REPLY = (
    "未执行：普通手动禁言、全员禁言和解除全员禁言请直接在旺商聊App里操作。"
    "\n仅服务端单独授权的专门汇报成员可在对应群发送「@成员 禁言128」，表示禁言128分钟；"
    "单次1至4320分钟（最多3天），不含踢人、解禁或名单权限。"
    "\n解除单个成员禁言：本群授权管理可发送「解禁 @成员」，请从客户端选择真正的@成员。"
    "\n自动广告处罚、防刷屏不受影响。"
    "\n每日定时禁言、定时解除可直接发送「定时禁言 23:00 08:00」，插件「运行与日志 → 定时管理」可查看和暂停。"
)

GROUP_KNOWLEDGE_COMMANDS = {
    "本群知识",
    "查看群知识",
    "本群知识容量",
    "本群知识范围",
    "添加群知识",
    "修改群知识",
    "删除群知识",
    "群知识引用",
}
KNOWLEDGE_COMMANDS = GROUP_KNOWLEDGE_COMMANDS

NO_ARGUMENT = {
    *WHITELIST_COMMANDS,
    *BLACKLIST_COMMANDS,
    "成员列表",
    "禁言名单",
    "下一页",
    "能力",
    "规则",
    "违规计数",
    "全员禁言",
    "解除全员禁言",
    "定时状态",
    "暂停定时",
    "恢复定时",
    "删除定时",
    "排名",
    "今日排名",
    "排行",
    "今日排行",
    "参加抽奖",
    "抽奖状态",
    "我的邀请",
    "邀请奖励",
    "我的邀请明细",
    "我拉了谁",
    "我邀请了谁",
    "我拉的人",
    "我邀请的人",
    "邀请统计说明",
    "为什么没统计",
    "为什么邀请没统计",
    "邀请为什么没统计",
    "拉人没统计",
    "开启抽奖",
    "立即开奖",
    "取消抽奖",
    "中奖名单",
    "抽奖记录",
    "抽奖设置",
    "开启邀请统计",
    "暂停邀请统计",
    "开启邀请奖励",
    "暂停邀请奖励",
    "邀请奖励状态",
    "邀请记录",
    "邀请统计",
    "管理员列表",
    "开启广告审核",
    "关闭广告审核",
    "开启防刷屏",
    "关闭防刷屏",
    "热群状态",
    "本群额度",
    "确认本群扣费",
    "开启热群",
    "关闭热群",
    "开启群聊回复",
    "关闭群聊回复",
    "开启自主接话",
    "关闭自主接话",
    "开启冷场发话",
    "关闭冷场发话",
    "本群知识",
    "本群知识容量",
    "本群知识范围",
}
WITH_ARGUMENT = {
    *WHITELIST_COMMANDS,
    *BLACKLIST_COMMANDS - {"黑名单列表"},
    "成员搜索",
    "结果",
    "禁言",
    "解禁",
    "踢出",
    "公告",
    "确认踢出",
    "定时禁言",
    "确认定时",
    "抽奖奖励",
    "中奖人数",
    "抽奖倒计时",
    "抽奖开始时间",
    "参与上限",
    "抽奖邀请门槛",
    "领奖联系人",
    "设置邀请奖励",
    "中奖名单",
    "添加管理员",
    "移除管理员",
    "确认授权",
    "添加关键词",
    "删除关键词",
    "修改关键词",
    "添加规则",
    "修改规则",
    "删除规则",
    "启用规则",
    "停用规则",
    "测试规则",
    "本群知识",
    "查看群知识",
    "添加群知识",
    "修改群知识",
    "删除群知识",
    "群知识引用",
}
ALIASES = {
    "参与抽奖": "参加抽奖",
    "定时发言": "定时广告",
    "解除禁言": "解禁",
    "解除黑名单": "移除黑名单",
    "删黑": "移除黑名单",
    "删白": "移除白名单",
    "加白": "白名单",
    "加黑": "黑名单",
    "今日排名": "排名",
    "排行": "排名",
    "今日排行": "排名",
    "发言排行": "排名",
    "发言排行榜": "排名",
    "排行榜": "排名",
    "我拉了谁": "我的邀请明细",
    "我邀请了谁": "我的邀请明细",
    "我拉的人": "我的邀请明细",
    "我邀请的人": "我的邀请明细",
    "为什么没统计": "邀请统计说明",
    "为什么邀请没统计": "邀请统计说明",
    "邀请为什么没统计": "邀请统计说明",
    "拉人没统计": "邀请统计说明",
}

INDEX_PATTERN = r"[1-9][0-9]{0,4}"
TOKEN_PATTERN = r"[a-zA-Z0-9_-]{8,128}"
SCHEDULE_PATTERN = r"[0-9]{2}:[0-9]{2}\s+[0-9]{2}:[0-9]{2}"
INDEX_COMMANDS = {
    "选择白名单",
    "选择黑名单",
    "删除关键词",
    "删除规则",
    "启用规则",
    "停用规则",
}
INDEX_TEXT_COMMANDS = {"修改关键词", "修改规则"}
TOKEN_COMMANDS = {"确认踢出", "确认定时", "确认授权", "确认白名单", "确认黑名单"}
MENTION_PREFIX_ACTIONS = frozenset(
    {
        "禁言",
        "解禁",
        "解除禁言",
        "踢出",
        "黑名单",
        "移除黑名单",
        "解除黑名单",
        "白名单",
        "移除白名单",
        "删黑",
        "删白",
        "加白",
        "加黑",
    }
)


def argument_pattern(command):
    """Share raw argument constraints between recognition and declarations."""
    if command in INDEX_COMMANDS:
        return INDEX_PATTERN
    if command in INDEX_TEXT_COMMANDS:
        return INDEX_PATTERN + r"\s+\S.*"
    if command in TOKEN_COMMANDS:
        return TOKEN_PATTERN
    if command == "定时禁言":
        return SCHEDULE_PATTERN
    if command in {"添加白名单", "移除白名单", "添加黑名单", "移除黑名单"}:
        return r"\S.{0,63}"
    return ""


def is_removed_group_command(text):
    """Identify retired chat entries without declaring an executable command.

    Args:
        text: Original message or cached command, not quoted content.

    Returns:
        Whether the message must be consumed without business actions or AI.
    """
    if not isinstance(text, str) or len(text) > 4096:
        return False
    text = text.strip()
    if any(char in text for char in ("\n", "\r", "\u2028", "\u2029", "\x00")):
        return False
    parts = text.removeprefix("/").split(maxsplit=1)
    if not parts:
        return False
    if parts[0] in {"管理", "群管"}:
        if len(parts) == 1:
            return parts[0] == "群管"
        parts = parts[1].removeprefix("/").split(maxsplit=1)
    return bool(parts and parts[0] in REMOVED_GROUP_COMMANDS)


def recognize_command(text: str) -> str:
    """Recognize a complete, single-line command, retaining legacy spellings.

    Args:
        text: Authenticated message text, not a nickname or quoted instruction.

    Returns:
        Canonical command and arguments, or an empty string for ordinary chat.
    """
    if not isinstance(text, str) or len(text) > 4096:
        return ""
    text = text.strip()
    if text in {"/chat", "/exit"}:
        return ""
    if any(char in text for char in ("\n", "\r", "\u2028", "\u2029", "\x00")):
        return ""
    if text.startswith(("管理 ", "/管理 ")):
        return ""
    if text.startswith("/"):
        text = text[1:]
    if text in {"参与玩家福利抽奖", "参与抽奖"}:
        return "参加抽奖"
    if text.startswith("群管") and text[2:3].isspace():
        text = text[2:].lstrip()
    if text.startswith("@"):
        mute = re.fullmatch(
            r"(@[^\r\n]{1,256}?)\s+禁言(?:\s*([0-9]{1,5})(?:分钟|分)?)?[。.!！]?",
            text,
        )
        if mute:
            return "禁言 " + mute[1] + (" " + mute[2] if mute[2] else "")
        match = re.fullmatch(
            r"(@[^\r\n]{1,256}?)\s+(黑名单|移除黑名单|解除黑名单|白名单|移除白名单|删黑|删白|加白|加黑)",
            text,
        )
        if match:
            return ALIASES.get(match[2], match[2]) + " " + match[1]
    prefix, marker, remainder = text.partition("@")
    if marker and prefix in MENTION_PREFIX_ACTIONS:
        text = ALIASES.get(prefix, prefix) + " @" + remainder
    parts = text.split(maxsplit=1)
    if not parts:
        return ""
    action = ALIASES.get(parts[0], parts[0])
    argument = parts[1].strip() if len(parts) == 2 else ""
    if action in BUSINESS_COMMANDS:
        return action + (" " + argument if argument else "")
    if action == "我的邀请" and re.fullmatch(r"(?:[1-9][0-9]?|100)", argument):
        return "我的邀请 " + argument
    if ALIASES.get(action, action) == "排名":
        if not argument:
            return "排名"
        return "排名 " + argument if re.fullmatch(r"[1-9][0-9]{0,4}", argument) else ""
    if action in NO_ARGUMENT and not argument:
        return ALIASES.get(action, action)
    if action not in WITH_ARGUMENT:
        return ""
    if not argument:
        return action if action in {"禁言", "解禁", "踢出", *BLACKLIST_COMMANDS} else ""
    if action in {"禁言", "解禁", "踢出", "添加管理员", "移除管理员"} and not (
        re.fullmatch(
            r"[1-9][0-9]{0,4}(?:\s+[0-9]{1,5})?"
            if action == "禁言"
            else r"[1-9][0-9]{0,4}",
            argument,
        )
        or argument.startswith("@")
    ):
        return ""
    pattern = argument_pattern(action)
    if pattern and not re.fullmatch(pattern, argument):
        return ""
    return action + " " + argument
