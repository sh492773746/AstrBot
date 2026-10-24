import json
from types import SimpleNamespace

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.ai_config import (
    AdminConfigDrafts,
)
from astrbot.builtin_stars.wangshangliao_moderation.main import Main


def _adapter():
    return SimpleNamespace(
        config={
            "id": "bot",
            "account_id": "100",
            "enabled_groups": ["5"],
            "moderation": {
                "enabled": True,
                "automation_enabled": True,
                "recall_enabled": True,
                "semantic": {
                    "enabled": True,
                    "provider_id": "",
                    "timeout_seconds": 12,
                    "context_limit": 20,
                },
                "permissions": {"5": ["mute", "recall"]},
                "auto_kick": {},
                "cooldown_seconds": 60,
                "mute_keywords": [],
                "kick_keywords": [],
            },
            "ai_routes": {
                "admin_provider_id": "admin-model",
                "groups": {"5": "customer-model"},
            },
        },
        account="100",
        groups={"5": "Group"},
        group_names={"5": "Group"},
    )


def test_model_route_selection_is_scoped_by_message_route():
    adapter = _adapter()
    main = object.__new__(Main)
    main.context = SimpleNamespace(
        get_provider_by_id=lambda provider_id: SimpleNamespace(id=provider_id)
    )
    selected = {}
    event = SimpleNamespace(
        platform=adapter,
        get_group_id=lambda: "5",
        set_extra=lambda key, value: selected.__setitem__(key, value),
    )

    main._select_route_provider(event, "customer_provider_id")
    assert selected["selected_provider"] == "customer-model"
    selected.clear()
    main._select_route_provider(event, "admin_provider_id")
    assert selected["selected_provider"] == "admin-model"


def test_missing_model_route_leaves_session_default_untouched():
    adapter = _adapter()
    adapter.config["ai_routes"]["groups"]["5"] = ""
    main = object.__new__(Main)
    main.context = SimpleNamespace(get_provider_by_id=lambda _: None)
    selected = {}
    event = SimpleNamespace(
        platform=adapter,
        get_group_id=lambda: "5",
        set_extra=lambda key, value: selected.__setitem__(key, value),
    )
    main._select_route_provider(event, "customer_provider_id")
    assert selected == {}


def test_private_customer_uses_customer_model_not_administrator_model():
    adapter = _adapter()
    adapter.config["ai_routes"]["customer_provider_id"] = "private-customer"
    main = object.__new__(Main)
    main.context = SimpleNamespace(get_provider_by_id=lambda _: object())
    selected = {}
    event = SimpleNamespace(
        platform=adapter,
        get_group_id=lambda: "",
        set_extra=selected.__setitem__,
    )
    main._select_route_provider(event, "customer_provider_id")
    assert selected["selected_provider"] == "private-customer"


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [True, False])
@pytest.mark.parametrize("admin", [True, False])
async def test_native_ai_reads_structured_stream_without_publishing_reasoning(
    monkeypatch,
    private,
    admin,
):
    from time import time
    from unittest.mock import AsyncMock

    from astrbot.builtin_stars.wangshangliao_moderation import content_rules
    from astrbot.builtin_stars.wangshangliao_moderation import main as plugin_module
    from astrbot.core.platform.sources.wangshangliao import event as event_module

    monkeypatch.setattr(event_module, "is_managed_account", lambda _: False)
    monkeypatch.setattr(content_rules, "flood_category", lambda *args: "")
    monkeypatch.setattr(
        plugin_module,
        "assess",
        AsyncMock(return_value={"decision": "allow"}),
    )
    plugin = object.__new__(Main)
    plugin.context = SimpleNamespace(get_provider_by_id=lambda _: object())
    extras = {
        "wangshangliao_payload": {"sender": "2", "created_at": time() * 1000},
    }
    event = SimpleNamespace(
        platform=_adapter(),
        message_obj=SimpleNamespace(message_id="route-only-test"),
        get_platform_name=lambda: "wangshangliao",
        get_group_id=lambda: "" if private else "5",
        get_sender_id=lambda: "2",
        is_private_chat=lambda: private,
        is_admin=lambda: admin,
        get_extra=extras.get,
        set_extra=extras.__setitem__,
        message_str="帮我创建抽奖",
        is_at_or_wake_command=True,
    )
    await plugin.moderate(event)
    assert extras.get("enable_streaming") is True
    assert extras.get("buffer_intermediate_messages") is True
    assert extras.get("enable_reasoning") is False
    assert extras.get("selected_provider") == (
        "admin-model" if private and admin else None if private else "customer-model"
    )


@pytest.mark.asyncio
async def test_retired_admin_drafts_cannot_create_or_confirm_settings(
    tmp_path, monkeypatch
):
    from astrbot.builtin_stars.wangshangliao_moderation import dashboard

    monkeypatch.setattr(dashboard, "instance_dir", lambda _: tmp_path)
    adapter = _adapter()
    context = SimpleNamespace(
        get_all_providers=lambda: [],
        platform_manager=SimpleNamespace(),
    )
    service = AdminConfigDrafts(context, None, None)

    def event(uid="2", session="bot:private:2", text="请把冷却改成90秒"):
        return SimpleNamespace(
            platform=adapter,
            account="100",
            get_sender_id=lambda: uid,
            unified_msg_origin=session,
            message_str=text,
            is_admin=lambda: True,
        )

    proposal = json.dumps(
        {
            "target": "moderation",
            "group": "Group",
            "changes": {"cooldown_seconds": 90},
        }
    )
    result = json.loads(await service.preview(event(), proposal))
    assert result == {"status": "rejected", "reason": "ai_management_retired"}
    for caller in (event(), event(uid="3"), event(session="another")):
        assert json.loads(await service.confirm(caller, "old-token")) == result
    assert service.drafts == {}
    assert adapter.config["moderation"]["cooldown_seconds"] == 60


@pytest.mark.asyncio
@pytest.mark.parametrize("recall", [True, False])
async def test_retired_progressive_draft_never_changes_rules(
    tmp_path, monkeypatch, recall
):
    from astrbot.builtin_stars.wangshangliao_moderation import dashboard

    monkeypatch.setattr(dashboard, "instance_dir", lambda _: tmp_path)
    adapter = _adapter()
    service = AdminConfigDrafts(
        SimpleNamespace(get_all_providers=lambda: []), None, None
    )
    event = SimpleNamespace(
        platform=adapter,
        get_sender_id=lambda: "2",
        unified_msg_origin="bot:private:2",
        message_str="开启递进禁言和撤回",
        is_admin=lambda: True,
    )
    proposal = json.dumps(
        {
            "target": "moderation",
            "group": "5",
            "changes": {"progressive_mute": True, "recall_enabled": recall},
        }
    )
    result = json.loads(await service.preview(event, proposal))
    assert result == {"status": "rejected", "reason": "ai_management_retired"}
    assert "progressive_mute" not in adapter.config["moderation"]
    assert not service.drafts


@pytest.mark.asyncio
async def test_config_draft_rejects_authorization_and_unknown_fields(
    tmp_path, monkeypatch
):
    from astrbot.builtin_stars.wangshangliao_moderation import dashboard

    monkeypatch.setattr(dashboard, "instance_dir", lambda _: tmp_path)
    adapter = _adapter()
    service = AdminConfigDrafts(
        SimpleNamespace(get_all_providers=lambda: []), None, None
    )
    event = SimpleNamespace(
        platform=adapter,
        get_sender_id=lambda: "2",
        unified_msg_origin="bot:private:2",
        message_str="改权限",
    )
    proposal = json.dumps(
        {
            "target": "moderation",
            "group": "5",
            "changes": {"permissions": {"5": ["kick"]}},
        }
    )
    denied = json.loads(await service.preview(event, proposal))
    assert denied["status"] == "rejected"
    assert not service.drafts

    bad_group = json.dumps(
        {
            "target": "moderation",
            "group": "unknown",
            "changes": {"cooldown_seconds": 90},
        }
    )
    assert json.loads(await service.preview(event, bad_group))["status"] == "rejected"


@pytest.mark.asyncio
async def test_manual_only_rejects_ai_kick_and_reenabling_automatic_kick(
    tmp_path, monkeypatch
):
    from astrbot.builtin_stars.wangshangliao_moderation import dashboard
    from astrbot.core.platform.sources.wangshangliao.policy import validate_policy

    monkeypatch.setattr(dashboard, "instance_dir", lambda _: tmp_path)
    adapter = _adapter()
    adapter.config["moderation"]["manual_kick_only"] = True
    event = SimpleNamespace(
        platform=adapter,
        get_platform_name=lambda: "wangshangliao",
        is_private_chat=lambda: True,
        is_admin=lambda: True,
        get_extra=lambda _: None,
        get_sender_id=lambda: "2",
        unified_msg_origin="bot:private:2",
        message_str="踢出成员并开启自动踢出",
    )
    main = object.__new__(Main)
    main.context = SimpleNamespace(get_config=lambda _: {"admins_id": ["2"]})
    result = json.loads(await main.private_management(event, "kick", "1"))
    assert result == {"status": "rejected", "reason": "ai_management_retired"}
    service = AdminConfigDrafts(
        SimpleNamespace(get_all_providers=lambda: []), None, None
    )
    for changes in ({"auto_kick": True}, {"manual_kick_only": False}):
        proposal = json.dumps(
            {"target": "moderation", "group": "5", "changes": changes}
        )
        assert (
            json.loads(await service.preview(event, proposal))["status"] == "rejected"
        )
    assert not service.drafts
    validate_policy(adapter.config["moderation"])
    with pytest.raises(ValueError, match="moderation_config"):
        validate_policy({"manual_kick_only": "yes"})


@pytest.mark.asyncio
async def test_management_tool_is_never_available_to_non_admin_private_chat():
    main = object.__new__(Main)
    event = SimpleNamespace(
        get_platform_name=lambda: "wangshangliao",
        is_private_chat=lambda: True,
        is_admin=lambda: False,
    )
    result = json.loads(await main.private_management(event, "config_preview", "{}"))
    assert result == {"status": "rejected", "reason": "ai_management_retired"}


@pytest.mark.asyncio
async def test_management_tool_is_removed_from_non_admin_ai_request():
    main = object.__new__(Main)

    class Tools:
        def __init__(self):
            self.tools = ["wsl_private_management", "other"]

        def get_tool(self, name):
            return name if name in self.tools else None

        def remove_tool(self, name):
            self.tools.remove(name)

    tools = Tools()
    request = SimpleNamespace(func_tool=tools, system_prompt="")
    event = SimpleNamespace(
        get_platform_name=lambda: "wangshangliao",
        is_private_chat=lambda: True,
        is_admin=lambda: False,
        platform=SimpleNamespace(config={"id": "bot"}),
        get_extra=lambda _: False,
    )
    await main.plain_text_request(event, request)
    assert "wsl_private_management" not in request.func_tool.tools


@pytest.mark.asyncio
@pytest.mark.parametrize("available", [True, False])
async def test_admin_request_only_exposes_management_tool_without_mutating_shared_tools(
    available,
):
    from astrbot.builtin_stars.wangshangliao_moderation.conversation_guidance import (
        READ_ONLY_GUIDANCE,
    )

    class Tools:
        def __init__(self):
            names = ["future_task", "send_message_to_user"]
            if available:
                names.append("wsl_private_management")
            self.tools = [SimpleNamespace(name=name) for name in names]

        def get_tool(self, name):
            return next((tool for tool in self.tools if tool.name == name), None)

    tools = Tools()
    original = list(tools.tools)
    request = SimpleNamespace(func_tool=tools, system_prompt="Existing persona")
    event = SimpleNamespace(
        get_platform_name=lambda: "wangshangliao",
        is_private_chat=lambda: True,
        is_admin=lambda: True,
        platform=SimpleNamespace(
            config={"id": "bot", "moderation": {"manual_kick_only": True}},
        ),
        get_extra=lambda _: None,
    )
    await Main.plain_text_request(None, event, request)
    assert request.func_tool.tools == []
    assert tools.tools == original
    assert READ_ONLY_GUIDANCE in request.system_prompt
    assert request.system_prompt.startswith("Existing persona")
    assert len(request.system_prompt) < 3000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "admin,management_history", [(False, True), (False, False), (True, True)]
)
async def test_customer_context_does_not_reuse_old_admin_tools(
    admin, management_history
):
    history = [{"role": "user", "content": "previous"}]
    if management_history:
        history.append(
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "old",
                        "type": "function",
                        "function": {
                            "name": "wsl_private_management",
                            "arguments": "{}",
                        },
                    }
                ],
            }
        )
    request = SimpleNamespace(func_tool=None, system_prompt="", contexts=history)
    event = SimpleNamespace(
        get_platform_name=lambda: "wangshangliao",
        is_private_chat=lambda: True,
        is_admin=lambda: admin,
        platform=SimpleNamespace(config={"id": "bot"}),
        get_extra=lambda _: None,
    )
    await Main.plain_text_request(None, event, request)
    assert request.contexts == [history[0]]
    assert len(history) == (2 if management_history else 1)


@pytest.mark.asyncio
async def test_revocation_archives_admin_conversation_instead_of_overwriting_it():
    from unittest.mock import AsyncMock

    old = SimpleNamespace(
        cid="admin-history", persona_id="business", history="retained"
    )
    new = SimpleNamespace(cid="customer-history", persona_id="business", history="[]")
    manager = SimpleNamespace(
        new_conversation=AsyncMock(return_value=new.cid),
        get_conversation=AsyncMock(return_value=new),
    )
    plugin = object.__new__(Main)
    plugin.context = SimpleNamespace(conversation_manager=manager)
    request = SimpleNamespace(
        func_tool=None,
        system_prompt="",
        conversation=old,
        contexts=[
            {
                "role": "assistant",
                "tool_calls": [{"function": {"name": "wsl_private_management"}}],
            }
        ],
        extra_user_content_parts=["current knowledge retrieval"],
    )
    event = SimpleNamespace(
        get_platform_name=lambda: "wangshangliao",
        is_private_chat=lambda: True,
        is_admin=lambda: False,
        platform=SimpleNamespace(config={"id": "bot"}),
        get_extra=lambda _: None,
        unified_msg_origin="private-session",
    )
    await plugin.plain_text_request(event, request)
    manager.new_conversation.assert_awaited_once_with(
        "private-session", title="旺商聊AI咨询（只读）", persona_id="business"
    )
    assert request.conversation is new and request.contexts == []
    assert old.history == "retained"
    assert request.extra_user_content_parts == ["current knowledge retrieval"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "private,admin", [(True, False), (False, False), (False, True)]
)
async def test_unprivileged_confirmation_is_handled_without_ai(private, admin):
    from unittest.mock import AsyncMock

    main = object.__new__(Main)
    from time import time

    extras = {
        "wangshangliao_payload": {"sender": "2", "created_at": time() * 1000},
        "wsl_test_scope": "private_ai",
    }
    stopped = []
    event = SimpleNamespace(
        get_platform_name=lambda: "wangshangliao",
        get_extra=lambda k: extras.get(k),
        set_extra=lambda k, v: extras.__setitem__(k, v),
        message_str="确认设置 1234567890abcdef",
        is_private_chat=lambda: private,
        is_admin=lambda: admin,
        plain_result=lambda text: text,
        send=AsyncMock(),
        stop_event=lambda: stopped.append(True),
    )
    await main.moderate(event)
    assert not hasattr(main, "ai_config")
    assert stopped == [True]
    if private:
        event.send.assert_awaited_once_with("无权限")
        assert extras["wsl_permission_denial"] is True
    else:
        event.send.assert_not_awaited()
