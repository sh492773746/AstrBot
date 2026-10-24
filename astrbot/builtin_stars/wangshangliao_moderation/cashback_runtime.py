"""Invitation collection and native queries for the integrated settlement page."""

import asyncio
import json
from pathlib import Path

from astrbot import logger as log


class CashbackRuntime:
    def __init__(self, context):
        self.context = context
        self.tasks = []

    async def invitation_reply(self, event, group, action):
        service = getattr(self, "service", None)
        if service is None:
            return None
        try:
            return await service.invitation_reply(event, group, action)
        except Exception as exc:
            log.error(
                "Cashback invitation query failed group=%s type=%s",
                group,
                type(exc).__name__,
            )
            try:
                await asyncio.to_thread(
                    service.store.operation_log,
                    "invitation_query_failed",
                    group=group,
                    level="error",
                    code="query_unavailable",
                    detail={"exception_type": type(exc).__name__},
                )
            except Exception:
                pass
            return "返现记录暂时查询不了，请稍后再试。"

    async def private_invitation_reply(self, event):
        service = getattr(self, "service", None)
        if service is None:
            return "邀请返现查询暂不可用，请稍后再试。"
        try:
            return await service.private_queries.read(event)
        except Exception as exc:
            from .observability import record_event

            record_event(
                event,
                "private_invitation_query",
                "rejected",
                error=type(exc).__name__,
            )
            return "邀请返现查询暂不可用，请稍后再试。"

    async def start(self):
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        from .cashback import CashbackService

        root = Path(get_astrbot_data_path()) / "plugin_data" / "wangshangliao_cashback"
        path = root / "collector.json"
        if not path.exists():
            log.info("Cashback collector not configured")
            return
        try:
            config = json.loads(path.read_text())
            collector = config["collector_id"]
            if set(config) != {"collector_id"} or not isinstance(collector, str):
                raise ValueError("Invalid collector configuration")
            self.service = CashbackService(self.context, root, collector)
            self.service.store.operation_log(
                "service_started", bot_id=self.service.collector_id
            )
            task = asyncio.create_task(self.service.poll())
            task.add_done_callback(self._done)
            self.tasks = [task]
            log.info("Integrated cashback collector started; activation remains opt-in")
        except Exception as exc:
            log.error(
                "Cashback collector disabled due to configuration error type=%s",
                type(exc).__name__,
            )

    @staticmethod
    def _done(task):
        if not task.cancelled() and task.exception():
            log.error(
                "Cashback background service stopped type=%s",
                type(task.exception()).__name__,
            )

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []
        if hasattr(self, "service"):
            self.service.store.operation_log(
                "service_stopped", bot_id=self.service.collector_id
            )
