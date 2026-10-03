"""Authenticated AstrBot administration for the unified Telegram collector."""

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from astrbot.api import AstrBotConfig
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, request
from astrbot.core.workspace import API_KEY_USERNAME_PREFIX


class TelegramCollection(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.session = None
        self.api_token = ""

    async def initialize(self):
        """Expose a dashboard-authenticated proxy without sharing service secrets."""
        credential_file = Path(
            os.environ.get(
                "TELEGRAM_COLLECTOR_CONFIG",
                "/opt/telegram-chat-collector-prod/config.json",
            )
        )
        private_config = json.loads(credential_file.read_text(encoding="utf-8-sig"))
        self.api_token = private_config["unified"]["tokens"]["admin"]
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=100))
        self.context.register_web_api(
            "/astrbot_plugin_telegram_collection/control",
            self.control,
            ["POST"],
            "Telegram collection administration",
        )

    async def control(self):
        """Proxy only explicitly supported collector resources for dashboard users."""
        if not request.username or request.username.startswith(API_KEY_USERNAME_PREFIX):
            return error_response("仅 WebUI 管理员可使用采集管理", status_code=403)
        body = await request.json()
        allowed = {
            "accounts",
            "subscriptions",
            "modules",
            "status",
            "join/prepare",
            "join/confirm",
            "activity",
        }
        if not isinstance(body, dict) or body.get("resource") not in allowed:
            return {"status": "error", "message": "不支持的操作"}
        url = self.config.get("api_url", "http://127.0.0.1:6190")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or not self.api_token
        ):
            return {"status": "error", "message": "统一采集服务尚未配置"}
        method = body.get("method", "GET")
        if method not in ("GET", "POST"):
            return {"status": "error", "message": "不支持的操作"}
        try:
            async with self.session.request(
                method,
                url.rstrip("/") + "/v2/" + body["resource"],
                json=body.get("data") if method == "POST" else None,
                headers={
                    "Authorization": "Bearer " + self.api_token,
                    "Idempotency-Key": body.get("request_id", ""),
                },
                allow_redirects=False,
            ) as response:
                result = await response.json()
                if response.status != 200:
                    messages = {
                        "account_unavailable": "账号未连接，请查看账号状态",
                        "confirmation_expired_or_used": "入群确认已过期或已提交，请刷新后核对",
                        "group_owned_by_other_account": "该关键词订阅已由另一个账号管理",
                        "group_shared_by_other_module": "其他模块仍使用此群，不能退出",
                        "telegram_rate_limited": "Telegram 限流，请稍后重试",
                        "idempotency_conflict": "操作编号冲突，请刷新页面",
                        "invalid_request": "配置不符合要求，请核对账号、群及订阅状态",
                    }
                    return {
                        "status": "error",
                        "message": messages.get(
                            result.get("error"), "采集服务暂不可用，请查看运行状态"
                        ),
                    }
                return {"status": "ok", "data": result}
        except (aiohttp.ClientError, TimeoutError):
            return {"status": "error", "message": "无法连接采集服务，已有采集配置保留"}

    async def terminate(self):
        """Close the private HTTP connection pool."""
        self.context.registered_web_apis[:] = [
            r
            for r in self.context.registered_web_apis
            if r[0] != "/astrbot_plugin_telegram_collection/control"
        ]
        if self.session:
            await self.session.close()
