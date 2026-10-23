"""Native bot-role gating and private account/group authorization regressions."""

import copy
import time
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from astrbot.builtin_stars.wangshangliao_moderation.policy import (
    GROUP_ADMIN_REQUIRED,
    handle,
    require_group_admin,
)
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from tests.test_wangshangliao_command_menu import step
from tests.test_wangshangliao_command_parity import setup as setup

pytestmark = pytest.mark.usefixtures("wsl_active_policy", "wsl_command_classifier")


@pytest_asyncio.fixture
async def private_setup(setup):
    """Bind a verified private manager to one group and keep other grants separate.

    Args:
        setup: Isolated two-group command service.

    Returns:
        Service, command engine and private event without friendship dependencies.
    """
    value, engine, event = setup
    other = value.store.tenant("1", "6")
    token = value.store.preview_control(
        "1", "6", other["version"], "dashboard/root", other["rules"], ["3"], None
    )
    value.store.change(
        "1",
        "6",
        other["version"],
        "dashboard/root",
        managers=["3"],
        control=True,
        confirmation=token,
    )
    value.store.discover("9", {"5": {"name": "Other account", "team": "905"}})
    value.store.import_group("9", "5", other["rules"], ["3"], "system/test")
    await value.refresh()
    value.store.remember_roster("1", value.roster.return_value)
    event.is_private_chat = lambda: True
    event.get_group_id = lambda: ""
    event.eligible = True
    event.platform.config.update(value.policy_values("1", event.platform.config))
    return value, engine, event


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["GROUP_ROLE_MEMBER", "", "untrusted"])
@pytest.mark.parametrize(
    "command",
    [
        "菜单",
        "帮助",
        "开通",
        "群列表",
        "选择群 1",
        "成员列表",
        "成员搜索 Target",
        "排名",
        "规则",
        "能力",
        "我的权限",
        "管理员列表",
        "添加管理员 3",
        "移除管理员 3",
        "确认授权 abcdef123456",
        "开启广告审核",
        "关闭防刷屏",
        "添加关键词 blocked",
        "删除关键词 1",
        "公告 blocked",
        "禁言 3 5",
        "解禁 3",
        "踢出 3",
        "确认踢出 abcdef123456",
        "全员禁言",
        "解除全员禁言",
        "定时禁言 23:00 08:00",
        "定时状态",
        "参加抽奖",
        "抽奖状态",
        "开启抽奖",
        "邀请统计",
        "我的邀请",
        "1",
        "/chat",
    ],
)
async def test_group_commands_uniformly_require_native_bot_admin(setup, role, command):
    value, _, event = setup
    before = value.store.tenants()
    event.platform.config["_open_native_roles"]["5"] = role
    reply = await step(value, event, command, native=True)
    assert reply == GROUP_ADMIN_REQUIRED
    assert value.store.tenants() == before
    event.platform.execute_action.assert_not_awaited()
    event.platform.get_moderation_members.assert_not_awaited()
    value.roster.assert_not_awaited()
    value.plugin.cashback.private_invitation_reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_nonadmin_group_chatter_does_not_send_repeated_notices(
    setup, monkeypatch
):
    value, _, event = setup
    event.platform.config["_open_native_roles"]["5"] = "GROUP_ROLE_MEMBER"
    moderation = AsyncMock()
    monkeypatch.setattr(
        "astrbot.builtin_stars.wangshangliao_moderation.open_service.handle",
        moderation,
    )
    assert not await step(value, event, "今天大家好", native=True)
    moderation.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_list_and_selection_are_bound_to_manager_and_bot(private_setup):
    value, engine, event = private_setup
    event.role = "admin"
    value.quota_admins = {"2"}
    reply = await engine.run(event, "群列表")
    assert "Group 5" in reply
    assert "Group 6" not in reply and "Other account" not in reply
    assert value.management.groups(event) == ["5"]
    assert "当前已选群：Group 5" in await engine.run(event, "选择群 1")
    assert "无效" in await engine.run(event, "选择群 2")
    assert "无效" in await engine.run(event, "选择群 6")
    event.platform.account = "9"
    assert await engine.run(event, "群列表") == "无权限"
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["account", "claimed_uid", "verified_uid"])
async def test_mismatched_private_identity_cannot_list_groups(private_setup, change):
    value, engine, event = private_setup
    event.actor_identity = replace(event.actor_identity, **{change: "9"})
    assert value.management.groups(event) == []
    assert await engine.run(event, "群列表") == "无权限"


@pytest.mark.asyncio
async def test_injected_foreign_selection_cannot_read_or_change_group(private_setup):
    value, engine, event = private_setup
    await engine.run(event, "群列表")
    await engine.run(event, "选择群 1")
    key = (id(event.platform), "1", "2", event.unified_msg_origin)
    engine.selections[key]["group"] = "6"
    assert await engine.run(event, "成员列表") == "无权限"
    assert await value.manager_command(event, "规则") == "无权限"
    assert await value.manager_command(event, "关闭防刷屏") == "无权限"
    value.roster.assert_not_awaited()
    event.platform.get_moderation_members.assert_not_awaited()
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["规则", "能力", "成员列表", "定时状态", "公告 hi"])
async def test_private_selected_group_reports_missing_bot_admin(private_setup, command):
    value, engine, event = private_setup
    event.platform.config["_open_native_roles"]["5"] = "GROUP_ROLE_MEMBER"
    assert "Group 5" in await engine.run(event, "群列表")
    assert GROUP_ADMIN_REQUIRED in await engine.run(event, "选择群 1")
    assert GROUP_ADMIN_REQUIRED == await step(value, event, command, native=True)
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_menu_does_not_preview_operations_without_bot_admin(
    private_setup,
):
    value, engine, event = private_setup
    await engine.run(event, "群列表")
    await engine.run(event, "选择群 1")
    event.platform.config["_open_native_roles"]["5"] = "GROUP_ROLE_MEMBER"
    for text in ("菜单", "5"):
        await step(value, event, text, native=True)
    assert GROUP_ADMIN_REQUIRED == await step(value, event, "1", native=True)
    state = value.navigation.sessions[value.navigation.key(event)]
    assert not state.pending and not state.confirm
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_revoked_private_manager_cannot_reuse_selected_group(private_setup):
    value, engine, event = private_setup
    await engine.run(event, "群列表")
    await engine.run(event, "选择群 1")
    tenant = value.store.tenant("1", "5")
    token = value.store.preview_control(
        "1", "5", tenant["version"], "dashboard/root", tenant["rules"], ["3"], None
    )
    value.store.change(
        "1",
        "5",
        tenant["version"],
        "dashboard/root",
        managers=["3"],
        control=True,
        confirmation=token,
    )
    await value.refresh()
    assert await engine.run(event, "成员列表") == "无权限"
    assert await value.manager_command(event, "关闭防刷屏") == "无权限"


@pytest.mark.asyncio
async def test_role_loss_invalidates_prepared_group_menu_and_promotion_keeps_rules(
    setup,
):
    value, _, event = setup
    for text in ("菜单", "3", "2", "2"):
        await step(value, event, text, native=True)
    before = copy.deepcopy(value.store.tenant("1", "5"))
    event.platform.config["_open_native_roles"]["5"] = "GROUP_ROLE_MEMBER"
    assert GROUP_ADMIN_REQUIRED == await step(value, event, "1", native=True)
    assert not value.navigation.sessions
    assert value.store.tenant("1", "5") == before
    event.platform.config["_open_native_roles"]["5"] = "GROUP_ROLE_ADMIN"
    assert "广告审核：开启" in await step(value, event, "规则", native=True)
    assert value.store.tenant("1", "5") == before


@pytest.mark.asyncio
async def test_fresh_roster_role_loss_prevents_private_actions(private_setup):
    _, engine, event = private_setup
    await engine.run(event, "群列表")
    await engine.run(event, "选择群 1")
    event.platform.get_moderation_members.return_value["groupMemberInfo"][0][
        "groupRole"
    ] = "GROUP_ROLE_MEMBER"
    with pytest.raises(ProtocolError, match="group_native_admin_required"):
        await engine.run(event, "公告 blocked")
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_moderation_and_activity_stop_without_native_admin(setup):
    _, engine, event = setup
    adapter = event.platform
    adapter.config["_open_native_roles"]["5"] = "GROUP_ROLE_MEMBER"
    assert not await handle(
        adapter,
        "5",
        "blocked",
        {"sender": "3", "text": "old", "created_at": time.time() * 1000},
    )
    with pytest.raises(ProtocolError, match="group_native_admin_required"):
        engine.activities.authorize(adapter, "5", "2")
    with pytest.raises(ProtocolError, match="group_native_admin_required"):
        engine.schedules.authorize(adapter, "2", event.unified_msg_origin, "5")
    adapter.execute_action.assert_not_awaited()


def test_legacy_nonpublic_policy_is_not_changed_by_public_native_gate():
    require_group_admin({"enabled_groups": ["5"]}, "5")
