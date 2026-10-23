import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telegram.ext import ApplicationHandlerStop

from astrbot.core.config.default import DEFAULT_CONFIG
from data.plugins.astrbot_plugin_telethon_ai import (
    adapter,
    native_pipeline,
    service_adapter,
)
from data.plugins.astrbot_plugin_telethon_ai.control import Control
from data.plugins.astrbot_plugin_telethon_ai.customer import Customer


@pytest.fixture
def service():
    config = {**service_adapter.DEFAULT, "telegram_token": "123456789:fixture-not-live"}
    instance = service_adapter.ServiceAdapter(config, {}, asyncio.Queue())
    yield instance


def test_owned_transport_never_enters_native_bus(service):
    with pytest.raises(PermissionError, match="AI bus"):
        service.commit_event(object())
    assert service._event_queue.empty()


@pytest.mark.asyncio
async def test_owned_transport_rejects_proactive_send(service):
    with pytest.raises(PermissionError, match="authenticated"):
        await service.send_by_session(None, None)


def test_only_service_owner_can_bind_hooks(service):
    with pytest.raises(ValueError):
        service.register_application_hook("another-plugin", Mock())
    callback = Mock()
    service.register_application_hook(adapter.NAME + ":control", callback)
    callback.assert_called_once_with(service.application)
    with pytest.raises(ValueError):
        service.register_application_hook(adapter.NAME + ":control", Mock())
    service.application.bot_data[adapter.NAME + ":ready"] = True
    service.unregister_application_hook(adapter.NAME + ":control")
    assert not service.application.bot_data.get(adapter.NAME + ":ready")


@pytest.mark.parametrize(
    "key,value",
    [
        ("service_role", "arbitrary"),
        ("telegram_required_plugin", "another"),
        ("telegram_dedicated_reporting", False),
        ("telegram_command_register", True),
        ("telegram_command_auto_refresh", True),
    ],
)
def test_insecure_service_configuration_rejected(key, value):
    config = {
        **service_adapter.DEFAULT,
        "telegram_token": "123456789:fixture",
        key: value,
    }
    with pytest.raises(ValueError):
        service_adapter.ServiceAdapter(config, {}, asyncio.Queue())


def test_control_and_customer_attach_to_owned_platform(service):
    plugin = SimpleNamespace(
        config={},
        closed=False,
        context=SimpleNamespace(get_platform_inst=lambda _: service),
    )
    control = Control(plugin)
    control.bind()
    assert control.application is service.application
    assert service.application.bot_data[adapter.NAME + ":ready"]
    control.detach()
    customer_platform = service_adapter.ServiceAdapter(
        {
            **service_adapter.DEFAULT,
            "service_role": "customer",
            "telegram_token": "123456789:fixture",
        },
        {},
        asyncio.Queue(),
    )
    plugin.context.get_platform_inst = lambda _: customer_platform
    customer = Customer(plugin, "Client")
    customer.bind()
    assert customer.application is customer_platform.application
    assert customer_platform.application.bot_data[adapter.NAME + ":ready"]
    customer.detach()
    assert not customer_platform.application.bot_data.get(adapter.NAME + ":ready")


@pytest.mark.asyncio
async def test_error_never_leaks_details_or_enqueues_message(service):
    await service.handle_error(object(), SimpleNamespace(error=RuntimeError("secret")))
    assert "secret" not in service.last_error.message
    assert service._event_queue.empty()


@pytest.fixture
def runtime(monkeypatch):
    conf = copy.deepcopy(DEFAULT_CONFIG)
    conf.update(
        admins_id=[],
        plugin_set=[adapter.NAME],
        disable_builtin_commands=True,
        kb_names=[],
    )
    conf["provider_settings"]["enable"] = True
    manager = SimpleNamespace(
        confs={"account": conf},
        get_conf_info=lambda _: {"id": "account"},
    )
    context = SimpleNamespace(
        astrbot_config_mgr=manager, _star_manager=object(), _db=object()
    )
    pipeline = native_pipeline.NativePipeline(context)
    created = []

    def build(ctx):
        instance = SimpleNamespace(ctx=ctx, initialize=AsyncMock(), execute=AsyncMock())
        created.append(instance)
        return instance

    monkeypatch.setattr(native_pipeline, "PipelineScheduler", build)
    return pipeline, manager, created


def event():
    import time

    return SimpleNamespace(
        unified_msg_origin="Account:GroupMessage:one", created_at=time.time()
    )


@pytest.mark.asyncio
async def test_native_scheduler_cached_and_rebuilt_after_config_save(runtime):
    pipeline, manager, created = runtime
    await asyncio.gather(pipeline.dispatch(event()), pipeline.dispatch(event()))
    assert len(created) == 1
    assert created[0].execute.await_count == 2
    assert created[0].ctx.astrbot_config is not manager.confs["account"]
    manager.confs["account"]["agent_runner"]["config"]["persona"]["persona_id"] = (
        "new-persona"
    )
    await pipeline.dispatch(event())
    assert len(created) == 2
    assert (
        created[1].ctx.astrbot_config["agent_runner"]["config"]["persona"]["persona_id"]
        == "new-persona"
    )
    await pipeline.close()
    assert not pipeline.schedulers


@pytest.mark.parametrize(
    "key,value",
    [
        ("plugin_set", ["*"]),
        ("admins_id", ["1"]),
        ("kb_names", ["private"]),
        ("disable_builtin_commands", False),
    ],
)
@pytest.mark.asyncio
async def test_pipeline_profile_isolation_fail_closed(runtime, key, value):
    pipeline, manager, created = runtime
    manager.confs["account"][key] = value
    with pytest.raises(RuntimeError, match="isolation"):
        await pipeline.dispatch(event())
    assert not created


@pytest.mark.asyncio
async def test_pipeline_never_falls_back_to_default(runtime):
    pipeline, manager, created = runtime
    manager.get_conf_info = lambda _: {"id": "default"}
    with pytest.raises(RuntimeError, match="explicitly"):
        await pipeline.dispatch(event())
    assert not created


@pytest.mark.asyncio
async def test_unload_cancels_inflight_requests(runtime):
    pipeline, _, created = runtime
    await pipeline.dispatch(event())
    started = asyncio.Event()

    async def block(_):
        started.set()
        await asyncio.Future()

    created[0].execute.side_effect = block
    task = asyncio.create_task(pipeline.dispatch(event()))
    await started.wait()
    await pipeline.close()
    assert task.cancelled()
    assert not pipeline.active


@pytest.mark.asyncio
async def test_private_control_handler_still_stops_ordinary_updates(service):
    plugin = SimpleNamespace(config={}, closed=False)
    control = Control(plugin)
    with pytest.raises(ApplicationHandlerStop):
        await control.ignore(SimpleNamespace(), SimpleNamespace())
