"""Platform plain-text formatting shared with the transport boundary."""

from astrbot.core.platform.sources.wangshangliao.text import (
    PLAIN_TEXT_INSTRUCTION,
    plain_text,
    redact_reply,
    strip_reasoning_markup,
)

_GROUP_COMMAND_ICONS = {
    "参加抽奖": "🎉",
    "抽奖状态": "🎁",
    "我的邀请": "🎟️",
    "邀请奖励": "🎁",
    "排名": "🏆",
    "禁言": "🔇",
    "解禁": "🔊",
    "踢出": "🚪",
    "公告": "📢",
    "全员禁言": "🔒",
    "解除全员禁言": "🔓",
    "帮助": "📋",
    "群管帮助": "📋",
}

_GROUP_ERROR_PREFIXES = (
    "无权限",
    "拒绝",
    "错误",
    "失败",
    "尚未",
    "没有",
    "无效",
    "未知",
    "请先",
    "当前没有",
    "已经",
    "不能",
)


def format_group_command_reply(command: str, response: str) -> str:
    """Add one semantic marker to plain-text group command responses."""
    existing_markers = (
        "🏆",
        "🎉",
        "🎁",
        "🎟️",
        "🔇",
        "🔊",
        "🚪",
        "📢",
        "🔒",
        "🔓",
        "📋",
        "⚠️",
        "🚫",
    )
    if not response or response.startswith(existing_markers):
        return response

    parts = command.strip().removeprefix("/").split(maxsplit=1)
    action = parts[0] if parts else ""
    if action == "群管":
        action = parts[1].split(maxsplit=1)[0] if len(parts) > 1 else action
    if response.startswith(("无权限", "拒绝")):
        icon = "🚫"
    elif action == "参加抽奖":
        if response == "参加抽奖成功。":
            icon = "🎉"
        elif response == "你已参加本次抽奖，请勿重复报名。":
            icon = "📋"
        else:
            icon = "⚠️"
    elif response.startswith(_GROUP_ERROR_PREFIXES):
        icon = "⚠️"
    else:
        icon = _GROUP_COMMAND_ICONS.get(action)
    return f"{icon} {response}" if icon else response


__all__ = [
    "PLAIN_TEXT_INSTRUCTION",
    "format_group_command_reply",
    "plain_text",
    "redact_reply",
    "strip_reasoning_markup",
]
