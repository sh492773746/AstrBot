import copy
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from astrbot.core.config.default import CONFIG_METADATA_3, DEFAULT_CONFIG
from astrbot.dashboard.services.config_service import ConfigProfileService
from data.plugins.astrbot_plugin_telethon_ai.profile_policy import NAME, ProfilePolicy


@pytest.fixture
def subject():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE tenants (platform TEXT)")
    db.execute("INSERT INTO tenants VALUES ('Client')")
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        admins_id=[], disable_builtin_commands=True, plugin_set=[NAME], kb_names=[]
    )
    config["provider_settings"]["enable"] = True
    config["provider_settings"]["proactive_capability"]["add_cron_tools"] = False
    manager = SimpleNamespace(
        default_conf={
            "platform": [
                {"id": "VIP", "type": "telegram"},
                {"id": "Account", "type": "telethon_ai"},
                {"id": "Client", "type": "telegram"},
                {"id": "Other", "type": "telegram"},
            ]
        },
        confs={
            key: copy.deepcopy(config)
            for key in ("vip", "ai", "client", "other", "default")
        },
        ucr=SimpleNamespace(
            umop_to_conf_id={
                "VIP::": "vip",
                "Account::": "ai",
                "Client::": "client",
                "Other::": "other",
            }
        ),
        delete_conf=AsyncMock(return_value=True),
    )
    for key in ("vip", "client"):
        manager.confs[key]["provider_settings"]["enable"] = False
    policy = ProfilePolicy(
        manager, {"control_platform_id": "VIP"}, SimpleNamespace(db=db)
    )
    manager.profile_policy = policy
    lifecycle = SimpleNamespace(
        astrbot_config_mgr=manager,
        reload_pipeline_scheduler=AsyncMock(),
        pipeline_scheduler_mapping={},
    )
    service = ConfigProfileService(lifecycle, runtime={"os": "linux"})
    yield policy, manager, service, lifecycle
    db.close()


@pytest.mark.parametrize(
    "config_id,role",
    [
        ("vip", "controller"),
        ("ai", "telethon"),
        ("client", "customer"),
    ],
)
def test_schema_is_routed_by_identity_not_display_name(subject, config_id, role):
    _, _, service, _ = subject
    result = service.get_profile(config_id)
    assert result["profile_role"]["role"] == role
    assert result["profile_role"]["editable"] == (role == "telethon")
    if role != "telethon":
        assert result["metadata"] == {}


def test_telethon_schema_only_contains_effective_fields(subject):
    policy, _, service, _ = subject
    original = copy.deepcopy(CONFIG_METADATA_3)
    result = service.get_profile("ai")
    metadata = result["metadata"]
    assert set(metadata) == {"ai_group", "platform_group"}
    assert set(metadata["ai_group"]["metadata"]) == {
        "agent_runner",
        "ai",
        "persona",
        "truncate_and_compress",
        "others",
    }
    paths = [
        path
        for section in metadata.values()
        for group in section["metadata"].values()
        for path in group["items"]
    ]
    assert "agent_runner.config.persona.persona_id" in paths
    assert "platform_settings.reply_prefix" in paths
    assert "platform_settings.unique_session" not in paths
    assert "platform_settings.reply_with_quote" not in paths
    assert all(policy.editable_path(path) for path in paths)
    assert CONFIG_METADATA_3 == original


@pytest.mark.parametrize("config_id", ["other", "default"])
def test_unrelated_profiles_keep_original_schema_and_mutability(subject, config_id):
    policy, manager, service, _ = subject
    result = service.get_profile(config_id)
    assert "profile_role" not in result
    assert set(result["metadata"]) == set(CONFIG_METADATA_3)
    config = copy.deepcopy(manager.confs[config_id])
    config["plugin_set"] = ["unrelated"]
    policy.validate(config_id, config)
    policy.validate_delete(config_id)


@pytest.mark.parametrize(
    "path,value",
    [
        (("plugin_set",), ["untrusted"]),
        (("admins_id",), ["123"]),
        (("disable_builtin_commands",), False),
        (("kb_names",), ["private"]),
        (("agent_runner", "runner_type"), "dify"),
        (("provider_settings", "web_search"), True),
        (("provider_tts_settings", "enable"), True),
        (("provider_settings", "proactive_capability", "add_cron_tools"), True),
        (("platform_settings", "segmented_reply", "enable"), True),
        (("dashboard", "enable"), False),
        (("platform_settings", "unique_session"), True),
    ],
)
@pytest.mark.asyncio
async def test_json_and_dashboard_payloads_cannot_change_isolation(
    subject, path, value
):
    _, manager, service, lifecycle = subject
    config = copy.deepcopy(manager.confs["ai"])
    nested = config
    for key in path[:-1]:
        nested = nested[key]
    nested[path[-1]] = value
    with pytest.raises(ValueError, match="不可修改"):
        await service.update_profile("ai", config)
    with pytest.raises(ValueError, match="不可修改"):
        await service.update_profile_from_dashboard_payload(
            {"conf_id": "ai", "config": config}
        )
    lifecycle.reload_pipeline_scheduler.assert_not_awaited()


@pytest.mark.parametrize("config_id", ["vip", "client"])
@pytest.mark.asyncio
async def test_management_bot_profiles_cannot_enable_ai(subject, config_id):
    _, manager, service, _ = subject
    config = copy.deepcopy(manager.confs[config_id])
    config["provider_settings"]["enable"] = True
    with pytest.raises(ValueError, match="不可修改"):
        await service.update_profile(config_id, config)


@pytest.mark.asyncio
async def test_model_persona_context_and_disable_ai_can_be_saved(subject):
    _, manager, service, lifecycle = subject
    config = copy.deepcopy(manager.confs["ai"])
    config["agent_runner"]["config"]["model"]["provider_id"] = "new-model"
    config["agent_runner"]["config"]["persona"]["persona_id"] = "new-persona"
    config["agent_runner"]["config"]["compression"]["max_turns"] = 10
    config["platform_settings"]["reply_prefix"] = "Hello "
    config["provider_settings"]["enable"] = False
    with patch("astrbot.dashboard.services.config_service.save_config") as save:
        await service.update_profile("ai", config)
    save.assert_called_once()
    lifecycle.reload_pipeline_scheduler.assert_awaited_once_with("ai")


@pytest.mark.parametrize("config_id", ["vip", "ai", "client"])
@pytest.mark.asyncio
async def test_bound_profiles_cannot_be_deleted(subject, config_id):
    _, manager, service, _ = subject
    with pytest.raises(ValueError, match="仍绑定"):
        await service.delete_profile(config_id)
    manager.delete_conf.assert_not_awaited()


def test_mixed_routes_fail_closed(subject):
    policy, manager, service, _ = subject
    manager.ucr.umop_to_conf_id["Other:FriendMessage:*"] = "ai"
    assert service.get_profile("ai")["profile_role"]["role"] == "conflict"
    assert service.get_profile("ai")["metadata"] == {}
    with pytest.raises(ValueError, match="共用"):
        policy.validate("ai", copy.deepcopy(manager.confs["ai"]))


def test_companion_controller_keeps_native_ai_config(subject):
    policy, _, service, _ = subject
    policy.config["controller_mode"] = "companion"
    assert "profile_role" not in service.get_profile("vip")


def test_missing_policy_preserves_official_behavior(subject):
    _, manager, service, _ = subject
    del manager.profile_policy
    assert "profile_role" not in service.get_profile("ai")
