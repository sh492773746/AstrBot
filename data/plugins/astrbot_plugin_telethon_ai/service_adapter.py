"""Plugin-owned Telegram transport; never forwards service updates to the AI bus."""

import asyncio
import hashlib

from filelock import FileLock
from telegram import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    Update,
)
from telegram.ext import ApplicationBuilder, TypeHandler

from astrbot.api.platform import Platform, PlatformMetadata, register_platform_adapter
from astrbot.core.platform.platform import PlatformStatus

from . import adapter, registry

TYPE = "telethon_ai_service"
DEFAULT = {
    "id": "VIP_DHBot",
    "type": TYPE,
    "enable": False,
    "service_role": "controller",
    "telegram_token": "",
    "telegram_required_plugin": adapter.NAME,
    "telegram_dedicated_reporting": True,
    "telegram_command_register": False,
    "telegram_command_auto_refresh": False,
}


@register_platform_adapter(
    TYPE,
    "AI account service controller and customer menus",
    default_config_tmpl=DEFAULT,
    adapter_display_name="AI 账号服务 Bot（总控 / 克隆）",
    support_streaming_message=False,
)
class ServiceAdapter(Platform):
    def __init__(self, platform_config, platform_settings, event_queue):
        super().__init__(platform_config, event_queue)
        self.role = platform_config.get("service_role")
        if self.role not in {"controller", "customer"}:
            raise ValueError("Service role must be controller or customer")
        if (
            platform_config.get("telegram_required_plugin") != adapter.NAME
            or platform_config.get("telegram_dedicated_reporting") is not True
            or platform_config.get("telegram_command_register") is not False
            or platform_config.get("telegram_command_auto_refresh") is not False
        ):
            raise ValueError("Service transport isolation settings are required")
        token = platform_config.get("telegram_token", "")
        if not token or not token.split(":", 1)[0].isdigit():
            raise ValueError("A valid service Bot token is required")
        self.required_plugin = adapter.NAME
        builder = ApplicationBuilder().token(token).concurrent_updates(False)
        if platform_config.get("telegram_api_base_url"):
            builder.base_url(platform_config["telegram_api_base_url"])
        if platform_config.get("telegram_file_base_url"):
            builder.base_file_url(platform_config["telegram_file_base_url"])
        self.application = builder.build()
        self.client = self.application.bot
        self.hooks = {}
        self.changed = asyncio.Event()
        self.stopped = asyncio.Event()
        self.polling_lock = asyncio.Lock()
        self.initialized = False
        self.running = False
        # All unhandled updates are consumed locally, including handler failures.
        self.application.add_handler(TypeHandler(Update, self.discard), group=1000)
        self.application.add_error_handler(self.handle_error)

    def meta(self):
        return PlatformMetadata(
            TYPE,
            "AI account service",
            self.config["id"],
            support_streaming_message=False,
            support_proactive_message=False,
        )

    def commit_event(self, event):
        raise PermissionError("Service Bot updates cannot enter the AstrBot AI bus")

    async def send_by_session(self, session, message_chain):
        raise PermissionError(
            "Service Bot messages require an authenticated menu action"
        )

    async def discard(self, update, context):
        return

    async def handle_error(self, update, context):
        self.record_error("Service update failed; details withheld")

    def register_application_hook(self, key, callback):
        expected = (
            adapter.NAME + ":control"
            if self.role == "controller"
            else "telethon-ai:customer"
        )
        if key != expected or (key in self.hooks and self.hooks[key] is not callback):
            raise ValueError("Service handler identity conflict")
        self.hooks[key] = callback
        callback(self.application)
        self.changed.set()

    def unregister_application_hook(self, key):
        self.hooks.pop(key, None)
        self.application.bot_data.pop(adapter.NAME + ":ready", None)
        self.changed.set()

    async def suspend_required_plugin(self, key):
        if key != adapter.NAME:
            raise PermissionError("Cannot suspend another service")
        self.application.bot_data.pop(adapter.NAME + ":ready", None)
        async with self.polling_lock:
            if self.application.updater.running:
                await self.application.updater.stop()
        self.status = PlatformStatus.STOPPED
        self.changed.set()

    async def run(self):
        """Poll only after the owning plugin has attached its service handlers.

        Raises:
            RuntimeError: The owning plugin, Bot identity or webhook is invalid.
        """
        context = adapter.CONTEXT
        if context is None:
            raise RuntimeError("Account service plugin is unavailable")
        configured = context.get_config().get("platform", [])
        for other in configured:
            if (
                other.get("id") != self.config["id"]
                and other.get("telegram_token") == self.config["telegram_token"]
            ):
                raise RuntimeError("Service token is attached to another platform")
        digest = hashlib.sha256(self.config["telegram_token"].encode()).hexdigest()
        lock = FileLock(str(registry.private_root() / f"bot-{digest}.lock"), timeout=0)
        with lock:
            try:
                await asyncio.wait_for(self.changed.wait(), timeout=15)
                if not self.application.bot_data.get(adapter.NAME + ":ready"):
                    raise RuntimeError("Service handlers are not ready")
                await self.application.initialize()
                self.initialized = True
                if (
                    str(self.client.id)
                    != self.config["telegram_token"].split(":", 1)[0]
                ):
                    raise RuntimeError("Service Bot identity mismatch")
                if (await self.client.get_webhook_info()).url:
                    raise RuntimeError("Existing webhook requires operator review")
                await self.application.start()
                self.running = True
                commands = (
                    [
                        ("start", "服务首页"),
                        ("bots", "我的服务机器人"),
                        ("create", "创建服务机器人"),
                        ("plans", "套餐状态"),
                        ("groups", "群管理"),
                        ("tgai", "平台管理（仅超管）"),
                    ]
                    if self.role == "controller"
                    else [
                        ("start", "服务首页"),
                        ("service", "服务与额度"),
                        ("pause", "暂停服务"),
                        ("resume", "申请恢复"),
                        ("renew", "申请续期"),
                        ("groups", "群管理"),
                    ]
                )
                await self.client.set_my_commands(
                    [BotCommand(*item) for item in commands]
                )
                await self.client.set_my_commands(
                    [BotCommand(*item) for item in commands],
                    scope=BotCommandScopeAllPrivateChats(),
                )
                await self.client.set_my_commands(
                    [
                        BotCommand(*item)
                        for item in (
                            ("rules", "群规"),
                            ("help", "群帮助"),
                            ("bindgroup", "绑定群"),
                            ("groups", "本群管理（仅管理员）"),
                            ("del", "回复消息删除"),
                            ("mute", "回复消息临时禁言"),
                            ("unmute", "回复消息解禁"),
                        )
                    ],
                    scope=BotCommandScopeAllGroupChats(),
                )
                allowed = (
                    [
                        "message",
                        "callback_query",
                        "my_chat_member",
                        "pre_checkout_query",
                        "managed_bot",
                    ]
                    if self.role == "controller"
                    else ["message", "callback_query", "my_chat_member"]
                )
                while not self.stopped.is_set():
                    self.changed.clear()
                    async with self.polling_lock:
                        ready = self.application.bot_data.get(adapter.NAME + ":ready")
                        if ready and not self.application.updater.running:
                            await self.application.updater.start_polling(
                                allowed_updates=allowed,
                                drop_pending_updates=False,
                                bootstrap_retries=0,
                            )
                            self.status = PlatformStatus.RUNNING
                        elif not ready and self.application.updater.running:
                            await self.application.updater.stop()
                            self.status = PlatformStatus.STOPPED
                    await self.changed.wait()
            finally:
                async with self.polling_lock:
                    if self.application.updater.running:
                        await self.application.updater.stop()
                if self.running:
                    await self.application.stop()
                if self.initialized:
                    await self.application.shutdown()
                self.running = False
                self.initialized = False
                self.status = PlatformStatus.STOPPED

    async def terminate(self):
        self.stopped.set()
        await self.suspend_required_plugin(adapter.NAME)
