"""Durable, group-scoped reply keyboards and non-game navigation."""

import asyncio
import hashlib
from pathlib import Path

from telegram import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter

from .store import Rejected, encode

QUERIES = {
    "💰 积分": "points",
    "🎁 签到": "checkin",
    "📜 历史": "history",
    "🧾 流水": "flow",
    "📊 输赢": "profit",
}
OTHER = {
    "📖 玩法规则",
    "🎨 制作头像",
    "📋 群规",
    "📝 常用说明",
    "💬 客服",
    "❓ 帮助",
    "帮助",
    "客服",
    "/help",
}


class GroupKeyboard:
    """Publish keyboards only to enrolled groups and persist uncertain sends."""

    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        self.lock = asyncio.Lock()
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS group_keyboards(
                chat TEXT PRIMARY KEY,signature TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',message INTEGER,
                next REAL NOT NULL DEFAULT 0,request INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '');
            UPDATE group_keyboards SET status='review',error='InterruptedSend'
                WHERE status='sending';
        """)

    def render(self, chat):
        """Build shared navigation without user balances or private admin entries.

        Args:
            chat: Registered group identifier.

        Returns:
            Keyboard markup, content labels and public explanatory text.
        """
        group = self.store.db.execute(
            "SELECT enabled,title FROM mod_groups WHERE chat=?", (str(chat),)
        ).fetchone()
        if not group or not group["enabled"]:
            return ReplyKeyboardRemove(), [], "本群功能已停用，群键盘已收起。"
        from .availability import snapshot

        available = snapshot(self.runtime, chat)
        rows = []
        games = any(
            available[key]
            for key in ("wheel", "slots", "mines", "k3", "activate", "duel")
        )
        if games:
            rows.append(["🎮 玩法大全"])
        if available["points"]:
            rows.append(["💰 积分", "🎁 签到"])
        if available["activate"] or available["k3"]:
            rows.extend([["📜 历史"], ["🧾 流水", "📊 输赢"]])
        if self.store.get("modules", {}).get("moderation"):
            community = [
                label
                for key, label in (("rules", "📋 群规"), ("notes", "📝 常用说明"))
                if available[key]
            ]
            if community:
                rows.append(community)
            if available["avatar"]:
                rows.append(["🎨 制作头像"])
        rows.append(["客服", "帮助"])
        title = " ".join(str(group["title"] or chat).split())[:60]
        text = (
            f"🎛 {title} · 群专属键盘\n"
            "玩法大全只收录游戏；积分、签到与查询请用下方键盘。\n"
            "点击输入框旁的键盘图标即可收起或展开。"
        )
        return (
            ReplyKeyboardMarkup(
                [
                    [
                        KeyboardButton(label, api_kwargs={"style": "primary"})
                        for label in row
                    ]
                    for row in rows
                ],
                resize_keyboard=True,
                is_persistent=False,
                one_time_keyboard=False,
                selective=False,
            ),
            [label for row in rows for label in row],
            text,
        )

    async def sync(self, chat=None):
        """Publish changed keyboards once without user-triggered resend commands.

        Args:
            chat: Optional single group to refresh.
        """
        async with self.lock:
            groups = self.store.db.execute(
                "SELECT g.chat,g.enabled,k.status,k.signature,k.next "
                "FROM mod_groups g LEFT JOIN group_keyboards k ON k.chat=g.chat "
                "WHERE (g.enabled=1 OR k.chat IS NOT NULL) AND (? IS NULL OR g.chat=?)",
                (
                    str(chat) if chat is not None else None,
                    str(chat) if chat is not None else None,
                ),
            ).fetchall()
            sent_count = 0
            for group in groups:
                from .availability import refresh

                await refresh(self.runtime, group["chat"])
                now = self.store.clock()
                if group["next"] and group["next"] > now:
                    continue
                if group["status"] in {
                    "sending",
                    "review",
                    "blocked",
                }:
                    continue
                markup, _, text = self.render(group["chat"])
                signature = hashlib.sha256(
                    encode([text, markup.to_dict()]).encode()
                ).hexdigest()
                if group["status"] == "sent" and group["signature"] == signature:
                    continue
                try:
                    info = await self.runtime.bot.get_chat(group["chat"])
                    if info.type != "supergroup":
                        continue
                    # Re-render after the network await so disabled groups get removal.
                    markup, _, text = self.render(group["chat"])
                    signature = hashlib.sha256(
                        encode([text, markup.to_dict()]).encode()
                    ).hexdigest()
                    with self.store.tx() as db:
                        db.execute(
                            "INSERT INTO group_keyboards(chat,signature,status) VALUES(?,?,'sending') "
                            "ON CONFLICT(chat) DO UPDATE SET signature=excluded.signature,status='sending',"
                            "error=''",
                            (group["chat"], signature),
                        )
                    message = await self.runtime.bot.send_message(
                        chat_id=group["chat"],
                        text=text,
                        reply_markup=markup,
                        disable_notification=True,
                        read_timeout=10,
                        write_timeout=10,
                        connect_timeout=5,
                    )
                    if type(message.message_id) is not int or message.message_id <= 0:
                        raise ValueError("MissingMessageId")
                    self.store.db.execute(
                        "UPDATE group_keyboards SET status='sent',message=?,next=?,error='' WHERE chat=?",
                        (message.message_id, now + 30, group["chat"]),
                    )
                except RetryAfter as exc:
                    delay = (
                        exc.retry_after.total_seconds()
                        if hasattr(exc.retry_after, "total_seconds")
                        else exc.retry_after
                    )
                    self.store.db.execute(
                        "UPDATE group_keyboards SET status='pending',next=?,error='RateLimit' WHERE chat=?",
                        (self.store.clock() + delay, group["chat"]),
                    )
                except (Forbidden, BadRequest) as exc:
                    self.store.db.execute(
                        "UPDATE group_keyboards SET status='blocked',error=? WHERE chat=?",
                        (type(exc).__name__, group["chat"]),
                    )
                except (NetworkError, TimeoutError, ConnectionError, ValueError) as exc:
                    self.store.db.execute(
                        "UPDATE group_keyboards SET status='review',error=? WHERE chat=?",
                        (type(exc).__name__, group["chat"]),
                    )
                sent_count += 1
                if sent_count >= 4:
                    break

    async def message(self, update):
        """Route keyboard labels using the real sender and original message ID.

        Args:
            update: New non-forwarded supergroup user text.

        Returns:
            Whether the keyboard consumed the message.
        """
        message = getattr(update, "message", None)
        user = getattr(update, "effective_user", None)
        if (
            not message
            or not user
            or user.is_bot
            or update.effective_chat.type != "supergroup"
            or getattr(message, "sender_chat", None)
            or getattr(message, "forward_origin", None)
            or getattr(message, "forward_date", None)
            or getattr(message, "is_automatic_forward", False)
        ):
            return False
        word = (message.text or "").strip()
        if word not in QUERIES and word not in OTHER:
            return False
        chat, uid = str(update.effective_chat.id), str(user.id)
        if not 0 <= self.store.clock() - message.date.timestamp() <= 60:
            return True
        await self.runtime.community.member(uid, chat, require_moderation=False)
        from .availability import refresh

        await refresh(self.runtime, chat)
        _, labels, _ = self.render(chat)
        if word in {"帮助", "❓ 帮助", "📖 玩法规则", "/help"} and "帮助" in labels:
            await self.runtime.group_game.render(
                update,
                (Path(__file__).parent / "docs/player-help.md").read_text(),
                fold_sections=True,
            )
            return True
        if word == "💬 客服":
            word = "客服"
        if word not in labels:
            raise Rejected("本群此功能已停用，键盘会自动更新。")
        if word in QUERIES:
            await self.runtime.group_game.text_game.query_points(update, QUERIES[word])
        elif word == "🎨 制作头像":
            await self.runtime.avatar.message(update, "/avatar")
        elif word in {"📋 群规", "📝 常用说明"}:
            from .community_ui import group_command

            await group_command(
                self.runtime, update, "/rules" if word == "📋 群规" else "/notes"
            )
        else:
            await self.runtime.group_game.render(
                update,
                "💬 客服\n"
                f"请私聊 @{self.runtime.bot.username}，发送 /start 后点击「客服」。\n"
                "使用说明与玩法请在本群发送「帮助」。客服只解答，不代操作。",
                fold_sections=True,
            )
        return True
