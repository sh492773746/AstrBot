"""Offline group configuration regression after retiring private messaging."""

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot.core.platform.sources.wangshangliao import (
    dashboard,
    registration,
    storage,
)
from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
from astrbot.core.platform.sources.wangshangliao.runtime_policy import (
    BUSINESS_FIELDS,
    PolicyRuntime,
    PolicySnapshot,
    runtime,
)
from astrbot.dashboard.services.platform_service import PlatformServiceError


@pytest.fixture
def configuration(monkeypatch):
    """Replace persistence and lifecycle operations with offline mocks."""
    current = {
        "id": "group-config",
        "type": "wangshangliao",
        "account_id": "1",
        "enabled_groups": ["5"],
        "reply_groups": {"5": True},
        "ai_routes": {"groups": {"5": "group-model"}},
        "proactive_send": {"enabled": True, "targets": ["5"]},
        "developer_test": {
            "sender_instance": "sender",
            "groups": ["5"],
            "scopes": ["group_commands", "group_ai", "group_rules"],
            "keywords": ["WSL_TEST_GROUP"],
            "seconds": 60,
            "budget": 2,
        },
        "moderation": {
            "enabled": True,
            "permissions": {"5": ["mute", "recall", "rename"]},
        },
    }
    monkeypatch.setattr(runtime, "snapshots", {})
    monkeypatch.setattr(runtime, "service", None)
    monkeypatch.setattr(runtime, "open_service", None)
    monkeypatch.setattr(
        registration, "registrations", SimpleNamespace(commit_lock=asyncio.Lock())
    )
    monkeypatch.setattr(
        storage, "Vault", Mock(return_value=SimpleNamespace(load=Mock()))
    )
    manager = SimpleNamespace(
        terminate_platform=AsyncMock(),
        load_platform=AsyncMock(),
    )
    service = SimpleNamespace(
        config={"platform": [copy.deepcopy(current)]},
        core_lifecycle=SimpleNamespace(platform_manager=manager),
    )
    return SimpleNamespace(
        current=current, service=service, manager=manager, persist=Mock()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize(
    "retired,error",
    [
        ({"reply_private": True}, "platform_config_fields"),
        ({"reply_private": False}, "platform_config_fields"),
        ({"ai_routes": {"admin_provider_id": ""}}, "ai_routes_invalid"),
        ({"ai_routes": {"admin_provider_id": "old-model"}}, "ai_routes_invalid"),
        ({"ai_routes": {"customer_provider_id": "old-model"}}, "ai_routes_invalid"),
        (
            {"developer_test": {"scopes": ["private_commands"]}},
            "developer_test_invalid",
        ),
        (
            {"developer_test": {"scopes": ["group_ai", "private_ai"]}},
            "developer_test_invalid",
        ),
        (
            {"proactive_send": {"enabled": True, "targets": ["5", "private/2"]}},
            "proactive_targets_invalid",
        ),
    ],
)
async def test_retired_configuration_cannot_enter_managed_or_legacy_save(
    configuration, monkeypatch, managed, retired, error
):
    fixture = configuration
    candidate = {**fixture.current, **retired}
    business = SimpleNamespace(active=True, save=AsyncMock())
    if managed:
        monkeypatch.setattr(runtime, "service", business)
        runtime.publish(
            fixture.current["id"],
            PolicySnapshot(
                "1",
                "revision",
                json.dumps(
                    {
                        key: fixture.current[key]
                        for key in BUSINESS_FIELDS
                        if key in fixture.current
                    }
                ),
                1,
            ),
        )
        candidate["_business_revision"] = "revision"
    original = copy.deepcopy(fixture.service.config)
    with pytest.raises(ValueError, match=error):
        await dashboard.save_configuration(
            fixture.service,
            candidate,
            "owner",
            fixture.current,
            persist=fixture.persist,
        )
    assert fixture.service.config == original
    fixture.persist.assert_not_called()
    fixture.manager.terminate_platform.assert_not_awaited()
    fixture.manager.load_platform.assert_not_awaited()
    business.save.assert_not_awaited()
    storage.Vault.assert_not_called()


@pytest.mark.asyncio
async def test_group_save_preserves_group_settings_without_inheriting_private_route(
    configuration,
):
    fixture = configuration
    fixture.current["reply_private"] = True
    fixture.current["ai_routes"]["admin_provider_id"] = "old-private-model"
    original = copy.deepcopy(fixture.current)
    candidate = copy.deepcopy(fixture.current)
    candidate.pop("reply_private")
    candidate.pop("ai_routes")
    candidate["nickname"] = "group bot"
    await dashboard.save_configuration(
        fixture.service,
        candidate,
        "owner",
        fixture.current,
        persist=fixture.persist,
    )
    saved = fixture.service.config["platform"][0]
    expected = copy.deepcopy(original)
    expected.pop("reply_private")
    expected["ai_routes"].pop("admin_provider_id")
    expected["nickname"] = "group bot"
    assert saved == expected
    assert fixture.current == original
    fixture.persist.assert_called_once()
    fixture.manager.terminate_platform.assert_awaited_once_with(original["id"])
    fixture.manager.load_platform.assert_awaited_once_with(expected)


@pytest.mark.asyncio
async def test_managed_group_roundtrip_does_not_restart_or_rewrite_snapshot(
    configuration, monkeypatch
):
    fixture = configuration
    values = {
        key: value for key, value in fixture.current.items() if key in BUSINESS_FIELDS
    }
    values["reply_private"] = True
    values["ai_routes"]["admin_provider_id"] = "old-private-model"
    values["developer_test"]["scopes"].append("private_ai")
    values["proactive_send"]["targets"].append("private/2")
    snapshot = PolicySnapshot("1", "revision", json.dumps(values), 1)
    current = {
        key: value
        for key, value in fixture.current.items()
        if key not in BUSINESS_FIELDS
    }
    fixture.service.config["platform"] = [copy.deepcopy(current)]
    monkeypatch.setattr(
        runtime, "service", SimpleNamespace(active=True, save=AsyncMock())
    )
    runtime.publish(current["id"], snapshot)
    candidate = runtime.view(current, public=True)
    await dashboard.save_configuration(
        fixture.service, candidate, "owner", current, persist=fixture.persist
    )
    assert runtime.snapshots[current["id"]] is snapshot
    assert json.loads(snapshot.payload) == values
    assert fixture.service.config["platform"] == [current]
    runtime.service.save.assert_not_awaited()
    fixture.persist.assert_not_called()
    fixture.manager.terminate_platform.assert_not_awaited()
    fixture.manager.load_platform.assert_not_awaited()


@pytest.mark.parametrize("source", ["transport", "snapshot", "overlay"])
@pytest.mark.parametrize("public", [False, True])
def test_runtime_exposes_only_group_settings_without_modifying_saved_values(
    configuration, source, public
):
    values = copy.deepcopy(configuration.current)
    values["reply_private"] = True
    values["ai_routes"].update(
        admin_provider_id="old-admin", customer_provider_id="old-customer"
    )
    values["developer_test"]["scopes"].extend(["private_commands", "private_ai"])
    values["proactive_send"]["targets"].append("private/2")
    original = copy.deepcopy(values)
    policy = PolicyRuntime()
    policy.service = SimpleNamespace(active=True)
    if source != "transport":
        policy.publish(
            values["id"], PolicySnapshot("1", "revision", json.dumps(values), 1)
        )
    if source == "overlay":
        policy.open_service = SimpleNamespace(
            integrated=True,
            active=True,
            settings={"paused": False},
            policy_values=lambda *_: copy.deepcopy(original),
        )
    view = policy.view(values, public=public)
    assert "reply_private" not in view
    assert view["enabled_groups"] == ["5"]
    if public or source != "transport":
        assert view["ai_routes"] == {"groups": {"5": "group-model"}}
        assert view["reply_groups"] == {"5": True}
        assert view["proactive_send"] == {"enabled": True, "targets": ["5"]}
        assert view["developer_test"]["scopes"] == [
            "group_commands",
            "group_ai",
            "group_rules",
        ]
        assert view["moderation"] == configuration.current["moderation"]
    else:
        assert view["reply_groups"] == {"5": False}
        assert view["proactive_send"]["enabled"] is False
    assert values == original
    if source != "transport":
        snapshot = policy.snapshot(values)
        assert json.loads(snapshot.payload) == original
        assert "reply_private" not in snapshot.values()
        assert snapshot.values()["ai_routes"] == {"groups": {"5": "group-model"}}


def test_adapter_retains_no_private_configuration_fields(configuration):
    values = copy.deepcopy(configuration.current)
    values["reply_private"] = True
    values["ai_routes"]["admin_provider_id"] = "old-private-model"
    values["developer_test"]["scopes"].append("private_ai")
    values["proactive_send"]["targets"].append("private/2")
    original = copy.deepcopy(values)
    adapter = WangshangliaoAdapter(values, {}, asyncio.Queue())
    assert adapter._transport_config == configuration.current
    assert not hasattr(adapter, "test_command_window")
    assert values == original


@pytest.mark.asyncio
@pytest.mark.parametrize("integrated", [False, True])
async def test_open_policy_drops_inherited_private_flag_without_republishing_it(
    configuration, tmp_path, integrated
):
    from tests.test_wangshangliao_integrated_service import service

    value = service(tmp_path)
    value.integrated = integrated
    value.store.discover("1", {"5": {"name": "group", "team": "1005"}})
    value.store.import_group(
        "1", "5", {"advertising": True, "flood": True, "keywords": []}, [], "system"
    )
    tenant = value.store.tenant("1", "5")
    value.store.native_role("1", "5", tenant["epoch"], "GROUP_ROLE_ADMIN")
    adapter = SimpleNamespace(
        account="1",
        dispatch_wakeup=asyncio.Event(),
        meta=lambda: SimpleNamespace(name="wangshangliao", id="group-config"),
    )
    value.context.platform_manager.platform_insts.append(adapter)
    value.delegated_mutes.refresh = AsyncMock()
    value.group_ai.refresh = AsyncMock()
    value.raid_defense.refresh = AsyncMock()
    base = copy.deepcopy(configuration.current)
    base["reply_private"] = True
    original = copy.deepcopy(base)
    previous = PolicySnapshot("1", "legacy", json.dumps(base), 1)
    runtime.publish("group-config", previous)

    await value.refresh()
    policy = value.policy_values("1", base)
    assert "reply_private" not in policy
    assert base == original
    assert policy["enabled_groups"] == ["5"]
    assert policy["ai_routes"] == base["ai_routes"]
    assert policy["reply_groups"] == {"5": True}
    assert policy["moderation"]["permissions"]["5"] == ["mute", "recall", "rename"]
    assert adapter.dispatch_wakeup.is_set()
    published = runtime.snapshots["group-config"]
    if integrated:
        assert published is previous
        assert json.loads(published.payload) == original
    else:
        assert published is not previous
        assert "reply_private" not in json.loads(published.payload)
        assert published.values()["enabled_groups"] == ["5"]
        assert published.values()["reply_groups"] == {"5": True}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope",
    ["private_commands", "private_ai", "group_commands", "group_ai", "group_rules"],
)
async def test_core_window_supports_groups_without_private_sender_state(
    configuration, monkeypatch, scope
):
    monkeypatch.setattr(runtime, "service", SimpleNamespace(active=True))
    receiver = SimpleNamespace(
        account="1",
        nim_account="9001",
        enabled_groups={"5"},
        config={"_business_active": True, "_business_epoch": 1},
        connection_state="online",
        stopping=asyncio.Event(),
        test_window=None,
        meta=lambda: SimpleNamespace(name="wangshangliao", id="receiver"),
    )
    sender = SimpleNamespace(
        account="2",
        nim_account="9002",
        config={"id": "sender"},
        connection_state="online",
        stopping=asyncio.Event(),
        meta=lambda: SimpleNamespace(name="wangshangliao", id="sender"),
    )
    manager = SimpleNamespace(platform_insts=[receiver, sender])
    payload = {
        "action": "test_open",
        "instance_id": "receiver",
        "sender_instance": "sender",
        "groups": ["5"],
        "scopes": [scope],
        "keywords": ["WSL_TEST_GROUP"],
        "seconds": 60,
        "budget": 2,
    }
    if scope.startswith("private_"):
        with pytest.raises(
            PlatformServiceError, match="test_window_arguments"
        ) as caught:
            await dashboard.handle_registration(manager, payload, "owner")
        assert caught.value.status_code == 400
        assert receiver.test_window is None
    else:
        result = await dashboard.handle_registration(manager, payload, "owner")
        assert result["window"]["active"] is True
        assert receiver.test_window.scopes == [scope]
        assert receiver.test_window.groups == ["5"]
        result = await dashboard.handle_registration(
            manager, {**payload, "action": "test_close"}, "owner"
        )
        assert result["window"]["active"] is False
        assert receiver.test_window is None
    assert not hasattr(sender, "test_command_window")
