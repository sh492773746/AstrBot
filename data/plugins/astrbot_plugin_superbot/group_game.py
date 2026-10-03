"""Group text orders and existing non-betting queries."""

from pathlib import Path

from telegram import InlineKeyboardButton as Button
from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest

from .game_broadcast import GameBroadcast
from .rich_text import cards, send_html
from .store import Rejected
from .text_game import TextGame


class GroupGame:
    """Retain historic tables without running the retired announcement workflow."""

    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS gg_forms(
                chat TEXT NOT NULL,uid TEXT NOT NULL,payload TEXT NOT NULL,
                expires REAL NOT NULL,PRIMARY KEY(chat,uid));
            CREATE TABLE IF NOT EXISTS gg_receipts(
                op TEXT PRIMARY KEY,chat TEXT NOT NULL,uid TEXT NOT NULL,
                issue INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'pending');
            CREATE TABLE IF NOT EXISTS gg_group_room(
                chat TEXT PRIMARY KEY,room TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS gg_players(uid TEXT PRIMARY KEY,name TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS gg_flows(
                chat TEXT NOT NULL,uid TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(chat,uid));
            CREATE TABLE IF NOT EXISTS gg_cooldowns(
                chat TEXT NOT NULL,uid TEXT NOT NULL,action TEXT NOT NULL,until REAL NOT NULL,
                PRIMARY KEY(chat,uid,action));
            CREATE TABLE IF NOT EXISTS gg_dispatch(
                id TEXT PRIMARY KEY,chat TEXT NOT NULL,op TEXT NOT NULL,kind TEXT NOT NULL,
                text TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',message INTEGER,
                next REAL NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS gg_dispatch_due ON gg_dispatch(status,next);
            CREATE TABLE IF NOT EXISTS gg_round_notices(
                chat TEXT NOT NULL,issue INTEGER NOT NULL,closes REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',message INTEGER,
                remaining INTEGER,next REAL NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(chat,issue));
            UPDATE gg_dispatch SET status='review',error='Interrupted' WHERE status='sending';
            UPDATE gg_round_notices SET status='review' WHERE status='sending';
            UPDATE gg_receipts SET status='review' WHERE status='sending';
            UPDATE gg_dispatch SET status='superseded' WHERE status='pending';
            UPDATE gg_round_notices SET status='superseded' WHERE status='pending';
            UPDATE notices SET status='superseded'
                WHERE id LIKE 'settle/gg:%' AND status='pending';
        """)
        self.broadcast = GameBroadcast(self)
        self.text_game = TextGame(self)

    def group_room(self, chat):
        """Return the persistent group-wide profile without silently switching it."""
        row = self.store.db.execute(
            "SELECT room FROM gg_group_room WHERE chat=?", (chat,)
        ).fetchone()
        if row:
            return row["room"]
        enabled = [k for k, v in self.runtime.game.rooms().items() if v["enabled"]]
        selected = (
            "room28" if "room28" in enabled else (enabled[0] if enabled else "room28")
        )
        self.store.db.execute(
            "INSERT OR IGNORE INTO gg_group_room(chat,room) VALUES(?,?)",
            (chat, selected),
        )
        return selected

    async def render(
        self, update, text, buttons=(), *, public=False, fold_sections=False
    ):
        """Render existing query controls without exposing betting actions."""
        uid, chat = str(update.effective_user.id), str(update.effective_chat.id)
        keyboard = [
            Button(
                label, callback_data="gg:" + self.store.callback(uid, chat, data)[3:]
            )
            for label, data in buttons
        ]
        markup = (
            InlineKeyboardMarkup(
                [keyboard[i : i + 3] for i in range(0, len(keyboard), 3)]
            )
            if keyboard
            else None
        )
        query = update.callback_query
        pages = cards(text, fold_sections=fold_sections)
        if not public:
            message = getattr(query, "message", None)
            ephemeral_id = getattr(message, "ephemeral_message_id", None) or (
                getattr(message, "api_kwargs", {}) or {}
            ).get("ephemeral_message_id")
            if ephemeral_id and len(pages) == 1:
                data = {
                    "chat_id": chat,
                    "receiver_user_id": int(uid),
                    "ephemeral_message_id": ephemeral_id,
                    "text": pages[0][1],
                    "parse_mode": "HTML",
                    "reply_markup": markup.to_dict()
                    if markup
                    else {"inline_keyboard": []},
                }
                try:
                    await self.runtime.bot._post("editEphemeralMessageText", data=data)
                except BadRequest as exc:
                    if "message is not modified" in str(exc).lower():
                        return
                    if not any(
                        term in str(exc).lower()
                        for term in (
                            "parse entities",
                            "unsupported start tag",
                            "can't find end tag",
                        )
                    ):
                        raise
                    data.update(text=pages[0][0], parse_mode=None)
                    await self.runtime.bot._post("editEphemeralMessageText", data=data)
            else:
                parameters = {"receiver_user_id": int(uid)}
                if query:
                    parameters["callback_query_id"] = query.id
                for index, (_, formatted) in enumerate(pages):
                    await send_html(
                        self.runtime.bot.send_message,
                        formatted,
                        chat_id=chat,
                        reply_markup=markup if index == len(pages) - 1 else None,
                        api_kwargs={"ephemeral_message_parameters": parameters},
                    )
            return
        for index, (_, formatted) in enumerate(pages):
            await send_html(
                self.runtime.bot.send_message,
                formatted,
                chat_id=chat,
                reply_markup=markup if index == len(pages) - 1 else None,
            )

    async def action(self, update, data, token=""):
        """Allow queries only; all historical betting callbacks fail closed."""
        op = data.get("action")
        if op not in {"play", "bets", "points", "checkin", "cancel"}:
            raise Rejected("旧下注入口已停用，请在本群发送 jnd 后直接发送文字下注")
        uid, chat = str(update.effective_user.id), str(update.effective_chat.id)
        if update.effective_chat.type != "supergroup" or getattr(
            getattr(update, "effective_message", None), "sender_chat", None
        ):
            raise Rejected("请使用超级群中的个人身份操作")
        if op in {"points", "checkin"} and getattr(update, "message", None):
            return await self.text_game.message(update, update.message.text)
        if op == "checkin":
            raise Rejected("请在本群发送“签到”，机器人将引用回复签到结果")
        if op == "bets":
            # The shared query path verifies membership once and fails closed on transport errors.
            return await self.text_game.queue_bets(update)
        await self.runtime.community.member(uid, chat, require_moderation=False)
        module = "points" if op == "points" else "game"
        if not self.store.get("modules", {}).get(module):
            raise Rejected("该功能尚未开放")
        if op == "points":
            return self.text_game.queue_callback_points(update)
        if op == "cancel":
            return
        if op == "play":
            return await self.render(
                update,
                (Path(__file__).parent / "docs/player-help.md").read_text(),
                fold_sections=True,
            )

    async def message(self, update, command, text):
        """Consume retired commands; route queries and plain text separately."""
        action = command.removeprefix("/")
        if action in {"bet", "game", "cancel"}:
            return True
        if action in {"play", "bets", "checkin"}:
            await self.action(update, {"action": action})
            return True
        return await self.text_game.message(update, text)

    async def tick(self):
        """Retired announcement scheduler; kept for maintenance compatibility."""

    async def deliver(self, key):
        """Never deliver retired outbox entries, including calls from old controls."""

    async def countdown_tick(self):
        """Continue only previously authorized known-ID announcement cleanup."""
        if self.runtime.application and self.runtime.application.running:
            await self.broadcast.cleanup()
