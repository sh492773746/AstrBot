"""AstrBot-native account management; no independent HTTP server."""

import asyncio
import copy
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from astrbot.api import AstrBotConfig
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request
from astrbot.core.config.default import DEFAULT_CONFIG
from astrbot.core.workspace import API_KEY_USERNAME_PREFIX

from . import (
    adapter,
    registry,
    service_adapter,  # noqa: F401 -- registers the plugin-owned platform
)
from .account_login import AccountLogin, LoginError
from .control import Control
from .customer import Customer
from .enrollment import Enrollment
from .group_control import GroupControl
from .group_names import GroupNames
from .native_pipeline import NativePipeline
from .notifications import Notifications
from .policy import Gate
from .profile_policy import ProfilePolicy
from .tenants import Tenants
from .user_names import UserNames

NAME = adapter.NAME


class TelethonAI(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.gate = Gate(adapter.state_path())
        self.tenants = Tenants(adapter.state_path().with_name("tenants.db"))
        self.group_control = GroupControl(self)
        self.control = Control(self)
        self.enrollment = Enrollment(self)
        self.customers = {}
        self.preview_lock = asyncio.Lock()
        self.last_preview = 0
        self.closed = False
        self.account_login = AccountLogin()
        self.account_sync_lock = asyncio.Lock()
        self.group_names = GroupNames(context)
        self.user_names = UserNames()
        self.notifications = Notifications(self.tenants, self.gate)
        self.notification_task = None
        self.profile_policy = ProfilePolicy(
            context.astrbot_config_mgr, config, self.tenants
        )
        self.pipeline = NativePipeline(context)
        adapter.CONTEXT = context
        adapter.PIPELINE = self.pipeline

    async def initialize(self):
        adapter.ACCOUNTS.clear()
        adapter.ACCOUNTS.update(
            {key: int(spec["user_id"]) for key, spec in registry.load().items()}
        )
        if self.config.get("controller_mode", "standalone") not in {
            "standalone",
            "companion",
        }:
            raise ValueError("Unknown controller mode")
        if self.config.get("provision_profiles", True):
            await self.provision()
        manager = self.context.astrbot_config_mgr
        existing = getattr(manager, "profile_policy", None)
        if existing is not None and existing is not self.profile_policy:
            raise ValueError("Another plugin already owns the profile policy")
        manager.profile_policy = self.profile_policy
        self.control.bind()
        self.bind_customers()
        await self.control.publish_menu()
        for endpoint, handler, methods in (
            ("status", self.page_status, ["GET"]),
            ("pause", self.page_pause, ["POST"]),
            ("resume", self.page_resume, ["POST"]),
            ("preview", self.page_preview, ["POST"]),
            ("account-login", self.page_account_login, ["POST"]),
            ("account-confirm", self.page_account_confirm, ["POST"]),
            ("account-sync", self.page_account_sync, ["POST"]),
        ):
            self.context.register_web_api(
                f"/{NAME}/{endpoint}", handler, methods, endpoint
            )
        self.notification_task = asyncio.create_task(self.notifications.run(self))
        self.group_control.task = asyncio.create_task(self.group_control.watch())

    @filter.on_platform_loaded()
    async def bind_control(self):
        if not self.closed:
            self.control.bind()
            self.bind_customers()

    def bind_customers(self):
        for row in self.tenants.db.execute("SELECT platform FROM tenants"):
            platform_id = row[0]
            if platform_id not in self.customers:
                self.customers[platform_id] = Customer(self, platform_id)
            self.customers[platform_id].bind()

    async def provision(self):
        manager = self.context.astrbot_config_mgr
        platform_id = self.config.get("control_platform_id", "VIP_DHBot")
        vip = self.context.get_config(f"{platform_id}:FriendMessage:0")
        model_id = self.config.get("model_provider_id") or (
            vip.get("agent_runner", {})
            .get("config", {})
            .get("model", {})
            .get("provider_id")
        )
        if not model_id and adapter.ACCOUNTS:
            raise ValueError(
                "VIP profile has no model selection; refusing implicit fallback"
            )
        for account in adapter.ACCOUNTS:
            platform_id = f"TelethonAI_{account}"
            persona_id = f"telethon-ai-{account}"
            if self.context.persona_manager.get_persona_v3_by_id(persona_id) is None:
                await self.context.persona_manager.create_persona(
                    persona_id,
                    (Path(__file__).parent / "SOUL.md").read_text(encoding="utf-8"),
                    tools=[],
                    skills=[],
                )
            name = f"Telethon AI - {account}"
            matches = [c for c in manager.get_conf_list() if c["name"] == name]
            if len(matches) > 1:
                raise ValueError("Duplicate Telethon profile; operator review required")
            if matches:
                conf_id = matches[0]["id"]
            else:
                conf = copy.deepcopy(DEFAULT_CONFIG)
                conf["admins_id"] = []
                conf["disable_builtin_commands"] = True
                conf["plugin_set"] = [NAME]
                conf["wake_prefix"] = []
                conf["provider_settings"]["enable"] = True
                conf["provider_settings"]["proactive_capability"]["add_cron_tools"] = (
                    False
                )
                conf["agent_runner"]["config"]["model"]["provider_id"] = model_id
                conf["agent_runner"]["config"]["model"]["fallback_provider_ids"] = []
                conf["agent_runner"]["config"]["persona"]["persona_id"] = persona_id
                conf["provider_settings"]["persona_pool"] = [persona_id]
                conf["kb_names"] = []
                conf["kb_agentic_mode"] = False
                conf_id = await manager.create_conf(conf, name=name)
            route = f"{platform_id}::"
            previous = manager.ucr.umop_to_conf_id.get(route)
            if previous and previous != conf_id:
                raise ValueError("Telethon profile routing conflict")
            await manager.ucr.update_route(route, conf_id)
            root = manager.default_conf
            platforms = root.setdefault("platform", [])
            existing = next((p for p in platforms if p.get("id") == platform_id), None)
            if existing is None:
                platform = copy.deepcopy(adapter.DEFAULT)
                platform.update(id=platform_id, account=account)
                platforms.append(platform)
                root.save_config()
            elif (
                existing.get("type") != "telethon_ai"
                or existing.get("account") != account
            ):
                raise ValueError("Telethon platform identity collision")

    def dashboard_admin(self):
        return (
            not self.closed
            and request.username
            and not request.username.startswith(API_KEY_USERNAME_PREFIX)
        )

    def accounts(self):
        rows = []
        for account, uid in adapter.ACCOUNTS.items():
            platform_id = f"TelethonAI_{account}"
            platform = self.context.get_platform_inst(platform_id)
            config = next(
                (
                    p
                    for p in self.context.get_config().get("platform", [])
                    if p.get("id") == platform_id
                ),
                {},
            )
            origin = f"{platform_id}:GroupMessage:0"
            profile = self.context.get_config(origin)
            profile_info = self.context.astrbot_config_mgr.get_conf_info(origin)
            runner = profile.get("agent_runner", {}).get("config", {})
            rows.append(
                {
                    "account": account,
                    "user_id": str(uid),
                    "username": getattr(platform, "username", None),
                    "platform_id": platform_id,
                    "profile_id": profile_info["id"],
                    "profile_name": profile_info["name"],
                    "state": platform.status.value if platform else "stopped",
                    "enabled": config.get("enable", False),
                    "paused": self.gate.paused(account),
                    "allowed_chats": config.get("allowed_chats", []),
                    "allowed_senders": config.get("allowed_senders", []),
                    "disclosure_confirmed": config.get("disclosure_confirmed", False),
                    "daily_limit": None,
                    "quota_scope": "tenant_authorization",
                    "model": runner.get("model", {}).get("provider_id", ""),
                    "persona": runner.get("persona", {}).get("persona_id", ""),
                }
            )
        return rows

    async def page_status(self):
        if not self.dashboard_admin():
            return error_response("Administrator required", status_code=403)
        status = await self.group_names.enrich(self.operational_status())
        bot = self.control.application.bot if self.control.application else None
        return json_response(await self.user_names.enrich(status, bot))

    async def page_account_login(self):
        if not self.dashboard_admin():
            return error_response("Administrator required", status_code=403)
        body = await request.json(default={})
        if not isinstance(body, dict):
            return error_response("Invalid request")
        try:
            return json_response(await self.account_login.start(request.username, body))
        except LoginError as exc:
            return error_response(str(exc), status_code=400)

    async def page_account_confirm(self):
        if not self.dashboard_admin():
            return error_response("Administrator required", status_code=403)
        body = await request.json(default={})
        if not isinstance(body, dict):
            return error_response("Invalid request")
        try:
            result = await self.account_login.confirm(request.username, body)
        except LoginError as exc:
            return error_response(str(exc), status_code=400)
        if result["state"] == "registered":
            try:
                await self.sync_accounts()
            except Exception:
                result["profile_pending"] = True
        return json_response(result)

    async def sync_accounts(self):
        async with self.account_sync_lock:
            records = registry.load()
            adapter.ACCOUNTS.update(
                {key: int(spec["user_id"]) for key, spec in records.items()}
            )
            await self.provision()

    async def page_account_sync(self):
        if not self.dashboard_admin():
            return error_response("Administrator required", status_code=403)
        try:
            await self.sync_accounts()
            return json_response({"synced": True})
        except Exception:
            return error_response(
                "账号已登记，但配置同步失败，请检查模型和路由。", status_code=409
            )

    def operational_status(self):
        accounts = self.accounts()
        now = time.time()
        current = datetime.fromtimestamp(now, timezone.utc)
        day = current.date().isoformat()
        reset_at = (
            current.replace(hour=0, minute=0, second=0, microsecond=0)
            + timedelta(days=1)
        ).timestamp()
        tenants = []
        selected = (
            self.tenants.db.execute(
                "SELECT id FROM tenants WHERE expires>? ORDER BY expires LIMIT 100",
                (now,),
            ).fetchall()
            + self.tenants.db.execute(
                "SELECT id FROM tenants WHERE expires<=? ORDER BY expires DESC LIMIT 100",
                (now,),
            ).fetchall()
        )
        for row in selected:
            info = self.tenants.summary(row["id"])
            info["remaining"] = max(0, info["budget"] - info["used"])
            info["expired"] = info["expires"] <= now
            enrollment = self.tenants.db.execute(
                "SELECT username FROM enrollments WHERE tenant=? AND bot=? AND owner=?",
                (info["id"], info["bot"], info["owner"]),
            ).fetchone()
            info["bot_username"] = enrollment[0] if enrollment else None
            notice = self.tenants.db.execute(
                "SELECT state FROM notifications WHERE tenant=? AND kind='expired' AND cycle=?",
                (info["id"], str(info["expires"])),
            ).fetchone()
            info["expiry_notice"] = notice[0] if notice else "pending"
            tenants.append(info)
        for account in accounts:
            account["daily_used"] = self.gate.db.execute(
                "SELECT COUNT(*) FROM attempts WHERE account=? AND day=?",
                (account["account"], day),
            ).fetchone()[0]
            lease = self.tenants.db.execute(
                "SELECT tenant FROM leases WHERE account=?", (account["account"],)
            ).fetchone()
            account["tenant"] = lease[0] if lease else None
        platform = self.control.platform
        return {
            "accounts": accounts,
            "group_management": self.group_control.status()
            if hasattr(self, "group_control")
            else {"bindings": [], "uncertain": 0},
            "tenants": tenants,
            "recent": [
                dict(row)
                for row in self.tenants.db.execute(
                    "SELECT account,tenant,created,state FROM reservations ORDER BY created DESC LIMIT 20"
                )
            ],
            "notifications": [
                dict(row)
                for row in self.tenants.db.execute(
                    "SELECT id,owner,bot,kind,account,state,created,delivered,error_code "
                    "FROM notifications ORDER BY id DESC LIMIT 20"
                )
            ],
            "tenant_counts": {
                "current": self.tenants.db.execute(
                    "SELECT COUNT(*) FROM tenants WHERE expires>?", (now,)
                ).fetchone()[0],
                "expired": self.tenants.db.execute(
                    "SELECT COUNT(*) FROM tenants WHERE expires<=?", (now,)
                ).fetchone()[0],
            },
            "checks": {
                "telegram_hooks": bool(
                    platform
                    and callable(getattr(platform, "register_application_hook", None))
                ),
                "hot_profile_pipeline": adapter.PIPELINE is not None
                and callable(getattr(adapter.PIPELINE, "dispatch", None)),
                "controller_bound": self.control.application is not None,
            },
            "compatibility": {
                "controller_transport": getattr(platform, "config", {}).get("type"),
                "account_pipeline": "plugin_owned",
                "native_config_ui": "optional_patched_core",
            },
            "daily_window": {"day": day, "timezone": "UTC", "reset_at": reset_at},
            "generated_at": now,
            "control_attached": self.control.application is not None,
            "tenant_count": self.tenants.db.execute(
                "SELECT COUNT(*) FROM tenants"
            ).fetchone()[0],
            "customer_entries": len(self.customers),
        }

    async def pause(self, account):
        self.gate.pause(account)
        platform = self.context.get_platform_inst(f"TelethonAI_{account}")
        if platform:
            await platform.terminate()

    async def page_pause(self):
        if not self.dashboard_admin():
            return error_response("Administrator required", status_code=403)
        body = await request.json(default={})
        if not isinstance(body, dict) or body.get("account") not in adapter.ACCOUNTS:
            return error_response("Unknown account")
        await self.pause(body["account"])
        return json_response({"paused": True})

    async def page_resume(self):
        if not self.dashboard_admin():
            return error_response("Administrator required", status_code=403)
        body = await request.json(default={})
        if (
            not isinstance(body, dict)
            or body.get("account") not in adapter.ACCOUNTS
            or body.get("confirm") is not True
        ):
            return error_response("Explicit account confirmation required")
        self.gate.pause(body["account"], False)
        # Connection still requires explicit platform enable/restart in AstrBot.
        return json_response({"paused": False, "connected": False})

    async def page_preview(self):
        if not self.dashboard_admin():
            return error_response("Administrator required", status_code=403)
        body = await request.json(default={})
        if not isinstance(body, dict) or body.get("account") not in adapter.ACCOUNTS:
            return error_response("Unknown account")
        prompt = body.get("prompt")
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 500:
            return error_response("Prompt must be 1-500 characters")
        if self.preview_lock.locked() or time.monotonic() - self.last_preview < 10:
            return error_response("Preview rate limit", status_code=429)
        async with self.preview_lock:
            self.last_preview = time.monotonic()
            umo = f"TelethonAI_{body['account']}:GroupMessage:preview"
            conf = self.context.get_config(umo)
            runner = conf.get("agent_runner", {}).get("config", {})
            persona_id = runner.get("persona", {}).get("persona_id")
            persona = self.context.persona_manager.get_persona_v3_by_id(persona_id)
            if not persona or not conf.get("provider_settings", {}).get("enable"):
                return error_response("Profile/model/persona unavailable")
            try:
                response = await asyncio.wait_for(
                    self.context.llm_generate(
                        chat_provider_id=runner["model"]["provider_id"],
                        prompt=prompt,
                        system_prompt=persona["prompt"],
                        contexts=[],
                        tools=None,
                        max_tokens=200,
                    ),
                    timeout=45,
                )
            except Exception:
                return error_response("Model preview failed", status_code=502)
            return json_response(
                {"reply": response.completion_text, "sent_to_telegram": False}
            )

    @filter.on_llm_request()
    async def guard(self, event, req):
        if event.get_platform_name() == "telethon_ai":
            req.func_tool = None
            if not isinstance(
                event, adapter.TelethonEvent
            ) or not event.adapter.tenants.can_send(event.reservation):
                event.stop_event()

    def is_admin(self, event):
        return (
            not self.closed
            and event.is_private_chat()
            and event.get_platform_id() == self.config.get("control_platform_id")
            and str(event.get_sender_id()) in self.config.get("admin_ids", [])
        )

    @filter.command("tgai")
    async def status_command(self, event):
        if not self.is_admin(event):
            event.stop_event()
            return
        yield event.plain_result(
            "\n".join(
                f"{r['account']}: {r['state']}; paused={r['paused']}; "
                f"model={r['model']}; persona={r['persona']}"
                for r in self.accounts()
            )
        )
        event.stop_event()

    @filter.command("tgaistop")
    async def stop_command(self, event):
        if not self.is_admin(event):
            event.stop_event()
            return
        for account in adapter.ACCOUNTS:
            await self.pause(account)
        yield event.plain_result("两个 Telethon AI 账号已紧急暂停。")
        event.stop_event()

    async def terminate(self):
        self.closed = True
        await self.group_control.close()
        await self.pipeline.close()
        manager = self.context.astrbot_config_mgr
        if getattr(manager, "profile_policy", None) is self.profile_policy:
            del manager.profile_policy
        if self.notification_task:
            self.notification_task.cancel()
            await asyncio.gather(self.notification_task, return_exceptions=True)
        await self.account_login.close()
        if (
            self.control.platform
            and self.control.standalone
            and getattr(self.control.platform, "required_plugin", None) == NAME
        ):
            await self.control.platform.suspend_required_plugin(NAME)
        for customer in self.customers.values():
            if (
                customer.platform
                and getattr(customer.platform, "required_plugin", None) == NAME
            ):
                await customer.platform.suspend_required_plugin(NAME)
        active = set(self.control.active)
        active.discard(asyncio.current_task())
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        self.control.detach()
        for customer in self.customers.values():
            customer.detach()
        for account in adapter.ACCOUNTS:
            platform = self.context.get_platform_inst(f"TelethonAI_{account}")
            if platform:
                await platform.terminate()
        adapter.CONTEXT = None
        adapter.PIPELINE = None
        self.gate.close()
        self.tenants.close()
