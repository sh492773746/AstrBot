"""Deleting an account persona is not silently undone by account synchronization."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from data.plugins.astrbot_plugin_telethon_ai import main
from data.plugins.astrbot_plugin_telethon_ai.native_pipeline import NativePipeline


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_bootstrap_only_creates_persona_for_new_profiles(monkeypatch, existing):
    monkeypatch.setattr(main.adapter, "ACCOUNTS", {"test": 123})
    personas = SimpleNamespace(
        get_persona_v3_by_id=lambda _: None, create_persona=AsyncMock()
    )
    manager = SimpleNamespace(
        get_conf_list=lambda: (
            [{"id": "p", "name": "Telethon AI - test"}] if existing else []
        ),
        create_conf=AsyncMock(return_value="p"),
        ucr=SimpleNamespace(umop_to_conf_id={}, update_route=AsyncMock()),
        default_conf={
            "platform": [
                {"id": "TelethonAI_test", "type": "telethon_ai", "account": "test"}
            ]
        },
    )
    value = main.TelethonAI.__new__(main.TelethonAI)
    value.config = {"model_provider_id": "model"}
    value.context = SimpleNamespace(
        persona_manager=personas, astrbot_config_mgr=manager, get_config=lambda _: {}
    )
    await value.provision()
    if existing:
        personas.create_persona.assert_not_awaited()
        manager.create_conf.assert_not_awaited()
    else:
        assert personas.create_persona.await_count == 1
        assert personas.create_persona.call_args.kwargs["tools"] == []
    manager.ucr.update_route.assert_awaited_once_with("TelethonAI_test::", "p")


@pytest.mark.asyncio
async def test_sync_respects_disabled_provisioning(monkeypatch):
    monkeypatch.setattr(main.adapter, "ACCOUNTS", {})
    monkeypatch.setattr(main.registry, "load", lambda: {"test": {"user_id": 123}})
    value = main.TelethonAI.__new__(main.TelethonAI)
    value.config = {"provision_profiles": False}
    value.account_sync_lock = asyncio.Lock()
    value.provision = AsyncMock()
    await value.sync_accounts()
    value.provision.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_selected_persona_blocks_native_default_fallback():
    conf = {
        "admins_id": [],
        "disable_builtin_commands": True,
        "plugin_set": [main.NAME],
        "kb_names": [],
        "provider_settings": {"enable": True},
        "agent_runner": {
            "runner_type": "local",
            "config": {"persona": {"persona_id": "deleted"}},
        },
    }
    context = SimpleNamespace(
        astrbot_config_mgr=SimpleNamespace(
            get_conf_info=lambda _: {"id": "p"}, confs={"p": conf}
        ),
        persona_manager=SimpleNamespace(
            get_persona_v3_by_id=MagicMock(return_value=None)
        ),
    )
    pipeline = NativePipeline(context)
    event = SimpleNamespace(
        created_at=time.time(), unified_msg_origin="account:group:1"
    )
    with pytest.raises(RuntimeError, match="persona was removed"):
        await pipeline.dispatch(event)
    assert not pipeline.active and not pipeline.schedulers
