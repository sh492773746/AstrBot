"""Policy saves preserve opt-ins from older dashboards without expanding grants."""

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot.core.platform.sources.wangshangliao import registration, storage
from astrbot.dashboard.services import config_service


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_disable", [False, True])
@pytest.mark.parametrize("pause_moderation", [False, True])
async def test_legacy_save_preserves_new_policy_and_explicit_disable_wins(
    monkeypatch, explicit_disable, pause_moderation
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
            "card_auto": {"5": True},
            "card_suffix": {"5": "·群员"},
            "permissions": {"5": ["mute", "recall", "kick", "rename"]},
        },
    }
    original = copy.deepcopy(current)
    legacy = copy.deepcopy(current)
    for field in (
        "content_rules_since",
        "semantic",
        "auto_kick",
        "card_auto",
        "card_suffix",
    ):
        legacy["moderation"].pop(field)
    legacy.pop("ai_routes")
    if explicit_disable:
        legacy["moderation"].update(semantic={"enabled": False}, auto_kick={"5": False})
    if pause_moderation:
        legacy["moderation"]["enabled"] = False
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
    assert policy["enabled"] is not pause_moderation
    assert policy["card_auto"] == {"5": True}
    assert policy["card_suffix"] == {"5": "·群员"}
    assert policy["permissions"] == current["moderation"]["permissions"]
    assert service.config["platform"][0]["ai_routes"] == current["ai_routes"]
    assert current == original


@pytest.mark.asyncio
async def test_transport_only_reauthentication_preserves_managed_policy(monkeypatch):
    """Accept the login-only payload without rewriting managed rules or grants."""
    from astrbot.core.platform.sources.wangshangliao.runtime_policy import (
        BUSINESS_FIELDS,
        PolicySnapshot,
        runtime,
    )

    current = {
        "id": "managed-login-fixture",
        "type": "wangshangliao",
        "enable": False,
        "account_id": "42",
        "login_method": "sms",
    }
    policy = PolicySnapshot(
        "42",
        "fixture-policy-revision",
        json.dumps(
            {
                "enabled_groups": ["34"],
                "reply_groups": {"34": False},
                "moderation": {
                    "enabled": False,
                    "permissions": {"34": ["recall"]},
                },
                "ai_routes": {"groups": {"34": "fixture-model"}},
            }
        ),
        1,
    )
    business_service = SimpleNamespace(active=True, save=AsyncMock())
    monkeypatch.setattr(runtime, "snapshots", {current["id"]: policy})
    monkeypatch.setattr(runtime, "service", business_service)
    tx = SimpleNamespace(
        account_id="42", nickname="Fixture account", saved={"fixture": "new-session"}
    )
    claim, discard = Mock(return_value=tx), AsyncMock()
    monkeypatch.setattr(
        registration,
        "registrations",
        SimpleNamespace(
            commit_lock=asyncio.Lock(),
            claim=claim,
            discard=discard,
            transactions={"fixture-registration": tx},
        ),
    )
    vault = SimpleNamespace(load=lambda: {"fixture": "previous"}, save=Mock())
    monkeypatch.setattr(storage, "Vault", lambda _: vault)
    persist = Mock()
    monkeypatch.setattr(config_service, "save_config", persist)
    platform_manager = SimpleNamespace(
        terminate_platform=AsyncMock(), load_platform=AsyncMock()
    )
    service = SimpleNamespace(
        config={"platform": [copy.deepcopy(current)]},
        core_lifecycle=SimpleNamespace(platform_manager=platform_manager),
    )
    candidate = {**current, "session_ref": "fixture-authenticated-session"}
    await config_service.BotConfigService.save_wangshangliao(
        service, candidate, "fixture-owner", current
    )
    saved = service.config["platform"][0]
    assert saved["account_id"] == "42"
    assert saved["nickname"] == "Fixture account"
    assert saved["enable"] is False
    assert "session_ref" not in saved
    assert not BUSINESS_FIELDS.intersection(saved)
    assert runtime.snapshots[current["id"]] == policy
    business_service.save.assert_not_awaited()
    platform_manager.terminate_platform.assert_awaited_once_with(current["id"])
    platform_manager.load_platform.assert_awaited_once_with(saved)
    discard.assert_awaited_once_with("fixture-registration")
    persist.assert_called_once()
