"""Exact native command syntax, independent of model interpretation."""

import re

NO_ARGUMENT = {
    "帮助",
    "群管帮助",
    "help",
    "sid",
    "我的权限",
    "群列表",
    "成员列表",
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
    "开启抽奖",
    "立即开奖",
    "取消抽奖",
    "中奖名单",
    "抽奖记录",
    "抽奖设置",
    "开启邀请奖励",
    "暂停邀请奖励",
    "邀请奖励状态",
    "邀请记录",
}
WITH_ARGUMENT = {
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
}
ALIASES = {
    "群管帮助": "帮助",
    "help": "帮助",
    "sid": "我的权限",
    "今日排名": "排名",
    "排行": "排名",
    "今日排行": "排名",
}


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
    if any(char in text for char in ("\n", "\r", "\u2028", "\u2029", "\x00")):
        return ""
    if text.startswith("/"):
        text = text[1:]
    if text == "群管":
        return "帮助"
    if text.startswith("群管") and text[2:3].isspace():
        text = text[2:].lstrip()
    for action in ("禁言", "解禁", "踢出"):
        if text.startswith(action + "@"):
            text = action + " " + text[len(action) :]
            break
    parts = text.split(maxsplit=1)
    if not parts:
        return ""
    action = parts[0]
    argument = parts[1].strip() if len(parts) == 2 else ""
    if ALIASES.get(action, action) == "排名":
        if not argument:
            return "排名"
        return "排名 " + argument if re.fullmatch(r"[1-9][0-9]{0,4}", argument) else ""
    if action in NO_ARGUMENT and not argument:
        return ALIASES.get(action, action)
    if action not in WITH_ARGUMENT:
        return ""
    if not argument:
        return action if action in {"禁言", "解禁", "踢出"} else ""
    if action in {"禁言", "解禁", "踢出"} and not (
        re.fullmatch(
            r"[1-9][0-9]{0,4}(?:\s+[0-9]{1,5})?"
            if action == "禁言"
            else r"[1-9][0-9]{0,4}",
            argument,
        )
        or argument.startswith("@")
    ):
        return ""
    if action == "选择群" and not re.fullmatch(r"[1-9][0-9]{0,4}", argument):
        return ""
    if action in {"确认踢出", "确认定时"} and not re.fullmatch(
        r"[a-zA-Z0-9_-]{8,128}", argument
    ):
        return ""
    if action == "定时禁言" and not re.fullmatch(
        r"[0-9]{2}:[0-9]{2}\s+[0-9]{2}:[0-9]{2}", argument
    ):
        return ""
    return action + " " + argument
