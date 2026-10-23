"""Retired AI mutations, account/group isolation and revocation regressions."""

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

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


def scope_event(private=True):
    event = native_event("群列表", private=private)
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
    assert not event.is_admin()
    commands = Commands()
    commands.management = restored
    assert await commands.run(event, "开启邀请统计") == "无权限"
    assert await commands.run(event, "设置邀请奖励 999") == "无权限"
    assert await commands.run(event, "开发门禁") == "无权限"


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
async def test_group_list_and_stale_selection_are_scope_checked(management):
    event = scope_event()
    await grant(management, event)
    commands = Commands()
    commands.management = management
    reply = await commands.run(event, "群列表")
    assert "Assigned" in reply and "Other" not in reply
    assert (
        await commands.run(event, "选择群 2") == "编号无效或过期，请重新发送“群列表”。"
    )
    await commands.run(event, "选择群 1")
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
        ("管理 群列表", "群列表"),
        ("/管理 选择群 1", "选择群 1"),
        ("管理 禁言 1 3", "禁言 1 3"),
        ("管理 设置邀请奖励 9", ""),
        ("管理 开启抽奖", ""),
        ("管理 请禁言 1", ""),
        ("管理 管理 群列表", ""),
        ("管理 公告 hi\n禁言 1", ""),
        ("管理 /chat", ""),
        ("管理 sid", "我的权限"),
    ],
)
def test_private_manager_envelope_admission_is_bounded(text, expected):
    assert management_command(text) == expected
    assert recognize_command(text) == ("私聊菜单" if expected else "")


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [True, False])
@pytest.mark.parametrize("private", [True, False])
@pytest.mark.parametrize(
    "action",
    [
        "groups",
        "mute",
        "unmute",
        "kick",
        "announce",
        "config_preview",
        "config_confirm",
        "card_preview",
        "card_execute",
        "schedule_confirm",
    ],
)
async def test_every_retired_ai_action_is_inert(admin, private, action):
    event = scope_event(private)
    event.role = "admin" if admin else "member"
    plugin = object.__new__(Main)
    plugin.commands = SimpleNamespace(execute=AsyncMock())
    assert json.loads(await plugin.private_management(event, action, "{}")) == {
        "status": "rejected",
        "reason": "ai_management_retired",
    }
    assert (
        json.loads(await plugin.card_tool(event, action, "{}"))["status"] == "rejected"
    )
    plugin.commands.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [True, False])
@pytest.mark.parametrize("private", [True, False])
async def test_all_ai_requests_have_no_tools_or_old_tool_history(admin, private):
    event = scope_event(private)
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
    req = ProviderRequest(
        func_tool=shared,
        system_prompt="persona",
        contexts=[
            {"role": "user", "content": "hello"},
            {"role": "tool", "content": "malicious"},
            {
                "role": "assistant",
                "tool_calls": [{"function": {"name": "wsl_private_management"}}],
            },
        ],
    )
    plugin = object.__new__(Main)
    plugin.context = CONTEXT
    await plugin.plain_text_request(event, req)
    assert req.func_tool.tools == []
    assert len(shared.tools) == 5
    assert req.contexts == [{"role": "user", "content": "hello"}]
    assert event.get_extra("_api_key_allow_admin_role") is False
    assert event.get_extra("wsl_ai_role") == "customer"
    assert "persona" in req.system_prompt


@pytest.mark.asyncio
async def test_native_private_gate_admits_only_fixed_management_envelope(management):
    event = scope_event()
    event.message_str = "管理 群列表"
    from astrbot.core.star.star_handler import star_handlers_registry

    assert await event.prepare_waking_check({"admins_id": []}, star_handlers_registry)
    assert not event.is_admin()
    commands = Commands()
    commands.management = management
    assert await commands.run(event, management_command(event.message_str)) == "无权限"
    await grant(management, event)
    assert "Assigned" in await commands.run(
        event, management_command(event.message_str)
    )
