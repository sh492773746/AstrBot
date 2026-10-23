"""本地 Agent 模式的 AstrBot 插件调用 Stage"""

import traceback
from collections.abc import AsyncGenerator
from typing import Any

from astrbot.core import logger
from astrbot.core.message.message_event_result import MessageEventResult
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.star.star import star_map
from astrbot.core.star.star_handler import EventType, StarHandlerMetadata

from ...context import PipelineContext, call_event_hook, call_handler
from ..stage import Stage


class StarRequestSubStage(Stage):
    async def initialize(self, ctx: PipelineContext) -> None:
        self.prompt_prefix = ctx.astrbot_config["provider_settings"]["prompt_prefix"]
        self.identifier = ctx.astrbot_config["provider_settings"]["identifier"]
        self.ctx = ctx

    async def process(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        activated_handlers: list[StarHandlerMetadata] = event.get_extra(
            "activated_handlers",
        )
        handlers_parsed_params: dict[str, dict[str, Any]] = event.get_extra(
            "handlers_parsed_params",
        )
        if not handlers_parsed_params:
            handlers_parsed_params = {}

        for handler in activated_handlers:
            if event.is_stopped():
                break
            params = handlers_parsed_params.get(handler.handler_full_name, {})
            md = star_map.get(handler.handler_module_path)
            if not md:
                logger.warning(
                    f"Cannot find plugin for given handler module path: {handler.handler_module_path}",
                )
                continue
            logger.debug(f"plugin -> {md.name} - {handler.handler_name}")
            component_context = None
            try:
                wrapper = call_handler(event, handler.handler, **params)
                component_id = handler.extras_configs.get("component_id")
                if component_id:
                    from astrbot.core.star.component_runtime import (
                        ExecutionContext,
                        RuntimeRejected,
                        component_runtime,
                    )

                    def authorize():
                        completion = getattr(event, "processing_completion", None)
                        if completion is not None and completion.cancelled():
                            raise RuntimeRejected("component_event_stopped")

                    component_context = ExecutionContext(
                        scope=(str(event.get_self_id()), str(event.get_group_id())),
                        authorize=authorize,
                        operation=lambda wrapper=wrapper: wrapper,
                        claimed=bool(event.get_extra("_component_claimed")),
                    )
                    wrapper = component_runtime.iterate(component_id, component_context)
                async for ret in wrapper:
                    yield ret
                if event.is_stopped():
                    break
                event.clear_result()  # 清除上一个 handler 的结果
            except Exception as e:
                traceback_text = traceback.format_exc()
                logger.error(traceback_text)
                logger.error(f"Star {handler.handler_full_name} handle error: {e}")

                await call_event_hook(
                    event,
                    EventType.OnPluginErrorEvent,
                    md.name,
                    handler.handler_name,
                    e,
                    traceback_text,
                )
                if component_context is not None:
                    spec = component_runtime.registry.specs.get(component_id)
                    if (
                        spec is not None
                        and not spec.critical
                        and not component_context.claimed
                        and not event.get_extra("_component_claimed")
                        and not event.is_stopped()
                    ):
                        event.clear_result()
                        continue

                if not event.is_stopped() and event.is_at_or_wake_command:
                    ret = f":(\n\n在调用插件 {md.name} 的处理函数 {handler.handler_name} 时出现异常：{e}"
                    event.set_result(MessageEventResult().message(ret))
                    yield
                    event.clear_result()

                event.stop_event()
