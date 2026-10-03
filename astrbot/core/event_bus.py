"""事件总线, 用于处理事件的分发和处理
事件总线是一个异步队列, 用于接收各种消息事件, 并将其发送到Scheduler调度器进行处理
其中包含了一个无限循环的调度函数, 用于从事件队列中获取新的事件, 并创建一个新的异步任务来执行管道调度器的处理逻辑

class:
    EventBus: 事件总线, 用于处理事件的分发和处理

工作流程:
1. 维护一个异步队列, 来接受各种消息事件
2. 无限循环的调度函数, 从事件队列中获取新的事件, 打印日志并创建一个新的异步任务来执行管道调度器的处理逻辑
"""

import asyncio
from asyncio import Queue
from functools import partial

from astrbot.core import logger
from astrbot.core.astrbot_config_mgr import AstrBotConfigManager
from astrbot.core.pipeline.context import PipelineContext
from astrbot.core.pipeline.scheduler import PipelineScheduler

from .platform import AstrMessageEvent


class EventBus:
    """用于处理事件的分发和处理"""

    def __init__(
        self,
        event_queue: Queue,
        pipeline_scheduler_mapping: dict[str, PipelineScheduler],
        astrbot_config_mgr: AstrBotConfigManager,
    ) -> None:
        self.event_queue = event_queue  # 事件队列
        # abconf uuid -> scheduler
        self.pipeline_scheduler_mapping = pipeline_scheduler_mapping
        self.astrbot_config_mgr = astrbot_config_mgr
        # 持有正在执行的 pipeline 任务的强引用, 防止 task 在 pending 状态被 GC 回收
        self._pending_tasks: set[asyncio.Task] = set()
        self._scheduler_lock = asyncio.Lock()

    async def ensure_scheduler(self, conf_id: str) -> PipelineScheduler:
        """Initialize hot-created profiles without routing them to defaults."""
        async with self._scheduler_lock:
            scheduler = self.pipeline_scheduler_mapping.get(conf_id)
            if scheduler is not None:
                return scheduler
            config = self.astrbot_config_mgr.confs.get(conf_id)
            base = self.pipeline_scheduler_mapping.get("default")
            if config is None or base is None:
                raise RuntimeError("Pipeline configuration unavailable")
            scheduler = PipelineScheduler(
                PipelineContext(
                    config, base.ctx.plugin_manager, conf_id, base.ctx.db_helper
                )
            )
            await scheduler.initialize()
            self.pipeline_scheduler_mapping[conf_id] = scheduler
            return scheduler

    async def dispatch(self) -> None:
        while True:
            event: AstrMessageEvent = await self.event_queue.get()
            completion = getattr(event, "processing_completion", None)
            if isinstance(completion, asyncio.Future) and completion.cancelled():
                continue
            conf_info = self.astrbot_config_mgr.get_conf_info(event.unified_msg_origin)
            conf_id = conf_info["id"]
            conf_name = conf_info.get("name") or conf_id
            self._print_event(event, conf_name)
            try:
                scheduler = await self.ensure_scheduler(conf_id)
            except Exception as exc:
                logger.error(
                    f"PipelineScheduler unavailable for id: {conf_id} "
                    f"({type(exc).__name__}), event ignored."
                )
                if isinstance(completion, asyncio.Future) and not completion.done():
                    completion.set_exception(RuntimeError("pipeline_missing"))
                continue
            task = asyncio.create_task(scheduler.execute(event))
            self._pending_tasks.add(task)
            task.add_done_callback(partial(self._on_task_done, completion=completion))
            if isinstance(completion, asyncio.Future):
                event.processing_task = task
                completion.add_done_callback(
                    lambda future, task=task: (
                        task.cancel() if future.cancelled() else None
                    )
                )

    def _on_task_done(self, task: asyncio.Task, completion=None) -> None:
        """pipeline 任务结束回调: 移除强引用并暴露未捕获的异常"""
        self._pending_tasks.discard(task)
        if isinstance(completion, asyncio.Future) and not completion.done():
            if task.cancelled():
                completion.cancel()
            elif task.exception() is not None:
                completion.set_exception(task.exception())
            else:
                completion.set_result(None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Pipeline task failed.", exc_info=exc)

    def _print_event(self, event: AstrMessageEvent, conf_name: str) -> None:
        """用于记录事件信息

        Args:
            event (AstrMessageEvent): 事件对象

        """
        # 如果有发送者名称: [平台名] 发送者名称/发送者ID: 消息概要
        if event.get_sender_name():
            logger.info(
                f"[{conf_name}] [{event.get_platform_id()}({event.get_platform_name()})] {event.get_sender_name()}/{event.get_sender_id()}: {event.get_message_outline()}",
                extra={"category": "user_chat"},
            )
        # 没有发送者名称: [平台名] 发送者ID: 消息概要
        else:
            logger.info(
                f"[{conf_name}] [{event.get_platform_id()}({event.get_platform_name()})] {event.get_sender_id()}: {event.get_message_outline()}",
                extra={"category": "user_chat"},
            )
