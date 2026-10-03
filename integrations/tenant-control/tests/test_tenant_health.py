"""Telegram transport alone must not report a failed business plugin as healthy."""

import asyncio
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


class HealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_transport_requires_loaded_and_bound_business_plugin(self):
        path = Path(__file__).resolve().parents[1] / "astrbot_plugin_tenant_health/main.py"
        spec = importlib.util.spec_from_file_location("tenant_health_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        app = SimpleNamespace(running=True, updater=SimpleNamespace(running=True))
        metadata = SimpleNamespace(activated=True, star_cls=SimpleNamespace(
            store=object(), application=app, stopping=False))
        context = SimpleNamespace(
            get_platform_inst=lambda _: SimpleNamespace(application=app),
            get_registered_star=lambda _: metadata,
        )
        instance = object.__new__(module.Main)
        instance.context = context
        instance.config = {"platform_id": "tenant-1"}
        with tempfile.TemporaryDirectory() as tmp:
            instance.path = Path(tmp) / "health.json"
            for active, expected in ((True, True), (False, False)):
                metadata.activated = active
                with patch.object(module.asyncio, "sleep",
                                  AsyncMock(side_effect=asyncio.CancelledError)):
                    with self.assertRaises(asyncio.CancelledError):
                        await instance.heartbeat()
                self.assertEqual(json.loads(instance.path.read_text())["ready"], expected)
