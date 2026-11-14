import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [True, False])
@pytest.mark.parametrize(
    "text",
    [
        "帮我创建抽奖",
        "踢出成员并开启自动踢出",
        "排名",
        "确认设置 1234567890abcdef",
    ],
)
async def test_private_messages_stop_before_models_or_management(
    monkeypatch, admin, text
):
    from astrbot.builtin_stars.wangshangliao_moderation import main as plugin_module
    from tests.test_wangshangliao_plain_commands import native_event

    adapter = _adapter()
    adapter.config["ai_routes"]["customer_provider_id"] = "private-customer"
    plugin = object.__new__(Main)
    plugin.context = SimpleNamespace(get_provider_by_id=Mock())
    plugin.commands = SimpleNamespace(run=AsyncMock())
    plugin.open_service = SimpleNamespace(on_event=AsyncMock())
    plugin.ai_runtime = SimpleNamespace(customer=AsyncMock())
    assess = AsyncMock()
    handle = AsyncMock()
    monkeypatch.setattr(plugin_module, "assess", assess)
    monkeypatch.setattr(plugin_module, "handle", handle)
    event = native_event(text, private=True)
    event.platform.config.update(adapter.config)
    event.role = "admin" if admin else "member"
    event.eligible = True
    event.send = AsyncMock()

    await plugin.moderate(event)

    assert event.is_stopped()
    assert event.get_extra("selected_provider") is None
    assert event.get_extra("enable_streaming") is None
    plugin.context.get_provider_by_id.assert_not_called()
    plugin.commands.run.assert_not_awaited()
    plugin.open_service.on_event.assert_not_awaited()
    plugin.ai_runtime.customer.assert_not_awaited()
    assess.assert_not_awaited()
    handle.assert_not_awaited()
    event.send.assert_not_awaited()
    assert not hasattr(plugin, "private_management")


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [True, False])
async def test_private_llm_request_stops_without_resuming_history_or_tools(admin):
    tools = SimpleNamespace(
        tools=[
            SimpleNamespace(name="wsl_private_management"),
            SimpleNamespace(name="other"),
        ]
    )
    original = list(tools.tools)
    history = [
        {
            "role": "assistant",
            "tool_calls": [{"function": {"name": "wsl_private_management"}}],
        }
    ]
    conversation = SimpleNamespace(cid="private-history", history="retained")
    request = SimpleNamespace(
        func_tool=tools,
        tool_calls_result=object(),
        system_prompt="Existing private persona",
        contexts=history,
        conversation=conversation,
    )
    manager = SimpleNamespace(
        new_conversation=AsyncMock(),
        get_conversation=AsyncMock(),
    )
    plugin = object.__new__(Main)
    plugin.context = SimpleNamespace(
        conversation_manager=manager,
        get_provider_by_id=Mock(),
        llm_generate=AsyncMock(),
    )
    event = SimpleNamespace(
        get_platform_name=lambda: "wangshangliao",
        is_private_chat=lambda: True,
        is_admin=lambda: admin,
        stop_event=Mock(),
    )

    await plugin.plain_text_request(event, request)

    event.stop_event.assert_called_once_with()
    assert request.func_tool.tools == []
    assert request.tool_calls_result is None
    assert tools.tools == original
    assert request.system_prompt == "Existing private persona"
    assert request.contexts is history
    assert request.conversation is conversation
    assert conversation.history == "retained"
    manager.new_conversation.assert_not_awaited()
    manager.get_conversation.assert_not_awaited()
    plugin.context.get_provider_by_id.assert_not_called()
    plugin.context.llm_generate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [True, False])
async def test_group_ai_reads_structured_stream_without_publishing_reasoning(
    monkeypatch,
    admin,
):
    from time import time

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
        get_group_id=lambda: "5",
        get_sender_id=lambda: "2",
        is_private_chat=lambda: False,
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
    assert extras.get("selected_provider") == "customer-model"


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
    assert not hasattr(Main, "private_management")
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
@pytest.mark.parametrize("available", [True, False])
@pytest.mark.parametrize("admin", [True, False])
async def test_group_request_removes_all_tools_without_mutating_shared_tools(
    available, admin
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
    request = SimpleNamespace(
        func_tool=tools, system_prompt="Existing persona", tool_calls_result=object()
    )
    extras = {}
    event = SimpleNamespace(
        get_platform_name=lambda: "wangshangliao",
        is_private_chat=lambda: False,
        is_admin=lambda: admin,
        platform=SimpleNamespace(
            config={"id": "bot", "moderation": {"manual_kick_only": True}},
        ),
        get_extra=extras.get,
        set_extra=extras.__setitem__,
        stop_event=Mock(),
    )
    await Main.plain_text_request(None, event, request)
    assert request.func_tool.tools == []
    assert request.tool_calls_result is None
    assert tools.tools == original
    assert READ_ONLY_GUIDANCE in request.system_prompt
    assert request.system_prompt.startswith("Existing persona")
    assert extras["wsl_ai_role"] == "customer"
    assert extras["_api_key_allow_admin_role"] is False
    assert extras["enable_reasoning"] is False
    event.stop_event.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "admin,management_history", [(False, True), (False, False), (True, True)]
)
async def test_group_customer_context_does_not_reuse_old_admin_tools(
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
        is_private_chat=lambda: False,
        is_admin=lambda: admin,
        platform=SimpleNamespace(config={"id": "bot"}),
        get_extra=lambda _: None,
        stop_event=Mock(),
    )
    await Main.plain_text_request(None, event, request)
    assert request.contexts == [history[0]]
    assert len(history) == (2 if management_history else 1)
    assert request.func_tool.tools == []
    event.stop_event.assert_not_called()


@pytest.mark.asyncio
async def test_group_revocation_archives_admin_conversation_instead_of_overwriting_it():
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
        is_private_chat=lambda: False,
        is_admin=lambda: False,
        platform=SimpleNamespace(config={"id": "bot"}),
        get_extra=lambda _: None,
        unified_msg_origin="bot:group:5/wsl-ai/100/customer",
        stop_event=Mock(),
    )
    await plugin.plain_text_request(event, request)
    manager.new_conversation.assert_awaited_once_with(
        event.unified_msg_origin,
        title="旺商聊AI咨询（只读）",
        persona_id="business",
    )
    assert request.conversation is new and request.contexts == []
    assert old.history == "retained"
    assert request.extra_user_content_parts == ["current knowledge retrieval"]
    assert request.func_tool.tools == []
    event.stop_event.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "old_origin",
    ["bot:group:6/wsl-ai/100/customer", "bot:group:5/wsl-ai/200/customer"],
)
async def test_group_conversation_from_another_scope_stops_before_reuse(old_origin):
    manager = SimpleNamespace(
        new_conversation=AsyncMock(),
        get_conversation=AsyncMock(),
    )
    plugin = object.__new__(Main)
    plugin.context = SimpleNamespace(conversation_manager=manager)
    old = SimpleNamespace(user_id=old_origin, history="retained")
    history = [{"role": "user", "content": "another scope"}]
    request = SimpleNamespace(
        func_tool=None, system_prompt="", conversation=old, contexts=history
    )
    event = SimpleNamespace(
        get_platform_name=lambda: "wangshangliao",
        is_private_chat=lambda: False,
        get_extra=lambda _: None,
        unified_msg_origin="bot:group:5/wsl-ai/100/customer",
        stop_event=Mock(),
        plain_result=lambda text: text,
        send=AsyncMock(),
    )

    await plugin.plain_text_request(event, request)

    event.stop_event.assert_called_once_with()
    event.send.assert_awaited_once_with("会话范围已变化，请在当前群重新提问。")
    manager.new_conversation.assert_not_awaited()
    manager.get_conversation.assert_not_awaited()
    assert request.conversation is old
    assert request.contexts is history
    assert old.history == "retained"
    assert request.func_tool.tools == []


@pytest.mark.asyncio
async def test_group_revocation_blocks_stale_privileged_final_output():
    extras = {"wsl_answer_nonce": "0123456789abcdef", "wsl_ai_role": "admin"}
    event = SimpleNamespace(
        get_platform_name=lambda: "wangshangliao",
        is_private_chat=lambda: False,
        is_ai_admin=lambda _: False,
        get_extra=extras.get,
        set_extra=extras.__setitem__,
    )
    plugin = object.__new__(Main)
    plugin.context = SimpleNamespace()
    response = SimpleNamespace(
        completion_text='WSL_FINAL_0123456789abcdef\n{"text":"已执行旧管理操作"}',
        reasoning_content="internal reasoning",
    )
    message = SimpleNamespace(role="assistant", content=["old privileged output"])

    await plugin.final_reply(event, SimpleNamespace(messages=[message]), response)

    assert extras["wsl_final_reply"] == "权限已变化，请重新发送请求。"
    assert response.result_chain.chain[0].text == extras["wsl_final_reply"]
    assert message.content[0].text == extras["wsl_final_reply"]
    assert response.reasoning_content is None
    assert extras["enable_reasoning"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "private,admin", [(True, False), (True, True), (False, False), (False, True)]
)
async def test_unprivileged_confirmation_is_handled_without_ai(private, admin):
    main = object.__new__(Main)
    main.ai_runtime = SimpleNamespace(customer=AsyncMock())
    main.commands = SimpleNamespace(run=AsyncMock())
    from time import time

    extras = {
        "wangshangliao_payload": {"sender": "2", "created_at": time() * 1000},
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
    event.send.assert_not_awaited()
    assert extras.get("wsl_permission_denial") is None
    main.ai_runtime.customer.assert_not_awaited()
    main.commands.run.assert_not_awaited()
