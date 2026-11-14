"""Retired chat entries cannot execute, become AI input or restore old settings."""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from astrbot.builtin_stars.wangshangliao_moderation import main, syntax
from astrbot.builtin_stars.wangshangliao_moderation.command_capabilities import (
    CAPABILITIES,
    COMMAND_SPECS,
    command_help,
)
from astrbot.builtin_stars.wangshangliao_moderation.commands import Commands
from astrbot.builtin_stars.wangshangliao_moderation.group_management import (
    MANAGER_COMMANDS,
    management_command,
)
from astrbot.builtin_stars.wangshangliao_moderation.member_blacklist import (
    COMMANDS as BLACKLIST_ENTRIES,
)
from astrbot.builtin_stars.wangshangliao_moderation.open_rpc import OpenCall, OpenRPC
from astrbot.builtin_stars.wangshangliao_moderation.open_service import OpenService
from astrbot.builtin_stars.wangshangliao_moderation.syntax import (
    ALIASES,
    BLACKLIST_COMMANDS,
    BUSINESS_COMMANDS,
    GROUP_KNOWLEDGE_COMMANDS,
    KNOWLEDGE_COMMANDS,
    NO_ARGUMENT,
    REMOVED_GROUP_COMMANDS,
    WITH_ARGUMENT,
    is_removed_group_command,
    recognize_command,
)
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.sources.wangshangliao import blacklist
from astrbot.core.platform.sources.wangshangliao.group_scope import group_revision
from tests.test_wangshangliao_commands import managed_group as managed_group
from tests.test_wangshangliao_plain_commands import native_event

pytestmark = pytest.mark.usefixtures("wsl_active_policy", "wsl_command_classifier")

OLD_COMMANDS = [
    "帮助",
    "群管帮助",
    "help",
    "我的权限",
    "sid",
    "黑名单禁言时长",
    "黑名单禁言时长 1分钟",
    "黑名单禁言时长 24小时",
    "黑名单禁言时长 29天",
    "黑名单禁言时长 0分钟",
    "黑名单禁言时长 1.5天",
]

PRIVATE_COMMANDS = [
    "菜单",
    "私聊菜单",
    "完整菜单",
    "群列表",
    "选择群",
    "客服列表",
    "选择客服",
    "退出客服",
    "客服设置",
    "客服话术",
    "客服风格",
    "客服长度",
    "客服链接",
    "客服知识",
    "添加客服知识",
    "删除客服知识",
    "确认客服",
    "我的知识",
    "查看我的知识",
    "我的知识容量",
    "添加我的知识",
    "修改我的知识",
    "删除我的知识",
    "我的套餐",
    "我的额度",
    "套餐说明",
    "结账",
    "结账结果",
    "确认已付款",
    "确认修改",
    "广告图片",
    "开发门禁",
]


@pytest_asyncio.fixture
async def scope(tmp_path, monkeypatch):
    """Bind temporary blacklist settings without importing retired menu fixtures.

    Args:
        tmp_path: Test-only persistence root.
        monkeypatch: Redirect blacklist storage away from business data.

    Yields:
        Service, event, current roster and isolated blacklist store.
    """
    monkeypatch.setattr(blacklist, "instance_dir", lambda _: tmp_path / "blacklist")
    context = SimpleNamespace(
        get_config=lambda *args: {"admins_id": []},
        platform_manager=SimpleNamespace(platform_insts=[]),
    )
    value = OpenService(
        SimpleNamespace(context=context, commands=Commands()),
        tmp_path / "service",
        integrated=True,
    )
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
    event = native_event("规则")
    adapter = event.platform
    adapter.connection_state = "online"
    adapter.stopping = asyncio.Event()
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
                        group: ["mute", "unmute", "recall"] for group in ("5", "6")
                    },
                },
            },
        )
    )
    rows = {
        uid: {"userId": uid, "nimId": peer, "groupRole": role, "userNick": name}
        for uid, peer, role, name in (
            ("1", "901", "GROUP_ROLE_ADMIN", "Bot"),
            ("2", "902", "GROUP_ROLE_OWNER", "Manager"),
            ("3", "903", "GROUP_ROLE_MEMBER", "Target"),
        )
    }

    async def roster(adapter, group):
        current = copy.deepcopy(rows)
        value.member_evidence[(str(adapter.account), group)] = current
        return current

    value.roster = AsyncMock(side_effect=roster)
    value.member_evidence[("1", "5")] = copy.deepcopy(rows)
    value.adapters = lambda: [adapter]
    try:
        yield value, event, rows, blacklist.BlacklistStore(event.platform.config["id"])
    finally:
        await value.member_blacklist.close()
        event.processing_completion.cancel()


@pytest.mark.parametrize("text", OLD_COMMANDS)
@pytest.mark.parametrize("prefix", ["", "/", "群管 ", "/群管 ", "管理 ", "/管理 "])
def test_removed_commands_are_tombstones_not_executable_syntax(text, prefix):
    assert is_removed_group_command(prefix + text)
    assert recognize_command(prefix + text) == ""
    assert management_command(prefix + text) == ""


def test_removed_names_and_aliases_are_absent_from_all_declarations():
    declared = (
        set(COMMAND_SPECS)
        | set(CAPABILITIES)
        | MANAGER_COMMANDS
        | NO_ARGUMENT
        | WITH_ARGUMENT
        | BLACKLIST_COMMANDS
        | BLACKLIST_ENTRIES
        | set(ALIASES)
        | set(ALIASES.values())
        | {alias for spec in COMMAND_SPECS.values() for alias in spec.aliases}
    )
    assert not REMOVED_GROUP_COMMANDS & declared
    assert not hasattr(main.Main, "moderation_help")
    assert recognize_command("群管") == ""
    assert is_removed_group_command("群管")


def test_private_management_entry_points_do_not_exist():
    assert not hasattr(main.Main, "private_management")
    assert not hasattr(main.Main, "card_tool")
    assert callable(main.Main._moderate)
    assert callable(main.Main.moderation_command)
    assert callable(main.Main.plain_text_request)


@pytest.mark.parametrize("action", PRIVATE_COMMANDS)
def test_private_only_tokens_are_removed_from_syntax_and_declarations(action):
    assert action not in NO_ARGUMENT | WITH_ARGUMENT | BUSINESS_COMMANDS
    assert action not in ALIASES
    assert action not in ALIASES.values()
    assert action not in COMMAND_SPECS
    for prefix in ("", "/", "群管 ", "/群管 "):
        assert recognize_command(prefix + action) == ""
        assert recognize_command(prefix + action + " 123456") == ""


def test_group_knowledge_commands_remain_without_personal_knowledge():
    assert not hasattr(syntax, "PERSONAL_KNOWLEDGE_COMMANDS")
    assert KNOWLEDGE_COMMANDS == GROUP_KNOWLEDGE_COMMANDS
    for command in ("本群知识", "本群知识容量", "本群知识范围"):
        assert recognize_command(command) == command
    for command in ("添加群知识", "修改群知识", "删除群知识", "群知识引用"):
        assert command in WITH_ARGUMENT
        assert COMMAND_SPECS[command].role == "server"
    assert COMMAND_SPECS["禁言"].role == "mute_delegate"


@pytest.mark.parametrize(
    "text",
    [
        "请发帮助",
        "有人说我的权限",
        "helpful",
        "sid123",
        "黑名单禁言时长是多少",
        "帮助\n公告 内容",
        "排名 2",
        None,
    ],
)
def test_retirement_guard_does_not_claim_normal_chat_or_other_commands(text):
    assert not is_removed_group_command(text)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", OLD_COMMANDS)
@pytest.mark.parametrize("integrated", [False, True])
async def test_removed_commands_stop_before_service_model_or_moderation(
    monkeypatch, text, integrated
):
    event = native_event("/群管 " + text)
    event.eligible = True
    event.is_at_or_wake_command = True
    plugin = object.__new__(main.Main)
    plugin.commands = SimpleNamespace(run=AsyncMock())
    plugin.open_service = SimpleNamespace(on_event=AsyncMock()) if integrated else None
    plugin.ai_runtime = SimpleNamespace(customer=AsyncMock())
    model, handler = AsyncMock(), AsyncMock()
    monkeypatch.setattr(main, "assess", model)
    monkeypatch.setattr(main, "handle", handler)
    try:
        await plugin.moderate(event)
        assert event.is_stopped()
        plugin.commands.run.assert_not_awaited()
        plugin.ai_runtime.customer.assert_not_awaited()
        if integrated:
            plugin.open_service.on_event.assert_not_awaited()
        event.platform.send_reply_text.assert_not_awaited()
        event.platform.execute_moderation.assert_not_awaited()
        model.assert_not_awaited()
        handler.assert_not_awaited()
    finally:
        event.processing_completion.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", OLD_COMMANDS)
async def test_cached_dispatch_and_model_requests_also_stop(text):
    event = SimpleNamespace(
        get_platform_name=lambda: "wangshangliao",
        is_private_chat=lambda: False,
        message_str=text,
        stop_event=Mock(),
    )
    plugin = object.__new__(main.Main)
    plugin.open_service = SimpleNamespace(on_event=AsyncMock())
    await plugin.moderation_command(event, text)
    plugin.open_service.on_event.assert_not_awaited()
    request = SimpleNamespace(func_tool="old-tools", tool_calls_result="old-result")
    await plugin.plain_text_request(event, request)
    assert not request.func_tool
    assert request.tool_calls_result is None
    assert event.stop_event.call_count == 2
    assert await Commands().run(event, text) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("text", OLD_COMMANDS)
async def test_retired_blacklist_duration_is_silent_and_preserves_custom_policy(
    scope, text
):
    value, event, _, store = scope
    store.configure("1", "5", 10080, actor="dashboard/test", guard=lambda: None)
    before = store.settings("1", "5")
    drafts = dict(value.member_blacklist.pending)
    value.roster.reset_mock()
    assert await value.member_blacklist.command(event, text) == ""
    assert store.settings("1", "5") == before
    assert value.member_blacklist.pending == drafts
    value.roster.assert_not_awaited()
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_cached_chat_duration_confirmation_cannot_commit(scope):
    value, event, _, store = scope
    store.configure("1", "5", 10080, actor="dashboard/test", guard=lambda: None)
    before = store.settings("1", "5")
    draft = await value.member_blacklist.run(
        event.platform,
        "5",
        "settings_preview",
        minutes=60,
        actor=event.actor_identity.principal,
        session=value.navigation.key(event),
        event=event,
    )
    event.message_obj.message_id = "new-confirmation"
    value.roster.reset_mock()
    assert (
        await value.member_blacklist.command(event, "确认黑名单 " + draft["token"])
        == ""
    )
    assert store.settings("1", "5") == before
    value.roster.assert_not_awaited()
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_webui_duration_preview_and_commit_still_preserve_member_history(scope):
    value, event, _, store = scope
    store.configure("1", "5", 10080, actor="dashboard/test", guard=lambda: None)
    store.set_member(
        "1", "5", "3", "903", "Target", True, event.actor_identity, lambda: None
    )
    before = store.settings("1", "5")
    members = store.read("1", "5")[1]
    value.adapters = lambda: [event.platform]
    rpc = OpenRPC(value)
    payload = {
        "account": "1",
        "group_id": "5",
        "tenant_version": value.tenants[("1", "5")]["version"],
    }
    preview = await rpc.dispatch(
        OpenCall(
            action="member_blacklist",
            actor="dashboard/test",
            payload={**payload, "operation": "settings_preview", "minutes": 60},
        )
    )
    assert store.settings("1", "5") == before
    result = await rpc.dispatch(
        OpenCall(
            action="member_blacklist",
            actor="dashboard/test",
            payload={**payload, "operation": "commit", "token": preview["token"]},
        )
    )
    assert result["saved"] and result["mute_minutes"] == 60
    assert store.settings("1", "5")["mute_minutes"] == 60
    assert store.read("1", "5")[1] == members
    assert store.settings("1", "6")["mute_minutes"] == 41760
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command", ["排名", "排行", "今日排行", "发言排行榜", "排行榜"]
)
@pytest.mark.parametrize("page", ["", " 2", " 99999"])
async def test_ranking_is_public_and_old_pages_cannot_select_later_members(
    managed_group, command, page
):
    engine, event = managed_group
    engine.management = None
    engine.rankings.read = AsyncMock(return_value="top twenty")
    event.platform.config["_open_native_roles"] = {}
    assert await engine.run(event, command + page) == "top twenty"
    engine.rankings.read.assert_awaited_once_with(
        event.platform, "5", "2", page=1, native_mentions=True
    )
    event.platform.get_moderation_members.assert_not_awaited()
    event.platform.execute_moderation.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "alias",
    ["排名", "今日排名", "排行", "今日排行", "发言排行", "发言排行榜", "排行榜"],
)
@pytest.mark.parametrize("page", ["", " 2"])
async def test_integrated_rankings_are_marked_before_dispatch_and_keep_native_format(
    scope, monkeypatch, alias, page
):
    value, event, _, _ = scope
    text = alias + page
    event.message_str = text
    event.get_extra("wangshangliao_payload").update(
        text=text,
        _open_revision=group_revision(event.platform.config, event.get_group_id()),
    )
    event.set_extra("wsl_ranking_result", False)
    reply = "1:Nick  One（有效发言3条）\n2:Nick（有效发言2条）"

    async def run(current, command):
        assert current is event
        assert command == "排名" + page
        assert current.get_extra("wsl_plain_command") is True
        assert current.get_extra("wsl_ranking_result") is True
        return reply

    value.plugin.commands.run = AsyncMock(side_effect=run)
    event.platform.send_reply_text.return_value = "accepted"
    monkeypatch.setattr(AstrMessageEvent, "send", AsyncMock())

    await value.on_event(event)

    value.plugin.commands.run.assert_awaited_once_with(event, "排名" + page)
    assert event.get_extra("wsl_keep_reply") is True
    event.platform.send_reply_text.assert_awaited_once()
    sent = event.platform.send_reply_text.await_args
    assert sent.args[0] == "5"
    assert sent.args[2] == reply
    assert sent.kwargs["auto_recall"] is True
    assert "mentions" not in sent.kwargs
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_integrated_nonranking_commands_clear_a_stale_ranking_mark(scope):
    value, event, _, _ = scope
    event.message_str = "成员列表"
    event.get_extra("wangshangliao_payload").update(
        text="成员列表",
        _open_revision=group_revision(event.platform.config, event.get_group_id()),
    )
    event.set_extra("wsl_ranking_result", True)

    async def run(current, command):
        assert current is event
        assert command == "成员列表"
        assert current.get_extra("wsl_plain_command") is True
        assert current.get_extra("wsl_ranking_result") is False
        return "Members"

    value.manager_command = AsyncMock(side_effect=run)
    event.send = AsyncMock()

    await value.on_event(event)

    value.manager_command.assert_awaited_once_with(event, "成员列表")
    event.send.assert_awaited_once()


def test_webui_guidance_remains_without_retired_command_instructions():
    for location in ("group", "private"):
        text = command_help(location, {"group_manager"}, include_server=True)
        assert "WebUI" in text
        assert "发送「帮助」" not in text
        assert "我的权限" not in text
        assert "黑名单禁言时长" not in text
    for command in ("排名", "今日排名", "排行", "今日排行"):
        assert COMMAND_SPECS[command].role == "public"
