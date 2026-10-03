"""AstrBot-owned control bot with native commands and Telegram service events."""

import asyncio
import json
import time
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet
from filelock import FileLock
from telegram import Update
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    ManagedBotUpdatedHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_root

from .tenant_control.control import COMMANDS, Control, publish_menu
from .tenant_control.runtime import credentials, tenant_defaults
from .tenant_control.store import Store

PLUGIN = "astrbot_plugin_tenant_control"
GROUP = -75
REQUIRED_UPDATES = {"message", "pre_checkout_query", "managed_bot", "callback_query"}


class Main(Star):
    """Own business handlers, never Telegram transport or Docker management."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context, config)
        self.config = config
        self.control = None
        self.store = None
        self.platform = None
        self.application = None
        self.reminder_task = None
        self.menu_task = None
        self.instance_lock = None
        self.operation_lock = asyncio.Lock()
        self.closing = False
        self.handlers = (
            CallbackQueryHandler(self.button_event, pattern=r"^tc:"),
            PreCheckoutQueryHandler(self.service_event),
            MessageHandler(filters.SUCCESSFUL_PAYMENT, self.service_event),
            ManagedBotUpdatedHandler(self.service_event),
            MessageHandler(filters.ALL, self.profile_guard),
        )

    async def initialize(self) -> None:
        """Activate only with explicit platform, shared DB, key and operator IDs."""
        if not self.config.get("enabled", False):
            return
        runtime = credentials(Path(get_astrbot_root()))
        if not self.config.get("platform_id"):
            raise ValueError("Configure the dedicated AstrBot control platform ID")
        raw_path = runtime["CONTROL_DB_PATH"]
        key = runtime["CONTROL_FERNET_KEY"]
        cipher = Fernet(key.encode())
        admins = {int(value) for value in self.config.get("admin_ids", [])}
        if not admins or any(value <= 0 for value in admins):
            raise ValueError("Configure verified numeric Telegram administrator IDs")
        defaults = tenant_defaults(self.config)
        if not self.profile_ok():
            raise ValueError(
                "Controller profile routing or isolation settings are invalid"
            )
        try:
            self.store = Store(Path(raw_path), group=runtime.get("CONTROL_DB_GROUP"))
            self.instance_lock = FileLock(
                raw_path + ".controller.lock", timeout=0, mode=0o660
            )
            self.instance_lock.acquire()
            self.control = Control(
                self.store, cipher, admins, self.config.get("support_text", "")
            )
            self.control.defaults = defaults
            self.control.trials_allowed = bool(
                self.config.get("allow_trial_grants", False)
            )
            self.control.status_provider = self.configuration_status
            platform = self.context.get_platform_inst(self.config["platform_id"])
            if platform is not None:
                self.bind_platform(platform)
            self.reminder_task = asyncio.create_task(
                self.remind(), name=f"{PLUGIN}:reminders"
            )
            self.menu_task = asyncio.create_task(
                self.sync_menu(), name=f"{PLUGIN}:menu"
            )
        except BaseException:
            await self.terminate()
            raise

    @filter.on_platform_loaded()
    async def platform_loaded(self) -> None:
        """Attach when AstrBot creates or replaces the configured adapter."""
        if self.control and not self.closing:
            platform = self.context.get_platform_inst(self.config["platform_id"])
            if platform is not None:
                self.bind_platform(platform)

    def bind_platform(self, platform) -> None:
        """Validate the configured adapter and follow its client rebuilds.

        Args:
            platform: AstrBot-owned platform instance from its lifecycle hook.
        """
        if platform.meta().id != self.config["platform_id"]:
            return
        if (
            platform.meta().name != "telegram"
            or not callable(getattr(platform, "register_application_hook", None))
            or not callable(getattr(platform, "unregister_application_hook", None))
            or not callable(getattr(platform, "suspend_required_plugin", None))
        ):
            raise ValueError("Control requires the reviewed AstrBot Telegram adapter")
        if (
            not REQUIRED_UPDATES
            <= set(platform.config.get("telegram_allowed_updates", []))
            or platform.config.get("telegram_required_plugin") != PLUGIN
        ):
            raise ValueError(
                "Configure control update subscriptions and plugin readiness"
            )
        if self.platform is platform:
            return
        self.control.purchase_allowed = bool(
            self.config.get("allow_test_purchase", False)
            and platform.config.get("telegram_api_base_url", "").rstrip("/")
            == "https://api.telegram.org/bot{token}/test"
        )
        if self.platform:
            self.platform.unregister_application_hook(PLUGIN)
        self.attach_application(None)
        self.platform = None
        try:
            platform.register_application_hook(PLUGIN, self.attach_application)
        except BaseException:
            platform.unregister_application_hook(PLUGIN)
            raise
        self.platform = platform

    def profile_ok(self, umo=None) -> bool:
        expected = self.config.get("profile_id", "")
        if not expected:
            return True
        manager = self.context.astrbot_config_mgr
        platform = self.config["platform_id"]
        sessions = (
            [umo]
            if umo
            else [
                f"{platform}:FriendMessage:1000000001",
                f"{platform}:FriendMessage:1",
                f"{platform}:GroupMessage:-1",
            ]
        )
        for session in sessions:
            if manager.ucr.get_conf_id_for_umop(session) != expected:
                return False
            conf = self.context.get_config(umo=session)
            if (
                conf.get("provider_settings", {}).get("enable", True)
                or set(conf.get("plugin_set", [])) != {PLUGIN}
                or not conf.get("disable_builtin_commands", False)
                or conf.get("wake_prefix") != ["/"]
            ):
                return False
        return True

    def configuration_status(self):
        gateway = self.store.db.execute(
            "SELECT value FROM settings WHERE key='gateway_heartbeat'"
        ).fetchone()
        gateway_ready = bool(gateway and 0 <= time.time() - float(gateway[0]) < 60)
        count = self.store.db.execute("SELECT COUNT(*) FROM bots").fetchone()[0]
        return (
            f"实例：{Path(get_astrbot_root()).name}\n平台：{self.config['platform_id']}\n"
            "插件：v0.5.1\n配置来源：AstrBot 插件配置 + 会话配置档\n"
            f"配置档：{self.config.get('profile_id') or '独立实例默认配置'}\n"
            f"路由校验：{'通过' if self.profile_ok() else '失败，消息阻断'}\n"
            "WebUI：不参与交付或运维业务\n"
            f"购买：{'仅测试环境' if self.control.purchase_allowed else '关闭'}\n"
            f"测试资格签发：{'开启（仅超管）' if self.control.trials_allowed else '关闭'}\n"
            f"Worker：{'就绪' if self.store.worker_ready() else '未就绪'}\n"
            f"模型网关：{'就绪' if gateway_ready else '未就绪'}\n租户数：{count}\n"
            f"模板：{self.control.defaults['template']}\n"
            f"新租户功能：{','.join(self.control.defaults['features']) or '无'}\n"
            f"新租户月配额：{self.control.defaults['model_quota']}"
        )

    async def profile_guard(self, update, context):
        chat = update.effective_chat
        if not chat:
            raise ApplicationHandlerStop
        kind = "FriendMessage" if chat.type == "private" else "GroupMessage"
        umo = f"{self.config['platform_id']}:{kind}:{chat.id}"
        if not self.profile_ok(umo):
            raise ApplicationHandlerStop

    def attach_application(self, application) -> None:
        """Move only this plugin's service handlers to the AstrBot-owned client.

        Args:
            application: Existing adapter application, or None during detachment.
        """
        if self.application is application:
            return
        if self.application:
            self.application.bot_data.pop(f"{PLUGIN}:ready", None)
            for handler in self.handlers:
                self.application.remove_handler(handler, GROUP)
        self.application = None
        if application is None or self.closing:
            return
        if application.bot_data.get(f"{PLUGIN}:ready"):
            raise ValueError("Another control plugin is already attached")
        for handler in self.handlers:
            application.add_handler(handler, GROUP)
        application.bot_data[f"{PLUGIN}:ready"] = True
        self.application = application

    @filter.platform_adapter_type(filter.PlatformAdapterType.TELEGRAM)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    @filter.command("start", alias=set(COMMANDS) - {"start"}, priority=100)
    async def command(self, event: AstrMessageEvent) -> None:
        """Handle subscriptions and tenant management through AstrBot commands."""
        if (
            self.closing
            or self.control is None
            or self.application is None
            or event.get_platform_id() != self.config["platform_id"]
        ):
            return
        update = event.message_obj.raw_message
        if (
            not isinstance(update, Update)
            or not update.message
            or not update.message.text
        ):
            return
        if not self.control._private(update):
            return
        if not self.profile_ok(
            f"{self.config['platform_id']}:FriendMessage:{update.effective_user.id}"
        ):
            event.stop_event()
            return
        parts = update.message.text.split()
        name, _, recipient = parts[0].lstrip("/").partition("@")
        if name not in COMMANDS or (
            recipient and recipient.lower() != self.application.bot.username.lower()
        ):
            return
        event.stop_event()
        async with self.operation_lock:
            if self.closing:
                return
            await getattr(self.control, COMMANDS[name])(
                update,
                SimpleNamespace(bot=self.application.bot, args=parts[1:]),
            )

    async def service_event(self, update, context) -> None:
        """Consume payment and managed-bot updates before the message pipeline.

        Args:
            update: Telegram service update received by AstrBot's adapter.
            context: Context belonging to that same adapter application.
        """
        async with self.operation_lock:
            if self.control:
                if update.pre_checkout_query:
                    if self.closing or not self.profile_ok(
                        f"{self.config['platform_id']}:FriendMessage:"
                        f"{update.pre_checkout_query.from_user.id}"
                    ):
                        await update.pre_checkout_query.answer(
                            ok=False,
                            error_message="Service restarting; please retry shortly.",
                        )
                    else:
                        await self.control.checkout(update, context)
                elif update.managed_bot:
                    await self.control.managed(update, context)
                elif update.message and update.message.successful_payment:
                    await self.control.payment(update, context)
        raise ApplicationHandlerStop

    async def button_event(self, update, context) -> None:
        """Keep private navigation out of the general AstrBot message pipeline."""
        async with self.operation_lock:
            if (
                self.control
                and not self.closing
                and self.profile_ok(
                    f"{self.config['platform_id']}:FriendMessage:{update.effective_user.id}"
                )
            ):
                await self.control.callback(update, context)
            elif update.callback_query:
                await update.callback_query.answer("服务正在重启，请稍后重试。")
        raise ApplicationHandlerStop

    async def sync_menu(self) -> None:
        """Retry menu registration after the adapter starts or replaces its client."""
        registered = None
        while not self.closing:
            application = self.application
            if application is not None and getattr(application, "running", False):
                status = (
                    Path(get_astrbot_root())
                    / "data/plugin_data"
                    / PLUGIN
                    / "runtime-status.json"
                )
                status.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                temp = status.with_suffix(".tmp")
                temp.write_text(
                    json.dumps(
                        {
                            "version": "v0.5.1",
                            "at": time.time(),
                            "ready": bool(
                                getattr(
                                    getattr(application, "updater", None),
                                    "running",
                                    False,
                                )
                                and self.profile_ok()
                            ),
                            "configstatus": self.configuration_status(),
                        },
                        ensure_ascii=False,
                    )
                )
                temp.chmod(0o600)
                temp.replace(status)
            if (
                application is not None
                and application is not registered
                and getattr(application, "running", False)
            ):
                try:
                    await publish_menu(application.bot)
                    registered = application
                except Exception:
                    self.logger.warning("Command menu registration failed; will retry")
            await asyncio.sleep(30)

    async def remind(self) -> None:
        """Use one plugin-owned reminder task, independent of Telegram clients."""
        await asyncio.sleep(30)
        while not self.closing:
            try:
                async with self.operation_lock:
                    if self.application and not self.closing:
                        await self.control.reminders(
                            SimpleNamespace(bot=self.application.bot)
                        )
            except Exception:
                self.logger.exception("Tenant reminders failed")
            await asyncio.sleep(3600)

    async def terminate(self) -> None:
        """Detach and drain plugin work without stopping AstrBot's adapter."""
        self.closing = True
        status = (
            Path(get_astrbot_root())
            / "data/plugin_data"
            / PLUGIN
            / "runtime-status.json"
        )
        if self.control:
            status.unlink(missing_ok=True)
        if self.menu_task:
            self.menu_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.menu_task
            self.menu_task = None
        if self.platform:
            await self.platform.suspend_required_plugin(PLUGIN)
            self.platform.unregister_application_hook(PLUGIN)
            self.platform = None
        self.attach_application(None)
        if self.reminder_task:
            self.reminder_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.reminder_task
            self.reminder_task = None
        async with self.operation_lock:
            if self.store:
                self.store.close()
                self.store = None
            self.control = None
            if self.instance_lock:
                self.instance_lock.release()
                self.instance_lock = None
