"""Private filesystem heartbeat; no network listener or Telegram messages."""

import asyncio
import json
import time
from contextlib import suppress
from pathlib import Path

from astrbot.api import AstrBotConfig
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path


class Main(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context, config)
        self.config = config
        self.task = None
        self.path = Path(get_astrbot_data_path()) / "tenant-health.json"

    async def initialize(self):
        self.path.unlink(missing_ok=True)
        self.task = asyncio.create_task(self.heartbeat())

    async def heartbeat(self):
        while True:
            platform = self.context.get_platform_inst(self.config["platform_id"])
            application = getattr(platform, "application", None)
            updater = getattr(application, "updater", None)
            metadata = self.context.get_registered_star("astrbot_plugin_superbot")
            plugin = getattr(metadata, "star_cls", None)
            business_ready = bool(
                metadata
                and metadata.activated
                and getattr(plugin, "store", None)
                and getattr(plugin, "application", None) is application
                and not getattr(plugin, "stopping", False)
            )
            report = {
                "platform_id": self.config["platform_id"],
                "ready": bool(
                    getattr(application, "running", False)
                    and getattr(updater, "running", False)
                    and business_ready
                ),
                "at": time.time(),
            }
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(report))
            temp.chmod(0o600)
            temp.replace(self.path)
            await asyncio.sleep(15)

    async def terminate(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        self.path.unlink(missing_ok=True)
