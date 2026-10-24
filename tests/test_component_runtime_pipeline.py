"""An opt-in observer may continue, but legacy or claimed failures still stop."""

import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.core.pipeline.process_stage import stage as process_stage
from astrbot.core.star import component_runtime as runtime_module
from astrbot.core.star.component_runtime import ComponentRuntime, current_execution
from astrbot.core.star.components import CapabilitySpec, ComponentRegistry

star_request = importlib.import_module(process_stage.StarRequestSubStage.__module__)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "migrated,claimed,stopped",
    [
        (True, False, False),
        (True, True, True),
        (False, False, True),
    ],
)
async def test_only_explicit_unclaimed_observer_failure_continues(
    monkeypatch,
    migrated,
    claimed,
    stopped,
):
    registry = ComponentRegistry()
    registry.register(
        CapabilitySpec(id="observer", plugin="fixture", critical=False),
        handler=lambda context: context.operation(),
        authorize=lambda context: context.check(),
    )
    runtime = ComponentRuntime(registry)
    monkeypatch.setattr(runtime_module, "component_runtime", runtime)
    handlers = [
        SimpleNamespace(
            handler_full_name=name,
            handler_module_path="fixture",
            handler_name=name,
            handler=name,
            extras_configs={"component_id": "observer"}
            if migrated and name == "bad"
            else {},
        )
        for name in ("bad", "good")
    ]
    monkeypatch.setitem(
        star_request.star_map, "fixture", SimpleNamespace(name="fixture")
    )
    monkeypatch.setattr(star_request, "call_event_hook", AsyncMock())

    async def handle(event, handler, **params):
        if handler == "bad":
            if claimed:
                current_execution.get().claim()
            raise RuntimeError("observer broke")
        yield "continued"

    monkeypatch.setattr(star_request, "call_handler", handle)
    extras = {"activated_handlers": handlers}
    state = {"stopped": False}
    event = SimpleNamespace(
        get_extra=lambda key: extras.get(key),
        get_self_id=lambda: "account",
        get_group_id=lambda: "group",
        is_at_or_wake_command=False,
        is_stopped=lambda: state["stopped"],
        stop_event=lambda: state.update(stopped=True),
        clear_result=lambda: None,
    )
    output = [
        frame async for frame in star_request.StarRequestSubStage().process(event)
    ]
    assert state["stopped"] is stopped
    assert output == ([] if stopped else ["continued"])
    await runtime.close("fixture")
