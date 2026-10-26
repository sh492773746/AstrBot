import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astrbot.core.platform.message_policy import (
    MessagePolicyDenied,
    allows_message,
    migrate_legacy_policy,
    require_message,
    validate_message_policy,
)
from astrbot.core.platform.platform import Platform


def test_unconfigured_platform_unchanged():
    assert allows_message({}, "private", "u")
    assert allows_message({}, "group", "g")


@pytest.mark.parametrize("mode", ["all", "deny_all", None])
def test_lajiaoliao_cannot_bypass_fixed_allowlist(mode):
    config = {"type": "lajiaoliao", "message_policy": {
        "group_mode": mode, "group_ids": ["g"], "private_enabled": True,
    }}
    assert not allows_message(config, "group", "g")
    with pytest.raises(MessagePolicyDenied):
        require_message(config, "group", "g")
    assert allows_message(config, "private", "u")


def test_lajiaoliao_groups_are_independent_and_default_closed():
    assert not allows_message({"type": "lajiaoliao"}, "private", "u")
    config = {"type": "lajiaoliao", "message_policy": {
        "group_mode": "allowlist", "group_ids": ["a", "b"],
        "private_enabled": False,
    }}
    assert allows_message(config, "group", "a")
    config["message_policy"]["group_ids"].remove("a")
    assert not allows_message(config, "group", "a")
    assert allows_message(config, "group", "b")
    assert not allows_message(config, "group", "unknown")
    assert not allows_message(config, "private", "u")


def test_fixed_policy_save_validation():
    config = {"type": "lajiaoliao", "message_policy": {
        "group_mode": "deny_all", "group_ids": ["stale"], "private_enabled": True,
    }}
    validate_message_policy(config)
    assert config["message_policy"] == {"group_mode": "allowlist", "group_ids": [], "private_enabled": True}
    config["message_policy"]["group_mode"] = "all"
    with pytest.raises(ValueError, match="allowlist_required"):
        validate_message_policy(config)
    other = {"type": "other", "message_policy": {"group_mode": "all"}}
    validate_message_policy(other)
    assert other["message_policy"]["group_mode"] == "all"


def test_bot_api_rejects_broad_policy_before_persistence():
    from astrbot.dashboard.services.config_service import BotConfigService

    async def run():
        lifecycle = SimpleNamespace(astrbot_config={"platform": []})
        service = BotConfigService(lifecycle)
        config = {"id": "synthetic", "type": "lajiaoliao",
                  "message_policy": {"group_mode": "all"}}
        with pytest.raises(ValueError, match="allowlist_required"):
            await service.create_bot(config)
        with pytest.raises(ValueError, match="allowlist_required"):
            await service.update_bot("synthetic", config)
        assert lifecycle.astrbot_config == {"platform": []}
    asyncio.run(run())


@pytest.mark.parametrize("groups", [[], ["g"]])
@pytest.mark.parametrize("private", [False, True])
def test_legacy_conversion(groups, private):
    policy = migrate_legacy_policy({
        "enabled_groups": groups, "reply_private": private,
    })
    config = {"message_policy": policy}
    assert allows_message(config, "private", "u") == private
    assert allows_message(config, "group", "g") == ("g" in groups)
    assert not allows_message(config, "group", "other")


def test_group_override_and_dynamic_outbound_check():
    policy = migrate_legacy_policy({
        "enabled_groups": ["a", "b"], "reply_groups": {"b": False},
    })
    config = {"message_policy": policy}
    require_message(config, "group", "a")
    with pytest.raises(MessagePolicyDenied):
        require_message(config, "group", "b")
    policy["group_mode"] = "deny_all"
    with pytest.raises(MessagePolicyDenied):
        require_message(config, "group", "a")


@pytest.mark.parametrize("policy", [{}, False, "all", {"group_mode": "allowlist", "group_ids": "g"}])
def test_invalid_policy_fails_closed(policy):
    assert not allows_message({"message_policy": policy}, "group", "g")


def test_ingress_denial_completes_without_enqueuing():
    async def run():
        queue = asyncio.Queue()
        platform = SimpleNamespace(
            config={"message_policy": {"group_mode": "deny_all"}},
            _event_queue=queue,
        )
        event = Mock()
        event.get_group_id.return_value = "g"
        event.processing_completion = asyncio.get_running_loop().create_future()
        Platform.commit_event(platform, event)
        assert queue.empty()
        assert event.processing_completion.result() is None
        event.stop_event.assert_called_once()
    asyncio.run(run())
