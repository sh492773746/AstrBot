import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.main import Main


def event(*, admin=True, private=True, platform="wangshangliao"):
    extras = {}
    return SimpleNamespace(
        is_admin=lambda: admin,
        is_private_chat=lambda: private,
        get_platform_name=lambda: platform,
        get_extra=extras.get,
        set_extra=extras.__setitem__,
        platform=SimpleNamespace(config={"id": "test"}),
        message_obj=SimpleNamespace(message_id="one"),
        message_str="请禁言目标，踢出目标",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs", [{"admin": False}, {"private": False}, {"platform": "telegram"}]
)
async def test_ai_rejects_inapplicable_identity(kwargs):
    plugin = Main(None)
    plugin.commands.execute = AsyncMock()
    result = json.loads(await plugin.private_management(event(**kwargs), "mute", "1"))
    assert result["status"] == "rejected"
    plugin.commands.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_ai_fixed_dispatch_and_unknown_stops_mutations():
    plugin = Main(None)
    plugin.commands.execute = AsyncMock(
        return_value={"status": "unknown", "operation_id": "ai/x"}
    )
    ev = event()
    assert (
        json.loads(await plugin.private_management(ev, "mute", "1"))["status"]
        == "unknown"
    )
    assert (
        json.loads(await plugin.private_management(ev, "kick", "1"))["status"]
        == "rejected"
    )
    plugin.commands.execute.assert_awaited_once_with(ev, "禁言", "1", ai=True)
    await plugin.private_management(ev, "result", "ai/x")
    assert plugin.commands.execute.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,value",
    [("grant_admin", "1"), ("mute", "1 公告 injected"), ("announce", "x" * 4097)],
)
async def test_ai_rejects_arbitrary_commands(action, value):
    plugin = Main(None)
    plugin.commands.execute = AsyncMock()
    assert (
        json.loads(await plugin.private_management(event(), action, value))["status"]
        == "rejected"
    )
    plugin.commands.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_closed_window_rejects_delayed_tool():
    plugin = Main(None)
    plugin.commands.execute = AsyncMock()
    ev = event()
    ev.set_extra("wsl_test_scope", "private_ai")
    result = json.loads(await plugin.private_management(ev, "mute", "1"))
    assert result["reason"] == "test_window_closed"
    plugin.commands.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_visibility_does_not_modify_shared_tools():
    from astrbot.core.agent.tool import FunctionTool, ToolSet

    shared = ToolSet(
        [FunctionTool(name="wsl_private_management", description="test", parameters={})]
    )
    from astrbot.core.provider.entities import ProviderRequest

    req = ProviderRequest(func_tool=shared, system_prompt="persona")
    await Main(None).plain_text_request(event(admin=False), req)
    assert req.func_tool.get_tool("wsl_private_management") is None
    assert shared.get_tool("wsl_private_management") is not None


@pytest.mark.asyncio
async def test_ai_member_snapshot_and_operation_id():
    plugin = Main(None)
    ev = event()
    ev.get_sender_id = lambda: "admin"
    ev.unified_msg_origin = "bot:private:admin"
    ev.get_group_id = lambda: ""
    member = {"userId": "20", "nimId": "nim20", "name": "Test"}
    ev.platform.account = "10"
    ev.platform.groups = {"5": {}}
    ev.platform.config["enabled_groups"] = ["5"]
    ev.platform.get_moderation_members = AsyncMock(
        return_value={"groupMemberInfo": [member]}
    )
    ev.platform.execute_moderation = AsyncMock(return_value={"status": "verified"})
    for action, value in [("groups", ""), ("select_group", "1"), ("members", "")]:
        await plugin.private_management(ev, action, value)
    first = json.loads(await plugin.private_management(ev, "mute", "1"))
    second = json.loads(await plugin.private_management(ev, "mute", "1"))
    assert first == second
    assert first["operation_id"].startswith("ai/")
    ev.platform.get_moderation_members.return_value = {
        "groupMemberInfo": [{**member, "nimId": "changed"}]
    }
    assert (
        json.loads(await plugin.private_management(ev, "kick", "1"))["status"]
        == "rejected"
    )
    assert ev.platform.execute_moderation.await_count == 2
