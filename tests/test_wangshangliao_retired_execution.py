"""Legacy public-worker entry points must never resume independent execution."""

import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from astrbot.builtin_stars.wangshangliao_moderation.open_rpc import (
    OpenCall,
    OpenRPC,
    call_open,
)
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from tests.test_wangshangliao_dashboard import headers as headers
from tests.test_wangshangliao_dashboard import page_app as page_app


@pytest.mark.asyncio
async def test_socket_retirement_does_not_read_or_remove_legacy_files(
    tmp_path, monkeypatch
):
    path = tmp_path / "control.sock"
    path.write_text("Preserve the old artifact for operator review.")
    monkeypatch.setenv("ASTRBOT_WSL_OPEN_SOCKET", str(path))
    rpc = OpenRPC(SimpleNamespace(), path)
    request = SimpleNamespace(json=AsyncMock())
    with pytest.raises(OpenError, match="isolated_mode_retired"):
        await rpc.start()
    with pytest.raises(OpenError, match="isolated_mode_retired"):
        await call_open("status", "dashboard/admin", {})
    response = await rpc.invoke(request)
    assert response.status == 410
    request.json.assert_not_awaited()
    await rpc.close()
    assert path.read_text() == "Preserve the old artifact for operator review."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    [
        "login",
        "login_save",
        "tenant",
        "limits",
        "model",
        "activation_code",
        "ai_budget",
    ],
)
async def test_obsolete_service_cannot_execute_inprocess_mutations(action):
    blocked = Mock(side_effect=AssertionError("Retired RPC attempted IO"))
    rpc = OpenRPC(
        SimpleNamespace(
            integrated=False,
            account_action=blocked,
            configure_model=blocked,
            resolve_model=blocked,
            context=SimpleNamespace(get_config=blocked),
            store=SimpleNamespace(configure=blocked, confirm_budget=blocked),
        )
    )
    payload = {
        "limits": {
            "version": 1,
            "paused": False,
            "personal_limit": 20,
            "global_limit": 1000,
            "admins": ["1"],
        },
        "ai_budget": {
            "version": 1,
            "confirmed": True,
            "provider": "existing/model",
            "evidence": "offline fixture",
        },
        "model": {
            "base_url": "https://model.example/v1",
            "model": "old",
            "api_key": "secret",
        },
    }.get(action, {})
    with pytest.raises(OpenError, match="isolated_mode_retired"):
        await rpc.dispatch(
            OpenCall(action=action, actor="dashboard/admin", payload=payload)
        )
    blocked.assert_not_called()


@pytest.mark.asyncio
async def test_dashboard_without_integrated_runtime_never_uses_network_fallback(
    page_app, headers, monkeypatch
):
    legacy = AsyncMock(side_effect=AssertionError("Network fallback is retired"))
    monkeypatch.setattr(
        "astrbot.builtin_stars.wangshangliao_moderation.open_rpc.call_open", legacy
    )
    app, page, *_ = page_app
    page.plugin = SimpleNamespace(open_service=None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        response = await client.get(
            "/api/v1/plugins/extensions/wangshangliao_moderation/open-service",
            headers=headers,
        )
    assert response.status_code == 503
    assert "停用" in response.text
    legacy.assert_not_awaited()


def test_retired_unit_cannot_start_a_legacy_copy():
    unit = (
        Path(__file__).resolve().parents[1] / "services/astrbot-open.service"
    ).read_text()
    assert "ExecStart=/usr/bin/false" in unit
    assert "ConditionPathExists=/nonexistent/" in unit
    assert "Restart=no" in unit
    assert "run_wsl_open.py" not in unit
    assert "Environment=" not in unit
    assert "[Install]" not in unit


@pytest.mark.parametrize("script", ["run_wsl_open", "deploy_wsl_open"])
@pytest.mark.parametrize("args", [[], ["--install", "--start"]])
def test_retired_launchers_refuse_all_flags(script, args, monkeypatch):
    monkeypatch.setattr("sys.argv", [script, *args])
    module = importlib.import_module("scripts." + script)
    with pytest.raises(SystemExit, match="retired") as error:
        module.main()
    assert error.value.code != 0
