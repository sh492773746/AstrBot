"""Shared command catalog and deterministic ownership of group input."""

import re
from dataclasses import dataclass

SYSTEM_COMMANDS = {
    "/my": "account",
    "/help": "help",
    "/points": "points",
    "/checkin": "checkin",
    "/history": "history",
    "/flow": "flow",
    "/profit": "profit",
    "/service": "service",
    "/points_rank": "points_rank",
    "/stats": "stats",
    "/stat": "stats",
    "/stat_week": "stats_week",
    "/lottery": "lottery",
}

NATURAL_COMMANDS = {
    "我的": "account",
    "帮助": "help",
    "积分": "points",
    "积分查询": "points",
    "签到": "checkin",
    "历史": "history",
    "流水": "flow",
    "ls": "flow",
    "输赢": "profit",
    "sy": "profit",
    "客服": "service",
    "玩法大全": "games",
    "取消": "exit",
    "退出": "exit",
    "积分排名": "points_rank",
    "活跃统计": "stats",
    "抽奖": "lottery",
    "群活动": "lottery",
}

CATALOG = {
    "help": "帮助与可用命令",
    "my": "我的本群记录",
    "service": "客服与本群联系",
    "points": "本群积分",
    "checkin": "本群签到",
    "history": "当前玩法历史",
    "flow": "近期流水",
    "profit": "今日与昨日输赢",
    "points_rank": "本群积分排名",
    "stats": "本群活跃统计",
    "lottery": "本群活动",
}
COMMAND = re.compile(r"^/([a-z][a-z0-9_]{0,31})(?:@([a-z0-9_]{5,32}))?$", re.I)


@dataclass(frozen=True)
class InputRoute:
    """Ownership established once, before any business parser runs."""

    accepted: bool
    reason: str = ""
    command: str = ""
    explicit: bool = False


def ownership(message, username, bot_id, *, strict=False):
    """Determine whether group input may enter this bot's business workflows.

    Args:
        message: Original Telegram message, not quoted or caption text.
        username: Live bot username.
        bot_id: Live bot numeric identity.
        strict: Whether bare group slash commands are disabled.

    Returns:
        Immutable routing decision; rejected input must not reach rewards or AI.
    """
    text = (getattr(message, "text", None) or "").strip()
    token = text.split(maxsplit=1)[0] if text else ""
    command, explicit = "", False
    if token.startswith("/"):
        match = COMMAND.fullmatch(token)
        if not match:
            return InputRoute(False, "malformed_command")
        command = "/" + match[1].lower()
        if match[2]:
            if match[2].casefold() != (username or "").casefold():
                return InputRoute(False, "foreign_command")
            explicit = True
        elif strict:
            return InputRoute(False, "strict_bare_command")
    if any(
        getattr(message, key, None)
        for key in (
            "forward_origin",
            "forward_date",
            "is_automatic_forward",
        )
    ):
        return InputRoute(False, "forward")
    reply = getattr(message, "reply_to_message", None)
    author = getattr(reply, "from_user", None)
    if (
        author
        and getattr(author, "is_bot", False)
        and str(getattr(author, "id", "")) != str(bot_id)
        and not explicit
    ):
        return InputRoute(False, "foreign_reply")
    return InputRoute(True, command=command, explicit=explicit)


def classify(text: str, username=None) -> str | None:
    """Classify exact aliases without stealing a foreign command.

    Args:
        text: Original user text.
        username: Live bot username for qualified commands.

    Returns:
        Registered action, or None.
    """
    value = " ".join((text or "").strip().split())
    if not value:
        return None
    token = value.split(" ", 1)[0]
    if token.startswith("/"):
        match = COMMAND.fullmatch(token)
        if not match or (
            match[2] and (not username or match[2].casefold() != username.casefold())
        ):
            return None
        return SYSTEM_COMMANDS.get("/" + match[1].lower())
    return NATURAL_COMMANDS.get(value.lower())
