"""Exercise the packaged AstrBot plugin without polling Telegram or starting Docker."""

import asyncio
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zipfile import ZipFile

from cryptography.fernet import Fernet
from filelock import Timeout
from telegram import Update
from telegram.ext import ApplicationHandlerStop
from tenant_control.bundle import build_plugin


class Adapter:
    def __init__(self):
        self.config = {
            "telegram_allowed_updates": [
                "message",
                "pre_checkout_query",
                "managed_bot",
                "callback_query",
            ],
            "telegram_required_plugin": "astrbot_plugin_tenant_control",
        }
        self.hooks = {}
        self.application = self.new_application()

    @staticmethod
    def new_application():
        return SimpleNamespace(
            bot_data={},
            bot=SimpleNamespace(
                username="control_bot",
                send_message=AsyncMock(),
                get_me=AsyncMock(return_value=SimpleNamespace(username="control_bot")),
                get_managed_bot_token=AsyncMock(return_value="managed-secret"),
                answer_callback_query=AsyncMock(),
                set_my_commands=AsyncMock(),
                set_chat_menu_button=AsyncMock(),
            ),
            add_handler=MagicMock(),
            remove_handler=MagicMock(),
            start=AsyncMock(),
            stop=AsyncMock(),
            shutdown=AsyncMock(),
            run_polling=MagicMock(),
        )

    def meta(self):
        return SimpleNamespace(id="tenant-control", name="telegram")

    def register_application_hook(self, key, callback):
        if key in self.hooks and self.hooks[key] != callback:
            raise ValueError("Hook already owned")
        self.hooks[key] = callback
        callback(self.application)

    def unregister_application_hook(self, key):
        self.hooks.pop(key, None)

    async def suspend_required_plugin(self, key):
        self.application.bot_data.pop(f"{key}:ready", None)


class PluginTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle_tmp = tempfile.TemporaryDirectory()
        root = Path(cls.bundle_tmp.name)
        cls.archive = build_plugin(root / "plugin.zip")
        with ZipFile(cls.archive) as archive:
            archive.extractall(root / "_tenant_control_test")
        sys.path.insert(0, str(root))
        cls.module = importlib.import_module("_tenant_control_test.main")

    @classmethod
    def tearDownClass(cls):
        from astrbot.core.star.star import star_map, star_registry
        from astrbot.core.star.star_handler import star_handlers_registry

        metadata = star_map.pop(cls.module.__name__, None)
        if metadata in star_registry:
            star_registry.remove(metadata)
        for handler in list(star_handlers_registry):
            if handler.handler_module_path == cls.module.__name__:
                star_handlers_registry.remove(handler)
        for name in list(sys.modules):
            if name == "_tenant_control_test" or name.startswith(
                "_tenant_control_test."
            ):
                sys.modules.pop(name)
        sys.path.remove(cls.bundle_tmp.name)
        cls.bundle_tmp.cleanup()

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ,
            {
                "CONTROL_DB_PATH": str(Path(self.tmp.name) / "control.db"),
                "CONTROL_FERNET_KEY": Fernet.generate_key().decode(),
                "CONTROL_ROTATED_TOKEN_ACK": "YES",
                "TENANT_DASHBOARD_BASE": "https://bot-{bot_id}.example",
            },
        )
        self.env.start()
        self.adapter = Adapter()
        self.context = SimpleNamespace(get_platform_inst=lambda _: self.adapter)
        self.config = {
            "enabled": True,
            "platform_id": "tenant-control",
            "admin_ids": ["999"],
            "support_text": "Private support",
        }
        self.plugin = self.module.Main(self.context, self.config)

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.env.stop()
        self.tmp.cleanup()

    def update(self, text="/plans", user_id=11, chat_type="private", **message_fields):
        return Update.de_json(
            {
                "update_id": 1,
                "message": {
                    "message_id": 1,
                    "date": 1,
                    "chat": {"id": user_id, "type": chat_type},
                    "from": {"id": user_id, "is_bot": False, "first_name": "Customer"},
                    "text": text,
                    **message_fields,
                },
            },
            self.adapter.application.bot,
        )

    def event(self, update=None, platform="tenant-control"):
        return SimpleNamespace(
            get_platform_id=lambda: platform,
            message_obj=SimpleNamespace(raw_message=update or self.update()),
            stop_event=MagicMock(),
        )

    async def test_package_has_no_worker_or_secrets(self):
        with ZipFile(self.archive) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {
                    "main.py",
                    "metadata.yaml",
                    "_conf_schema.json",
                    "requirements.txt",
                    "tenant_control/__init__.py",
                    "tenant_control/control.py",
                    "tenant_control/store.py",
                    "tenant_control/runtime.py",
                    "README.md",
                    "controller-config.example.json",
                },
            )
            self.assertNotIn(
                b"Application.builder", archive.read("tenant_control/control.py")
            )

    async def test_disabled_plugin_does_not_open_store_or_attach(self):
        self.plugin.config["enabled"] = False
        await self.plugin.initialize()
        self.assertFalse(Path(os.environ["CONTROL_DB_PATH"]).exists())
        self.assertFalse(self.adapter.hooks)
        self.assertIsNone(self.plugin.reminder_task)

    async def test_missing_rotation_ack_fails_closed(self):
        with patch.dict(os.environ, {"CONTROL_ROTATED_TOKEN_ACK": ""}):
            with self.assertRaisesRegex(ValueError, "Rotate"):
                await self.plugin.initialize()
        self.assertIsNone(self.plugin.store)
        self.assertFalse(self.adapter.hooks)

    def configure_profile(self):
        from astrbot.core.umop_config_router import UmopConfigRouter

        router = UmopConfigRouter(SimpleNamespace())
        router.umop_to_conf_id = {"tenant-control::": "vip", "::": "other"}
        self.config["profile_id"] = "vip"
        conf = {
            "provider_settings": {"enable": False},
            "plugin_set": [self.module.PLUGIN],
            "disable_builtin_commands": True,
            "wake_prefix": ["/"],
        }
        self.context.astrbot_config_mgr = SimpleNamespace(ucr=router)
        self.context.get_config = lambda **_: conf
        return router, conf

    async def test_profile_first_match_and_unsafe_settings_fail_closed(self):
        router, conf = self.configure_profile()
        self.assertTrue(self.plugin.profile_ok())
        router.umop_to_conf_id = {"::": "other", "tenant-control::": "vip"}
        self.assertFalse(self.plugin.profile_ok())
        router.umop_to_conf_id = {"tenant-control::": "vip"}
        conf["provider_settings"]["enable"] = True
        with self.assertRaisesRegex(ValueError, "profile"):
            await self.plugin.initialize()
        self.assertIsNone(self.plugin.control)

    async def test_changed_profile_blocks_messages_and_callback(self):
        router, conf = self.configure_profile()
        await self.plugin.initialize()
        conf["provider_settings"]["enable"] = True
        with self.assertRaises(ApplicationHandlerStop):
            await self.plugin.profile_guard(self.update("hello"), None)
        await self.press("tc:home")
        self.adapter.application.bot.send_message.assert_not_awaited()
        conf["provider_settings"]["enable"] = False
        router.umop_to_conf_id = {
            "tenant-control:FriendMessage:11": "other",
            "tenant-control::": "vip",
        }
        await self.press("tc:home")
        self.adapter.application.bot.send_message.assert_not_awaited()

    async def test_config_status_admin_only_and_no_secrets(self):
        self.configure_profile()
        await self.plugin.initialize()
        await self.plugin.command(self.event(self.update("/configstatus", user_id=11)))
        self.adapter.application.bot.send_message.assert_not_awaited()
        await self.plugin.command(self.event(self.update("/configstatus", user_id=999)))
        text = self.adapter.application.bot.send_message.await_args.kwargs["text"]
        self.assertIn("路由校验：通过", text)
        self.assertIn("购买：关闭", text)
        self.assertNotIn(os.environ["CONTROL_FERNET_KEY"], text)
        self.assertNotIn(os.environ["CONTROL_DB_PATH"], text)

    async def test_test_purchase_flag_does_not_enable_production_payments(self):
        self.config["allow_test_purchase"] = True
        await self.plugin.initialize()
        self.assertFalse(self.plugin.control.purchase_allowed)

    async def test_official_test_environment_requires_explicit_opt_in(self):
        self.adapter.config["telegram_api_base_url"] = (
            "https://api.telegram.org/bot{token}/test"
        )
        self.config["allow_test_purchase"] = True
        await self.plugin.initialize()
        self.assertTrue(self.plugin.control.purchase_allowed)

    async def test_missing_payment_subscriptions_cannot_activate(self):
        self.adapter.config["telegram_allowed_updates"] = ["message"]
        with self.assertRaisesRegex(ValueError, "subscriptions"):
            await self.plugin.initialize()
        self.assertIsNone(self.plugin.store)
        self.assertFalse(self.adapter.hooks)

    async def test_native_command_reuses_astrbot_transport_and_stops_pipeline(self):
        await self.plugin.initialize()
        event = self.event()
        await self.plugin.command(event)
        event.stop_event.assert_called_once()
        self.adapter.application.bot.send_message.assert_awaited_once()
        self.adapter.application.run_polling.assert_not_called()
        self.adapter.application.start.assert_not_awaited()

    def button_update(self, data, user_id=11, chat_id=11, chat_type="private"):
        return Update.de_json(
            {
                "update_id": 2,
                "callback_query": {
                    "id": "test-button",
                    "chat_instance": "test",
                    "data": data,
                    "from": {"id": user_id, "is_bot": False, "first_name": "Customer"},
                    "message": {
                        "message_id": 3,
                        "date": 1,
                        "chat": {"id": chat_id, "type": chat_type},
                        "from": {"id": 900, "is_bot": True, "first_name": "Control"},
                        "text": "Menu",
                    },
                },
            },
            self.adapter.application.bot,
        )

    async def press(self, data, **kwargs):
        with self.assertRaises(ApplicationHandlerStop):
            await self.plugin.button_event(
                self.button_update(data, **kwargs),
                SimpleNamespace(bot=self.adapter.application.bot),
            )

    async def test_home_keyboard_and_unconfigured_plan_picker(self):
        await self.plugin.initialize()
        await self.plugin.command(self.event(self.update("/start")))
        markup = self.adapter.application.bot.send_message.await_args.kwargs[
            "reply_markup"
        ]
        self.assertEqual([len(row) for row in markup.inline_keyboard], [2, 2, 2])
        await self.press("tc:plans")
        sent = self.adapter.application.bot.send_message.await_args.kwargs
        self.assertIn("暂未开放", sent["text"])
        self.assertEqual(
            sent["reply_markup"].inline_keyboard[0][0].callback_data, "tc:home"
        )

    async def test_callback_uses_customer_not_menu_author(self):
        await self.plugin.initialize()
        handler = AsyncMock()
        with patch.object(self.plugin.control, "renew", handler):
            await self.press("tc:renew:123:month")
        request, context = handler.await_args.args
        self.assertEqual(request.effective_user.id, 11)
        self.assertEqual(context.args, ["123", "month"])

    async def test_callback_rejects_group_foreign_chat_and_admin_actions(self):
        await self.plugin.initialize()
        for data, kwargs in (
            ("tc:home", {"chat_type": "group", "chat_id": -123}),
            ("tc:home", {"chat_id": 12}),
            ("tc:adminprice:first:1", {}),
            ("tc:renew:123:first", {}),
            ("tc:dashboard:not-a-number", {}),
        ):
            await self.press(data, **kwargs)
        self.adapter.application.bot.send_message.assert_not_awaited()
        self.assertEqual(
            self.adapter.application.bot.answer_callback_query.await_count, 5
        )

    async def test_callback_cannot_read_another_tenants_dashboard(self):
        await self.plugin.initialize()
        with patch.object(self.plugin.store, "db") as db:
            db.execute.return_value.fetchone.return_value = None
            await self.press("tc:dashboard:456")
            self.assertEqual(db.execute.call_args.args[1], (456, 11))
        self.assertIn(
            "尚未就绪",
            self.adapter.application.bot.send_message.await_args.kwargs["text"],
        )

    async def test_non_admin_cannot_confirm_operation(self):
        await self.plugin.initialize()
        confirm = MagicMock()
        with patch.object(self.plugin.store, "confirm_operation", confirm):
            await self.press("tc:confirm:" + "a" * 32)
        confirm.assert_not_called()

    async def test_management_returns_telegram_link_not_password(self):
        await self.plugin.initialize()
        with patch.object(self.plugin.store, "db") as db:
            db.execute.return_value.fetchone.return_value = {
                "id": 123,
                "username": "tenant_test_bot",
                "status": "active",
            }
            await self.press("tc:manage:123")
            self.assertEqual(db.execute.call_args.args[1], (123, 11))
        sent = self.adapter.application.bot.send_message.await_args.kwargs
        self.assertEqual(
            sent["reply_markup"].inline_keyboard[0][0].url,
            "https://t.me/tenant_test_bot?start=manage",
        )
        self.assertNotIn("密码", sent["text"])

    async def test_callback_purchase_keeps_worker_readiness_gate(self):
        await self.plugin.initialize()
        await self.press("tc:buy:first")
        self.assertIn(
            "暂未就绪",
            self.adapter.application.bot.send_message.await_args.kwargs["text"],
        )
        self.assertEqual(
            self.plugin.store.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0
        )

    async def test_menu_scoped_to_private_chats_without_admin_commands(self):
        await self.module.publish_menu(self.adapter.application.bot)
        args = self.adapter.application.bot.set_my_commands.await_args
        self.assertEqual(args.kwargs["scope"].type, "all_private_chats")
        self.assertFalse(any(item.command.startswith("admin") for item in args.args[0]))
        self.adapter.application.bot.set_chat_menu_button.assert_awaited_once()

    async def test_foreign_platform_group_and_other_bot_commands_are_ignored(self):
        await self.plugin.initialize()
        for event in (
            self.event(platform="production"),
            self.event(self.update(chat_type="group")),
            self.event(self.update("/plans@different_bot")),
        ):
            await self.plugin.command(event)
            event.stop_event.assert_not_called()
        self.adapter.application.bot.send_message.assert_not_awaited()

    async def test_every_native_command_dispatches_with_original_arguments(self):
        await self.plugin.initialize()
        for command, method in self.module.COMMANDS.items():
            handler = AsyncMock()
            with patch.object(self.plugin.control, method, handler):
                await self.plugin.command(
                    self.event(self.update(f"/{command} 123 month"))
                )
            handler.assert_awaited_once()
            self.assertEqual(handler.await_args.args[1].args, ["123", "month"])
            self.assertIs(handler.await_args.args[1].bot, self.adapter.application.bot)

    async def test_astrbot_filters_accept_command_alias_and_reject_group(self):
        from astrbot.api.platform import (
            AstrBotMessage,
            MessageMember,
            MessageType,
            PlatformMetadata,
        )
        from astrbot.core.platform.sources.telegram.tg_event import (
            TelegramPlatformEvent,
        )
        from astrbot.core.star.star_handler import star_handlers_registry

        message = AstrBotMessage()
        message.type = MessageType.FRIEND_MESSAGE
        message.self_id = "123"
        message.sender = MessageMember("11", "Customer")
        message.message = []
        message.message_str = "renew 456 month"
        message.raw_message = self.update("/renew 456 month")
        event = TelegramPlatformEvent(
            message.message_str,
            message,
            PlatformMetadata(name="telegram", description="", id="tenant-control"),
            "11",
            self.adapter.application.bot,
        )
        event.is_at_or_wake_command = True
        handler = next(
            item
            for item in star_handlers_registry
            if item.handler_module_path == self.module.__name__
            and item.handler_name == "command"
        )
        self.assertTrue(all(rule.filter(event, {}) for rule in handler.event_filters))
        message.type = MessageType.GROUP_MESSAGE
        self.assertFalse(all(rule.filter(event, {}) for rule in handler.event_filters))

    async def test_terminate_drains_active_command_before_closing_database(self):
        await self.plugin.initialize()
        started = asyncio.Event()
        release = asyncio.Event()
        original_store = self.plugin.store

        async def home(*_):
            started.set()
            await release.wait()
            original_store.db.execute("SELECT 1")

        self.plugin.control.home = home
        command = asyncio.create_task(
            self.plugin.command(self.event(self.update("/start")))
        )
        await started.wait()
        closing = asyncio.create_task(self.plugin.terminate())
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        release.set()
        await command
        await closing
        self.assertIsNone(self.plugin.store)

    async def test_late_platform_load_and_client_rebuild_detach_old_handlers(self):
        self.context.get_platform_inst = lambda _: None
        await self.plugin.initialize()
        self.assertIsNone(self.plugin.application)
        self.context.get_platform_inst = lambda _: self.adapter
        await self.plugin.platform_loaded()
        old = self.adapter.application
        self.adapter.application = self.adapter.new_application()
        self.adapter.hooks[self.module.PLUGIN](self.adapter.application)
        self.assertEqual(old.remove_handler.call_count, 5)
        self.assertNotIn(f"{self.module.PLUGIN}:ready", old.bot_data)
        self.assertEqual(self.adapter.application.add_handler.call_count, 5)
        await self.plugin.platform_loaded()
        self.assertEqual(self.adapter.application.add_handler.call_count, 5)
        await self.plugin.terminate()
        self.assertFalse(self.adapter.hooks)
        self.assertEqual(self.adapter.application.remove_handler.call_count, 5)
        self.adapter.application.stop.assert_not_awaited()
        self.adapter.application.shutdown.assert_not_awaited()
        self.assertIsNone(self.plugin.reminder_task)

    async def test_one_controller_per_database_and_reload_releases_lock(self):
        await self.plugin.initialize()
        other = self.module.Main(self.context, dict(self.config))
        with self.assertRaises(Timeout):
            await other.initialize()
        self.assertTrue(self.plugin.instance_lock.is_locked)
        await self.plugin.terminate()
        replacement = self.module.Main(self.context, dict(self.config))
        try:
            await replacement.initialize()
            self.assertTrue(replacement.instance_lock.is_locked)
        finally:
            await replacement.terminate()

    async def test_service_payment_callback_is_consumed_and_idempotent(self):
        await self.plugin.initialize()
        store = self.plugin.store
        store.set_price("first", 45, 999)
        order = store.create_order(11, "first")
        update = self.update(
            text=None,
            successful_payment={
                "currency": "XTR",
                "total_amount": 45,
                "invoice_payload": order["id"],
                "telegram_payment_charge_id": "unique-payment",
                "provider_payment_charge_id": "",
            },
        )
        for _ in range(2):
            with self.assertRaises(ApplicationHandlerStop):
                await self.plugin.service_event(
                    update, SimpleNamespace(bot=self.adapter.application.bot)
                )
        self.assertEqual(
            store.db.execute(
                "SELECT COUNT(*) FROM audit WHERE action='stars_payment'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            store.db.execute(
                "SELECT status FROM orders WHERE id=?", (order["id"],)
            ).fetchone()[0],
            "paid",
        )

    async def test_already_received_payment_is_processed_during_unload(self):
        await self.plugin.initialize()
        self.plugin.closing = True
        self.plugin.control.payment = AsyncMock()
        update = self.update(
            text=None,
            successful_payment={
                "currency": "XTR",
                "total_amount": 45,
                "invoice_payload": "pending",
                "telegram_payment_charge_id": "unique-payment",
                "provider_payment_charge_id": "",
            },
        )
        with self.assertRaises(ApplicationHandlerStop):
            await self.plugin.service_event(
                update, SimpleNamespace(bot=self.adapter.application.bot)
            )
        self.plugin.control.payment.assert_awaited_once()

    async def test_checkout_is_rejected_while_draining(self):
        await self.plugin.initialize()
        self.plugin.closing = True
        query = SimpleNamespace(answer=AsyncMock())
        with self.assertRaises(ApplicationHandlerStop):
            await self.plugin.service_event(
                SimpleNamespace(pre_checkout_query=query),
                SimpleNamespace(bot=self.adapter.application.bot),
            )
        self.assertFalse(query.answer.await_args.kwargs["ok"])

    async def test_managed_update_reaches_same_store_and_encrypts_token(self):
        await self.plugin.initialize()
        store = self.plugin.store
        store.set_price("first", 45, 999)
        order = store.create_order(11, "first")
        store.settle(order["id"], 11, 45, "unique-payment")
        update = Update.de_json(
            {
                "update_id": 2,
                "managed_bot": {
                    "user": {"id": 11, "is_bot": False, "first_name": "Customer"},
                    "bot": {
                        "id": 123,
                        "is_bot": True,
                        "first_name": "Tenant",
                        "username": "tenant_bot",
                    },
                },
            },
            self.adapter.application.bot,
        )
        with self.assertRaises(ApplicationHandlerStop):
            await self.plugin.service_event(
                update, SimpleNamespace(bot=self.adapter.application.bot)
            )
        row = store.db.execute("SELECT * FROM bots WHERE id=123").fetchone()
        self.assertEqual(row["owner_id"], 11)
        self.assertNotEqual(row["token_cipher"], b"managed-secret")
        self.assertEqual(len(store.bots_for(11)), 1)
