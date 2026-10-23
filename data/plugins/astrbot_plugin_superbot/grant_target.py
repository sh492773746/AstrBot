"""Resolve grant aliases to verified, immutable Telegram user IDs."""

import re

from telegram.error import TelegramError

from .store import Rejected


async def resolve(runtime, target, expected=None):
    """Resolve a known username and verify its current account before granting.

    Args:
        runtime: Bot runtime with a private-user label store.
        target: Numeric UID, @username, or t.me username link.
        expected: Previously confirmed UID that must not change.

    Returns:
        Numeric target and optional verified username for the confirmation.
    """
    target = str(target).strip()
    if re.fullmatch(r"[1-9][0-9]{0,19}", target):
        return {"target": str(int(target))}
    match = re.fullmatch(
        r"(?:@|(?:https?://)?t\.me/)([A-Za-z][A-Za-z0-9_]{3,31})/?",
        target,
        re.IGNORECASE,
    )
    if not match:
        raise Rejected(
            "请填写数字 UID、@用户名或 t.me/用户名，不支持群邀请或消息链接。"
        )
    username = match[1].lower()
    rows = runtime.store.db.execute(
        "SELECT uid FROM user_labels WHERE lower(username)=?", (username,)
    ).fetchall()
    if len(rows) != 1:
        raise Rejected(
            "未找到唯一账号，请让对方先私聊机器人发送 /start，再重新填写；也可使用数字 UID。"
        )
    uid = rows[0]["uid"]
    if expected is not None and uid != str(expected):
        raise Rejected("目标账号已变化，请重新填写授权信息。")
    try:
        chat = await runtime.bot.get_chat(
            int(uid), read_timeout=10, write_timeout=10, connect_timeout=10
        )
    except TelegramError:
        raise Rejected(
            "暂时无法核对账号，请让对方先私聊机器人发送 /start，稍后重试。"
        ) from None
    if (
        chat.type != "private"
        or str(chat.id) != uid
        or (chat.username or "").lower() != username
    ):
        raise Rejected(
            "用户名已变更或账号不匹配，请让对方重新发送 /start，再重新填写。"
        )
    return {"target": uid, "target_username": username}
