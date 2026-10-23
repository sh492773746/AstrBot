"""Durable, group-scoped reply keyboards and non-game navigation."""

import asyncio
import hashlib
from pathlib import Path

from telegram import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter

from .command_router import classify
from .store import Rejected, encode

QUERIES = {
    "💰 积分": "points",
    "🎁 签到": "checkin",
    "📜 历史": "history",
    "🧾 流水": "flow",
    "📊 输赢": "profit",
}
OTHER = {
    "🎨 制作头像",
    "💬 客服",
    "❓ 帮助",
    "帮助",
    "客服",
    "/help",
    "我的",
    "👤 我的",
    "/my",
}
BUTTON_STYLES = {
    "🎮 玩法大全": "primary",
    "💰 积分": "success",
    "🎁 签到": "success",
    "📜 历史": "danger",
    "🧾 流水": "danger",
    "📊 输赢": "danger",
    "🎨 制作头像": "primary",
    "客服": "danger",
    "帮助": "primary",
}


class GroupKeyboard:
    """Publish keyboards only to enrolled groups and persist uncertain sends."""

    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        self.lock = asyncio.Lock()
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS group_keyboard_settings(
                chat TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                version INTEGER NOT NULL DEFAULT 1,
                updated REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS group_keyboards(
                chat TEXT PRIMARY KEY,signature TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',message INTEGER,
                next REAL NOT NULL DEFAULT 0,request INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '');
            UPDATE group_keyboards SET status='review',error='InterruptedSend'
                WHERE status='sending';
        """)
        columns = {
            row["name"]
            for row in self.store.db.execute("PRAGMA table_info(group_keyboards)")
        }
        if "attempts" not in columns:
            self.store.db.execute(
                "ALTER TABLE group_keyboards ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"
            )

    def render(self, chat):
        """Build shared navigation without user balances or private admin entries.

        Args:
            chat: Registered group identifier.

        Returns:
            Keyboard markup, content labels and public explanatory text.
        """
        group = self.store.db.execute(
            "SELECT g.enabled,g.title,COALESCE(s.enabled,0) AS keyboard_enabled "
            "FROM mod_groups g LEFT JOIN group_keyboard_settings s ON s.chat=g.chat "
            "WHERE g.chat=?",
            (str(chat),),
        ).fetchone()
        if not group or not group["enabled"] or not group["keyboard_enabled"]:
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
        activities = getattr(self.runtime, "activities", None)
        if activities and activities.config(chat)["enabled"]:
            rows.extend([["积分排名", "活跃统计"], ["群活动"]])
        if available["points"]:
            rows.append(["💰 积分", "🎁 签到"])
        if available["activate"] or available["k3"]:
            rows.extend([["📜 历史"], ["🧾 流水", "📊 输赢"]])
        if self.store.get("modules", {}).get("moderation"):
            if available["avatar"]:
                rows.append(["🎨 制作头像"])
        rows.append(["客服", "帮助"])
        rows.append(["👤 我的"])
        title = " ".join(str(group["title"] or chat).split())[:60]
        # Keep the keyboard anchor short; rules and command details live in /help.
        text = f"🎛 {title} · 群专属键盘"
        return (
            ReplyKeyboardMarkup(
                [
                    [
                        KeyboardButton(
                            label,
                            api_kwargs={"style": BUTTON_STYLES.get(label, "primary")},
                        )
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

    async def help(self, update):
        """Render the same folded player help used by the visible help button."""
        message = getattr(update, "message", None)
        user = getattr(update, "effective_user", None)
        chat = getattr(update, "effective_chat", None)
        if (
            not message
            or not user
            or not chat
            or chat.type != "supergroup"
            or user.is_bot
        ):
            return False
        await self.runtime.community.member(
            str(user.id), str(chat.id), require_moderation=False
        )
        from .availability import refresh

        await refresh(self.runtime, chat.id)
        _, labels, _ = self.render(chat.id)
        if not self.store.db.execute(
            "SELECT 1 FROM mod_groups WHERE chat=? AND enabled=1",
            (str(chat.id),),
        ).fetchone():
            raise Rejected("本群功能已停用，暂时无法查看帮助。")
        await self.runtime.group_game.render(
            update,
            (Path(__file__).parent / "docs/player-help.md").read_text(),
            fold_sections=True,
        )
        return True

    async def sync(self, chat=None):
        """Publish changed keyboards once without user-triggered resend commands.

        Args:
            chat: Optional single group to refresh.
        """
        async with self.lock:
            groups = self.store.db.execute(
                "SELECT g.chat,g.enabled,COALESCE(s.enabled,0) AS keyboard_enabled,"
                "k.status,k.signature,k.next "
                "FROM mod_groups g "
                "LEFT JOIN group_keyboard_settings s ON s.chat=g.chat "
                "LEFT JOIN group_keyboards k ON k.chat=g.chat "
                "WHERE ((g.enabled=1 AND COALESCE(s.enabled,0)=1) "
                "OR k.status='sent') AND (? IS NULL OR g.chat=?)",
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
                if group["status"] == "sending":
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
                            "error='',attempts=attempts+1",
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
                        "UPDATE group_keyboards SET status='sent',message=?,next=0,error='',attempts=0 WHERE chat=?",
                        (message.message_id, group["chat"]),
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
                        "UPDATE group_keyboards SET status='blocked',next=?,error=? WHERE chat=?",
                        (self.store.clock() + 600, type(exc).__name__, group["chat"]),
                    )
                except (NetworkError, TimeoutError, ConnectionError, ValueError) as exc:
                    self.store.db.execute(
                        "UPDATE group_keyboards SET status='review',next=?,error=? WHERE chat=?",
                        (self.store.clock() + 30, type(exc).__name__, group["chat"]),
                    )
                sent_count += 1
                if sent_count >= 4:
                    break

    async def _is_admin(self, chat, uid):
        member = await self.runtime.bot.get_chat_member(chat, int(uid))
        if member.status not in {"creator", "administrator"}:
            return False
        try:
            await self.runtime.tenants.verify_designation(uid, chat, member.status)
        except Rejected:
            return False
        return True

    async def set_for_chat(self, chat_id, user_id, enabled):
        """Set a group's keyboard after rechecking Telegram administrator status."""
        from .group_authority import GroupAuthority, require

        chat_id = str(chat_id)
        if type(enabled) is not bool:
            raise Rejected("无效的键盘设置。")
        async with GroupAuthority(self.runtime).session(user_id, chat_id):
            with self.store.tx() as db:
                require(self.store, user_id, chat_id, db=db)
                db.execute(
                    "INSERT INTO group_keyboard_settings(chat,enabled,version,updated) "
                    "VALUES(?,?,1,?) ON CONFLICT(chat) DO UPDATE SET enabled=excluded.enabled,"
                    "version=version+1,updated=excluded.updated",
                    (chat_id, int(enabled), self.store.clock()),
                )
                saved_version = db.execute(
                    "SELECT version FROM group_keyboard_settings WHERE chat=?",
                    (chat_id,),
                ).fetchone()[0]
                db.execute(
                    "UPDATE mod_groups SET version=version+1 WHERE chat=?", (chat_id,)
                )
                self.store.audit(
                    db,
                    user_id,
                    "group_keyboard_switch",
                    {"chat": chat_id, "enabled": enabled},
                )
        if enabled:
            try:
                await self.sync(chat_id)
                state = self.store.db.execute(
                    "SELECT status,error FROM group_keyboards WHERE chat=?",
                    (chat_id,),
                ).fetchone()
                if state and state["status"] in {"blocked", "review"}:
                    raise RuntimeError(state["error"] or "KeyboardDeliveryFailed")
            except Exception:
                with self.store.tx() as db:
                    db.execute(
                        "UPDATE group_keyboard_settings SET enabled=0,version=version+1,updated=? WHERE chat=? AND version=?",
                        (self.store.clock(), chat_id, saved_version),
                    )
                raise Rejected("群键盘发送失败，请确认机器人有发言权限后重试。")
            return True
        await self.runtime.bot.send_message(
            chat_id=int(chat_id),
            text="群键盘已关闭，可发送“键盘开启”重新打开。",
            reply_markup=ReplyKeyboardRemove(),
            disable_notification=True,
        )
        return True

    async def set_enabled(self, update, enabled):
        """Enable or disable this group's keyboard from a group message."""
        chat = getattr(update, "effective_chat", None)
        user = getattr(update, "effective_user", None)
        if not chat or not user or chat.type != "supergroup":
            raise Rejected("群键盘只能在超级群中设置。")
        return await self.set_for_chat(chat.id, user.id, enabled)

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
        if word in {"键盘开启", "开启键盘"}:
            return await self.set_enabled(update, True)
        if word in {"键盘关闭", "关闭键盘"}:
            return await self.set_enabled(update, False)
        if word not in QUERIES and word not in OTHER:
            return False
        chat, uid = str(update.effective_chat.id), str(user.id)
        if not 0 <= self.store.clock() - message.date.timestamp() <= 60:
            return True
        await self.runtime.community.member(uid, chat, require_moderation=False)
        from .availability import refresh

        await refresh(self.runtime, chat)
        _, labels, _ = self.render(chat)
        if (
            word in {"帮助", "❓ 帮助", "/help"}
            or classify(word, self.runtime.bot.username) == "help"
        ):
            return await self.help(update)
        if classify(word, self.runtime.bot.username) == "account" or word == "👤 我的":
            await self.runtime.group_game.action(update, {"action": "account"})
            return True
        if word == "💬 客服":
            word = "客服"
        if word == "客服":
            await self.runtime.group_game.render(
                update,
                "💬 客服\n"
                f"请联系本群管理员或私聊 @{self.runtime.bot.username}。\n"
                "积分、广告、退款和群内业务由本群管理员负责。",
                fold_sections=True,
            )
            return True
        if word not in labels:
            # Query/help text remains available while the optional keyboard is
            # disabled; only old keyboard labels are ignored in that state.
            if classify(word, self.runtime.bot.username) in {
                "points",
                "checkin",
                "history",
                "flow",
                "profit",
                "service",
            }:
                return False
            raise Rejected("本群此功能已停用，键盘会自动更新。")
        if word in QUERIES:
            await self.runtime.group_game.text_game.query_points(update, QUERIES[word])
        elif word == "🎨 制作头像":
            await self.runtime.avatar.message(update, "/avatar")
        else:
            await self.runtime.group_game.render(
                update,
                "💬 客服\n"
                f"请私聊 @{self.runtime.bot.username}，发送 /start 后点击「客服」。\n"
                "使用说明与玩法请在本群发送「帮助」。客服只解答，不代操作。",
                fold_sections=True,
            )
        return True
