import asyncio
import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_lajiaoliao_instance_management import adapter_class as adapter_class

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from data.plugins.astrbot_plugin_lajiaoliao.event import (
    LajiaoLiaoEvent,
    chain_to_parts,
    incoming_to_message,
)
from data.plugins.astrbot_plugin_lajiaoliao.protocol import LajiaoLiaoProtocolError


def tool():
    path = Path(__file__).resolve().parents[1] / (
        "services/lajiaoliao-bridge/optimize_reply_config.py"
    )
    spec = importlib.util.spec_from_file_location("reply_config_tool", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def original():
    return {
        "agent_runner": {
            "runner_type": "local", "config": {
                "model": {"provider_id": "old", "fallback_provider_ids": ["old-backup"]},
                "persona": {"persona_id": "existing-persona"},
                "compression": {"max_turns": -1, "trim_turns": 1},
            },
        },
        "provider_settings": {"enable": True, "display_reasoning_text": True},
        "provider_tts_settings": {"enable": True},
        "platform_settings": {
            "unique_session": True, "id_whitelist": ["original-permission"],
            "rate_limit": {"time": 60, "count": 10},
            "segmented_reply": {"enable": True},
        },
        "admins_id": ["real-admin"],
        "plugin_set": ["builtin_commands"],
        "dashboard": {"jwt_secret": "synthetic"},
        "platform": [{"session_ref": "synthetic"}],
        "provider": [{"key": "synthetic"}],
        "provider_sources": [{"key": "synthetic"}],
    }


def test_profile_is_scoped_and_preserves_authority_persona_and_rate_limit():
    module = tool()
    before = original()
    untouched = copy.deepcopy(before)
    result = module.reply_profile(before, "native")
    assert before == untouched
    assert not set(result) & set(module.SYSTEM_KEYS)
    assert result["agent_runner"]["config"]["model"] == {
        "provider_id": "native", "fallback_provider_ids": [],
    }
    assert result["provider_settings"]["provider_pool"] == ["native"]
    assert result["provider_settings"]["display_reasoning_text"] is False
    assert result["provider_settings"]["streaming_response"] is False
    assert result["provider_tts_settings"]["enable"] is False
    assert result["platform_settings"]["reply_with_quote"] is True
    assert result["platform_settings"]["segmented_reply"]["enable"] is False
    assert result["admins_id"] == before["admins_id"]
    assert result["platform_settings"]["rate_limit"] == before["platform_settings"]["rate_limit"]
    assert result["platform_settings"]["id_whitelist"] == ["original-permission"]
    assert result["agent_runner"]["config"]["persona"] == before["agent_runner"]["config"]["persona"]
    assert result["plugin_set"] == ["builtin_commands", "astrbot_plugin_lajiaoliao"]
    assert module.reply_profile(result, "native") == result


def test_external_runner_is_not_silently_replaced():
    before = original()
    before["agent_runner"]["runner_type"] = "dify"
    with pytest.raises(ValueError, match="local_agent_runner"):
        tool().reply_profile(before, "native")


def test_merge_save_explicitly_clears_old_system_credentials():
    module = tool()
    before = original()
    expected = module.reply_profile(before, "native")
    saved = copy.deepcopy(before)
    saved.update(module.profile_update_payload(expected))
    assert not saved["dashboard"]
    assert not saved["platform"]
    assert not saved["provider"]
    assert not saved["provider_sources"]
    assert module.reply_profile(saved, "native") == expected
    assert not set(expected) & set(module.SYSTEM_KEYS)


def test_routes_cover_group_private_without_modifying_other_platforms():
    routes = {
        "telegram::": "tg", "wangshangliao_x::": "wsl",
        "lajiaoliao_one:GroupMessage:group:test": "existing",
    }
    untouched = dict(routes)
    plan = tool().plan_profiles(["lajiaoliao_one", "lajiaoliao_two"], routes)
    assert routes == untouched
    assert plan[0]["reuse"] is True
    assert plan[0]["catchall"] == "lajiaoliao_one::"
    assert plan[1]["source_profile"] == "default"
    assert plan[1]["reuse"] is False
    from astrbot.core.umop_config_router import UmopConfigRouter
    router = UmopConfigRouter(None)
    router.umop_to_conf_id = {**routes, plan[0]["catchall"]: "existing",
                             plan[1]["catchall"]: "independent"}
    assert router.get_conf_id_for_umop("lajiaoliao_one:FriendMessage:private:uid") == "existing"
    assert router.get_conf_id_for_umop("lajiaoliao_one:GroupMessage:group:new") == "existing"
    assert router.get_conf_id_for_umop("lajiaoliao_two:GroupMessage:group:test") == "independent"
    assert router.get_conf_id_for_umop("telegram:FriendMessage:uid") == "tg"
    assert router.get_conf_id_for_umop("wangshangliao_x:GroupMessage:group") == "wsl"


@pytest.mark.parametrize("pattern", ["::", "*::", "lajiaoliao_*::"])
def test_shared_wildcard_route_requires_manual_review(pattern):
    with pytest.raises(ValueError, match="shared_wildcard"):
        tool().plan_profiles(["lajiaoliao_one"], {pattern: "shared"})


def test_shared_profile_is_cloned_and_conflicting_instance_profiles_are_rejected():
    module = tool()
    routes = {"lajiaoliao_one::": "shared", "telegram::": "shared"}
    assert module.plan_profiles(["lajiaoliao_one"], routes)[0]["reuse"] is False
    with pytest.raises(ValueError, match="multiple_instance_profiles"):
        module.plan_profiles(["lajiaoliao_one"], {
            "lajiaoliao_one:GroupMessage:group:a": "first",
            "lajiaoliao_one:GroupMessage:group:b": "second",
        })


def test_compatible_gateway_cannot_be_selected_as_structured_provider():
    module = tool()
    config = {
        "provider": [{"id": "native", "enable": True, "provider_source_id": "source"}],
        "provider_sources": [{"id": "source", "type": module.NATIVE_TYPE}],
    }
    module.validate_provider(config, "native")
    config["provider_sources"][0]["type"] = "openai_chat_completion"
    with pytest.raises(ValueError, match="structured_native"):
        module.validate_provider(config, "native")


def test_reasoning_chain_is_rejected_without_editing_normal_answer_text():
    thought = MessageChain([Plain("synthetic private reasoning")], type="reasoning")
    with pytest.raises(LajiaoLiaoProtocolError) as rejected:
        chain_to_parts(thought)
    assert rejected.value.delivery_status == "rejected"
    text = "An ordinary explanation about thinking must remain unchanged."
    assert chain_to_parts(MessageChain([Plain(text)])) == [{"type": "text", "text": text}]


@pytest.mark.parametrize("kind", ["private", "group"])
def test_event_reasoning_rejection_never_calls_bridge(kind):
    async def run():
        platform = SimpleNamespace(
            send_parts=AsyncMock(), member_ids={},
            meta=lambda: SimpleNamespace(name="lajiaoliao", id="one"),
        )
        message = incoming_to_message({
            "message_id": "original", "kind": kind, "sender_id": "member",
            "target_id": "test" if kind == "group" else "member", "text": "input",
        }, "bot")
        event = LajiaoLiaoEvent(message, platform)
        with pytest.raises(LajiaoLiaoProtocolError, match="reasoning_output_blocked"):
            await event.send(MessageChain([Plain("synthetic thought")], type="reasoning"))
        platform.send_parts.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["private", "group"])
def test_proactive_reasoning_rejection_never_calls_bridge(kind, adapter_class):
    async def run():
        from astrbot.core.platform.message_session import MessageSession
        from astrbot.core.platform.message_type import MessageType

        adapter = object.__new__(adapter_class)
        adapter.member_ids = {}
        adapter.send_parts = AsyncMock()
        session = MessageSession(
            platform_name="one",
            message_type=(
                MessageType.GROUP_MESSAGE if kind == "group"
                else MessageType.FRIEND_MESSAGE
            ),
            session_id=kind + ":target",
        )
        with pytest.raises(LajiaoLiaoProtocolError, match="reasoning_output_blocked"):
            await adapter.send_by_session(
                session, MessageChain([Plain("synthetic thought")], type="reasoning")
            )
        adapter.send_parts.assert_not_awaited()

    asyncio.run(run())
