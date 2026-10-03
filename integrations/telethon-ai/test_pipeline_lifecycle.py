import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.core import event_bus


def bus(configs):
    base = SimpleNamespace(
        ctx=SimpleNamespace(plugin_manager=object(), db_helper=object())
    )
    return event_bus.EventBus(
        asyncio.Queue(), {"default": base}, SimpleNamespace(confs=configs)
    )


@pytest.mark.asyncio
async def test_hot_profile_initialized_once_with_own_config(monkeypatch):
    config = {"profile": "isolated"}
    instance = bus({"new": config})
    built = []

    def create(context):
        scheduler = SimpleNamespace(ctx=context, initialize=AsyncMock())
        built.append(scheduler)
        return scheduler

    monkeypatch.setattr(event_bus, "PipelineScheduler", create)
    a, b = await asyncio.gather(
        instance.ensure_scheduler("new"), instance.ensure_scheduler("new")
    )
    assert a is b and len(built) == 1
    assert a.ctx.astrbot_config is config
    assert a.ctx.astrbot_config_id == "new"
    assert (
        a.ctx.plugin_manager
        is instance.pipeline_scheduler_mapping["default"].ctx.plugin_manager
    )
    a.initialize.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_profile_never_falls_back():
    instance = bus({})
    with pytest.raises(RuntimeError):
        await instance.ensure_scheduler("missing")
    assert set(instance.pipeline_scheduler_mapping) == {"default"}


@pytest.mark.asyncio
async def test_initialization_failure_not_cached_and_retryable(monkeypatch):
    instance = bus({"new": {}})
    scheduler = SimpleNamespace(initialize=AsyncMock(side_effect=ValueError("invalid")))
    monkeypatch.setattr(event_bus, "PipelineScheduler", lambda context: scheduler)
    with pytest.raises(ValueError):
        await instance.ensure_scheduler("new")
    assert "new" not in instance.pipeline_scheduler_mapping
    scheduler.initialize.side_effect = None
    assert await instance.ensure_scheduler("new") is scheduler


@pytest.mark.asyncio
async def test_dispatch_survives_missing_profile_then_processes_valid_one(monkeypatch):
    instance = bus({"new": {"isolated": True}})
    instance.astrbot_config_mgr.get_conf_info = lambda origin: {"id": origin}
    monkeypatch.setattr(instance, "_print_event", lambda *args: None)
    scheduler = SimpleNamespace(initialize=AsyncMock(), execute=AsyncMock())
    monkeypatch.setattr(event_bus, "PipelineScheduler", lambda context: scheduler)
    loop = asyncio.get_running_loop()
    missing = SimpleNamespace(
        unified_msg_origin="missing", processing_completion=loop.create_future()
    )
    valid = SimpleNamespace(
        unified_msg_origin="new", processing_completion=loop.create_future()
    )
    task = asyncio.create_task(instance.dispatch())
    try:
        await instance.event_queue.put(missing)
        await instance.event_queue.put(valid)
        with pytest.raises(RuntimeError, match="pipeline_missing"):
            await asyncio.wait_for(missing.processing_completion, 2)
        await asyncio.wait_for(valid.processing_completion, 2)
        scheduler.execute.assert_awaited_once_with(valid)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
