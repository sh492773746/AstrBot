"""Exercise the packaged plugin on unchanged upstream, under a network namespace."""

import asyncio
import copy
import importlib
import json
import os
from pathlib import Path

ROOT = Path(os.environ["ASTRBOT_ROOT"]).resolve()
assert ROOT.name in {
    "portable-v040",
    "portable-v050",
    "portable-v051",
    "portable-v052",
} and "astrbot-compat" in str(ROOT)


async def main():
    from telegram.ext import ApplicationBuilder
    from telegram.request import BaseRequest

    calls = []

    class FixtureRequest(BaseRequest):
        async def initialize(self):
            pass

        async def shutdown(self):
            pass

        @property
        def read_timeout(self):
            return 5

        async def do_request(self, url, method, request_data=None, **kwargs):
            bot = url.split("/bot", 1)[1].split(":", 1)[0]
            action = url.rsplit("/", 1)[-1]
            values = request_data.parameters if request_data else {}
            calls.append((bot, action, values))
            if action == "getMe":
                result = {
                    "id": int(bot),
                    "is_bot": True,
                    "first_name": "Fixture",
                    "username": f"fixture{bot}bot",
                    "can_manage_bots": True,
                }
            elif action == "getWebhookInfo":
                result = {
                    "url": "",
                    "has_custom_certificate": False,
                    "pending_update_count": 0,
                }
            elif action == "getUpdates":
                await asyncio.sleep(0.1)
                result = []
            elif action == "sendMessage":
                result = {
                    "message_id": len(calls),
                    "date": 1800000000,
                    "chat": {
                        "id": int(values["chat_id"]),
                        "type": "supergroup"
                        if int(values["chat_id"]) < 0
                        else "private",
                    },
                    "text": values["text"],
                }
            elif action == "getChat":
                result = {
                    "id": int(values["chat_id"]),
                    "type": "supergroup",
                    "title": "Fixture group",
                    "accent_color_id": 0,
                    "max_reaction_count": 0,
                    "accepted_gift_types": {
                        "unlimited_gifts": False,
                        "limited_gifts": False,
                        "unique_gifts": False,
                        "premium_subscription": False,
                        "gifts_from_channels": False,
                    },
                    "permissions": {"can_send_messages": True},
                }
            elif action == "getChatMember":
                uid = int(values["user_id"])
                result = {
                    "user": {"id": uid, "is_bot": uid > 1000, "first_name": "Fixture"},
                    "status": "administrator",
                    "can_be_edited": False,
                    "is_anonymous": False,
                    "can_manage_chat": True,
                    "can_delete_messages": True,
                    "can_manage_video_chats": False,
                    "can_restrict_members": True,
                    "can_promote_members": False,
                    "can_change_info": True,
                    "can_invite_users": True,
                    "can_post_stories": False,
                    "can_edit_stories": False,
                    "can_delete_stories": False,
                }
                if int(values["chat_id"]) == -100986 and uid == 1:
                    result = {"user": result["user"], "status": "member"}
            elif action in {"setMyCommands", "deleteWebhook", "answerCallbackQuery"}:
                result = True
            else:
                raise AssertionError(f"Unexpected fixture API method: {action}")
            return 200, json.dumps({"ok": True, "result": result}).encode()

    build = ApplicationBuilder.build

    def build_fixture(builder):
        return build(
            builder.request(FixtureRequest()).get_updates_request(FixtureRequest())
        )

    ApplicationBuilder.build = build_fixture
    from astrbot.core import LogBroker, astrbot_config, db_helper
    from astrbot.core.config.default import DEFAULT_CONFIG
    from astrbot.core.core_lifecycle import AstrBotCoreLifecycle
    from astrbot.core.event_bus import EventBus
    from astrbot.core.platform.sources.telegram.tg_adapter import (
        TelegramPlatformAdapter,
    )
    from astrbot.core.utils.astrbot_path import get_astrbot_config_path
    from astrbot.core.utils.metrics import Metric

    assert "astrbot-compat" in str(importlib.import_module("astrbot").__file__)
    assert not hasattr(TelegramPlatformAdapter, "register_application_hook")
    assert not hasattr(EventBus, "ensure_scheduler")

    # Only isolated fake credentials and generated configuration are used.
    os.umask(0o077)
    Path(get_astrbot_config_path()).mkdir(parents=True, exist_ok=True)
    astrbot_config["dashboard"]["enable"] = False
    astrbot_config["provider_settings"]["enable"] = False
    astrbot_config["plugin_set"] = ["astrbot_plugin_telethon_ai"]
    astrbot_config["platform"] = [
        {
            "id": "VIP_DHBot",
            "type": "telethon_ai_service",
            "enable": True,
            "service_role": "controller",
            "telegram_token": "123456789:isolated-fixture",
            "telegram_required_plugin": "astrbot_plugin_telethon_ai",
            "telegram_dedicated_reporting": True,
            "telegram_command_register": False,
            "telegram_command_auto_refresh": False,
        }
    ]
    astrbot_config.save_config()
    (
        Path(get_astrbot_config_path()) / "astrbot_plugin_telethon_ai_config.json"
    ).write_text(
        json.dumps(
            {
                "control_platform_id": "VIP_DHBot",
                "admin_ids": ["1"],
                "provision_profiles": False,
                "controller_mode": "standalone",
                "support_text": "fixture support",
                "model_provider_id": "",
            }
        )
    )
    lifecycle = AstrBotCoreLifecycle(LogBroker(), db_helper)
    report = {
        "upstream": "v4.28.1",
        "network_namespace": True,
        "production_credentials": False,
    }
    try:
        await asyncio.wait_for(lifecycle.initialize(), 90)
        module = importlib.import_module("data.plugins.astrbot_plugin_telethon_ai.main")
        plugin = next(
            star.star_cls
            for star in lifecycle.star_context.get_all_stars()
            if star.name == "astrbot_plugin_telethon_ai" and star.activated
        )
        assert isinstance(plugin, module.TelethonAI)
        controller = lifecycle.star_context.get_platform_inst("VIP_DHBot")
        assert controller is plugin.control.platform
        for _ in range(100):
            if controller.application.updater.running:
                break
            await asyncio.sleep(0.1)
        assert controller.application.updater.running
        assert lifecycle.event_queue.empty()
        report["full_core_initialize"] = "passed"
        report["plugin_owned_controller_polling"] = "passed"

        from telegram import Update

        def update(user, text, bot):
            return Update.de_json(
                {
                    "update_id": 1,
                    "message": {
                        "message_id": 1,
                        "date": 1800000000,
                        "chat": {"id": user, "type": "private"},
                        "from": {"id": user, "is_bot": False, "first_name": "Fixture"},
                        "text": text,
                        "entities": [
                            {
                                "type": "bot_command",
                                "offset": 0,
                                "length": len(text.split()[0]),
                            }
                        ],
                    },
                },
                bot,
            )

        await controller.application.process_update(
            update(1, "/start", controller.client)
        )
        assert any(
            action == "sendMessage" and "AI 账号服务" in values["text"]
            for _, action, values in calls
        )
        await controller.application.process_update(
            update(2, "/tgai", controller.client)
        )
        assert not plugin.control.pending
        assert lifecycle.event_queue.empty()
        report["controller_commands_and_no_ai_fallthrough"] = "passed"

        # Enrollment creates a dynamic plugin-owned customer platform without a core patch.
        store = plugin.tenants
        store.grant("fixture", 1)
        tenant = store.enroll(
            1,
            222222222,
            "fixturecustomerbot",
            plugin.enrollment.cipher.encrypt(b"222222222:isolated-fixture"),
        )
        await plugin.enrollment.activate("222222222")
        await plugin.enrollment.activate("222222222")
        store.set_enabled("fixture", tenant, True)
        customer = lifecycle.star_context.get_platform_inst("AIClient_222222222")
        assert customer.config["type"] == "telethon_ai_service"
        assert (
            len(
                [
                    p
                    for p in lifecycle.platform_manager.get_insts()
                    if p.meta().id == customer.meta().id
                ]
            )
            == 1
        )
        for _ in range(100):
            if customer.application.updater.running:
                break
            await asyncio.sleep(0.1)
        assert customer.application.updater.running
        await customer.application.process_update(
            update(1, "/service", customer.client)
        )
        assert any(
            bot == "222222222"
            and action == "sendMessage"
            and "AI 账号服务" in values["text"]
            for bot, action, values in calls
        )
        await customer.application.process_update(update(2, "/pause", customer.client))
        assert store.summary(tenant)["enabled"]
        assert any(
            bot == "222222222" and action == "sendMessage" and "仅限" in values["text"]
            for bot, action, values in calls
        )
        assert lifecycle.event_queue.empty()
        report["dynamic_customer_enrollment_and_owner_check"] = "passed"

        if hasattr(plugin, "group_control"):
            # Exercise real PTB routing and upstream lifecycle; only HTTP is simulated.
            group_command = update(
                1, "/bindgroup@" + customer.client.username, customer.client
            ).to_dict()
            group_command["message"]["chat"] = {
                "id": -100987,
                "type": "supergroup",
                "title": "Fixture group",
            }
            await customer.application.process_update(
                Update.de_json(group_command, customer.client)
            )
            assert (
                plugin.group_control.store.db.execute(
                    "SELECT COUNT(*) FROM bindings"
                ).fetchone()[0]
                == 0
            )
            token = plugin.group_control.store.db.execute(
                "SELECT token FROM confirmations WHERE platform=? AND actor='1'",
                (customer.config["id"],),
            ).fetchone()[0]
            confirmed = Update.de_json(
                {
                    "update_id": 2,
                    "callback_query": {
                        "id": "fixture-query",
                        "chat_instance": "fixture-chat",
                        "from": {"id": 1, "is_bot": False, "first_name": "Fixture"},
                        "data": "gc:" + token,
                        "message": {
                            "message_id": 2,
                            "date": 1800000000,
                            "chat": {"id": 1, "type": "private"},
                            "text": "Confirm",
                        },
                    },
                },
                customer.client,
            )
            assert plugin.customers[customer.config["id"]].group_handler.check_update(
                confirmed
            )
            await customer.application.process_update(confirmed)
            binding = plugin.group_control.store.binding(
                customer.config["id"], "-100987"
            )
            assert binding["owner"] == "1" and not binding["welcome_enabled"]
            assert not store.db.execute(
                "SELECT 1 FROM groups WHERE chat='-100987'"
            ).fetchone()
            plugin.group_control.store.set_config(
                1, customer.config["id"], "-100987", "rules", "Fixture rules"
            )
            group_command["message"]["text"] = "/rules@" + customer.client.username
            group_command["message"]["entities"][0]["length"] = len(
                group_command["message"]["text"]
            )
            await customer.application.process_update(
                Update.de_json(group_command, customer.client)
            )
            assert any(
                action == "sendMessage" and values.get("text") == "Fixture rules"
                for _, action, values in calls
            )
            assert lifecycle.event_queue.empty()
            report["native_customer_group_bind_confirm_rules_no_ai"] = "passed"

            public_command = update(
                2, "/bindgroup@" + controller.client.username, controller.client
            ).to_dict()
            public_command["message"]["chat"] = {
                "id": -100986,
                "type": "supergroup",
                "title": "Public fixture group",
            }
            await controller.application.process_update(
                Update.de_json(public_command, controller.client)
            )
            assert not plugin.group_control.store.db.execute(
                "SELECT 1 FROM bindings WHERE chat='-100986'"
            ).fetchone()
            public_token = plugin.group_control.store.db.execute(
                "SELECT token FROM confirmations WHERE platform='VIP_DHBot' AND actor='2' "
                "AND chat='-100986'"
            ).fetchone()[0]
            public_confirm = confirmed.to_dict()
            public_confirm["callback_query"]["from"]["id"] = 2
            public_confirm["callback_query"]["message"]["chat"]["id"] = 2
            public_confirm["callback_query"]["data"] = "gc:" + public_token
            await controller.application.process_update(
                Update.de_json(public_confirm, controller.client)
            )
            public_binding = plugin.group_control.store.binding("VIP_DHBot", "-100986")
            assert public_binding["public"] and public_binding["owner"] == "2"
            assert public_binding["tenant"] is None
            assert (
                not public_binding["welcome_enabled"]
                and public_binding["automatic"] is None
            )
            private_before = len(calls)
            await controller.application.process_update(
                update(2, "/tgaiassign forged collector", controller.client)
            )
            assert not store.db.execute("SELECT 1 FROM leases").fetchone()
            assert (
                len([c for c in calls[private_before:] if c[1] == "sendMessage"]) == 0
            )
            assert lifecycle.event_queue.empty()
            report[
                "public_group_admin_self_authorization_without_platform_privileges"
            ] = "passed"

        conf = copy.deepcopy(DEFAULT_CONFIG)
        conf.update(
            admins_id=[],
            plugin_set=["astrbot_plugin_telethon_ai"],
            disable_builtin_commands=True,
            kb_names=[],
        )
        conf["provider_settings"]["enable"] = True
        conf["provider_settings"]["proactive_capability"]["add_cron_tools"] = False
        conf["agent_runner"]["config"]["persona"]["persona_id"] = "default"
        profile_id = await plugin.context.astrbot_config_mgr.create_conf(
            conf, name="Fixture account"
        )
        await plugin.context.astrbot_config_mgr.ucr.update_route(
            "FixtureAccount::", profile_id
        )
        assert profile_id not in lifecycle.pipeline_scheduler_mapping
        from astrbot.api.event import AstrMessageEvent
        from astrbot.api.message_components import Plain
        from astrbot.api.platform import (
            AstrBotMessage,
            MessageMember,
            MessageType,
            PlatformMetadata,
        )

        msg = AstrBotMessage()
        msg.type = MessageType.GROUP_MESSAGE
        msg.group_id = "-1001"
        msg.self_id = "333"
        msg.sender = MessageMember("1", "Fixture")
        msg.message_id = "1"
        msg.message_str = "/unregistered"
        msg.message = [Plain(text=msg.message_str)]
        msg.session_id = "fixture-tenant_group_user"

        async def no_metrics(*args, **kwargs):
            return

        Metric.upload = no_metrics
        event = AstrMessageEvent(
            msg.message_str,
            msg,
            PlatformMetadata("telethon_ai", "Fixture", "FixtureAccount"),
            msg.session_id,
        )
        await plugin.pipeline.dispatch(event)
        scheduler = plugin.pipeline.schedulers[profile_id][1]
        assert len(scheduler.stages) == 9
        conf = plugin.context.astrbot_config_mgr.confs[profile_id]
        conf["agent_runner"]["config"]["compression"]["max_turns"] = 6
        await plugin.pipeline.dispatch(event)
        assert plugin.pipeline.schedulers[profile_id][1] is not scheduler
        assert lifecycle.event_queue.empty()
        report["hot_profile_native_pipeline_and_refresh"] = "passed"

        # A deterministic provider exercises native persona/conversation generation
        # and the actual Telethon sender without contacting a model or Telegram.
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from astrbot.core.provider.entities import LLMResponse
        from astrbot.core.provider.provider import Provider
        from astrbot.core.provider.register import register_provider_adapter

        model_requests = []

        @register_provider_adapter("isolated_fixture", "Offline compatibility fixture")
        class FixtureProvider(Provider):
            def get_current_key(self):
                return ""

            def set_key(self, key):
                pass

            async def get_models(self):
                return ["offline"]

            async def text_chat(self, **kwargs):
                model_requests.append(kwargs)
                assert not kwargs.get("func_tool")
                return LLMResponse(
                    role="assistant", completion_text="Offline fixture reply"
                )

        provider = FixtureProvider({"id": "offline", "type": "isolated_fixture"}, {})
        provider.set_model("offline")
        lifecycle.provider_manager.inst_map["offline"] = provider
        lifecycle.provider_manager.provider_insts.append(provider)
        lifecycle.provider_manager.curr_provider_inst = provider
        conf["agent_runner"]["config"]["model"]["provider_id"] = "offline"
        conf["provider_settings"]["streaming_response"] = False
        await plugin.context.persona_manager.create_persona(
            "fixture-persona",
            "You are the disclosed offline fixture AI.",
            tools=[],
            skills=[],
        )
        conf["agent_runner"]["config"]["persona"]["persona_id"] = "fixture-persona"
        await plugin.context.astrbot_config_mgr.ucr.update_route(
            "TelethonAI_fixture::", profile_id
        )
        account = module.adapter.TelethonAdapter(
            {
                **module.adapter.DEFAULT,
                "id": "TelethonAI_fixture",
                "account": "fixture",
                "allowed_chats": ["-1001"],
                "allowed_senders": ["1"],
                "disclosure_confirmed": True,
            },
            {},
            lifecycle.event_queue,
        )
        account.stopping = False
        account.client = SimpleNamespace(
            send_message=AsyncMock(), disconnect=AsyncMock()
        )
        plugin.tenants.assign("fixture", tenant, "fixture")
        plugin.tenants.authorize_group("fixture", tenant, "-1001")
        reservation, _ = account.tenants.reserve("fixture", "-1001", 7)
        msg.message_str = "hello"
        from astrbot.api.message_components import At

        msg.message = [At(qq="333"), Plain(text=msg.message_str)]
        msg.message_id = "7"
        native_event = module.adapter.TelethonEvent(
            msg, account, "fixture-peer", reservation
        )
        await plugin.pipeline.dispatch(native_event)
        assert len(model_requests) == 1
        assert "disclosed offline fixture" in json.dumps(
            model_requests[0]["contexts"], default=str
        )
        account.client.send_message.assert_awaited_once()
        assert account.client.send_message.call_args.args[:2] == (
            "fixture-peer",
            "[AI] Offline fixture reply",
        )
        assert (
            account.tenants.db.execute(
                "SELECT state FROM reservations WHERE id=?", (reservation,)
            ).fetchone()[0]
            == "sent"
        )
        admission = {"daily_limit": 1, "cooldown_seconds": 10}
        for mid in range(105):
            assert account.gate.admit(
                "fixture", "-1001", 1000 + mid, admission, now=100 + mid * 10
            )
        before_authorization = account.tenants.summary(tenant)
        assert before_authorization["used"] == 1
        for mid in range(before_authorization["budget"] - 1):
            reservation, _ = account.tenants.reserve("fixture", "-1001", 2000 + mid)
            account.tenants.finish(reservation, "sent")
        from data.plugins.astrbot_plugin_telethon_ai.tenants import Denied

        try:
            account.tenants.reserve("fixture", "-1001", 9999)
        except Denied:
            pass
        else:
            raise AssertionError("Authorization budget was bypassed")
        report["uncapped_account_admission_and_authorization_budget_enforced"] = (
            "passed"
        )
        account.gate.close()
        account.tenants.close()
        report["native_model_persona_reply_and_quota_settlement"] = (
            "passed_with_fixture_provider"
        )

        from datetime import datetime, timedelta, timezone

        import httpx
        import jwt

        from astrbot.dashboard.server import AstrBotDashboard

        dashboard = AstrBotDashboard(
            lifecycle, db_helper, lifecycle.dashboard_shutdown_event
        )
        token = jwt.encode(
            {
                "username": astrbot_config["dashboard"]["username"],
                "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
            },
            astrbot_config["dashboard"]["jwt_secret"],
            algorithm="HS256",
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=dashboard.asgi_app),
            base_url="http://isolated.test",
        ) as api:
            denied = await api.get("/api/plug/astrbot_plugin_telethon_ai/status")
            assert denied.status_code == 401
            api.headers["Authorization"] = "Bearer " + token

            # Name enrichment is not part of this probe, and may call getChat.
            async def no_enrichment(status, bot):
                return status

            plugin.user_names.enrich = no_enrichment
            response = await api.get("/api/plug/astrbot_plugin_telethon_ai/status")
            assert response.status_code == 200 and response.json()["control_attached"]
            entry = await api.get(
                "/api/v1/plugins/astrbot_plugin_telethon_ai/pages/accounts"
            )
            assert entry.status_code == 200 and entry.json()["status"] == "ok"
        report["upstream_page_and_authenticated_status_api"] = "passed"

        previous_plugin = plugin
        await lifecycle.plugin_manager.reload("astrbot_plugin_telethon_ai")
        plugin = next(
            star.star_cls
            for star in lifecycle.star_context.get_all_stars()
            if star.name == "astrbot_plugin_telethon_ai" and star.activated
        )
        assert plugin is not previous_plugin and previous_plugin.pipeline.closed
        for _ in range(100):
            if (
                controller.application.updater.running
                and customer.application.updater.running
            ):
                break
            await asyncio.sleep(0.1)
        assert controller.application.updater.running
        assert customer.application.updater.running
        assert controller.application.handlers[-74] == plugin.control.handlers
        assert len(customer.application.handlers[-80]) == 1
        await customer.application.process_update(
            update(1, "/service", customer.client)
        )
        assert lifecycle.event_queue.empty()
        report["hot_reload_resumes_single_service_handler"] = "passed"

        status = plugin.operational_status()
        assert (
            status["checks"]["telegram_hooks"]
            and status["checks"]["hot_profile_pipeline"]
        )
        assert status["checks"]["controller_bound"]
        await lifecycle.plugin_manager._terminate_plugin(
            next(
                star
                for star in lifecycle.star_context.get_all_stars()
                if star.name == "astrbot_plugin_telethon_ai"
            )
        )
        assert not controller.application.updater.running
        assert not customer.application.updater.running
        assert plugin.pipeline.closed
        report["plugin_unload_stops_polling_and_pipeline"] = "passed"
        report["live_telegram_or_model_test"] = "not performed"
        report["role_aware_native_config_ui"] = (
            "optional patched-core feature, not upstream"
        )
    finally:
        await lifecycle.stop()
    (ROOT / "portable-result.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
