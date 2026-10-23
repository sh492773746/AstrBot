"""Native Dashboard delegation preserves authorization and failure boundaries."""

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.core.platform.sources.wangshangliao import dashboard, registration
from astrbot.dashboard.services import config_service, platform_service


@pytest.mark.asyncio
async def test_configuration_delegate_preserves_writer_and_owner(monkeypatch):
    delegate = AsyncMock()
    monkeypatch.setattr(dashboard, "save_configuration", delegate)
    service = SimpleNamespace()
    proposed = {"id": "fixture", "type": "wangshangliao"}
    current = {"id": "fixture", "account_id": "1"}
    await config_service.BotConfigService.save_wangshangliao(
        service, proposed, "principal", current
    )
    delegate.assert_awaited_once_with(
        service, proposed, "principal", current, persist=config_service.save_config
    )


@pytest.mark.asyncio
async def test_registration_delegate_preserves_request_and_manager(monkeypatch):
    delegate = AsyncMock(return_value={"status": "pending"})
    monkeypatch.setattr(dashboard, "handle_registration", delegate)
    manager = SimpleNamespace()
    service = platform_service.PlatformService(
        SimpleNamespace(platform_manager=manager)
    )
    payload = {"action": "poll", "registration_code": "fixture"}
    result = await service.handle_platform_registration(
        "wangshangliao", payload, "principal"
    )
    assert result == {"status": "pending"}
    delegate.assert_awaited_once_with(manager, payload, "principal")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner", "payload", "error"),
    [
        ("", {"action": "start"}, "dashboard_login_required"),
        (
            "principal",
            {"action": "logout", "instance_id": "other"},
            "instance_not_found",
        ),
        (
            "principal",
            {"action": "logout", "instance_id": "fixture", "extra": "invalid"},
            "registration_input",
        ),
        (
            "principal",
            {"action": "groups", "instance_id": "other"},
            "instance_not_found",
        ),
    ],
)
async def test_native_registration_denials_never_start_or_terminate(
    monkeypatch, owner, payload, error
):
    action = AsyncMock()
    monkeypatch.setattr(registration, "registrations", SimpleNamespace(action=action))
    manager = SimpleNamespace(
        astrbot_config={"platform": []},
        platform_insts=[],
        terminate_platform=AsyncMock(),
    )
    with pytest.raises(platform_service.PlatformServiceError, match=error) as caught:
        await dashboard.handle_registration(manager, payload, owner)
    assert caught.value.status_code == 400
    action.assert_not_awaited()
    manager.terminate_platform.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_failure_keeps_fixed_error_without_private_exception(monkeypatch):
    action = AsyncMock(side_effect=RuntimeError("private-token-and-provider-body"))
    monkeypatch.setattr(registration, "registrations", SimpleNamespace(action=action))
    manager = SimpleNamespace(astrbot_config={"platform": []}, platform_insts=[])
    with pytest.raises(platform_service.PlatformServiceError) as caught:
        await dashboard.handle_registration(manager, {"action": "poll"}, "principal")
    assert str(caught.value) == "registration_failed"
    assert caught.value.status_code == 500


def test_native_service_body_does_not_import_builtin_business_package():
    assert "astrbot.builtin_stars" not in inspect.getsource(dashboard)
    assert "registrations.claim" not in inspect.getsource(
        config_service.BotConfigService.save_wangshangliao
    )
    assert "execute_moderation" not in inspect.getsource(
        platform_service.PlatformService.handle_platform_registration
    )
