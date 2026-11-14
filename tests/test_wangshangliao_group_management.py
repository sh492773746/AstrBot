"""Group grants, retired private entry points and read-only AI isolation."""

import os
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from astrbot.builtin_stars.wangshangliao_moderation.commands import Commands
from astrbot.builtin_stars.wangshangliao_moderation.group_management import (
    GroupManagement,
    management_command,
)
from astrbot.builtin_stars.wangshangliao_moderation.main import Main
from astrbot.builtin_stars.wangshangliao_moderation.schedules import GroupSchedules
from astrbot.builtin_stars.wangshangliao_moderation.syntax import recognize_command
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from astrbot.core.provider.entities import ProviderRequest
from tests.test_wangshangliao_plain_commands import native_event

pytestmark = pytest.mark.usefixtures("wsl_active_policy", "wsl_command_classifier")
CONTEXT = SimpleNamespace(get_config=lambda _: {"admins_id": ["9"]})


@pytest_asyncio.fixture
async def management(tmp_path):
    item = GroupManagement(CONTEXT, tmp_path)
    await item.load()
    return item


def scope_event(private=False):
    event = native_event("group fixture", private=private)
    event.platform.config["enabled_groups"] = ["5", "6"]
    event.platform.groups["6"] = "906"
    event.platform.connection_state = "online"
    event.platform.execute_action = AsyncMock(return_value={"status": "accepted"})
    event.platform.group_names = {"5": "Assigned", "6": "Other"}
    event.platform.get_moderation_members = AsyncMock(
        return_value={
            "complete": True,
            "groupMemberInfo": [
                {"userId": "2", "nimId": "902", "groupRole": "GROUP_ROLE_ADMIN"}
            ],
        }
    )
    return event


async def grant(management, event, group="5", users=None, version=0):
    return await management.save(
        event.platform,
        group,
        ["2"] if users is None else users,
        version,
        "dashboard:test",
        lambda: None,
    )


@pytest.mark.asyncio
async def test_grant_survives_reload_without_framework_admin_or_finance(management):
    event = scope_event()
    assert not event.is_admin()
    assert not management.allowed(event)
    await grant(management, event)
    restored = GroupManagement(CONTEXT, management.path.parent)
    await restored.load()
    assert restored.allowed(event, "5")
    assert not restored.allowed(event, "6")
    assert restored.groups(event) == ["5"]
    assert not event.is_admin()
    assert not event.is_ai_admin(CONTEXT)
    commands = Commands()
    commands.management = restored
    commands.activities.command = AsyncMock()
    assert await commands.run(event, "开启邀请统计") is None
    assert await commands.run(event, "设置邀请奖励 999") is None
    assert await commands.run(event, "开发门禁") is None
    commands.activities.command.assert_not_awaited()
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["account", "instance", "group", "epoch", "paused", "capability"]
)
async def test_binding_cannot_transfer_or_bypass_pause(management, change):
    event = scope_event()
    await grant(management, event)
    if change == "account":
        event.platform.account = "3"
    elif change == "instance":
        event.platform.config["id"] = "other"
    elif change == "group":
        event.platform.config["enabled_groups"] = ["6"]
    elif change == "epoch":
        event.platform.config["_business_epoch"] = 99
    elif change == "paused":
        event.platform.config["_business_active"] = False
    else:
        event.set_extra("_api_key_allow_admin_role", False)
    assert not management.allowed(event, "5")


@pytest.mark.asyncio
async def test_group_actions_ignore_stale_selection_and_recheck_grant(management):
    event = scope_event()
    await grant(management, event)
    commands = Commands()
    commands.management = management
    assert management.groups(event) == ["5"]
    key = (
        id(event.platform),
        event.platform.account,
        event.get_sender_id(),
        event.unified_msg_origin,
    )
    commands.selections[key] = {"group": "6", "expires": time.monotonic() + 600}
    assert "服务端已接受" in await commands.run(event, "公告 allowed")
    event.platform.execute_action.assert_awaited_once()
    request = event.platform.execute_action.await_args.args[0]
    assert (request.action, request.group, request.text) == ("announce", 5, "allowed")
    event.platform.execute_action.reset_mock()
    event.message_obj.group_id = "6"
    assert await commands.run(event, "公告 forbidden") == "无权限"
    event.platform.execute_action.assert_not_awaited()
    event.message_obj.group_id = "5"
    await grant(management, event, users=[], version=1)
    assert await commands.run(event, "公告 forbidden") == "无权限"
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_action_rechecks_grant_after_roster_wait(management):
    event = scope_event(private=False)
    await grant(management, event)
    commands = Commands()
    commands.management = management

    async def revoke(_):
        management.assignments[management.key(event.platform, "5")] = ()
        return {"complete": True, "groupMemberInfo": [{"userId": "2"}]}

    event.platform.get_moderation_members.side_effect = revoke
    with pytest.raises(ProtocolError, match="command_admin_revoked"):
        await commands.run(event, "公告 forbidden")
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_manager_left_group_cannot_act(management):
    event = scope_event(private=False)
    await grant(management, event)
    commands = Commands()
    commands.management = management
    event.platform.get_moderation_members.return_value = {
        "complete": True,
        "groupMemberInfo": [],
    }
    assert await commands.run(event, "公告 forbidden") == "无权限"
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_version_conflict_does_not_overwrite_or_audit(management):
    event = scope_event()
    await grant(management, event)
    with pytest.raises(ProtocolError, match="group_management_changed"):
        await grant(management, event, users=[], version=0)
    with sqlite3.connect(management.path) as db:
        assert db.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 1
    assert management.allowed(event, "5")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "users", [["999"], ["2", "2"], ["1"], [2], ["2; DELETE"], [], ["2"]]
)
async def test_save_validates_members_and_guard(management, users):
    event = scope_event()
    if users in ([], ["2"]):
        await grant(management, event, users=users)
    else:
        with pytest.raises(ProtocolError):
            await grant(management, event, users=users)
        assert not management.allowed(event)


@pytest.mark.asyncio
async def test_schedule_creator_revocation_uses_exact_group(management):
    event = scope_event()
    event.platform.stopping = SimpleNamespace(is_set=lambda: False)
    await grant(management, event)
    schedules = GroupSchedules(CONTEXT)
    schedules.management = management
    event.platform.config["moderation"] = {
        "enabled": True,
        "permissions": {
            "5": ["mute_all", "unmute_all"],
            "6": ["mute_all", "unmute_all"],
        },
    }
    schedules.authorize(event.platform, "2", event.unified_msg_origin, "5")
    with pytest.raises(ProtocolError, match="schedule_owner_revoked"):
        schedules.authorize(event.platform, "2", event.unified_msg_origin, "6")
    await grant(management, event, users=[], version=1)
    with pytest.raises(ProtocolError, match="schedule_owner_revoked"):
        schedules.authorize(event.platform, "2", event.unified_msg_origin, "5")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("管理 群列表", ""),
        ("/管理 选择群 1", ""),
        ("管理 禁言 1 3", "禁言 1 3"),
        ("管理 设置邀请奖励 9", ""),
        ("管理 开启抽奖", ""),
        ("管理 请禁言 1", ""),
        ("管理 管理 群列表", ""),
        ("管理 公告 hi\n禁言 1", ""),
        ("管理 /chat", ""),
        ("管理 sid", ""),
    ],
)
def test_legacy_manager_parser_never_registers_a_private_command(text, expected):
    assert management_command(text) == expected
    assert recognize_command(text) == ""


@pytest.mark.parametrize(
    "entry,tool_name",
    [
        ("private_management", "wsl_private_management"),
        ("card_tool", "wsl_card_tool"),
    ],
)
def test_retired_ai_hooks_and_handler_registrations_are_absent(
    entry, tool_name, tmp_path
):
    """Inspect real import-time registrations outside the suite's mutable registry.

    Args:
        entry: Retired method that must remain absent.
        tool_name: Retired tool that must never register.
        tmp_path: Isolated application root for the fresh interpreter.
    """
    assert not hasattr(Main, entry)
    assert not hasattr(object.__new__(Main), entry)
    code = textwrap.dedent(
        """\
        import sys

        from astrbot.builtin_stars.wangshangliao_moderation.main import Main
        from astrbot.core.provider.register import llm_tools
        from astrbot.core.star.star_handler import EventType, star_handlers_registry

        entry, tool_name = sys.argv[1:]
        assert not hasattr(Main, entry)
        assert not hasattr(object.__new__(Main), entry)
        handlers = star_handlers_registry.get_handlers_by_module_name(Main.__module__)
        assert any(handler.handler_name == "moderation_command" for handler in handlers)
        assert not any(
            handler.handler_name == entry
            or handler.event_type is EventType.OnCallingFuncToolEvent
            for handler in handlers
        )
        assert llm_tools.get_func(tool_name) is None
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", code, entry, tool_name],
        cwd=Path(__file__).resolve().parents[1],
        env={
            **os.environ,
            "ASTRBOT_ROOT": str(tmp_path),
            "ASTRBOT_DISABLE_METRICS": "1",
            "ASTRBOT_DISABLE_FILE_LOGGING": "1",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [True, False])
@pytest.mark.parametrize("private", [True, False])
@pytest.mark.parametrize(
    "action",
    [
        "禁言",
        "解禁",
        "踢出",
        "公告",
        "全员禁言",
        "解除全员禁言",
        "确认踢出",
        "确认定时",
    ],
)
async def test_ai_command_dispatch_rejects_before_service_access(
    admin, private, action
):
    event = scope_event(private)
    event.role = "admin" if admin else "member"
    commands = object.__new__(Commands)
    assert await commands.execute(event, action, "old-token", ai=True) == {
        "status": "rejected",
        "reason": "ai_management_retired",
    }
    assert vars(commands) == {}
    event.platform.execute_action.assert_not_awaited()
    event.platform.get_moderation_members.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [True, False])
async def test_group_ai_requests_have_no_tools_or_old_tool_history(admin):
    event = scope_event()
    event.role = "admin" if admin else "member"
    shared = ToolSet(
        [
            FunctionTool(name=n, description="test", parameters={})
            for n in [
                "wsl_private_management",
                "run_code",
                "delete_friend",
                "wsl_my_invitations",
                "astr_kb_search",
            ]
        ]
    )
    history = [
        {"role": "user", "content": "hello"},
        {"role": "tool", "content": "malicious"},
        {"role": "function", "content": "legacy-result"},
        {
            "role": "assistant",
            "tool_calls": [{"function": {"name": "wsl_private_management"}}],
        },
        {"role": "assistant", "function_call": {"name": "delete_friend"}},
    ]
    req = ProviderRequest(func_tool=shared, system_prompt="persona", contexts=history)
    plugin = object.__new__(Main)
    plugin.context = CONTEXT
    await plugin.plain_text_request(event, req)
    assert req.func_tool.tools == []
    assert req.func_tool is not shared
    assert len(shared.tools) == 5
    assert req.tool_calls_result is None
    assert req.contexts == [{"role": "user", "content": "hello"}]
    assert len(history) == 5
    assert event.get_extra("_api_key_allow_admin_role") is False
    assert event.get_extra("wsl_ai_role") == "customer"
    assert req.system_prompt.startswith("persona")
    assert "malicious" not in req.system_prompt
    assert "legacy-result" not in req.system_prompt
    assert not event.is_stopped()
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [True, False])
async def test_private_ai_request_stops_without_processing_history_or_persona(admin):
    event = scope_event(private=True)
    event.role = "admin" if admin else "member"
    shared = ToolSet(
        [FunctionTool(name="wsl_private_management", description="test", parameters={})]
    )
    history = [{"role": "tool", "content": "private-history-canary"}]
    req = ProviderRequest(
        func_tool=shared, system_prompt="private-persona-canary", contexts=history
    )
    req.tool_calls_result = Mock()
    plugin = object.__new__(Main)
    await plugin.plain_text_request(event, req)
    assert req.func_tool.empty() and req.func_tool is not shared
    assert len(shared.tools) == 1
    assert req.tool_calls_result is None
    assert req.contexts is history
    assert req.system_prompt == "private-persona-canary"
    assert event.is_stopped()
    assert event.get_extra("wsl_ai_role") is None
    assert vars(plugin) == {}
    event.platform.send_reply_text.assert_not_awaited()
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_ai_rejects_conversation_from_a_different_scope():
    event = scope_event()
    event.send = AsyncMock()
    req = ProviderRequest(
        system_prompt="persona",
        conversation=SimpleNamespace(
            user_id=event.get_ai_conversation_origin(CONTEXT) + "/other-group"
        ),
        contexts=[{"role": "user", "content": "other-group-canary"}],
    )
    plugin = object.__new__(Main)
    plugin.context = CONTEXT
    await plugin.plain_text_request(event, req)
    assert req.func_tool.empty()
    assert req.system_prompt == "persona"
    assert event.is_stopped()
    event.send.assert_awaited_once()
    assert event.get_extra("wsl_ai_role") is None
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [True, False])
@pytest.mark.parametrize("text", ["管理 群列表", "/管理 选择群 1", "公告 forbidden"])
async def test_private_grant_cannot_wake_or_execute(management, admin, text):
    event = scope_event(private=True)
    event.role = "admin" if admin else "member"
    event.message_str = text
    await grant(management, event)
    assert management.allowed(event, "5")
    event.platform.get_moderation_members.reset_mock()
    registry = Mock()
    assert not await event.prepare_waking_check(
        {"admins_id": ["2"] if admin else []}, registry
    )
    assert event.is_stopped()
    assert not event.eligible
    assert registry.mock_calls == []
    commands = Commands()
    commands.management = management
    assert await commands.run(event, text) is None
    assert commands.selections == {}
    event.platform.get_moderation_members.assert_not_awaited()
    event.platform.execute_action.assert_not_awaited()
    event.platform.send_reply_text.assert_not_awaited()
