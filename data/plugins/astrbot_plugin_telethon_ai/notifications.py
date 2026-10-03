"""Durable lifecycle notices sent only by the control Bot to the bound owner."""

import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram.error import BadRequest, Forbidden, RetryAfter

from astrbot.api import logger

from .user_names import username


class Notifications:
    def __init__(self, store, gate):
        self.store = store
        self.gate = gate
        self.lock = asyncio.Lock()
        self.retry_until = 0
        # A crashed send may already have reached Telegram; do not replay it.
        with store.db:
            store.db.execute(
                "UPDATE notifications SET state='uncertain',error_code='interrupted' "
                "WHERE state='sending'"
            )
            obsolete = store.db.execute(
                "SELECT id,tenant FROM notifications WHERE kind='daily' AND state='pending'"
            ).fetchall()
            for notice in obsolete:
                store.db.execute(
                    "UPDATE notifications SET state='cancelled',error_code='account_daily_cap_removed' "
                    "WHERE id=?",
                    (notice["id"],),
                )
                store.audit(
                    "notifier",
                    "notification_cancelled",
                    tenant=notice["tenant"],
                    notification=notice["id"],
                    reason="account_daily_cap_removed",
                )

    def enqueue(self, tenant, kind, cycle, account="", now=None):
        now = time.time() if now is None else now
        with self.store.db:
            cursor = self.store.db.execute(
                "INSERT OR IGNORE INTO notifications"
                "(tenant,owner,bot,kind,cycle,account,created) VALUES (?,?,?,?,?,?,?)",
                (
                    tenant["id"],
                    tenant["owner"],
                    tenant["bot"],
                    kind,
                    cycle,
                    account,
                    now,
                ),
            )
            if cursor.rowcount:
                self.store.audit(
                    "notifier",
                    "notification_queued",
                    tenant=tenant["id"],
                    kind=kind,
                    notification=cursor.lastrowid,
                )

    def discover(self, accounts, now=None):
        now = time.time() if now is None else now
        for row in self.store.db.execute("SELECT * FROM tenants").fetchall():
            if row["expires"] <= now:
                self.enqueue(row, "expired", str(row["expires"]), now=now)
                continue
            tenant = self.store.summary(row["id"])
            if tenant["used"] >= tenant["budget"]:
                self.enqueue(row, "quota", f"{row['expires']}:{row['budget']}", now=now)
                continue

    def current(self, notice, accounts, now):
        row = self.store.db.execute(
            "SELECT * FROM tenants WHERE id=? AND owner=? AND bot=?",
            (notice["tenant"], notice["owner"], notice["bot"]),
        ).fetchone()
        if (
            row is None
            or not str(row["owner"]).isascii()
            or not str(row["owner"]).isdigit()
            or int(row["owner"]) <= 0
        ):
            return None
        tenant = self.store.summary(row["id"])
        kind = notice["kind"]
        if kind == "expired":
            return (
                tenant
                if row["expires"] <= now and notice["cycle"] == str(row["expires"])
                else None
            )
        if row["expires"] <= now:
            return None
        if kind == "quota":
            return (
                tenant
                if (
                    tenant["used"] >= tenant["budget"]
                    and notice["cycle"] == f"{row['expires']}:{row['budget']}"
                )
                else None
            )
        return None

    def text(self, notice, tenant, now):
        enrollment = self.store.db.execute(
            "SELECT username FROM enrollments WHERE tenant=? AND owner=? AND bot=?",
            (tenant["id"], tenant["owner"], tenant["bot"]),
        ).fetchone()
        name = username(enrollment[0]) if enrollment else None
        bot = "@" + name if name else f"Bot ID：{tenant['bot']}"
        if notice["kind"] == "expired":
            expiry = datetime.fromtimestamp(
                tenant["expires"], ZoneInfo("Asia/Shanghai")
            ).strftime("%Y-%m-%d %H:%M")
            return (
                f"克隆服务到期通知\n管理机器人：{bot}\n"
                f"到期时间：{expiry}（北京时间）\n"
                "该克隆服务的 AI 群聊权益、自动群管与新增处罚已停止，并移入到期回收桶。"
                "历史记录保留，不会删除数据。\n"
                "可在管理机器人发送 /renew 提交续期申请。"
            )
        if notice["kind"] == "quota":
            return (
                f"累计额度用完通知\n管理机器人：{bot}\n"
                f"请求额度：{tenant['used']} / {tenant['budget']}\n"
                "该服务的累计请求额度已用完，不会每日自动恢复。"
                "请联系平台管理员处理或在管理机器人提交续期申请。"
            )
        raise ValueError("Unsupported lifecycle notification kind")

    def settle(self, notice, state, error_code=None, message_id=None):
        with self.store.db:
            self.store.db.execute(
                "UPDATE notifications SET state=?,error_code=?,message_id=?,delivered=? "
                "WHERE id=? AND state='sending'",
                (
                    state,
                    error_code,
                    message_id,
                    time.time() if state == "sent" else None,
                    notice["id"],
                ),
            )
            self.store.audit(
                "notifier",
                "notification_result",
                tenant=notice["tenant"],
                notification=notice["id"],
                kind=notice["kind"],
                state=state,
            )

    async def tick(self, bot, accounts, now=None):
        clock = time.time if now is None else lambda: now
        stamp = clock()
        async with self.lock:
            self.discover(accounts, stamp)
            if bot is None or stamp < self.retry_until:
                return
            pending = self.store.db.execute(
                "SELECT * FROM notifications WHERE state='pending' AND next_attempt<=? "
                "ORDER BY CASE kind WHEN 'expired' THEN 0 ELSE 1 END,id LIMIT 10",
                (stamp,),
            ).fetchall()
            for notice in pending:
                stamp = clock()
                tenant = self.current(notice, accounts, stamp)
                if tenant is None:
                    with self.store.db:
                        self.store.db.execute(
                            "UPDATE notifications SET state='cancelled',error_code='condition_changed' "
                            "WHERE id=? AND state='pending'",
                            (notice["id"],),
                        )
                        self.store.audit(
                            "notifier",
                            "notification_cancelled",
                            tenant=notice["tenant"],
                            notification=notice["id"],
                        )
                    continue
                text = self.text(notice, tenant, stamp)
                with self.store.db:
                    cursor = self.store.db.execute(
                        "UPDATE notifications SET state='sending' WHERE id=? AND state='pending'",
                        (notice["id"],),
                    )
                if not cursor.rowcount:
                    continue
                try:
                    result = await asyncio.wait_for(
                        bot.send_message(
                            chat_id=int(notice["owner"]),
                            text=text,
                            parse_mode=None,
                            disable_web_page_preview=True,
                        ),
                        timeout=15,
                    )
                except RetryAfter as exc:
                    delay = exc.retry_after
                    if hasattr(delay, "total_seconds"):
                        delay = delay.total_seconds()
                    self.retry_until = time.time() + max(30, delay)
                    with self.store.db:
                        self.store.db.execute(
                            "UPDATE notifications SET state='pending',next_attempt=?,error_code='rate_limited' WHERE id=?",
                            (self.retry_until, notice["id"]),
                        )
                    return
                except (Forbidden, BadRequest):
                    self.settle(notice, "blocked", "recipient_unavailable")
                except asyncio.CancelledError:
                    self.settle(notice, "uncertain", "interrupted")
                    raise
                except Exception:
                    self.settle(notice, "uncertain", "delivery_unconfirmed")
                else:
                    self.settle(notice, "sent", message_id=result.message_id)

    async def run(self, plugin):
        while not plugin.closed:
            try:
                app = plugin.control.application
                bot = app.bot if app and app.running else None
                await self.tick(bot, plugin.accounts())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error(
                    "Lifecycle notification check failed; review notification status"
                )
            await asyncio.sleep(30)
