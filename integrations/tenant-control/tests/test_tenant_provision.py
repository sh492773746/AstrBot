"""Chat-only tenant profiles retain ownership boundaries without embeddings."""

import importlib
import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))


class ProvisionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module = importlib.import_module(
            "data.plugins.astrbot_plugin_superbot.provision"
        )
        self.values = {}
        self.store = SimpleNamespace(
            get=lambda key: self.values.get(key),
            tx=lambda: nullcontext(None),
            put=lambda db, key, value: self.values.update({key: value}),
            audit=MagicMock(),
        )
        self.manager = SimpleNamespace(
            confs={},
            get_conf_list=lambda: [],
            ucr=SimpleNamespace(umop_to_conf_id={}, update_route=AsyncMock()),
        )

        async def create(config, name):
            self.manager.confs["profile"] = config
            return "profile"

        self.manager.create_conf = AsyncMock(side_effect=create)
        self.context = SimpleNamespace(
            astrbot_config_mgr=self.manager,
            kb_manager=MagicMock(),
            get_provider_by_id=MagicMock(),
        )

    async def test_no_knowledge_and_no_builtin_admin_on_first_and_repeated_start(self):
        for _ in range(2):
            result = await self.module.prepare(
                self.context,
                self.store,
                "tenant-123",
                "Gemini/chat",
                "",
                chat_only_tenant=True,
            )
            self.assertEqual(result, "profile")
        self.manager.create_conf.assert_awaited_once()
        config = self.manager.confs["profile"]
        self.assertEqual(config["admins_id"], [])
        self.assertEqual(config["kb_names"], [])
        self.assertTrue(config["disable_builtin_commands"])
        self.assertFalse(config["dashboard"]["enable"])
        self.assertFalse(
            config["provider_settings"]["proactive_capability"]["add_cron_tools"]
        )
        self.assertEqual(
            config["agent_runner"]["config"]["model"]["provider_id"], "Gemini/chat"
        )
        self.context.get_provider_by_id.assert_not_called()
        self.assertFalse(self.context.kb_manager.mock_calls)

    async def test_shared_production_platform_and_conflicting_route_rejected(self):
        with self.assertRaises(self.module.Rejected):
            await self.module.prepare_chat_only(
                self.context, self.store, "production", "chat"
            )
        self.manager.ucr.umop_to_conf_id["tenant-123:*:*"] = "other"
        with self.assertRaises(self.module.Rejected):
            await self.module.prepare_chat_only(
                self.context, self.store, "tenant-123", "chat"
            )

    async def test_profile_permission_changes_fail_closed(self):
        await self.module.prepare_chat_only(
            self.context, self.store, "tenant-123", "chat"
        )
        self.manager.confs["profile"]["admins_id"] = ["11"]
        with self.assertRaises(self.module.Rejected):
            await self.module.prepare_chat_only(
                self.context, self.store, "tenant-123", "chat"
            )
