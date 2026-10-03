"""Plugin lifecycle around AstrBot's unchanged native pipeline stages."""

import asyncio
import copy
import json
import time

from astrbot.core.pipeline.context import PipelineContext
from astrbot.core.pipeline.scheduler import PipelineScheduler

from . import adapter


class NativePipeline:
    def __init__(self, context):
        self.context = context
        self.schedulers = {}
        self.lock = asyncio.Lock()
        self.slots = asyncio.Semaphore(4)
        self.active = set()
        self.closed = False

    async def dispatch(self, event):
        """Run an admitted account event through the native AstrBot scheduler.

        Args:
            event: Authorized Telethon event with a durable quota reservation.

        Raises:
            RuntimeError: Configuration, lifecycle, or request age is invalid.
        """
        task = asyncio.current_task()
        self.active.add(task)
        try:
            async with asyncio.timeout(110), self.slots:
                if self.closed or time.time() - event.created_at > 110:
                    raise RuntimeError("Account request expired or plugin stopped")
                manager = self.context.astrbot_config_mgr
                info = manager.get_conf_info(event.unified_msg_origin)
                conf_id = info["id"]
                if conf_id == "default" or conf_id not in manager.confs:
                    raise RuntimeError("Account profile is not explicitly bound")
                conf = manager.confs[conf_id]
                if (
                    conf.get("admins_id") != []
                    or conf.get("disable_builtin_commands") is not True
                    or conf.get("plugin_set") != [adapter.NAME]
                    or conf.get("kb_names") != []
                    or conf.get("agent_runner", {}).get("runner_type") != "local"
                    or not conf.get("provider_settings", {}).get("enable")
                ):
                    raise RuntimeError("Account profile isolation is invalid")
                signature = json.dumps(conf, sort_keys=True, ensure_ascii=True)
                async with self.lock:
                    cached = self.schedulers.get(conf_id)
                    if cached is None or cached[0] != signature:
                        scheduler = PipelineScheduler(
                            PipelineContext(
                                copy.deepcopy(dict(conf)),
                                self.context._star_manager,
                                conf_id,
                                self.context._db,
                            )
                        )
                        await scheduler.initialize()
                        self.schedulers[conf_id] = (signature, scheduler)
                    else:
                        scheduler = cached[1]
                await scheduler.execute(event)
        finally:
            self.active.discard(task)

    async def close(self):
        self.closed = True
        tasks = self.active - {asyncio.current_task()}
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.schedulers.clear()
