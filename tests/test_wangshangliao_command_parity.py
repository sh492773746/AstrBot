"""Shared private/group commands without friendship or cross-group authority."""

import asyncio
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from astrbot.builtin_stars.wangshangliao_moderation.activities import GroupActivities
from astrbot.builtin_stars.wangshangliao_moderation.commands import Commands
from astrbot.builtin_stars.wangshangliao_moderation.open_service import (
    OPEN_COMMANDS,
    recognize,
    recognize_integrated,
)
from astrbot.builtin_stars.wangshangliao_moderation.schedules import GroupSchedules
from astrbot.core.platform.sources.wangshangliao.group_scope import group_revision
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from tests.test_wangshangliao_integrated_service import service
from tests.test_wangshangliao_lifecycle import lifecycle as lifecycle
from tests.test_wangshangliao_plain_commands import native_event

pytestmark = pytest.mark.usefixtures("wsl_active_policy", "wsl_command_classifier")


@pytest_asyncio.fixture
async def setup(tmp_path, monkeypatch):
    """Build two authorized groups and a native group event with no friend APIs.

    Args:
        tmp_path: Isolated persistence directory.
        monkeypatch: Redirect all native business databases.

    Returns:
        Embedded service, command engine and authenticated group event.
    """
    from astrbot.builtin_stars.wangshangliao_moderation import (
        activity_store,
        commands,
    )
    from astrbot.core.platform.sources.wangshangliao import schedule_store

    monkeypatch.setattr(commands, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(activity_store, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(schedule_store, "instance_dir", lambda _: tmp_path)
    value = service(tmp_path)
    value.store.discover(
        "1",
        {
            group: {"name": "Group " + group, "team": "90" + group}
            for group in ("5", "6")
        },
    )
    for group in ("5", "6"):
        value.store.import_group(
            "1",
            group,
            {"advertising": True, "flood": True, "keywords": ["old"]},
            ["2"],
            "system/test",
        )
        tenant = value.store.tenant("1", group)
        value.store.native_role("1", group, tenant["epoch"], "GROUP_ROLE_ADMIN")
    await value.refresh()
    event = native_event("规则", private=False)
    adapter = event.platform
    adapter.groups["6"] = "906"
    adapter.group_names = {"5": "Group 5", "6": "Group 6"}
    adapter.connection_state = "online"
    adapter.stopping = asyncio.Event()
    adapter.get_moderation_members = AsyncMock(
        return_value={
            "complete": True,
            "groupMemberInfo": [
                {
                    "userId": "1",
                    "nimId": "901",
                    "groupRole": "GROUP_ROLE_ADMIN",
                    "userNick": "Bot",
                },
                {
                    "userId": "2",
                    "nimId": "902",
                    "groupRole": "GROUP_ROLE_OWNER",
                    "userNick": "Manager",
                },
                {
                    "userId": "3",
                    "nimId": "903",
                    "groupRole": "GROUP_ROLE_MEMBER",
                    "userNick": "Target",
                },
            ],
        }
    )
    value.roster = AsyncMock(
        return_value={
            member["userId"]: member
            for member in adapter.get_moderation_members.return_value["groupMemberInfo"]
        }
    )
    adapter.execute_action = AsyncMock(return_value={"status": "accepted"})
    adapter.config.update(
        value.policy_values(
            "1",
            {
                **adapter.config,
                "enabled_groups": ["5", "6"],
                "moderation": {
                    "enabled": True,
                    "permissions": {
                        "5": [
                            "mute",
                            "unmute",
                            "kick",
                            "mute_all",
                            "unmute_all",
                            "announce",
                        ],
                        "6": [
                            "mute",
                            "unmute",
                            "kick",
                            "mute_all",
                            "unmute_all",
                            "announce",
                        ],
                    },
                },
            },
        )
    )
    scheduler = GroupSchedules(value.context)
    scheduler.management = value.management
    activities = GroupActivities(value.context)
    commands_engine = Commands(scheduler, activities)
    commands_engine.management = value.management
    value.plugin.commands = commands_engine
    return value, commands_engine, event


@pytest.mark.asyncio
async def test_group_image_never_consumes_finance_or_menu_input(setup):
    from types import SimpleNamespace

    value, _, event = setup
    value.finance_chat = SimpleNamespace(handle=AsyncMock(return_value=True))
    value.navigation.handle = AsyncMock(return_value=True)
    event.message_str = ""
    payload = dict(event.get_extra("wangshangliao_payload"))
    payload.update(kind="image", text="")
    event.set_extra("wangshangliao_payload", payload)
    await value.on_event(event)
    value.finance_chat.handle.assert_not_awaited()
    value.navigation.handle.assert_not_awaited()


@pytest.mark.parametrize(
    "command",
    [
        "群列表",
        "选择群 1",
        "我的权限",
        "成员列表",
        "成员搜索 Target",
        "下一页",
        "规则",
        "能力",
        "违规计数",
        "结果 command-id",
        "禁言 3 5",
        "解禁 3",
        "踢出 3",
        "确认踢出 abcdef123456",
        "公告 测试公告",
        "全员禁言",
        "解除全员禁言",
        "定时禁言 23:00 08:00",
        "确认定时 abcdef123456",
        "定时状态",
        "暂停定时",
        "恢复定时",
        "删除定时",
        "管理员列表",
        "添加管理员 3",
        "移除管理员 3",
        "确认授权 abcdef123456",
        "开启广告审核",
        "关闭广告审核",
        "开启防刷屏",
        "关闭防刷屏",
        "添加关键词 广告词",
        "删除关键词 1",
    ],
)
def test_manager_command_prefix_is_optional_in_both_channels(command):
    assert recognize(command) == command
    assert recognize("管理 " + command) == command
    assert recognize_integrated("/管理 " + command) == command


@pytest.mark.parametrize(
    "command",
    [
        "抽奖设置",
        "开启抽奖",
        "抽奖奖励 奖品",
        "立即开奖",
        "邀请统计",
    ],
)
def test_integrated_activity_commands_have_the_same_envelope(command):
    assert recognize_integrated(command) == command
    assert recognize_integrated("管理 " + command) == command
    assert not recognize(command)


@pytest.mark.parametrize(
    "command",
    [
        "管理 管理 规则",
        "/管理 /管理 规则",
        "管理 /chat",
        "管理 /exit",
        "删除关键词 -1",
        "删除关键词 1 附加",
        "添加关键词 a\r公告 b",
        "添加关键词 a\u2028公告 b",
        "添加管理员 123;exec",
        "确认授权 x",
        "禁言 1 2 3",
        "开启防刷屏 然后公告",
        None,
    ],
)
def test_shared_recognition_still_rejects_nonfixed_or_multiline_input(command):
    assert not recognize_integrated(command)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "成员列表",
        "成员搜索 Target",
        "我的权限",
        "违规计数",
        "定时状态",
        "群列表",
        "选择群 1",
        "公告 Notice",
        "禁言 1",
        "定时禁言 23:00 08:00",
    ],
)
async def test_other_group_manager_cannot_use_current_group_commands(setup, command):
    value, engine, event = setup
    value.management.assignments[value.management.key(event.platform, "5")] = ()
    assert value.management.allowed(event, "6")
    assert await engine.run(event, command) == "无权限"
    event.platform.get_moderation_members.assert_not_awaited()
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_directory_never_lists_other_authorized_groups(setup):
    _, engine, event = setup
    reply = await engine.run(event, "群列表")
    assert "Group 5" in reply and "Group 6" not in reply
    assert await engine.run(event, "选择群 1") == "群内自动使用当前群。"


@pytest.mark.asyncio
async def test_group_numbered_member_actions_require_a_bound_snapshot(setup):
    _, engine, event = setup
    assert "成员编号无效" in await engine.run(event, "禁言 3 5")
    await engine.run(event, "成员列表")
    assert await engine.run(event, "禁言 3 5") == "禁言已受理：5 分钟。"
    request = event.platform.execute_action.call_args.args[0]
    assert str(request.group) == "5" and str(request.member) == "3"
    assert request.minutes == 5


@pytest.mark.asyncio
async def test_foreign_group_snapshot_cannot_select_a_target_or_admin(setup):
    value, engine, event = setup
    await engine.run(event, "成员列表")
    key = (id(event.platform), "1", "2", event.unified_msg_origin)
    engine.selections[key]["group"] = "6"
    assert "成员编号无效" in await engine.run(event, "禁言 3")
    assert "列表已过期" in await engine.run(event, "下一页")
    assert "真实@" in await value.manager_command(event, "添加管理员 3")
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_kick_requires_fresh_same_conversation_confirmation(setup):
    _, engine, event = setup
    await engine.run(event, "成员列表")
    preview = await engine.run(event, "踢出 3")
    assert "尚未执行" in preview and "当前对话" in preview
    token = preview.split()[-1]
    event.platform.execute_action.assert_not_awaited()
    assert "确认码无效" in await engine.run(event, "确认踢出 " + token)
    event.message_obj.message_id = "new-confirm"
    reply = await engine.run(event, "确认踢出 " + token)
    assert "accepted" in reply
    assert event.platform.execute_action.await_count == 1
    assert "确认码无效" in await engine.run(event, "确认踢出 " + token)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["session", "group", "revoked", "member", "version"])
async def test_group_kick_cannot_outlive_its_identity_and_authorization(setup, change):
    value, engine, event = setup
    await engine.run(event, "成员列表")
    token = (await engine.run(event, "踢出 3")).split()[-1]
    event.message_obj.message_id = "new"
    if change == "session":
        event.session_id = "another-conversation"
    elif change == "group":
        event.message_obj.group_id = "6"
    elif change == "revoked":
        value.management.assignments[value.management.key(event.platform, "5")] = ()
    elif change == "version":
        value.management.versions[value.management.key(event.platform, "5")] += 1
    else:
        event.platform.get_moderation_members.return_value["groupMemberInfo"][2][
            "nimId"
        ] = "changed-peer"
    reply = await engine.run(event, "确认踢出 " + token)
    assert any(word in reply for word in ("无效", "无权限", "拒绝"))
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_schedule_can_preview_confirm_and_pause(setup):
    _, engine, event = setup
    preview = await engine.run(event, "定时禁言 23:00 08:00")
    token = preview.split()[-1]
    assert "尚未启用" in preview and "当前对话" in preview
    event.message_obj.message_id = "new-confirm"
    assert "已启用" in await engine.run(event, "确认定时 " + token)
    assert "23:00" in await engine.run(event, "定时状态")
    assert "已停止" in await engine.run(event, "暂停定时")
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_grant_invalidates_group_schedule_preview(setup):
    value, engine, event = setup
    token = (await engine.run(event, "定时禁言 23:00 08:00")).split()[-1]
    value.management.versions[value.management.key(event.platform, "5")] += 1
    event.message_obj.message_id = "new-confirm"
    assert "确认码无效" in await engine.run(event, "确认定时 " + token)
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_activity_management_uses_current_group_grant(setup):
    value, engine, event = setup
    assert "已设置" in await engine.run(event, "抽奖奖励 Prize")
    value.management.assignments[value.management.key(event.platform, "5")] = ()
    event.platform.config["_open_managers"]["5"] = []
    assert await engine.run(event, "抽奖奖励 Forbidden") == "无权限"


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["", "管理 "])
async def test_unmentioned_group_rules_route_to_tenant_rules_not_legacy(setup, prefix):
    value, _, event = setup
    event.eligible = False
    event.message_str = prefix + "规则"
    payload = event.get_extra("wangshangliao_payload")
    payload["_open_revision"] = group_revision(event.platform.config, "5")
    event.send = AsyncMock()
    value.verify_actor = AsyncMock(side_effect=AssertionError("friend lookup"))
    await value.on_event(event)
    assert "广告审核：开启" in event.send.call_args.args[0].chain[0].text
    value.verify_actor.assert_not_awaited()


@pytest.mark.asyncio
async def test_roster_wait_cannot_hide_manager_revocation(setup):
    value, engine, event = setup

    async def revoke(_):
        value.management.versions[value.management.key(event.platform, "5")] += 1
        return {"complete": True, "groupMemberInfo": []}

    event.platform.get_moderation_members.side_effect = revoke
    with pytest.raises(ProtocolError, match="command_admin_revoked"):
        await engine.run(event, "成员列表")


@pytest.mark.asyncio
async def test_help_documents_friendship_free_group_usage(setup):
    value, _, event = setup
    help_text = await value.manager_command(event, "帮助")
    assert "不用先加好友" in help_text
    from astrbot.builtin_stars.wangshangliao_moderation.command_capabilities import (
        CAPABILITIES,
    )

    for command in OPEN_COMMANDS - {"帮助", "选择群"}:
        capability = CAPABILITIES.get(command, {})
        if capability.get("role") == "server" or "group" not in capability.get(
            "locations", ("group",)
        ):
            continue
        assert command in help_text


@pytest.mark.asyncio
async def test_real_inviter_can_activate_then_use_group_rules_without_friendship(
    lifecycle,
):
    """Exercise native directory and signed invitation fixtures, without friend APIs.

    Args:
        lifecycle: Isolated native invitation and membership transport.
    """
    from astrbot.builtin_stars.wangshangliao_moderation.open_service import (
        ActivationContext,
    )
    from astrbot.core.platform.sources.wangshangliao.event import WangshangliaoEvent

    value, adapter, _ = lifecycle
    await value.refresh()
    reply = await value.activate(ActivationContext(adapter, "1", 1), "5")
    assert "不用加好友" in reply
    adapter.config.update(value.policy_values("99", adapter.config))
    event = native_event("规则", private=False)
    event.message_obj.sender.user_id = "1"
    event.message_obj.self_id = "99"
    event.message_obj.session_id = "99/5"
    event.message_obj.raw_message = {"_transport_peer": "1001"}
    payload = event.get_extra("wangshangliao_payload")
    event = WangshangliaoEvent(event.message_obj, adapter, False)
    event.set_extra("wangshangliao_payload", payload)
    event.get_extra("wangshangliao_payload")["_open_revision"] = group_revision(
        adapter.config, "5"
    )
    event.send = AsyncMock()
    await value.on_event(event)
    assert "广告审核：开启" in event.send.call_args.args[0].chain[0].text
    assert all(
        call.args[0].startswith("/v1/group/")
        for call in adapter.business.request.await_args_list
    )
