"""Durable per-group rounds, public rosters and private read-only pagination."""

import asyncio
import json
from collections import defaultdict
from html import escape

from telegram import InlineKeyboardButton as Button
from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter

from .rich_text import send_html
from .rules import NAMES, PLAY_NAMES, STATUS_NAMES
from .store import Rejected


class GameBroadcast:
    """Share group locks with outbox delivery; never mutate game accounting."""

    def __init__(self, group_game):
        self.owner = group_game
        self.runtime, self.store = group_game.runtime, group_game.store
        self.locks = defaultdict(asyncio.Lock)
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS gb_cleanup(
                job TEXT PRIMARY KEY,status TEXT NOT NULL DEFAULT 'pending',
                next REAL NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '');
            CREATE INDEX IF NOT EXISTS gb_cleanup_due ON gb_cleanup(status,next);
            CREATE TABLE IF NOT EXISTS gb_policy(
                chat TEXT PRIMARY KEY,enabled INTEGER NOT NULL DEFAULT 0,
                version INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS gb_rounds(
                chat TEXT NOT NULL,issue INTEGER NOT NULL,closes REAL NOT NULL,
                roster TEXT,final TEXT,edit_state TEXT NOT NULL DEFAULT 'ready',
                revision INTEGER NOT NULL DEFAULT 0,last_text TEXT NOT NULL DEFAULT '',
                next REAL NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(chat,issue));
            UPDATE gb_rounds SET edit_state='review',error='Interrupted'
                WHERE edit_state='sending';
        """)
        if "attempts" not in {
            r["name"] for r in self.store.db.execute("PRAGMA table_info(gb_cleanup)")
        }:
            self.store.db.execute(
                "ALTER TABLE gb_cleanup ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"
            )
        self.store.db.execute(
            "UPDATE gb_cleanup SET status=CASE WHEN attempts>=5 THEN 'review' ELSE 'pending' END,"
            "next=max(next,?+CASE attempts WHEN 2 THEN 15 WHEN 3 THEN 45 WHEN 4 THEN 120 ELSE 5 END),"
            "error='Interrupted' WHERE status='sending'",
            (self.store.clock(),),
        )
        if self.store.get("gb_cutover") is None:
            latest = self.store.db.execute("SELECT MAX(issue) FROM draws").fetchone()[0]
            with self.store.tx() as db:
                self.store.put(db, "gb_cutover", latest + 2 if latest else 0)
        columns = {
            r["name"] for r in self.store.db.execute("PRAGMA table_info(gg_dispatch)")
        }
        for name, definition in (
            ("format", "TEXT"),
            ("markup", "TEXT"),
            ("policy_version", "INTEGER"),
            ("issue", "INTEGER"),
        ):
            if name not in columns:
                self.store.db.execute(
                    f"ALTER TABLE gg_dispatch ADD COLUMN {name} {definition}"
                )
        for row in self.store.db.execute(
            "SELECT id,text FROM gg_dispatch WHERE status='pending' AND format IS NULL AND kind IN ('accepted','settlement')"
        ).fetchall():
            # Legacy game templates put the internal voucher on its own final line.
            text = "\n".join(
                line
                for line in row["text"].splitlines()
                if not line.startswith("凭证 ")
            )
            if text.startswith("🎯 模拟下注结算"):
                text = text.replace("🎯 模拟下注结算", "加拿大28开奖啦", 1)
            self.store.db.execute(
                "UPDATE gg_dispatch SET text=? WHERE id=?", (text, row["id"])
            )
        self.store.db.execute(
            "CREATE INDEX IF NOT EXISTS gb_rounds_open ON gb_rounds(final,edit_state)"
        )
        self.store.db.execute(
            "CREATE INDEX IF NOT EXISTS gg_dispatch_round ON gg_dispatch(chat,issue,kind)"
        )
        self.store.db.execute(
            "CREATE INDEX IF NOT EXISTS gg_receipts_round ON gg_receipts(chat,issue,uid)"
        )
        if not self.store.get("receipt_index_migrated", False):
            with self.store.tx() as db:
                for receipt in db.execute("SELECT op FROM gg_receipts").fetchall():
                    if db.execute(
                        "SELECT 1 FROM bets WHERE id GLOB ? AND receipt_op IS NOT NULL AND receipt_op<>? LIMIT 1",
                        (receipt["op"] + "/*", receipt["op"]),
                    ).fetchone():
                        raise Rejected("历史订单关联存在冲突，请核查后迁移")
                    db.execute(
                        "UPDATE bets SET receipt_op=? WHERE id GLOB ?",
                        (receipt["op"], receipt["op"] + "/*"),
                    )
                self.store.put(db, "receipt_index_migrated", True)

    def roster(self, chat, issue):
        """Aggregate accepted bets only, preserving first-acceptance order.

        Args:
            chat: Originating registered group.
            issue: Exact canonical issue number.

        Returns:
            Per-UID immutable-ready records without full IDs in visible text.
        """
        rows = self.store.db.execute(
            "SELECT r.uid,coalesce(p.name,'玩家') name,count(b.id) n,sum(b.amount) stake,"
            "sum(b.payout) payout FROM gg_receipts r JOIN bets b ON b.receipt_op=r.op "
            "LEFT JOIN gg_players p ON p.uid=r.uid WHERE r.chat=? AND r.issue=? "
            "GROUP BY r.uid ORDER BY min(r.rowid)",
            (chat, issue),
        ).fetchall()
        return [dict(row) for row in rows]

    async def cleanup(self):
        """Remove superseded known announcements; unknown results need review."""
        now = self.store.clock()
        for row in self.store.db.execute(
            "SELECT c.job,c.attempts,d.chat,d.message,d.issue,d.kind FROM gb_cleanup c "
            "JOIN gg_dispatch d ON d.id=c.job WHERE c.status='pending' AND c.next<=? "
            "AND d.status='sent' AND d.message IS NOT NULL ORDER BY d.rowid LIMIT 5",
            (now,),
        ).fetchall():
            async with self.locks[row["chat"]]:
                current = self.store.db.execute(
                    "SELECT status,next,attempts FROM gb_cleanup WHERE job=?",
                    (row["job"],),
                ).fetchone()
                if (
                    current["status"] != "pending"
                    or current["next"] > self.store.clock()
                ):
                    continue
                if not self.store.db.execute(
                    "SELECT 1 FROM gg_dispatch WHERE chat=? AND kind IN ('round_open','round_close','round_result') "
                    "AND (issue>? OR (issue=? AND CASE kind WHEN 'round_open' THEN 0 WHEN 'round_close' THEN 1 ELSE 2 END > ?)) "
                    "AND status='sent' AND message IS NOT NULL",
                    (
                        row["chat"],
                        row["issue"],
                        row["issue"],
                        {"round_open": 0, "round_close": 1, "round_result": 2}[
                            row["kind"]
                        ],
                    ),
                ).fetchone():
                    continue
                if not self.store.db.execute(
                    "UPDATE gb_cleanup SET status='sending',attempts=attempts+1 WHERE job=? AND status='pending'",
                    (row["job"],),
                ).rowcount:
                    continue
                if row["kind"] == "round_open":
                    self.store.db.execute(
                        "UPDATE gb_rounds SET edit_state='closed' WHERE chat=? AND issue=?",
                        (row["chat"], row["issue"]),
                    )
                status, retry, error = "deleted", 0, ""
                try:
                    await self.runtime.bot.delete_message(
                        chat_id=row["chat"],
                        message_id=row["message"],
                        read_timeout=5,
                        write_timeout=5,
                        connect_timeout=5,
                    )
                except RetryAfter as exc:
                    self.store.db.execute(
                        "UPDATE gb_cleanup SET attempts=attempts-1 WHERE job=?",
                        (row["job"],),
                    )
                    delay = exc.retry_after
                    delay = (
                        delay.total_seconds()
                        if hasattr(delay, "total_seconds")
                        else delay
                    )
                    status, retry, error = (
                        "pending",
                        now + max(1, float(delay)),
                        "RateLimited",
                    )
                except BadRequest as exc:
                    status = (
                        "deleted"
                        if "message to delete not found" in str(exc).lower()
                        else "blocked"
                    )
                    error = "" if status == "deleted" else "BadRequest"
                except Forbidden:
                    status, error = "blocked", "Forbidden"
                except (NetworkError, TimeoutError, ConnectionError) as exc:
                    attempts = current["attempts"] + 1
                    status = "pending" if attempts < 5 else "review"
                    retry = (
                        self.store.clock() + (5, 15, 45, 120)[attempts - 1]
                        if attempts < 5
                        else 0
                    )
                    error = type(exc).__name__
                except asyncio.CancelledError:
                    # The durable sending claim is resumed with its budget on restart.
                    raise
                except BaseException as exc:
                    self.store.db.execute(
                        "UPDATE gb_cleanup SET status='review',error=? WHERE job=?",
                        (type(exc).__name__, row["job"]),
                    )
                    if not isinstance(exc, Exception):
                        raise
                    self.runtime.report("round_cleanup", exc)
                    continue
                self.store.db.execute(
                    "UPDATE gb_cleanup SET status=?,next=?,error=? WHERE job=?",
                    (status, retry, error, row["job"]),
                )
                if row["kind"] == "round_open":
                    self.store.db.execute(
                        "UPDATE gb_rounds SET edit_state='closed' WHERE chat=? AND issue=?",
                        (row["chat"], row["issue"]),
                    )

    async def configure(self, ui, update, payload, token):
        """Reject retired broadcast controls, including historical callbacks."""
        raise Rejected("周期公告已停用，不再提供连续播报设置")

    async def tick(self):
        """Retired scheduler; historical cleanup runs independently."""

    async def view(self, update, query):
        """Show shared rosters or only the clicking member's own paginated bets."""
        try:
            _, raw_issue, kind, raw_page = str(query.data).split(":")
            issue, page = int(raw_issue), max(0, int(raw_page))
        except (ValueError, TypeError):
            raise Rejected("名单入口无效") from None
        uid, chat = str(update.effective_user.id), str(update.effective_chat.id)
        if kind not in {"roster", "mine"} or update.effective_chat.type != "supergroup":
            raise Rejected("名单入口无效")
        await self.runtime.community.member(uid, chat, require_moderation=False)
        phase = self.store.db.execute(
            "SELECT * FROM gb_rounds WHERE chat=? AND issue=?", (chat, issue)
        ).fetchone()
        if not phase:
            raise Rejected("本群没有这期记录")
        if kind == "roster":
            rows = (
                json.loads(phase["final"] or phase["roster"])
                if phase["final"] or phase["roster"]
                else self.roster(chat, issue)
            )
            lines = [
                f"{r['name']} · 投注{r['stake']}积分"
                + (f" · 返还{r['payout']}积分" if phase["final"] else "")
                for r in rows
            ]
        else:
            rows = self.store.db.execute(
                "SELECT b.* FROM gg_receipts r JOIN bets b ON b.receipt_op=r.op WHERE r.chat=? AND r.issue=? AND r.uid=? ORDER BY r.rowid,b.id",
                (chat, issue, uid),
            ).fetchall()
            lines = [
                f"{PLAY_NAMES.get(r['play'], '其他玩法')} · {NAMES.get(r['room'], '倍率')} · 投注{r['amount']}积分 · {STATUS_NAMES.get(r['status'], '待核查')} · 返还{r['payout']}积分"
                for r in rows
            ]
        count = max(1, (len(lines) + 9) // 10)
        page = min(page, count - 1)
        text = f"<b>第{issue}期 · {'全部名单' if kind == 'roster' else '我的明细'}</b>\n第{page + 1}/{count}页\n\n"
        text += (
            "\n".join(escape(line) for line in lines[page * 10 : page * 10 + 10])
            or "暂无记录"
        )
        buttons = []
        if page:
            buttons.append(
                Button("上一页", callback_data=f"gb:{issue}:{kind}:{page - 1}")
            )
        if page + 1 < count:
            buttons.append(
                Button("下一页", callback_data=f"gb:{issue}:{kind}:{page + 1}")
            )
        parameters = {"receiver_user_id": int(uid), "callback_query_id": query.id}
        # Keep all paginated read-only content personal; never edit the shared announcement.
        await send_html(
            self.runtime.bot.send_message,
            text,
            chat_id=chat,
            reply_markup=InlineKeyboardMarkup([buttons]) if buttons else None,
            api_kwargs={"ephemeral_message_parameters": parameters},
        )
