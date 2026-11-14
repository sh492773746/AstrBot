"""Retired tools are absent and group AI never inherits execution authority."""

import ast
import inspect
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.main import Main
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from tests.test_wangshangliao_plain_commands import native_event


def test_private_management_method_and_tool_registration_do_not_exist():
    assert not hasattr(Main, "private_management")
    tree = ast.parse(inspect.getsource(Main))
    assert not any(
        isinstance(node, ast.Constant) and node.value == "wsl_private_management"
        for node in ast.walk(tree)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
@pytest.mark.parametrize("value", ["1", "{}", "确认设置 stolen", "x" * 4097])
async def test_group_ai_strips_cached_tools_and_rejects_direct_execution(admin, value):
    event = native_event(value)
    event.role = "admin" if admin else "member"
    event.send = AsyncMock()
    context = SimpleNamespace(
        get_config=lambda *args, **kwargs: {"admins_id": ["2"] if admin else []}
    )
    plugin = object.__new__(Main)
    plugin.context = context
    plugin.commands = SimpleNamespace(execute=AsyncMock())
    handler = AsyncMock()
    shared_tools = ToolSet(
        [
            FunctionTool(
                name=name,
                description="fixture",
                parameters={},
                handler=handler,
            )
            for name in (
                "wsl_private_management",
                "wsl_card_tool",
                "astrbot_execute_shell",
            )
        ]
    )
    history = [
        {"role": "user", "content": "previous"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "cached-management",
                    "type": "function",
                    "function": {"name": "wsl_private_management", "arguments": value},
                }
            ],
        },
        {"role": "tool", "content": "cached management result"},
    ]
    request = SimpleNamespace(
        func_tool=shared_tools,
        tool_calls_result="cached result",
        contexts=history,
        system_prompt="Existing persona",
    )
    try:
        await plugin.plain_text_request(event, request)
        assert request.func_tool.empty()
        assert request.func_tool is not shared_tools
        assert len(shared_tools.tools) == 3
        assert request.tool_calls_result is None
        assert request.contexts == [history[0]]
        assert len(history) == 3
        assert request.system_prompt.startswith("Existing persona")
        assert event.get_extra("wsl_ai_role") == "customer"
        assert event.get_extra("_api_key_allow_admin_role") is False
        run = SimpleNamespace(context=SimpleNamespace(event=event, context=context))
        for tool in shared_tools.tools:
            results = [
                result async for result in FunctionToolExecutor.execute(tool, run)
            ]
            assert len(results) == 1 and results[0].isError
            assert (
                json.loads(results[0].content[0].text)["reason"]
                == "tool_not_authorized"
            )
        handler.assert_not_awaited()
        plugin.commands.execute.assert_not_awaited()
        event.send.assert_not_awaited()
        event.platform.execute_moderation.assert_not_awaited()
        event.platform.send_reply_text.assert_not_awaited()
    finally:
        event.processing_completion.cancel()
