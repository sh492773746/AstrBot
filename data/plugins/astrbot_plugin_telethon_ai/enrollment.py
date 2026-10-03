"""Managed Bot enrollment into one AstrBot, without Docker or customer WebUI."""

import asyncio
import copy
import os
import time

from cryptography.fernet import Fernet
from telegram import Bot

from astrbot.core.config.default import DEFAULT_CONFIG

from . import adapter, registry
from .customer import isolated_profile
from .service_adapter import TYPE
from .tenants import Denied


class Enrollment:
    def __init__(self, plugin):
        self.plugin = plugin
        self.lock = asyncio.Lock()
        path = registry.private_root() / "managed-bots.key"
        if not path.exists():
            try:
                with path.open("xb") as file:
                    os.chmod(path, 0o600)
                    file.write(Fernet.generate_key())
            except FileExistsError:
                pass
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise ValueError("Encryption key must be private")
        self.cipher = Fernet(path.read_bytes())

    async def managed(self, update, context):
        item = update.managed_bot
        if item is None:
            return
        owner, bot = str(item.user.id), str(item.bot.id)
        store = self.plugin.tenants
        async with self.lock:
            existing = store.db.execute(
                "SELECT * FROM enrollments WHERE bot=?", (bot,)
            ).fetchone()
            if existing:
                if existing["owner"] != owner:
                    raise Denied("Owner mismatch")
            else:
                if store.pending_grant(owner) is None:
                    raise Denied("No trial grant")
                token = await context.bot.get_managed_bot_token(int(bot))
                async with Bot(token) as probe:
                    identity = await probe.get_me()
                    webhook = await probe.get_webhook_info()
                if str(identity.id) != bot or webhook.url:
                    raise Denied("Bot identity mismatch or existing webhook")
                store.enroll(
                    owner,
                    bot,
                    item.bot.username or "",
                    self.cipher.encrypt(token.encode()),
                )
            await self.activate(bot)

    async def activate(self, bot):
        store = self.plugin.tenants
        record = store.db.execute(
            "SELECT * FROM enrollments WHERE bot=?", (str(bot),)
        ).fetchone()
        if record is None:
            raise Denied("Unknown enrollment")
        tenant = store.summary(record["tenant"])
        if tenant["expires"] <= time.time():
            raise Denied("Trial expired")
        manager = self.plugin.context.astrbot_config_mgr
        platform_id = tenant["platform"]
        route = f"{platform_id}::"
        name = f"AI Service - {bot}"
        matches = [c for c in manager.get_conf_list() if c["name"] == name]
        if len(matches) > 1:
            raise Denied("Duplicate customer profiles")
        current = manager.ucr.umop_to_conf_id.get(route)
        if current and (not matches or current != matches[0]["id"]):
            raise Denied("Customer route collision")
        if matches:
            profile_id = matches[0]["id"]
            conf = manager.confs[profile_id]
            if not isolated_profile(conf):
                raise Denied("Customer profile no longer isolated")
        else:
            conf = copy.deepcopy(DEFAULT_CONFIG)
            conf["admins_id"] = []
            conf["disable_builtin_commands"] = True
            conf["plugin_set"] = [adapter.NAME]
            conf["wake_prefix"] = ["/"]
            conf["provider_settings"]["enable"] = False
            conf["kb_names"] = []
            conf["dashboard"]["enable"] = False
            profile_id = await manager.create_conf(conf, name=name)
        await manager.ucr.update_route(route, profile_id)
        for message_type in ("FriendMessage", "GroupMessage"):
            if (
                manager.ucr.get_conf_id_for_umop(f"{platform_id}:{message_type}:0")
                != profile_id
            ):
                raise Denied("Customer profile route overridden")
        root = manager.default_conf
        platforms = root.setdefault("platform", [])
        entry = next((p for p in platforms if p.get("id") == platform_id), None)
        token = self.cipher.decrypt(record["token_cipher"]).decode()
        if entry is None:
            if any(p.get("telegram_token") == token for p in platforms):
                raise Denied("Token already attached to another platform")
            entry = {
                "id": platform_id,
                "type": TYPE,
                "service_role": "customer",
                "enable": False,
                "telegram_token": token,
                "telegram_allowed_updates": [
                    "message",
                    "callback_query",
                    "my_chat_member",
                ],
                "telegram_required_plugin": adapter.NAME,
                "telegram_command_register": False,
                "telegram_command_auto_refresh": False,
                "telegram_dedicated_reporting": True,
            }
            platforms.append(entry)
            root.save_config()
        elif (
            entry.get("type") not in {"telegram", TYPE}
            or entry.get("telegram_token") != token
        ):
            raise Denied("Platform identity conflict")
        elif (
            entry.get("telegram_required_plugin") != adapter.NAME
            or entry.get("telegram_command_register") is not False
            or entry.get("telegram_command_auto_refresh") is not False
            or entry.get("telegram_dedicated_reporting") is not True
            or entry.get("telegram_allowed_updates")
            not in (["message"], ["message", "callback_query", "my_chat_member"])
        ):
            raise Denied(
                "Customer Bot platform requires a reviewed isolation migration"
            )
        elif entry.get("type") == TYPE and entry.get("service_role") != "customer":
            raise Denied("Customer Bot service role mismatch")
        running = self.plugin.context.get_platform_inst(platform_id)
        if running is not None and (
            getattr(running, "required_plugin", None) != adapter.NAME
            or getattr(running, "config", {}).get("telegram_command_register")
            is not False
            or getattr(running, "config", {}).get("telegram_dedicated_reporting")
            is not True
        ):
            raise Denied(
                "Customer Bot platform must be restarted with isolation enabled"
            )
        # Profile and owner record exist before the transport can accept updates.
        entry["enable"] = True
        root.save_config()
        if self.plugin.context.get_platform_inst(platform_id) is None:
            await self.plugin.context.platform_manager.load_platform(entry)
        self.plugin.bind_customers()
        with store.db:
            store.db.execute(
                "UPDATE enrollments SET state='active',error_code=NULL WHERE bot=?",
                (str(bot),),
            )
            store.audit(
                "enrollment", "native_bot_attached", tenant=tenant["id"], bot=str(bot)
            )

    async def retry(self, bot):
        async with self.lock:
            await self.activate(bot)
