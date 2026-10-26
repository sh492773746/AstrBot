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
    "黑名单禁言时长",
}

BUSINESS_COMMANDS = {
    "关键词回复",
    "添加回复",
    "修改回复",
    "删除回复",
    "启用回复",
    "停用回复",
    "定时广告",
    "广告图片",
    "添加广告",
    "修改广告",
    "删除广告",
    "启用广告",
    "停用广告",
    "确认回复",
    "确认广告",
    "客服设置",
    "客服话术",
    "客服风格",
    "客服长度",
    "客服链接",
    "客服知识",
    "添加客服知识",
    "删除客服知识",
    "确认客服",
}
# Recognized only to explain their retirement; public dispatch never executes them.
RETIRED_CHAT_ACTIONS = frozenset({"禁言", "全员禁言", "解除全员禁言"})
RETIRED_CHAT_REPLY = (
    "手动禁言、全员禁言和解除全员禁言请直接在旺商聊App里操作，机器人不再接受这些聊天命令。"
    "\n解除单个成员禁言：本群授权管理可发送「解禁 @成员」，请从客户端选择真正的@成员。"
    "\n自动广告处罚、防刷屏不受影响。"
    "\n每日定时禁言、定时解除可直接发送「定时禁言 23:00 08:00」，插件「运行与日志 → 定时管理」可查看和暂停。"
)

PERSONAL_KNOWLEDGE_COMMANDS = {
    "我的知识",
    "查看我的知识",
    "我的知识容量",
    "添加我的知识",
    "修改我的知识",
    "删除我的知识",
}
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
KNOWLEDGE_COMMANDS = PERSONAL_KNOWLEDGE_COMMANDS | GROUP_KNOWLEDGE_COMMANDS

NO_ARGUMENT = {
    *WHITELIST_COMMANDS,
    *BLACKLIST_COMMANDS,
    "客服列表",
    "退出客服",
    "结账",
    "结账结果",
    "菜单",
    "私聊菜单",
    "帮助",
    "群管帮助",
    "help",
    "sid",
    "我的权限",
    "群列表",
    "成员列表",
    "禁言名单",
    "下一页",
    "能力",
    "规则",
    "违规计数",
    "开发门禁",
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
    "我的套餐",
    "我的额度",
    "套餐说明",
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
    "我的知识",
    "我的知识容量",
    "本群知识",
    "本群知识容量",
    "本群知识范围",
}
WITH_ARGUMENT = {
    *WHITELIST_COMMANDS,
    *BLACKLIST_COMMANDS - {"黑名单列表"},
    "选择客服",
    "确认已付款",
    "确认修改",
    "选择群",
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
    "我的知识",
    "查看我的知识",
    "添加我的知识",
    "修改我的知识",
    "删除我的知识",
    "本群知识",
    "查看群知识",
    "添加群知识",
    "修改群知识",
    "删除群知识",
    "群知识引用",
}
ALIASES = {
    "定时发言": "定时广告",
    "完整菜单": "私聊菜单",
    "解除禁言": "解禁",
    "解除黑名单": "移除黑名单",
    "我的额度": "我的套餐",
    "菜单": "私聊菜单",
    "群管帮助": "帮助",
    "help": "帮助",
    "sid": "我的权限",
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
FINANCE_CODE_PATTERN = r"[0-9]{6}"
SCHEDULE_PATTERN = r"[0-9]{2}:[0-9]{2}\s+[0-9]{2}:[0-9]{2}"
INDEX_COMMANDS = {
    "选择白名单",
    "选择黑名单",
    "选择群",
    "选择客服",
    "删除关键词",
    "删除规则",
    "启用规则",
    "停用规则",
}
INDEX_TEXT_COMMANDS = {"修改关键词", "修改规则"}
TOKEN_COMMANDS = {"确认踢出", "确认定时", "确认授权", "确认白名单", "确认黑名单"}
FINANCE_CODE_COMMANDS = {"确认已付款", "确认修改"}


def argument_pattern(command):
    """Share raw argument constraints between recognition and declarations."""
    if command in INDEX_COMMANDS:
        return INDEX_PATTERN
    if command in INDEX_TEXT_COMMANDS:
        return INDEX_PATTERN + r"\s+\S.*"
    if command in TOKEN_COMMANDS:
        return TOKEN_PATTERN
    if command in FINANCE_CODE_COMMANDS:
        return FINANCE_CODE_PATTERN
    if command == "定时禁言":
        return SCHEDULE_PATTERN
    if command == "黑名单禁言时长":
        return r"[0-9]{1,5}\s*(?:分钟|小时|天)"
    if command in {"添加白名单", "移除白名单", "添加黑名单", "移除黑名单"}:
        return r"\S.{0,63}"
    return ""


INVITATION_NUMBER = r"(?:[1-9][0-9]?|100)"
INVITATION_DETAIL = (
    r"(?:明细|我拉了谁|我邀请了谁|"
    r"(?:(?:昨日|昨天|今日|今天)(?:邀请(?:好友)?(?:记录|明细)|明细)))"
)


def private_invitation_request(text):
    """Parse bounded own-record requests without accepting account or group IDs.

    Args:
        text: Actual sender message, not model-supplied tool arguments.

    Returns:
        Number, detail mode and period, or None for unsupported text.
    """
    if not isinstance(text, str) or len(text) > 256:
        return None
    text = text.strip().removeprefix("/")
    if any(c in text for c in ("\n", "\r", "\u2028", "\u2029", "\x00")):
        return None
    match = re.fullmatch(
        r"(?:我的邀请\s+)?(?P<number>"
        + INVITATION_NUMBER
        + r")(?:\s+(?P<detail>"
        + INVITATION_DETAIL
        + r"))?",
        text,
    )
    number = int(match["number"]) if match else None
    suffix = match["detail"] if match else None
    if not match:
        if text == "我的邀请":
            return {"number": None, "detail": False, "period": "all"}
        if re.fullmatch(r"我的邀请\s+" + INVITATION_DETAIL, text):
            suffix = text.split(maxsplit=1)[1]
        elif re.fullmatch(r"(?:昨日|昨天|今日|今天)邀请(?:好友)?(?:记录|明细)", text):
            suffix = text
        else:
            return None
    period = (
        "yesterday"
        if suffix and suffix.startswith(("昨日", "昨天"))
        else "day"
        if suffix and suffix.startswith(("今日", "今天"))
        else None
    )
    return {"number": number, "detail": True if suffix else None, "period": period}


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
    # These exact public private-chat controls use the existing menu admission.
    # Main handles the original text; group invocation remains private-only.
    if text in {"/chat", "/exit"}:
        return "私聊菜单"
    if any(char in text for char in ("\n", "\r", "\u2028", "\u2029", "\x00")):
        return ""
    if text.startswith(("管理 ", "/管理 ")):
        from .group_management import management_command

        # Admit only a fixed envelope; Main checks the actual group grant.
        return "私聊菜单" if management_command(text) else ""
    if text.startswith("/"):
        text = text[1:]
    if text == "参与玩家福利抽奖":
        return "参加抽奖"
    own = private_invitation_request(text)
    if own and not text.isdigit():
        return "我的邀请" + (
            " " + str(own["number"]) if own["number"] is not None else ""
        )
    if text == "群管":
        return "帮助"
    if text.startswith("群管") and text[2:3].isspace():
        text = text[2:].lstrip()
    if text.startswith("@"):
        match = re.fullmatch(
            r"(@[^\r\n]{1,256}?)\s+(黑名单|移除黑名单|解除黑名单|白名单)", text
        )
        if match:
            return ALIASES.get(match[2], match[2]) + " " + match[1]
    for action in (
        "禁言",
        "解禁",
        "解除禁言",
        "踢出",
        "黑名单",
        "移除黑名单",
        "解除黑名单",
        "白名单",
    ):
        if text.startswith(action + "@"):
            text = ALIASES.get(action, action) + " " + text[len(action) :]
            break
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
