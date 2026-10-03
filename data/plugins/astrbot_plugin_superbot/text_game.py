"""Durable text acceptance, quoted feedback and independent short-lived cleanup."""

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from telegram import MessageEntity, ReplyParameters
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter

from .bet_text import NUMERIC_PAIRS, ItemLimit, is_candidate, parse_partial
from .duel import Duel
from .game_hours import description, is_open
from .k3 import play_name as k3_play_name
from .play_center import PlayCenter
from .reply_style import style_reply
from .round_results import RoundResults
from .rules import PLAY_NAMES
from .store import Rejected, encode

logger = logging.getLogger(__name__)
BACKOFF = (5, 15, 45, 120)


class TextGame:
    """Keep synchronous ledger transactions separate from Telegram transport."""

    def __init__(self, group):
        self.group, self.runtime, self.store = group, group.runtime, group.store
        self.workers = {}
        self.k3 = getattr(self.runtime, "k3", None)
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS gt_sessions(
                chat TEXT NOT NULL,uid TEXT NOT NULL,expires REAL NOT NULL,
                activated REAL NOT NULL,last_message INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(chat,uid));
            CREATE TABLE IF NOT EXISTS gt_requests(
                chat TEXT NOT NULL,source INTEGER NOT NULL,uid TEXT NOT NULL,
                issue INTEGER,items TEXT NOT NULL,result TEXT NOT NULL,
                text TEXT NOT NULL,at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',message INTEGER,
                next REAL NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(chat,source));
            CREATE INDEX IF NOT EXISTS gt_feedback_due ON gt_requests(status,next);
            CREATE INDEX IF NOT EXISTS gt_feedback_chat_status ON gt_requests(chat,status);
            CREATE INDEX IF NOT EXISTS gt_feedback_archive_age ON gt_requests(status,at);
            CREATE TABLE IF NOT EXISTS gt_delivery_timing(
                chat TEXT NOT NULL,source INTEGER NOT NULL,issue INTEGER,
                kind TEXT NOT NULL,queued_at REAL NOT NULL,sent_at REAL NOT NULL,
                message INTEGER NOT NULL,PRIMARY KEY(chat,source));
            CREATE INDEX IF NOT EXISTS gt_delivery_timing_age ON gt_delivery_timing(sent_at);
            CREATE TABLE IF NOT EXISTS gt_archive(
                chat TEXT NOT NULL,source INTEGER NOT NULL,request TEXT NOT NULL,
                deletion TEXT NOT NULL,archived REAL NOT NULL,PRIMARY KEY(chat,source));
            CREATE TABLE IF NOT EXISTS gt_delete(
                chat TEXT NOT NULL,message INTEGER NOT NULL,source INTEGER NOT NULL,
                due REAL NOT NULL,next REAL NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(chat,message));
            CREATE INDEX IF NOT EXISTS gt_delete_due ON gt_delete(status,next);
            CREATE INDEX IF NOT EXISTS gt_delete_source ON gt_delete(chat,source,status);
            CREATE TABLE IF NOT EXISTS gt_pacing(
                chat TEXT PRIMARY KEY,next REAL NOT NULL);
            UPDATE gt_requests SET status='review',error='Interrupted'
                WHERE status='sending';
        """)
        if "last_message" not in {
            r["name"] for r in self.store.db.execute("PRAGMA table_info(gt_sessions)")
        }:
            self.store.db.execute(
                "ALTER TABLE gt_sessions ADD COLUMN last_message INTEGER NOT NULL DEFAULT 0"
            )
        columns = {
            r["name"] for r in self.store.db.execute("PRAGMA table_info(gt_requests)")
        }
        for name in ("username", "user_name"):
            if name not in columns:
                self.store.db.execute(
                    f"ALTER TABLE gt_requests ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
                )
        columns = {
            r["name"] for r in self.store.db.execute("PRAGMA table_info(gt_delete)")
        }
        for name, definition in (
            ("receiver", "TEXT NOT NULL DEFAULT ''"),
            ("ephemeral", "INTEGER"),
        ):
            if name not in columns:
                self.store.db.execute(
                    f"ALTER TABLE gt_delete ADD COLUMN {name} {definition}"
                )

        self.results = RoundResults(self.group)
        self.duel = Duel(self)
        self.center = PlayCenter(self)
        self.results_after = 0
        self.archive_after = 0
        # Deletes are idempotent and retain their actual-attempt budget on restart.
        for row in self.store.db.execute(
            "SELECT * FROM gt_delete WHERE status='sending'"
        ).fetchall():
            attempts = row["attempts"]
            self.store.db.execute(
                "UPDATE gt_delete SET status=?,next=?,error='Interrupted' "
                "WHERE chat=? AND message=?",
                (
                    "pending" if attempts < 5 else "review",
                    self.store.clock() + BACKOFF[max(0, min(attempts - 1, 3))],
                    row["chat"],
                    row["message"],
                ),
            )

    async def k3_message(self, update, text):
        """Route three-dice activation, exit, and wagers."""
        if not self.k3 or update.effective_chat.type != "supergroup":
            return False
        chat, uid = str(update.effective_chat.id), str(update.effective_user.id)
        word = text.strip().lower()
        if word in {"快三", "k3"}:
            if not self.k3.enabled(chat):
                raise Rejected(self.k3.unavailable(chat))
            self.k3.activate(chat, uid, update.message.message_id, self.store.clock())
            return True
        if word in {"取消", "退出", "退出快三"}:
            self.k3.deactivate(chat, uid, update.message.message_id, self.store.clock())
            self.store.db.execute(
                "UPDATE gt_sessions SET expires=0,last_message=? "
                "WHERE chat=? AND uid=? AND last_message<?",
                (update.message.message_id, chat, uid, update.message.message_id),
            )
            return True
        session = self.store.db.execute(
            "SELECT expires FROM k3_sessions WHERE chat=? AND uid=?", (chat, uid)
        ).fetchone()
        if not session or session["expires"] <= self.store.clock():
            return False
        try:
            items, skipped = self.k3.parse_partial(text)
        except Rejected as exc:
            # An active session owns wager-looking text. Never let a malformed or
            # unsupported K3 order fall through into the Canada28 parser.
            if is_candidate(text) or re.match(
                r"^(大|小|单|双|豹子|对子|顺子|和值|三同)", text.strip(), re.I
            ):
                raise Rejected(
                    "当前在积分快三房间。该格式不是有效快三下注；"
                    "支持“大100、大单50”及 da/x/d/s/dd/ds/xd/xs/bz/dz/sz "
                    "等兼容缩写。发送“取消”可退出房间。"
                ) from exc
            return False
        player = (
            str(
                getattr(update.effective_user, "full_name", None)
                or getattr(update.effective_user, "first_name", None)
                or f"用户…{uid[-4:]}"
            )
            .replace("\n", " ")
            .replace("\r", " ")[:60]
        )
        self.store.db.execute(
            "INSERT INTO gg_players(uid,name) VALUES(?,?) "
            "ON CONFLICT(uid) DO UPDATE SET name=excluded.name",
            (uid, player),
        )
        self.store.db.execute(
            "INSERT INTO user_labels(uid,username) VALUES(?,?) "
            "ON CONFLICT(uid) DO UPDATE SET username=excluded.username",
            (uid, getattr(update.effective_user, "username", None) or ""),
        )
        duplicate = self.store.db.execute(
            "SELECT 1 FROM k3_requests WHERE chat=? AND source=?",
            (chat, update.message.message_id),
        ).fetchone()
        issue, total, balance = self.k3.place(
            chat, uid, update.message.message_id, items, self.store.clock()
        )
        if duplicate:
            return True
        row = self.store.db.execute(
            "SELECT * FROM k3_rounds WHERE chat=? AND issue=?", (chat, issue)
        ).fetchone()
        await self.k3.panel(row)
        feedback = (
            f"🎲 @{getattr(update.effective_user, 'username', None) or update.effective_user.first_name}\n"
            f"第{issue}期已受理\n"
            + "、".join(f"{k3_play_name(play)} {amount}" for play, amount in items)
            + f"\n合计扣分：{total}积分\n"
            f"剩余积分：{balance}"
            + (
                f"\n已跳过 {skipped} 项不属于快三和值范围的数字下注。"
                if skipped
                else ""
            )
        )
        try:
            sent = await self.runtime.bot.send_message(
                chat_id=chat,
                text=feedback,
                parse_mode=None,
                reply_parameters=ReplyParameters(
                    message_id=update.message.message_id,
                    allow_sending_without_reply=False,
                ),
            )
            message_id = getattr(sent, "message_id", None)
            if isinstance(message_id, int) and message_id > 0:
                due = self.store.clock() + 10
                self.store.db.execute(
                    "INSERT OR IGNORE INTO gt_delete(chat,message,source,due,next,receiver,ephemeral) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (chat, message_id, update.message.message_id, due, due, "", None),
                )
        except Exception as exc:
            logger.warning(
                "K3 feedback failed chat=%s source=%s: %s",
                chat,
                update.message.message_id,
                exc,
            )
        return True

    def remember_user(self, db, update, source):
        """Persist the reply recipient from Telegram, never from message text.

        Args:
            db: Current outbox transaction.
            update: Telegram update providing the actual message author.
            source: Original source message or synthetic callback identifier.
        """
        user = update.effective_user
        db.execute(
            "INSERT INTO user_labels(uid,username) VALUES(?,?) "
            "ON CONFLICT(uid) DO UPDATE SET username=excluded.username",
            (str(user.id), getattr(user, "username", None) or ""),
        )
        db.execute(
            "UPDATE gt_requests SET username=?,user_name=? WHERE chat=? AND source=? AND uid=?",
            (
                str(getattr(user, "username", None) or ""),
                str(
                    getattr(user, "full_name", None)
                    or getattr(user, "first_name", None)
                    or f"玩家{user.id}"
                )
                .replace("\n", " ")
                .replace("\r", " ")[:60],
                str(update.effective_chat.id),
                source,
                str(user.id),
            ),
        )

    async def message(self, update, text):
        """Accept one new plain user message, atomically rejecting invalid batches.

        Args:
            update: Telegram update, never an edited-message event.
            text: Plain original text, not captions or quoted content.

        Returns:
            Whether this message belongs to the text-game workflow.
        """
        message = getattr(update, "message", None)
        user = update.effective_user
        if (
            not message
            or not user
            or getattr(user, "is_bot", False)
            or update.effective_chat.type != "supergroup"
            or getattr(message, "sender_chat", None)
            or getattr(message, "forward_origin", None)
            or getattr(message, "forward_date", None)
            or getattr(message, "is_automatic_forward", False)
            or not isinstance(getattr(message, "text", None), str)
        ):
            return False
        # The adapter passes trimmed text for commands; size validation uses the original.
        text = message.text
        chat, uid = str(update.effective_chat.id), str(user.id)
        word = text.strip().lower()
        if self.k3:
            handled = await self.k3_message(update, text)
            if handled:
                return True
        if await self.center.message(update):
            return True
        if (chat, uid) in self.center.participants and self.store.db.execute(
            "SELECT 1 FROM duels d JOIN game_panels p ON p.duel=d.id "
            "WHERE d.chat=? AND (d.first=? OR d.second=?) "
            "AND d.status IN ('invited','choosing','pending')",
            (chat, uid, uid),
        ).fetchone():
            # Panel participants use buttons; never reinterpret their wager-like text.
            if word not in {
                "玩法",
                "游戏中心",
                "玩法中心",
                "玩法大全",
                "🎮 玩法大全",
                "积分",
                "积分查询",
                "签到",
                "历史",
                "流水",
                "ls",
                "输赢",
                "sy",
            }:
                return True
        if await self.duel.message(update, text):
            return True
        explicit_queries = {
            "快三历史": ("history", "k3"),
            "快三流水": ("flow", "k3"),
            "快三输赢": ("profit", "k3"),
            "加拿大历史": ("history", "canada"),
            "加拿大流水": ("flow", "canada"),
            "加拿大输赢": ("profit", "canada"),
        }
        if word in explicit_queries:
            action, game_kind = explicit_queries[word]
            return await self.query_points(update, action, game_kind=game_kind)
        if word in {"流水", "ls", "/ls", "/ls@" + self.runtime.bot.username.lower()}:
            return await self.query_points(update, "flow", game_kind="active")
        if word in {"输赢", "sy", "/sy", "/sy@" + self.runtime.bot.username.lower()}:
            return await self.query_points(update, "profit", game_kind="active")
        if word in {
            "历史",
            "签到",
            "/checkin",
            "/checkin@" + self.runtime.bot.username.lower(),
        }:
            return await self.query_points(
                update,
                "history" if word == "历史" else "checkin",
                game_kind="active" if word == "历史" else None,
            )
        if word in {
            "积分",
            "积分查询",
            "/points",
            "/points@" + self.runtime.bot.username.lower(),
        }:
            return await self.query_points(update)
        activation = word in {"jnd", "加拿大", "canada"}
        exit_session = word == "退出加拿大"
        numeric_pairs = len(text) <= 512 and bool(NUMERIC_PAIRS.fullmatch(text.strip()))
        k3_candidate = bool(
            re.match(
                r"^(?:和值(?:[3-9]|1[0-8])\s*\d+|三同(?:111|222|333|444|555|666)\s*\d+)",
                text.strip(),
                re.I,
            )
        )
        if not activation and not exit_session and self.k3 and not k3_candidate:
            try:
                self.k3.parse(text)
                k3_candidate = True
            except Rejected:
                pass
        if (
            not activation
            and not exit_session
            and not numeric_pairs
            and not is_candidate(text)
            and not k3_candidate
        ):
            return False
        items, skipped = [], 0
        parse_error = None
        canada_candidate = activation or exit_session
        if not activation and not exit_session:
            try:
                items, skipped = parse_partial(text)
                canada_candidate = True
            except ItemLimit as exc:
                parse_error = str(exc)
                canada_candidate = True
            except Rejected:
                # Consume malformed candidates without SQL, membership requests or replies.
                if not k3_candidate:
                    return True
        enabled = self.store.db.execute(
            "SELECT enabled FROM mod_groups WHERE chat=?", (chat,)
        ).fetchone()
        if not enabled or not enabled[0]:
            return True
        sent_at = message.date.timestamp()
        if exit_session:
            self.store.db.execute(
                "INSERT INTO gt_sessions(chat,uid,expires,activated,last_message) "
                "VALUES(?,?,0,?,?) ON CONFLICT(chat,uid) DO UPDATE SET expires=0,"
                "activated=excluded.activated,last_message=excluded.last_message "
                "WHERE excluded.last_message>gt_sessions.last_message",
                (chat, uid, sent_at, message.message_id),
            )
            return True
        if activation:
            from .tenants import local_config, platform_group

            if not platform_group(self.store, chat) and not local_config(
                self.store, chat, "canada_enabled", False
            ):
                return True
            try:
                await self.runtime.community.member(uid, chat, require_moderation=False)
            except (Rejected, NetworkError, Forbidden, BadRequest):
                return True
            now = self.store.clock()
            if not 0 <= now - sent_at <= 60:
                return True
            with self.store.tx() as db:
                if self.k3:
                    db.execute(
                        "UPDATE k3_sessions SET expires=0,last_message=? "
                        "WHERE chat=? AND uid=? AND last_message<?",
                        (message.message_id, chat, uid, message.message_id),
                    )
                if db.execute(
                    "SELECT 1 FROM gt_requests WHERE chat=? AND source=?",
                    (chat, message.message_id),
                ).fetchone():
                    return True
                if not db.execute(
                    "INSERT INTO gt_sessions(chat,uid,expires,activated,last_message) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(chat,uid) DO UPDATE SET expires=excluded.expires,"
                    "activated=excluded.activated,last_message=excluded.last_message "
                    "WHERE excluded.last_message>gt_sessions.last_message",
                    (chat, uid, sent_at + 1800, sent_at, message.message_id),
                ).rowcount:
                    return True
                issue, feedback = self.activation_text(chat, uid)
                db.execute(
                    "INSERT INTO gt_requests(chat,source,uid,issue,items,result,text,at) "
                    "VALUES(?,?,?,?,'[]','activated',?,?)",
                    (chat, message.message_id, uid, issue, feedback, now),
                )
                self.remember_user(db, update, message.message_id)
            return True
        now = self.store.clock()
        canada_session = self.store.db.execute(
            "SELECT expires FROM gt_sessions WHERE chat=? AND uid=?", (chat, uid)
        ).fetchone()
        k3_session = (
            self.store.db.execute(
                "SELECT expires FROM k3_sessions WHERE chat=? AND uid=?", (chat, uid)
            ).fetchone()
            if self.k3
            else None
        )
        if not (
            (canada_session and canada_session["expires"] > now)
            or (k3_session and k3_session["expires"] > now)
        ):
            if not 0 <= now - sent_at <= 60:
                return True
            with self.store.tx() as db:
                if db.execute(
                    "SELECT 1 FROM gt_requests WHERE chat=? AND source=?",
                    (chat, message.message_id),
                ).fetchone():
                    return True
                db.execute(
                    "INSERT INTO gt_requests(chat,source,uid,issue,items,result,text,at) "
                    "VALUES(?,?,?,NULL,'[]','room_required',?,?)",
                    (
                        chat,
                        message.message_id,
                        uid,
                        "🎮 尚未进入下注房间\n"
                        "请从「玩法大全」选择加拿大28或积分快三，激活后重新发送。\n"
                        "本条未下注、未扣分。",
                        now,
                    ),
                )
                self.remember_user(db, update, message.message_id)
            return True
        if not canada_candidate:
            # A valid K3-looking order belongs to the active room. If that room is
            # Canada28, consume it there rather than guessing or switching parsers.
            return True
        source = message.message_id
        if self.store.db.execute(
            "SELECT 1 FROM gt_requests WHERE chat=? AND source=?", (chat, source)
        ).fetchone():
            return True
        started, issue, error = time.monotonic(), None, None
        try:
            issue, _ = self.runtime.game.current()
            room = self.group.group_room(chat)
            rules = self.runtime.game.rooms(chat=chat).get(room)
            await self.runtime.community.member(uid, chat, require_moderation=False)
        except Rejected as exc:
            error = str(exc)
        except (NetworkError, Forbidden, BadRequest, TimeoutError, ConnectionError):
            error = "群成员身份核验失败，请稍后重发"
        with self.store.tx() as db:
            # Network verification may interleave duplicate updates.
            if db.execute(
                "SELECT 1 FROM gt_requests WHERE chat=? AND source=?", (chat, source)
            ).fetchone():
                return True
            db.execute("SAVEPOINT text_order")
            try:
                session = db.execute(
                    "SELECT * FROM gt_sessions WHERE chat=? AND uid=?", (chat, uid)
                ).fetchone()
                now = self.store.clock()
                if (
                    not session
                    or session["expires"] <= now
                    or sent_at < session["activated"]
                ):
                    raise Rejected("请先发送 jnd 激活")
                if error:
                    raise Rejected(error)
                if parse_error:
                    raise Rejected(parse_error)
                if not self.store.get("text_betting_enabled", True, db):
                    raise Rejected("文字下注暂时停用")
                if not db.execute(
                    "SELECT 1 FROM mod_groups WHERE chat=? AND enabled=1", (chat,)
                ).fetchone():
                    raise Rejected("此群功能已停用")
                current, _ = self.runtime.game.current(db)
                draw = db.execute(
                    "SELECT at FROM draws ORDER BY issue DESC LIMIT 1"
                ).fetchone()
                if (
                    current != issue
                    or not draw
                    or not 0 <= now - sent_at <= 60
                    or sent_at < draw["at"]
                ):
                    raise Rejected("消息已过期或期次已变化，请重新发送")
                if (
                    self.group.group_room(chat) != room
                    or self.runtime.game.rooms(db, chat=chat).get(room) != rules
                ):
                    raise Rejected("本群倍率规则已变化，请重新发送")
                op = f"gg:text:{chat}:{source}"
                for index, (play, amount) in enumerate(items):
                    self.runtime.game._place(
                        db, uid, room, issue, [play], amount, f"{op}/{index}", chat=chat
                    )
                    db.execute(
                        "UPDATE bets SET receipt_op=? WHERE id=?",
                        (op, f"{op}/{index}/0"),
                    )
                db.execute(
                    "INSERT INTO gg_receipts(op,chat,uid,issue,status) VALUES(?,?,?,?,'silent')",
                    (op, chat, uid, issue),
                )
                player = (
                    str(
                        getattr(user, "full_name", None)
                        or getattr(user, "first_name", None)
                        or f"玩家{uid[-4:]}"
                    )
                    .replace("\n", " ")
                    .replace("\r", " ")[:60]
                )
                db.execute(
                    "INSERT INTO gg_players(uid,name) VALUES(?,?) "
                    "ON CONFLICT(uid) DO UPDATE SET name=excluded.name",
                    (uid, player),
                )
                result = "accepted"
                feedback = (
                    f"第 {issue} 期 · 已受理\n"
                    + "、".join(f"{PLAY_NAMES[p]} {n}" for p, n in items)
                    + f"\n合计扣分：{sum(n for _, n in items)} · 剩余积分：{self.store.balance(uid, chat)}"
                    + (
                        f"\n已跳过 {skipped} 段格式无效内容（未下注、未扣分）。"
                        if skipped
                        else ""
                    )
                )
            except Rejected as exc:
                db.execute("ROLLBACK TO text_order")
                result, feedback = "rejected", f"{exc}\n未下注、未扣分"
            finally:
                db.execute("RELEASE text_order")
            db.execute(
                "INSERT INTO gt_requests(chat,source,uid,issue,items,result,text,at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    chat,
                    source,
                    uid,
                    issue,
                    encode(items),
                    result,
                    feedback,
                    self.store.clock(),
                ),
            )
            self.store.audit(
                db,
                uid,
                "text_bet_" + result,
                {"chat": chat, "message": source, "issue": issue},
            )
            self.remember_user(db, update, source)
        logger.info(
            "Text acceptance chat=%s source=%s elapsed_ms=%.1f",
            chat,
            source,
            (time.monotonic() - started) * 1000,
        )
        return True

    async def queue_bets(self, update):
        """Route the legacy private query to the current room's period flow.

        Args:
            update: Group message or an already authorized query callback.
        """
        callback_id = None
        if not getattr(update, "message", None):
            callback_id = update.callback_query.id
            update = SimpleNamespace(
                effective_user=update.effective_user,
                effective_chat=update.effective_chat,
                message=SimpleNamespace(
                    message_id=0,
                    date=datetime.fromtimestamp(self.store.clock(), timezone.utc),
                ),
            )
        return await self.query_points(
            update, "flow", callback_id=callback_id, game_kind="active", private=True
        )

    def queue_callback_points(self, update):
        """Queue a verified legacy points button without quoting another author.

        Args:
            update: Callback whose requester membership was already verified.
        """
        chat, uid = str(update.effective_chat.id), str(update.effective_user.id)
        with self.store.tx() as db:
            cooldown = db.execute(
                "SELECT until FROM gg_cooldowns WHERE chat=? AND uid=? AND action='points'",
                (chat, uid),
            ).fetchone()
            now = self.store.clock()
            if cooldown and cooldown[0] > now:
                return
            source = db.execute(
                "SELECT MIN(COALESCE(MIN(source),0),0)-1 FROM gt_requests WHERE chat=?",
                (chat,),
            ).fetchone()[0]
            db.execute(
                "INSERT INTO gt_requests(chat,source,uid,items,result,text,at) VALUES(?,?,?,'[]','points',?,?)",
                (chat, source, uid, f"你的积分：{self.store.balance(uid, chat)}", now),
            )
            self.remember_user(db, update, source)
            db.execute(
                "INSERT INTO gg_cooldowns(chat,uid,action,until) VALUES(?,?,'points',?) "
                "ON CONFLICT(chat,uid,action) DO UPDATE SET until=excluded.until",
                (chat, uid, now + 5),
            )

    def archive_completed(self):
        """Archive up to 200 confirmed-deleted replies older than thirty days.

        Keep primary-key tombstones forever for replay and synthetic-ID safety.
        Unresolved deliveries and deletion errors are never archived.
        """
        now = self.store.clock()
        with self.store.tx() as db:
            rows = db.execute(
                "SELECT r.* FROM gt_requests r WHERE r.status='sent' AND r.at<? "
                "AND EXISTS(SELECT 1 FROM gt_delete d WHERE d.chat=r.chat AND d.source=r.source AND d.status='deleted') "
                "AND NOT EXISTS(SELECT 1 FROM gt_delete d WHERE d.chat=r.chat AND d.source=r.source AND d.status<>'deleted') "
                "ORDER BY r.at LIMIT 200",
                (now - 30 * 86400,),
            ).fetchall()
            for row in rows:
                jobs = db.execute(
                    "SELECT * FROM gt_delete WHERE chat=? AND source=?",
                    (row["chat"], row["source"]),
                ).fetchall()
                db.execute(
                    "INSERT INTO gt_archive(chat,source,request,deletion,archived) VALUES(?,?,?,?,?)",
                    (
                        row["chat"],
                        row["source"],
                        encode(dict(row)),
                        encode([dict(j) for j in jobs]),
                        now,
                    ),
                )
                db.execute(
                    "UPDATE gt_requests SET status='archived',text='',items='[]',username='',user_name='' WHERE chat=? AND source=?",
                    (row["chat"], row["source"]),
                )
                db.execute(
                    "UPDATE gt_delete SET status='archived' WHERE chat=? AND source=?",
                    (row["chat"], row["source"]),
                )

    def active_game(self, chat, uid, now=None):
        """Return the currently active mutually-exclusive betting room."""
        now = self.store.clock() if now is None else now
        canada = self.store.db.execute(
            "SELECT expires,activated FROM gt_sessions WHERE chat=? AND uid=?",
            (chat, uid),
        ).fetchone()
        k3 = (
            self.store.db.execute(
                "SELECT expires,activated FROM k3_sessions WHERE chat=? AND uid=?",
                (chat, uid),
            ).fetchone()
            if self.k3
            else None
        )
        live = []
        if canada and canada["expires"] > now:
            live.append(("canada", canada["activated"]))
        if k3 and k3["expires"] > now:
            live.append(("k3", k3["activated"]))
        return max(live, key=lambda item: item[1])[0] if live else None

    async def query_points(
        self,
        update,
        action="points",
        *,
        callback_id=None,
        game_kind=None,
        private=False,
    ):
        """Atomically enqueue a group query or check-in with its reward.

        Args:
            update: Validated new plain group message.
            action: Points, history, flow, profit or checkin.
            callback_id: Optional center click identity, allocated atomically with its outbox row.
            game_kind: ``canada``, ``k3`` or ``active`` for game queries.
            private: Deliver legacy /bets flow only to its requester.

        Returns:
            True, consuming the query even when unavailable or throttled.
        """
        chat, uid = str(update.effective_chat.id), str(update.effective_user.id)
        message = update.message
        module = "game" if action in {"history", "profit", "flow"} else "points"
        now = self.store.clock()
        if action in {"history", "profit", "flow"}:
            game_kind = (
                self.active_game(chat, uid, now)
                if game_kind in {None, "active"}
                else game_kind
            )
            if game_kind not in {"canada", "k3"}:
                game_kind = "none"
        if (
            not self.store.get("modules", {}).get(module)
            or not 0 <= now - message.date.timestamp() <= 60
            or self.store.db.execute(
                "SELECT 1 FROM gt_requests WHERE chat=? AND source=?",
                (chat, message.message_id),
            ).fetchone()
        ):
            return True
        cooldown = self.store.db.execute(
            "SELECT until FROM gg_cooldowns WHERE chat=? AND uid=? AND action=?",
            (chat, uid, action),
        ).fetchone()
        if cooldown and cooldown[0] > now:
            return True
        try:
            await self.runtime.community.member(uid, chat, require_moderation=False)
        except (
            Rejected,
            NetworkError,
            Forbidden,
            BadRequest,
            TimeoutError,
            ConnectionError,
        ):
            return True
        with self.store.tx() as db:
            now = self.store.clock()
            cooldown = db.execute(
                "SELECT until FROM gg_cooldowns WHERE chat=? AND uid=? AND action=?",
                (chat, uid, action),
            ).fetchone()
            if (
                not self.store.get("modules", {}, db).get(module)
                or not 0 <= now - message.date.timestamp() <= 60
                or (cooldown and cooldown[0] > now)
                or db.execute(
                    "SELECT 1 FROM gt_requests WHERE chat=? AND source=?",
                    (chat, message.message_id),
                ).fetchone()
                or not db.execute(
                    "SELECT 1 FROM mod_groups WHERE chat=? AND enabled=1", (chat,)
                ).fetchone()
            ):
                return True
            if callback_id is not None:
                if db.execute(
                    "SELECT 1 FROM game_panel_clicks WHERE id=?", (callback_id,)
                ).fetchone():
                    return True
                message.message_id = db.execute(
                    "SELECT MIN(COALESCE(MIN(source),0),0)-1 FROM gt_requests WHERE chat=?",
                    (chat,),
                ).fetchone()[0]
                db.execute(
                    "INSERT INTO game_panel_clicks VALUES(?,?)",
                    (callback_id, message.message_id),
                )
            if game_kind == "none":
                feedback = (
                    "尚未进入下注房间\n"
                    "请先从「玩法大全」进入加拿大28或积分快三，再查询历史、流水或输赢。\n"
                    "也可直接发送“加拿大历史”或“快三历史”等明确查询。"
                )
            elif action == "flow":
                if game_kind == "k3":
                    rows = db.execute(
                        "SELECT issue,SUM(amount) stake,SUM(payout) payout FROM k3_bets "
                        "WHERE uid=? AND chat=? GROUP BY issue "
                        "HAVING SUM(CASE WHEN status NOT IN ('won','lost') THEN 1 ELSE 0 END)=0 "
                        "ORDER BY issue DESC LIMIT 10",
                        (uid, chat),
                    ).fetchall()
                else:
                    rows = db.execute(
                        "SELECT issue,SUM(amount) stake,SUM(payout) payout FROM bets "
                        "WHERE uid=? AND points_chat=? GROUP BY issue "
                        "HAVING SUM(CASE WHEN status NOT IN ('win','won','lose','refund') THEN 1 ELSE 0 END)=0 "
                        "ORDER BY issue DESC LIMIT 10",
                        (uid, chat),
                    ).fetchall()
                midnight = datetime.fromtimestamp(
                    now, ZoneInfo("Asia/Shanghai")
                ).replace(hour=0, minute=0, second=0, microsecond=0)
                table, chat_column, time_column, statuses = (
                    ("k3_bets", "chat", "created", "('won','lost')")
                    if game_kind == "k3"
                    else ("bets", "points_chat", "at", "('win','won','lose','refund')")
                )
                daily = db.execute(
                    f"SELECT COALESCE(SUM(amount),0),COALESCE(SUM(payout),0) FROM {table} "
                    f"WHERE uid=? AND {chat_column}=? AND {time_column}>=? AND {time_column}<? "
                    f"AND status IN {statuses}",
                    (
                        uid,
                        chat,
                        midnight.timestamp(),
                        (midnight + timedelta(days=1)).timestamp(),
                    ),
                ).fetchone()
                game_label = "积分快三" if game_kind == "k3" else "加拿大28"
                feedback = f"{game_label} · 本群近期流水（最近10期已结算）\n" + (
                    "\n".join(
                        f"第 {r['issue']} 期 · 投注{r['stake']} · 返还{r['payout']} · 净变动{r['payout'] - r['stake']:+d}"
                        for r in rows
                    )
                    or "暂无已结算期次"
                )
                feedback += (
                    f"\n\n今日统计 · {midnight:%Y-%m-%d}（北京时间）"
                    f"\n投注{daily[0]} · 返还{daily[1]} · 净变动{daily[1] - daily[0]:+d}"
                    "\n按下注日统计已结算订单，不限上方10期；待开奖及异常退款不计入。"
                )
            elif action == "profit":
                midnight = datetime.fromtimestamp(
                    now, ZoneInfo("Asia/Shanghai")
                ).replace(hour=0, minute=0, second=0, microsecond=0)
                totals = []
                for day in (midnight, midnight - timedelta(days=1)):
                    totals.append(
                        db.execute(
                            (
                                "SELECT COALESCE(SUM(payout-amount),0) FROM k3_bets "
                                "WHERE uid=? AND chat=? AND created>=? AND created<? "
                                "AND status IN ('won','lost')"
                                if game_kind == "k3"
                                else "SELECT COALESCE(SUM(payout-amount),0) FROM bets "
                                "WHERE uid=? AND points_chat=? AND at>=? AND at<? "
                                "AND status IN ('win','won','lose','refund')"
                            ),
                            (
                                uid,
                                chat,
                                day.timestamp(),
                                (day + timedelta(days=1)).timestamp(),
                            ),
                        ).fetchone()[0]
                    )
                feedback = (
                    f"{'积分快三' if game_kind == 'k3' else '加拿大28'} · 本群输赢详情\n\n"
                    "积分输赢\n"
                    f"今日输赢：{totals[0]:+d} 积分\n"
                    f"昨日输赢：{totals[1]:+d} 积分\n\n"
                    "CNY 输赢\n未接入 · 暂无输赢数据\n\n"
                    "USDT 输赢\n未接入 · 不计广告余额\n\n"
                    f"北京时间 · 今日 {midnight:%Y-%m-%d}\n"
                    "按下注日统计已结算订单，净输赢＝返还－投注。\n"
                    "仅本人本群；待开奖、异常退款、签到及人工加减分不计入。"
                )
            elif action == "history":
                lines = []
                if game_kind == "k3":
                    rows = db.execute(
                        "SELECT issue,state FROM k3_rounds "
                        "WHERE chat=? AND status='settled' ORDER BY issue DESC LIMIT 10",
                        (chat,),
                    ).fetchall()
                    for row in rows:
                        state = json.loads(row["state"] or "{}")
                        balls = state.get("values", [])
                        total = state.get("sum", sum(balls))
                        if len(balls) == 3:
                            lines.append(
                                f"第 {row['issue']} 期：{' + '.join(map(str, balls))} = {total}"
                            )
                else:
                    rows = db.execute(
                        "SELECT issue,balls FROM draws WHERE conflict=0 "
                        "ORDER BY issue DESC LIMIT 10"
                    ).fetchall()
                    for row in rows:
                        balls = json.loads(row["balls"])
                        total = sum(balls)
                        lines.append(
                            f"第 {row['issue']} 期：{' + '.join(map(str, balls))} = {total} · "
                            + ("大" if total >= 14 else "小")
                            + ("单" if total % 2 else "双")
                        )
                game_label = "积分快三" if game_kind == "k3" else "加拿大28"
                feedback = f"{game_label} · 最近开奖记录（{len(lines)}条）\n" + (
                    "\n".join(lines) or "暂无可靠开奖记录"
                )
            elif action == "checkin":
                try:
                    changed = self.runtime.points.checkin(uid, chat, db=db)
                    feedback = (
                        "签到成功" if changed else "今天已签到，不会重复发放积分"
                    ) + f"\n本群积分：{self.store.balance(uid, chat)}"
                except Rejected as exc:
                    feedback = str(exc)
            else:
                feedback = f"你的积分：{self.store.balance(uid, chat)}"
            db.execute(
                "INSERT INTO gt_requests(chat,source,uid,items,result,text,at) "
                "VALUES(?,?,?,'[]',?,?,?)",
                (
                    chat,
                    message.message_id,
                    uid,
                    "bets" if private else action,
                    feedback,
                    now,
                ),
            )
            db.execute(
                "INSERT INTO gg_cooldowns(chat,uid,action,until) VALUES(?,?,?,?) "
                "ON CONFLICT(chat,uid,action) DO UPDATE SET until=excluded.until",
                (chat, uid, action, now + 5),
            )
            self.remember_user(db, update, message.message_id)
        return True

    def activation_text(self, chat, uid):
        """Build a current, non-speculative status for an activated session.

        Args:
            chat: Registered group ID.
            uid: Activated user ID.

        Returns:
            Verified current issue (or None), and the public reply text.
        """
        now = self.store.clock()
        draw = self.store.db.execute(
            "SELECT * FROM draws ORDER BY issue DESC LIMIT 1"
        ).fetchone()
        reliable = bool(
            draw
            and not draw["conflict"]
            and not self.store.get("keno_error", "")
            and 0 <= now - draw["received"] <= 60
            and draw["at"] <= now < draw["at"] + 210
        )
        issue = draw["issue"] + 1 if reliable else None
        period = (
            f"当前期号：第 {issue} 期"
            if issue is not None
            else "当前期号：待核实"
            + (f"（最近开奖第 {draw['issue']} 期）" if draw else "")
        )
        if not self.store.get("modules", {}).get("game"):
            state = "游戏模块未开放，暂不可下注"
        elif not self.store.get("text_betting_enabled", True):
            state = "文字下注暂停"
        elif not is_open(self.store):
            state = "休息中，暂不可下注；" + description(self.store)
        elif not reliable:
            state = "开奖数据核查中，暂不可下注"
        elif now >= draw["at"] + 190:
            state = "已封盘，等待开奖"
        elif (
            not self.runtime.game.rooms()
            .get(self.group.group_room(chat), {})
            .get("enabled")
        ):
            state = "本群倍率未开放，暂不可下注"
        else:
            # Reuse the admission gate so a status reply cannot bypass its checks.
            try:
                issue, closes = self.runtime.game.current()
                seconds = max(0, int(closes - now))
                state = f"可下注 · 距封盘 {seconds // 60:02d}:{seconds % 60:02d}"
            except Rejected as exc:
                state = str(exc)
        session = self.store.db.execute(
            "SELECT expires FROM gt_sessions WHERE chat=? AND uid=?", (chat, uid)
        ).fetchone()
        remaining = max(0, int(session["expires"] - now)) if session else 0
        return issue, (
            "加拿大28已激活\n"
            + period
            + "\n状态："
            + state
            + f"\n本群激活剩余：{remaining // 60:02d}:{remaining % 60:02d}"
            + "\n状态以发送时为准，实际下注仍需校验。"
        )

    async def delivery_loop(self):
        """Run no more than four per-chat feedback workers; await them on shutdown."""
        try:
            while True:
                for chat, task in list(self.workers.items()):
                    if task.done():
                        del self.workers[chat]
                        try:
                            task.result()
                        except Exception as exc:
                            self.runtime.report("text_feedback", exc)
                if self.runtime.application and self.runtime.application.running:
                    if self.store.clock() >= self.archive_after:
                        self.archive_completed()
                        self.archive_after = self.store.clock() + 3600
                    if self.store.clock() >= self.results_after:
                        self.results.tick()
                        self.duel.tick()
                        self.results_after = self.store.clock() + 1
                    rows = self.store.db.execute(
                        "SELECT r.chat FROM gt_requests r LEFT JOIN gt_pacing p ON p.chat=r.chat "
                        "WHERE r.status='pending' AND r.next<=? AND COALESCE(p.next,0)<=? "
                        "AND NOT EXISTS(SELECT 1 FROM gt_requests older WHERE older.chat=r.chat "
                        "AND older.status IN ('pending','sending') AND older.rowid<r.rowid) "
                        "GROUP BY r.chat ORDER BY MIN(r.rowid)",
                        (self.store.clock(), self.store.clock()),
                    ).fetchall()
                    for row in rows:
                        if len(self.workers) >= 4:
                            break
                        if row["chat"] not in self.workers:
                            self.workers[row["chat"]] = asyncio.create_task(
                                self.deliver(row["chat"])
                            )
                await asyncio.sleep(0.1)
        finally:
            tasks = list(self.workers.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.workers.clear()

    async def deliver(self, chat):
        """Claim and send the oldest request for a chat, never guessing send results.

        Args:
            chat: Exact target chat ID.
        """
        now = self.store.clock()
        with self.store.tx() as db:
            if db.execute(
                "SELECT 1 FROM gt_requests WHERE chat=? AND status='sending'", (chat,)
            ).fetchone():
                return
            row = db.execute(
                "SELECT * FROM gt_requests WHERE chat=? AND status='pending' ORDER BY rowid LIMIT 1",
                (chat,),
            ).fetchone()
            pacing = db.execute(
                "SELECT next FROM gt_pacing WHERE chat=?", (chat,)
            ).fetchone()
            if not row or row["next"] > now or (pacing and pacing[0] > now):
                return
            if not db.execute(
                "SELECT 1 FROM mod_groups WHERE chat=? AND enabled=1", (chat,)
            ).fetchone():
                db.execute(
                    "UPDATE gt_requests SET status='blocked',error='GroupDisabled' "
                    "WHERE chat=? AND source=?",
                    (chat, row["source"]),
                )
                return
            text = row["text"]
            if row["result"] == "points":
                if not self.store.get("modules", {}, db).get("points"):
                    db.execute(
                        "UPDATE gt_requests SET status='superseded' WHERE chat=? AND source=?",
                        (chat, row["source"]),
                    )
                    return
                text = f"你的积分：{self.store.balance(row['uid'], chat)}"
                db.execute(
                    "UPDATE gt_requests SET text=? WHERE chat=? AND source=?",
                    (text, chat, row["source"]),
                )
            if row["result"] == "activated":
                session = db.execute(
                    "SELECT * FROM gt_sessions WHERE chat=? AND uid=?",
                    (chat, row["uid"]),
                ).fetchone()
                if (
                    not session
                    or session["expires"] <= now
                    or session["last_message"] != row["source"]
                ):
                    db.execute(
                        "UPDATE gt_requests SET status='superseded' WHERE chat=? AND source=?",
                        (chat, row["source"]),
                    )
                    return
                issue, text = self.activation_text(chat, row["uid"])
                db.execute(
                    "UPDATE gt_requests SET issue=?,text=? WHERE chat=? AND source=?",
                    (issue, text, chat, row["source"]),
                )
            db.execute(
                "UPDATE gt_requests SET status='sending' WHERE chat=? AND source=?",
                (chat, row["source"]),
            )
        status, retry, error = "review", 0, ""
        try:
            kwargs = {}
            if row["result"] == "bets":
                kwargs["api_kwargs"] = {
                    "ephemeral_message_parameters": {
                        "receiver_user_id": int(row["uid"])
                    }
                }
            if row["result"] != "settlement" and row["source"] > 0:
                kwargs["reply_parameters"] = ReplyParameters(
                    message_id=row["source"], allow_sending_without_reply=False
                )
            prefix = ""
            if row["result"] == "duel_result":
                metadata = json.loads(row["items"])
                if isinstance(metadata, dict) and metadata.get("reply_to", 0) > 0:
                    kwargs["reply_parameters"] = ReplyParameters(
                        message_id=metadata["reply_to"],
                        allow_sending_without_reply=False,
                    )
            if row["uid"]:
                label = (
                    ("@" + row["username"])
                    if row["username"]
                    else (row["user_name"] or f"玩家{row['uid']}")
                )
                prefix = label + "\n"
                kwargs["entities"] = [
                    MessageEntity(
                        type=MessageEntity.MENTION
                        if row["username"]
                        else MessageEntity.TEXT_LINK,
                        offset=0,
                        length=len(label.encode("utf-16-le")) // 2,
                        url=None if row["username"] else f"tg://user?id={row['uid']}",
                    )
                ]
            formatting = (
                json.loads(row["items"]) if row["result"] == "settlement" else {}
            )
            text, emphasis = style_reply(
                text,
                row["result"],
                prefix,
                formatting.get("fold_lines", ())
                if isinstance(formatting, dict)
                else (),
            )
            kwargs.setdefault("entities", []).extend(emphasis)
            if row["result"] == "settlement" and isinstance(formatting, dict):
                rendered_lines = text.splitlines(keepends=True)
                for mention in formatting.get("mentions", ()):
                    index, label, uid, username = mention
                    if 0 <= index < len(rendered_lines) and rendered_lines[
                        index
                    ].startswith(label):
                        start = sum(len(line) for line in rendered_lines[:index])
                        kwargs["entities"].append(
                            MessageEntity(
                                type=MessageEntity.MENTION
                                if username
                                else MessageEntity.TEXT_LINK,
                                offset=len(text[:start].encode("utf-16-le")) // 2,
                                length=len(label.encode("utf-16-le")) // 2,
                                url=None if username else f"tg://user?id={uid}",
                            )
                        )
            if row["result"] == "duel_result" and isinstance(metadata, dict):
                for label, uid in metadata.get("mentions", ()):
                    offset = text.find(label)
                    if offset >= 0:
                        kwargs["entities"].append(
                            MessageEntity(
                                type=MessageEntity.TEXT_LINK,
                                offset=len(text[:offset].encode("utf-16-le")) // 2,
                                length=len(label.encode("utf-16-le")) // 2,
                                url=f"tg://user?id={uid}",
                            )
                        )
            if getattr(self.runtime, "group_keyboard", None) and row["result"] in {
                "points",
                "checkin",
                "activation",
                "history",
                "flow",
                "profit",
            }:
                from .availability import refresh

                await refresh(self.runtime, chat)
                kwargs["reply_markup"] = self.runtime.group_keyboard.render(chat)[0]
            message = await self.runtime.bot.send_message(
                chat_id=chat,
                text=text,
                parse_mode=None,
                **kwargs,
                read_timeout=10,
                write_timeout=10,
                connect_timeout=5,
            )
            ephemeral = None
            if row["result"] == "bets":
                ephemeral = getattr(message, "ephemeral_message_id", None) or (
                    getattr(message, "api_kwargs", {}) or {}
                ).get("ephemeral_message_id")
            message_id = ephemeral if row["result"] == "bets" else message.message_id
            if not isinstance(message_id, int) or message_id <= 0:
                raise ValueError("MissingMessageId")
            now = self.store.clock()
            delete_due = now + {
                "settlement": 30,
                "history": 60,
                "duel": 120,
                "duel_result": 30,
            }.get(row["result"], 10)
            with self.store.tx() as db:
                db.execute(
                    "UPDATE gt_requests SET status='sent',message=?,error='' WHERE chat=? AND source=?",
                    (message_id, chat, row["source"]),
                )
                if row["result"] in {"settlement", "duel_result"}:
                    db.execute(
                        "INSERT OR IGNORE INTO gt_delivery_timing VALUES(?,?,?,?,?,?,?)",
                        (
                            chat,
                            row["source"],
                            row["issue"],
                            row["result"],
                            row["at"],
                            now,
                            message_id,
                        ),
                    )
                    db.execute(
                        "DELETE FROM gt_delivery_timing WHERE rowid IN ("
                        "SELECT rowid FROM gt_delivery_timing WHERE sent_at<? LIMIT 100)",
                        (now - 30 * 86400,),
                    )
                key = (
                    db.execute(
                        "SELECT MIN(COALESCE(MIN(message),0),0)-1 FROM gt_delete WHERE chat=?",
                        (chat,),
                    ).fetchone()[0]
                    if ephemeral
                    else message_id
                )
                db.execute(
                    "INSERT INTO gt_delete(chat,message,source,due,next,receiver,ephemeral) VALUES(?,?,?,?,?,?,?)",
                    (
                        chat,
                        key,
                        row["source"],
                        delete_due,
                        delete_due,
                        row["uid"] if ephemeral else "",
                        ephemeral,
                    ),
                )
                db.execute(
                    "INSERT INTO gt_pacing(chat,next) VALUES(?,?) "
                    "ON CONFLICT(chat) DO UPDATE SET next=excluded.next",
                    (chat, now + 1),
                )
            logger.info(
                "Text feedback chat=%s source=%s wait_ms=%.1f",
                chat,
                row["source"],
                (now - row["at"]) * 1000,
            )
            return
        except RetryAfter as exc:
            delay = exc.retry_after
            delay = delay.total_seconds() if hasattr(delay, "total_seconds") else delay
            status, retry, error = (
                "pending",
                self.store.clock() + max(1, float(delay)),
                "RateLimited",
            )
        except (BadRequest, Forbidden) as exc:
            status, error = "blocked", type(exc).__name__
        except asyncio.CancelledError:
            self.store.db.execute(
                "UPDATE gt_requests SET status='review',error='Interrupted' WHERE chat=? AND source=?",
                (chat, row["source"]),
            )
            raise
        except Exception as exc:
            error = type(exc).__name__
        self.store.db.execute(
            "UPDATE gt_requests SET status=?,next=?,error=? WHERE chat=? AND source=?",
            (status, retry, error, chat, row["source"]),
        )

    async def cleanup(self):
        """Delete only known bot feedback IDs with bounded, persistent retry."""
        rows = self.store.db.execute(
            "SELECT * FROM gt_delete WHERE status='pending' AND next<=? ORDER BY next LIMIT 20",
            (self.store.clock(),),
        ).fetchall()
        for row in rows:
            if not self.store.db.execute(
                "UPDATE gt_delete SET status='sending',attempts=attempts+1 "
                "WHERE chat=? AND message=? AND status='pending'",
                (row["chat"], row["message"]),
            ).rowcount:
                continue
            attempts = row["attempts"] + 1
            status, retry, error = "deleted", 0, ""
            try:
                if row["ephemeral"] is not None:
                    await self.runtime.bot._post(
                        "deleteEphemeralMessage",
                        data={
                            "chat_id": row["chat"],
                            "receiver_user_id": int(row["receiver"]),
                            "ephemeral_message_id": row["ephemeral"],
                        },
                        read_timeout=5,
                        write_timeout=5,
                        connect_timeout=5,
                    )
                else:
                    await self.runtime.bot.delete_message(
                        chat_id=row["chat"],
                        message_id=row["message"],
                        read_timeout=5,
                        write_timeout=5,
                        connect_timeout=5,
                    )
            except RetryAfter as exc:
                delay = exc.retry_after
                delay = (
                    delay.total_seconds() if hasattr(delay, "total_seconds") else delay
                )
                attempts -= 1
                status, retry, error = (
                    "pending",
                    self.store.clock() + max(1, float(delay)),
                    "RateLimited",
                )
            except BadRequest as exc:
                status = (
                    "deleted"
                    if "message to delete not found" in str(exc).lower()
                    or (
                        row["ephemeral"] is not None
                        and any(
                            value in str(exc).lower()
                            for value in ("message_id_invalid", "message not found")
                        )
                    )
                    else "blocked"
                )
                error = "" if status == "deleted" else "BadRequest"
            except Forbidden:
                status, error = "blocked", "Forbidden"
            except (NetworkError, TimeoutError, ConnectionError) as exc:
                status = "pending" if attempts < 5 else "review"
                retry = (
                    self.store.clock() + BACKOFF[attempts - 1] if attempts < 5 else 0
                )
                error = type(exc).__name__
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                status, error = "review", type(exc).__name__
            self.store.db.execute(
                "UPDATE gt_delete SET status=?,next=?,error=?,attempts=? WHERE chat=? AND message=?",
                (status, retry, error, attempts, row["chat"], row["message"]),
            )
            logger.info(
                "Text cleanup chat=%s message=%s status=%s delay_ms=%.1f",
                row["chat"],
                row["message"],
                status,
                max(0, self.store.clock() - row["due"]) * 1000,
            )
