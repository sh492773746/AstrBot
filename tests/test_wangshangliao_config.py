"""Policy saves preserve opt-ins from older dashboards without expanding grants."""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot.core.platform.sources.wangshangliao import registration, storage
from astrbot.dashboard.services import config_service


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_disable", [False, True])
async def test_legacy_save_preserves_new_policy_and_explicit_disable_wins(
    monkeypatch, explicit_disable
):
    current = {
        "id": "fixture",
        "type": "wangshangliao",
        "account_id": "1",
        "enabled_groups": ["5"],
        "ai_routes": {
            "admin_provider_id": "admin-model",
            "groups": {"5": "customer-model"},
        },
        "moderation": {
            "enabled": True,
            "automation_enabled": True,
            "content_rules_since": 1700000000,
            "semantic": {"enabled": True, "provider_id": "fixture-model"},
            "auto_kick": {"5": True},
            "permissions": {"5": ["mute", "recall", "kick"]},
        },
    }
    original = copy.deepcopy(current)
    legacy = copy.deepcopy(current)
    for field in ("content_rules_since", "semantic", "auto_kick"):
        legacy["moderation"].pop(field)
    legacy.pop("ai_routes")
    if explicit_disable:
        legacy["moderation"].update(semantic={"enabled": False}, auto_kick={"5": False})
    monkeypatch.setattr(
        registration, "registrations", SimpleNamespace(commit_lock=asyncio.Lock())
    )
    monkeypatch.setattr(storage, "Vault", lambda _: SimpleNamespace(load=lambda: None))
    monkeypatch.setattr(config_service, "save_config", Mock())
    service = SimpleNamespace(
        config={"platform": [copy.deepcopy(current)]},
        core_lifecycle=SimpleNamespace(
            platform_manager=SimpleNamespace(
                terminate_platform=AsyncMock(),
                load_platform=AsyncMock(),
            )
        ),
    )
    await config_service.BotConfigService.save_wangshangliao(
        service, legacy, "fixture-owner", current
    )
    policy = service.config["platform"][0]["moderation"]
    assert policy["semantic"]["enabled"] is not explicit_disable
    assert policy["auto_kick"]["5"] is not explicit_disable
    assert policy["content_rules_since"] == 1700000000
    assert policy["permissions"] == current["moderation"]["permissions"]
    assert service.config["platform"][0]["ai_routes"] == current["ai_routes"]
    assert current == original
