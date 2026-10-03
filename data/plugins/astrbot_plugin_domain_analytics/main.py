"""Connect the analytics service to an AstrBot-owned Telegram application."""

import asyncio
import contextlib
import json
from datetime import timedelta
from urllib.parse import urlsplit

import aiohttp
from telegram import InputFile, Update
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter
from telegram.ext import ApplicationHandlerStop, TypeHandler

from astrbot.api import AstrBotConfig
from astrbot.api.star import Context, Star
from astrbot.core.utils.bot_ingress_limit import BotIngressLimit

METHODS = {
    "sendMessage",
    "editMessageText",
    "answerCallbackQuery",
    "sendDocument",
    "setMyCommands",
    "setChatMenuButton",
}
GROUP = -100


class DomainAnalytics(Star):
    """Keep Telegram transport in AstrBot and business state in PostgreSQL."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.application = None
        self.session = None
        self.tasks = []
        self.lock = asyncio.Lock()
        self.ingress_limit = BotIngressLimit()
        self.handler = TypeHandler(Update, self.receive)

    async def initialize(self):
        """Restore pending ingress and receipts before starting bridge workers."""
        if not self.config.get("enabled"):
            return
        self.url = self.config["api_url"].rstrip("/")
        parsed = urlsplit(self.url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("The analytics API must use loopback")
        if len(self.config.get("api_token", "")) < 32:
            raise ValueError("Configure a bridge token")
        self.pending = await self.get_kv_data("pending_updates", {}) or {}
        self.receipt = await self.get_kv_data("pending_receipt", None)
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            headers={"Authorization": f"Bearer {self.config['api_token']}"},
        )
        self.tasks = [
            asyncio.create_task(self.ingress()),
            asyncio.create_task(self.egress()),
        ]

    async def api(self, path, data):
        """Call the private bridge endpoint without logging credentials.

        Args:
            path: Bridge resource path.
            data: JSON request payload.

        Returns:
            The decoded response.
        """
        async with self.session.post(
            self.url + path, json=data, allow_redirects=False
        ) as r:
            r.raise_for_status()
            return await r.json()

    async def receive(self, update, context):
        """Persist an update and prevent unrelated handlers from receiving it.

        Args:
            update: Original Telegram update.
            context: Telegram callback context.
        """
        if update.message or update.callback_query:
            user = update.effective_user
            if not user or not self.ingress_limit.allow(user.id):
                raise ApplicationHandlerStop
            async with self.lock:
                if (
                    str(update.update_id) not in self.pending
                    and len(self.pending) >= 500
                ):
                    self.logger.warning(
                        "Analytics ingress capacity reached; new update rejected"
                    )
                    raise ApplicationHandlerStop
                self.pending[str(update.update_id)] = json.loads(update.to_json())
                await self.put_kv_data("pending_updates", self.pending)
        raise ApplicationHandlerStop

    async def ingress(self):
        """Bind across adapter rebuilds and forward durable incoming updates."""
        failed = False
        while True:
            try:
                platform = self.context.get_platform_inst(self.config["platform_id"])
                application = getattr(platform, "application", None)
                if application is not None and application is not self.application:
                    if self.application:
                        self.application.remove_handler(self.handler, GROUP)
                        self.application.bot_data.pop("domain_analytics_ready", None)
                    self.application = application
                    application.add_handler(self.handler, GROUP)
                    application.bot_data["domain_analytics_ready"] = True
                for key in list(self.pending):
                    await self.api("/updates", self.pending[key])
                    async with self.lock:
                        self.pending.pop(key, None)
                        await self.put_kv_data("pending_updates", self.pending)
                failed = False
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not failed:
                    self.logger.warning(
                        "Analytics ingress unavailable (%s)", type(exc).__name__
                    )
                failed = True
            await asyncio.sleep(1)

    async def egress(self):
        """Claim once, execute through the shared bot, and persist the receipt."""
        failed = False
        while True:
            try:
                if self.receipt:
                    result = await self.api("/ack", self.receipt)
                    if not result.get("accepted"):
                        raise RuntimeError("Receipt rejected")
                    await self.put_kv_data("pending_receipt", None)
                    self.receipt = None
                if self.application is None or not self.application.running:
                    await asyncio.sleep(1)
                    continue
                job = (await self.api("/claim", {})).get("job")
                if not job:
                    await asyncio.sleep(0.5)
                    continue
                receipt = {
                    "id": job["id"],
                    "lease": job["lease"],
                    "status": "uncertain",
                }
                # A crash after claiming or sending is never automatically retried.
                try:
                    if job["method"] not in METHODS:
                        raise ValueError("Unsupported Telegram method")
                    data = job["payload"]["params"].copy()
                    file = job["payload"].get("file")
                    if file:
                        data["document"] = InputFile(
                            file["content"].encode(), filename=file["name"]
                        )
                    result = await self.application.bot._post(
                        job["method"],
                        data,
                        read_timeout=25,
                        write_timeout=25,
                        connect_timeout=10,
                        pool_timeout=10,
                    )
                    receipt.update(status="sent", result=result)
                except RetryAfter as exc:
                    seconds = exc.retry_after
                    if isinstance(seconds, timedelta):
                        seconds = seconds.total_seconds()
                    receipt.update(status="rejected", result={"retry_after": seconds})
                except (BadRequest, Forbidden, ValueError):
                    receipt.update(
                        status="rejected", result={"error": "request_rejected"}
                    )
                except NetworkError:
                    receipt["result"] = {"error": "network_result_unknown"}
                except Exception as exc:
                    receipt["result"] = {"error": type(exc).__name__}
                self.receipt = receipt
                await self.put_kv_data("pending_receipt", receipt)
                failed = False
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not failed:
                    self.logger.warning(
                        "Analytics egress unavailable (%s)", type(exc).__name__
                    )
                failed = True
                await asyncio.sleep(3)

    async def terminate(self):
        """Detach handlers and stop bridge workers without replaying messages."""
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self.application:
            updater = self.application.updater
            if updater and updater.running:
                await updater.stop()
            self.application.remove_handler(self.handler, GROUP)
            self.application.bot_data.pop("domain_analytics_ready", None)
        if self.session:
            await self.session.close()
