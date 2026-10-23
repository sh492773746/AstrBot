"""Numbered tree navigation, confirmation and shared native command regressions."""

import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.command_menu import (
    ENTRY_WORDS,
    READ_COMMANDS,
    TREE,
    CommandMenu,
    Session,
    overview,
)
from astrbot.builtin_stars.wangshangliao_moderation.open_service import (
    OPEN_COMMANDS,
    recognize,
    recognize_integrated,
)
from astrbot.builtin_stars.wangshangliao_moderation.syntax import (
    PERSONAL_KNOWLEDGE_COMMANDS,
)
from astrbot.core.platform.sources.wangshangliao.group_scope import group_revision
from astrbot.core.platform.sources.wangshangliao.text import plain_text
from tests.test_wangshangliao_command_parity import setup as setup
from tests.test_wangshangliao_plain_commands import native_event

pytestmark = pytest.mark.usefixtures("wsl_active_policy", "wsl_command_classifier")


@pytest.mark.parametrize("with_whitelist", [False, True])
def test_dismiss_preserves_optional_whitelist_and_legacy_menu_cancellation(
    with_whitelist,
):
    owner = SimpleNamespace()
    if with_whitelist:
        owner.member_whitelist = SimpleNamespace(cancel=Mock())
    menu = CommandMenu(owner)
    key = ("bot", "sender", "origin", "group")
    menu.key = lambda event: key
    event = SimpleNamespace(message_obj=SimpleNamespace(message_id="fresh"))
    state = menu.sessions[key] = Session(expires=time.monotonic() + 60, path=[0])
    menu.dismiss(event)
    assert state.closed and not state.path and state.message == "fresh"
    if with_whitelist:
        owner.member_whitelist.cancel.assert_called_once_with(event)


def leaves(items=TREE, path=()):
    """Enumerate every fixed leaf with its actual displayed navigation path.

    Args:
        items: Current menu level.
        path: Parent numeric selections.

    Returns:
        Leaf/path pairs for inventory and reachability tests.
    """
    result = []
    for number, item in enumerate(items, 1):
        if item.children:
            result.extend(leaves(item.children, (*path, number)))
        else:
            result.append((item, (*path, number)))
    return result


async def step(value, event, text, *, native=False, pace=True, full=True):
    """Deliver a fresh menu message without sleeping through platform cooldowns.

    Args:
        value: Isolated service.
        event: Native fixture whose sender and conversation remain unchanged.
        text: Actual user input.
        native: Exercise the full plugin event route, not only navigation.
        pace: Advance only the command cooldown for routine test steps.

    Returns:
        Last emitted text, or an empty string for a consumed duplicate.
    """
    if full and text in ENTRY_WORDS:
        text = "完整菜单"
    if pace:
        value.command_at = {k: v - 4 for k, v in value.command_at.items()}
    event.message_obj.message_id += "/next"
    event.message_str = text
    event.send = AsyncMock()
    payload = event.get_extra("wangshangliao_payload")
    payload["text"] = text
    payload["_open_revision"] = group_revision(
        event.platform.config, event.get_group_id() or ""
    )
    if native:
        await value.on_event(event)
    else:
        await value.navigation.handle(event, text)
    if not event.send.await_count:
        return ""
    result = event.send.call_args.args[0]
    return result if isinstance(result, str) else result.chain[0].text


def test_tree_covers_every_shared_fixed_command_without_new_authority():
    from astrbot.builtin_stars.wangshangliao_moderation.activities import (
        ADMIN_COMMANDS,
        PUBLIC_COMMANDS,
    )

    commands = {item.command for item, _ in leaves()}
    assert (OPEN_COMMANDS - {"帮助"}) | ADMIN_COMMANDS | PUBLIC_COMMANDS <= commands
    assert {"开通", "认领", "/chat", "/exit", "排名", "开发门禁"} <= commands
    assert not any("好友" in command for command in commands)
    assert "0 返回" in overview() and "取消" in overview()
    assert "邀请查询" not in overview(False)


@pytest.mark.parametrize("command", ["开启自主接话", "关闭自主接话"])
def test_autonomous_commands_use_the_native_classifier_and_preserve_menu_numbers(
    command,
):
    from astrbot.builtin_stars.wangshangliao_moderation.syntax import recognize_command

    assert recognize_command(command) == command
    assert recognize_integrated(command) == command
    entries = {path: item for item, path in leaves()}
    assert entries[(9, 8, 1)].command == "开启自主接话"
    assert entries[(9, 8, 2)].command == "关闭自主接话"
    assert entries[(9, 7, 1)].command == "本群知识"
    assert entries[(9, 3, 1)].command == "开启群聊回复"


@pytest.mark.parametrize("number", ["0", "1", "100", "999"])
def test_menu_numbers_are_admitted_without_becoming_an_executable_command(number):
    assert recognize(number) in {"0", "菜单选择"}
    assert recognize_integrated(number) in {"0", "菜单选择"}


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", sorted(ENTRY_WORDS))
async def test_menu_entries_start_the_same_numbered_tree(setup, entry):
    value, _, event = setup
    reply = await step(value, event, entry, native=True)
    assert "├─ 1." in reply and "└─ 10." in reply
    assert "0 返回" in reply and "取消" in reply
    assert value.navigation.sessions[value.navigation.key(event)].path == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entry",
    ["帮助", "群管帮助", "help", "/帮助", "/help", "管理 帮助", "/管理 帮助"],
)
@pytest.mark.parametrize("private", [False, True])
async def test_help_menu_keeps_tree_and_shared_permission_catalog(
    setup, entry, private
):
    value, _, template = setup
    event = native_event(entry, private=private)
    event.platform = template.platform
    value.chat = AsyncMock()
    value.group_ai.observe = AsyncMock()
    reply = await step(value, event, entry, native=True, full=False)
    assert "【命令菜单】" in reply
    assert "本群状态" in reply and "插件设置" in reply
    assert "查看定时状态" not in reply
    assert "独立财务角色：" not in reply
    value.chat.assert_not_awaited()
    value.group_ai.observe.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
async def test_exact_displayed_labels_select_without_ai_or_unconfirmed_writes(
    setup, private
):
    value, _, template = setup
    event = native_event("菜单", private=private)
    event.platform = template.platform
    if private:
        value.verify_actor = AsyncMock()
    value.chat = AsyncMock()
    value.group_ai.observe = AsyncMock()
    value.menu_command = AsyncMock()
    await step(value, event, "菜单", native=True)
    assert "命令菜单 > 开通与身份" in await step(
        value, event, "开通与身份", native=True
    )
    await step(value, event, "0", native=True)
    await step(value, event, "审核规则", native=True)
    await step(value, event, "广告审核", native=True)
    assert "即将执行：关闭广告审核" in await step(
        value, event, "关闭广告审核", native=True
    )
    value.menu_command.assert_not_awaited()
    assert value.tenants[("1", "5")]["rules"]["advertising"]
    assert event.get_extra("_component_claimed")
    value.chat.assert_not_awaited()
    value.group_ai.observe.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_root_label_reopens_a_lost_session_without_executing(setup):
    value, _, template = setup
    event = native_event("开通与身份", private=True)
    event.platform = template.platform
    value.chat = AsyncMock()
    value.menu_command = AsyncMock()
    reply = await step(value, event, "开通与身份", native=True)
    assert "命令菜单 > 开通与身份" in reply
    assert "开通本群" in reply
    value.chat.assert_not_awaited()
    value.menu_command.assert_not_awaited()
    value.plugin.private_interaction.handle.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "private,text",
    [
        (False, "1.2.3.4.5.6.7.8.9.10."),
        (False, "999"),
        (True, "1.2.3.4.5.6.7.8.9.10."),
        (True, "在吗"),
        (True, "审核"),
        (True, "999"),
    ],
)
async def test_invalid_active_menu_input_returns_feedback_without_ai(
    setup, private, text
):
    value, _, template = setup
    event = native_event("菜单", private=private)
    event.platform = template.platform
    value.chat = AsyncMock()
    value.group_ai.observe = AsyncMock()
    value.menu_command = AsyncMock()
    await step(value, event, "菜单", native=True)
    reply = await step(value, event, text, native=True)
    assert "请按本页的数字选择" in reply
    assert "命令菜单" in reply
    assert event.get_extra("_component_claimed")
    value.chat.assert_not_awaited()
    value.group_ai.observe.assert_not_awaited()
    value.menu_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_ordinary_text_still_reaches_keywords_during_menu_navigation(setup):
    value, _, event = setup
    await step(value, event, "菜单", native=True)
    value.rule_replies.observe = AsyncMock(return_value=True)
    value.group_ai.observe = AsyncMock()
    value.menu_command = AsyncMock()
    assert not value.navigation.accepts(event, "我要链接")
    assert await step(value, event, "我要链接", native=True) == ""
    value.rule_replies.observe.assert_awaited_once_with(event)
    value.group_ai.observe.assert_awaited_once_with(event, consumed=True)
    value.menu_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_private_confirmation_only_reopens_the_root(setup):
    value, _, template = setup
    event = native_event("菜单", private=True)
    event.platform = template.platform
    await step(value, event, "菜单", native=True)
    state = value.navigation.sessions[value.navigation.key(event)]
    state.confirm, state.preview_message = "关闭防刷屏", "old"
    state.expires = 0
    value.chat = AsyncMock()
    value.menu_command = AsyncMock()
    reply = await step(value, event, "1", native=True)
    assert "菜单已失效" in reply
    assert "【命令菜单】" in reply
    state = value.navigation.sessions[value.navigation.key(event)]
    assert not state.confirm and not state.path
    value.chat.assert_not_awaited()
    value.menu_command.assert_not_awaited()
    value.plugin.cashback.private_invitation_reply.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
async def test_rules_only_menu_hides_ai_without_relabeling_business_rules_as_ai(
    setup, private
):
    value, _, template = setup
    with value.store.connect(write=True) as db:
        db.execute("UPDATE tenants SET rules_only=1 WHERE account='1'")
    await value.refresh()
    event = native_event("菜单", private=private)
    event.platform = template.platform
    value.chat = AsyncMock()
    value.plan_command = AsyncMock(side_effect=AssertionError("not an AI branch"))
    reply = await step(value, event, "菜单", native=True)
    assert "私聊AI咨询" not in reply and "群聊与热群AI" not in reply
    assert "群关键词回复与定时广告" in reply
    reply = await step(value, event, "群关键词回复与定时广告", native=True)
    assert "添加回复" in reply and "添加广告" in reply
    assert "【AI套餐说明】" not in reply
    value.plan_command.assert_not_awaited()
    value.chat.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["3", "4"])
async def test_plan_selection_does_not_repeat_plan_details(setup, choice):
    value, _, template = setup
    event = native_event("菜单", private=True)
    event.platform = template.platform
    await step(value, event, "菜单")
    await step(value, event, "8")
    reply = await step(value, event, choice)
    assert reply.count("【AI套餐说明】") == 1
    assert "Plus：20 USDT" in reply


@pytest.mark.asyncio
async def test_nested_back_and_cancel_never_execute_operations(setup):
    value, _, event = setup
    value.menu_command = AsyncMock()
    await step(value, event, "菜单")
    assert "审核规则" in await step(value, event, "3")
    assert "开启广告审核" in await step(value, event, "2")
    assert "即将执行" in await step(value, event, "2")
    assert "审核规则 > 广告审核" in await step(value, event, "0")
    assert "查看规则与关键词" in await step(value, event, "0")
    assert "已取消" in await step(value, event, "取消")
    assert "已退出" in await step(value, event, "1")
    value.menu_command.assert_not_awaited()
    assert "├─ 1." in await step(value, event, "菜单")


@pytest.mark.asyncio
@pytest.mark.parametrize("backend_preview", [False, True])
async def test_native_text_rendering_keeps_confirmation_zero_as_zero(
    setup, backend_preview
):
    value, _, event = setup
    if backend_preview:
        value.menu_command = AsyncMock(return_value="确认踢出 abcdef123456")
        selections = ("菜单", "4", "1", "1", "1")
    else:
        value.menu_command = AsyncMock()
        selections = ("菜单", "3", "2", "2")
    for text in selections:
        preview = await step(value, event, text)
    rendered = plain_text(preview)
    assert "1. 确认执行" in rendered
    assert "0. 放弃并返回" in rendered
    assert "2. 放弃并返回" not in rendered
    assert "0. 放弃并返回" in plain_text(rendered)
    await step(value, event, "0")
    assert not value.navigation.sessions[value.navigation.key(event)].confirm


@pytest.mark.asyncio
async def test_mutation_requires_fresh_numeric_confirmation_then_real_grant(setup):
    value, _, event = setup
    await step(value, event, "菜单", native=True)
    await step(value, event, "3", native=True)
    await step(value, event, "2", native=True)
    assert "尚未执行" not in await step(value, event, "2", native=True)
    assert value.store.tenant("1", "5")["rules"]["advertising"]
    assert "已保存" in await step(value, event, "1", native=True)
    assert not value.store.tenant("1", "5")["rules"]["advertising"]
    assert value.store.tenant("1", "6")["rules"]["advertising"]


@pytest.mark.asyncio
async def test_revoked_grant_cannot_use_a_prepared_confirmation(setup):
    value, _, event = setup
    await step(value, event, "菜单")
    await step(value, event, "3")
    await step(value, event, "2")
    await step(value, event, "2")
    value.management.assignments[value.management.key(event.platform, "5")] = ()
    reply = await step(value, event, "1")
    assert "无权限" in reply
    assert value.store.tenant("1", "5")["rules"]["advertising"]


@pytest.mark.asyncio
async def test_changed_version_cancels_confirmation_and_cannot_skip_next_preview(setup):
    value, _, event = setup
    await step(value, event, "菜单")
    await step(value, event, "3")
    await step(value, event, "2")
    await step(value, event, "2")
    value.tenants[("1", "5")]["version"] += 1
    assert "已变化" in await step(value, event, "1")
    assert "即将执行" in await step(value, event, "2")
    assert value.store.tenant("1", "5")["rules"]["advertising"]


@pytest.mark.asyncio
async def test_member_wizard_uses_current_native_snapshot_and_backend_kick_confirmation(
    setup,
):
    value, _, event = setup
    await step(value, event, "菜单", native=True)
    await step(value, event, "4", native=True)
    assert "成员序号" in await step(value, event, "1", native=True)
    assert "即将执行：踢出 3" in await step(value, event, "3", native=True)
    preview = await step(value, event, "1", native=True)
    assert "1. 确认执行" in preview and "确认踢出 " not in preview
    event.platform.execute_action.assert_not_awaited()
    assert "已受理" in await step(value, event, "1", native=True)
    assert event.platform.execute_action.await_count == 1


@pytest.mark.asyncio
async def test_plain_announcement_is_data_not_a_second_command(setup):
    value, _, event = setup
    await step(value, event, "菜单", native=True)
    await step(value, event, "5", native=True)
    await step(value, event, "1", native=True)
    assert "输入格式不正确" in await step(value, event, "hello\n全员禁言", native=True)
    assert "即将执行：公告 hello" in await step(value, event, "hello", native=True)
    assert "服务端已接受" in await step(value, event, "1", native=True)
    request = event.platform.execute_action.call_args.args[0]
    assert request.action == "announce" and request.text == "hello"


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["group", "peer", "account", "conversation"])
async def test_navigation_isolated_by_real_sender_account_group_and_conversation(
    setup, scope
):
    value, _, event = setup
    await step(value, event, "菜单")
    await step(value, event, "3")
    await step(value, event, "2")
    await step(value, event, "2")
    old = value.navigation.key(event)
    if scope == "group":
        event.message_obj.group_id = "6"
    elif scope == "peer":
        event.actor_identity = replace(event.actor_identity, peer="other-peer")
    elif scope == "account":
        event.platform.account = "other-account"
        event.actor_identity = replace(event.actor_identity, account="other-account")
    else:
        event.unified_msg_origin += "/other"
    value.menu_command = AsyncMock()
    await step(value, event, "1")
    value.menu_command.assert_not_awaited()
    assert value.navigation.sessions[old].confirm


@pytest.mark.asyncio
async def test_deduplicated_confirmation_cannot_execute_twice(setup):
    value, _, event = setup
    await step(value, event, "菜单")
    await step(value, event, "3")
    await step(value, event, "2")
    await step(value, event, "2")
    value.menu_command = AsyncMock(return_value="saved")
    await step(value, event, "1")
    await value.navigation.handle(event, "1")
    value.menu_command.assert_awaited_once()


@pytest.mark.asyncio
async def test_number_selection_does_not_become_someone_elses_financial_query(setup):
    value, _, event = setup
    event.is_private_chat = lambda: True
    event.get_group_id = lambda: ""
    event.eligible = True
    value.verify_actor = AsyncMock()
    await step(value, event, "菜单", native=True)
    await step(value, event, "1", native=True)
    value.plugin.cashback.private_invitation_reply.assert_not_awaited()
    await step(value, event, "0", native=True)
    await step(value, event, "7", native=True)
    assert "本人返现" in await step(value, event, "1", native=True)
    assert "本人返现" in await step(value, event, "1", native=True)
    assert value.plugin.cashback.private_invitation_reply.await_count == 2
    await step(value, event, "取消", native=True)
    await step(value, event, "1", native=True)
    assert value.plugin.cashback.private_invitation_reply.await_count == 2
    value.plugin.private_interaction.handle.assert_awaited_once()


@pytest.mark.asyncio
async def test_existing_direct_invitation_numeric_selection_without_tree_still_works(
    setup,
):
    value, _, event = setup
    event.is_private_chat = lambda: True
    event.get_group_id = lambda: ""
    event.eligible = True
    value.verify_actor = AsyncMock()
    await step(value, event, "1", native=True)
    value.plugin.cashback.private_invitation_reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_direct_chat_control_cancels_pending_menu_not_interpreted_as_keyword(
    setup,
):
    value, _, event = setup
    await step(value, event, "菜单")
    await step(value, event, "3")
    await step(value, event, "4")
    assert not await value.navigation.handle(event, "/chat")
    assert not value.navigation.sessions
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_menu_sessions_and_private_navigation_do_not_bypass_command_cooldown(
    setup,
    monkeypatch,
):
    from astrbot.builtin_stars.wangshangliao_moderation import open_service

    value, _, event = setup
    trace = Mock()
    monkeypatch.setattr(open_service, "record_event", trace)
    await step(value, event, "菜单", native=True)
    await step(value, event, "3", native=True)
    await step(value, event, "1", native=True)
    assert await step(value, event, "1", native=True, pace=False) == ""
    trace.assert_called_once_with(event, "menu", "cooldown")


@pytest.mark.asyncio
async def test_menu_state_is_bounded_and_expired_confirmations_are_not_replayed(setup):
    value, _, event = setup
    value.navigation.sessions = {
        ("1", str(n), "session", "5"): Session(time.monotonic() + 600)
        for n in range(4096)
    }
    assert "繁忙" in await step(value, event, "菜单")
    value.navigation.sessions.clear()
    await step(value, event, "菜单")
    state = value.navigation.sessions[value.navigation.key(event)]
    state.confirm, state.preview_message = "关闭防刷屏", "old"
    state.expires = 0
    value.menu_command = AsyncMock()
    await step(value, event, "1")
    value.menu_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_member_wizard_can_page_without_turning_page_number_into_a_target(setup):
    value, _, event = setup
    value.menu_command = AsyncMock(return_value="member page")
    await step(value, event, "菜单")
    await step(value, event, "4")
    assert "99. 下一页" in await step(value, event, "1")
    await step(value, event, "99")
    value.menu_command.assert_awaited_with(event, "下一页")
    assert "即将执行：踢出 3" in await step(value, event, "3")
    value.menu_command.assert_awaited_with(event, "下一页")


def test_only_member_unmute_is_restored_and_other_manual_mutes_stay_retired():
    from astrbot.builtin_stars.wangshangliao_moderation.syntax import (
        RETIRED_CHAT_ACTIONS,
    )

    commands = {item.command for item, _ in leaves()}
    assert not commands & RETIRED_CHAT_ACTIONS
    assert not OPEN_COMMANDS & RETIRED_CHAT_ACTIONS
    assert "解禁" in commands & OPEN_COMMANDS
    assert TREE[3].children[6].command == "解禁"
    assert {"定时禁言", "确认定时", "定时状态", "恢复定时"} <= commands
    assert "解除时间" in TREE[4].children[1].label


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("prefix", ["", "管理 ", "/管理 ", "群管 "])
@pytest.mark.parametrize("command", ["禁言 @Target 5", "全员禁言", "解除全员禁言"])
async def test_retired_chat_mutes_only_explain_app_without_executing(
    setup, private, prefix, command
):
    value, engine, event = setup
    if private:
        event.is_private_chat = lambda: True
        event.get_group_id = lambda: ""
        event.eligible = True
        value.verify_actor = AsyncMock()
    engine.run = AsyncMock(return_value="should never execute")
    reply = await step(value, event, prefix + command, native=True)
    assert "旺商聊App" in reply
    assert "定时解除" in reply
    engine.run.assert_not_awaited()
    event.platform.execute_action.assert_not_awaited()
    assert await step(value, event, prefix + command, native=True, pace=False) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("help_entry", [False, True])
async def test_legacy_registered_handlers_delegate_to_current_public_dispatch(
    setup, help_entry
):
    from astrbot.builtin_stars.wangshangliao_moderation.main import Main

    value, engine, event = setup
    plugin = object.__new__(Main)
    plugin.open_service = value
    plugin.commands = engine
    engine.run = AsyncMock()
    event.send = AsyncMock()
    event.message_str = "群管帮助" if help_entry else "群管 禁言 @Target 5"
    payload = event.get_extra("wangshangliao_payload")
    payload["text"] = event.message_str
    payload["_open_revision"] = group_revision(
        event.platform.config, event.get_group_id()
    )
    if help_entry:
        await plugin.moderation_help(event)
        result = event.send.call_args.args[0].chain[0].text
        assert "命令菜单" in result
    else:
        await plugin.moderation_command(event, "禁言 @Target 5")
        result = event.send.call_args.args[0].chain[0].text
        assert "旺商聊App" in result
    engine.run.assert_not_awaited()
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_menu_controls_only_selected_authorized_group(setup):
    value, _, event = setup
    event.is_private_chat = lambda: True
    event.get_group_id = lambda: ""
    event.eligible = True
    value.verify_actor = AsyncMock()
    for text in ("菜单", "2", "1", "1"):
        await step(value, event, text, native=True)
    reply = await step(value, event, "开启冷场发话", native=True)
    assert "服务端维护" in reply
    assert not value.group_ai.configs
    assert ("1", "6") not in value.group_ai.configs


@pytest.mark.asyncio
async def test_group_ai_modes_are_server_owned_even_through_fixed_commands(
    setup,
):
    value, _, event = setup
    from astrbot.builtin_stars.wangshangliao_moderation.group_ai import MODE_COMMANDS

    for command in MODE_COMMANDS:
        assert "服务端维护" in await step(value, event, command, native=True)
        assert not value.group_ai.configs
        assert ("1", "6") not in value.group_ai.configs
        assert not value.budget["confirmed"]
        event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_enabling_idle_mode_alone_does_not_enable_group_ai(setup):
    value, _, event = setup
    assert "服务端维护" in await step(value, event, "开启冷场发话", native=True)
    assert not value.group_ai.configs


@pytest.mark.asyncio
async def test_pending_group_ai_confirmation_is_invalid_after_other_config_save(setup):
    from astrbot.builtin_stars.wangshangliao_moderation.group_ai import DEFAULT

    value, _, event = setup
    for text in ("菜单", "9", "2", "1"):
        await step(value, event, text, native=True)
    tenant = value.store.tenant("1", "5")
    value.group_ai.configure(
        "1",
        "5",
        0,
        tenant["version"],
        DEFAULT,
        "dashboard/test",
        guard=lambda: None,
        control=True,
    )
    await value.group_ai.refresh()
    assert "已变化" in await step(value, event, "1", native=True)
    assert value.group_ai.configs[("1", "5")]["version"] == 1
    assert not value.group_ai.configs[("1", "5")]["config"]["idle"]


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
async def test_other_group_manager_cannot_enable_idle_topics(setup, private):
    value, _, event = setup
    value.management.assignments[value.management.key(event.platform, "5")] = ()
    if private:
        event.is_private_chat = lambda: True
        event.get_group_id = lambda: ""
        event.eligible = True
        value.verify_actor = AsyncMock()
    reply = await step(value, event, "开启冷场发话", native=True)
    assert "无权限" in reply or "先发送" in reply
    assert not value.group_ai.configs


@pytest.mark.asyncio
async def test_zero_is_always_back_and_unlimited_is_an_explicit_value(setup):
    value, _, event = setup
    value.menu_command = AsyncMock(return_value="saved")
    for text in ("菜单", "6", "4", "4"):
        await step(value, event, text)
    assert "抽奖设置" in await step(value, event, "0")
    value.menu_command.assert_not_awaited()
    await step(value, event, "4")
    assert "即将执行：参与上限 0" in await step(value, event, "不限")
    await step(value, event, "1")
    value.menu_command.assert_awaited_once_with(event, "参与上限 0")


@pytest.mark.asyncio
async def test_private_management_group_selection_is_not_invitation_selection(setup):
    value, engine, event = setup
    event.is_private_chat = lambda: True
    event.get_group_id = lambda: ""
    event.eligible = True
    value.verify_actor = AsyncMock()
    for text in ("菜单", "2", "1"):
        await step(value, event, text, native=True)
    assert "当前已选群" in await step(value, event, "2", native=True)
    key = (
        id(event.platform),
        event.platform.account,
        event.get_sender_id(),
        event.unified_msg_origin,
    )
    assert engine.selections[key]["group"] == "6"
    value.plugin.cashback.private_invitation_reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_parameter_is_not_rejected_as_a_second_ai_question(setup):
    value, _, event = setup
    for text in ("菜单", "5", "1"):
        await step(value, event, text, native=True)
    event.get_extra("wangshangliao_payload")["_ai_busy"] = True
    assert "即将执行：公告 Notice" in await step(value, event, "Notice", native=True)
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_invitation_details_preserve_numbered_detail_mode(setup):
    value, _, event = setup
    event.is_private_chat = lambda: True
    event.get_group_id = lambda: ""
    event.eligible = True
    value.verify_actor = AsyncMock()
    requested = []

    async def read(current):
        requested.append(current.message_str)
        return "records"

    value.plugin.cashback.private_invitation_reply.side_effect = read
    for text in ("菜单", "7", "2", "1"):
        await step(value, event, text, native=True)
    assert requested == ["我的邀请 明细", "我的邀请 1 明细"]


@pytest.mark.asyncio
async def test_private_activation_code_verifies_membership_in_its_actual_group(setup):
    value, _, event = setup
    event.is_private_chat = lambda: True
    event.get_group_id = lambda: ""
    event.eligible = True
    value.verify_actor = AsyncMock()
    value.activate = AsyncMock(return_value="activated")
    assert await value.menu_command(event, "认领 6 abcdef123456") == "activated"
    value.verify_actor.assert_awaited_once_with(event, "6")
    value.activate.assert_awaited_once_with(event, "6", "abcdef123456")


@pytest.mark.asyncio
async def test_read_results_cannot_inject_a_confirmation_command(setup):
    value, _, event = setup
    value.menu_command = AsyncMock(return_value="关键词：确认授权 abcdef123456")
    await step(value, event, "菜单")
    await step(value, event, "3")
    await step(value, event, "1")
    state = value.navigation.sessions[value.navigation.key(event)]
    assert not state.confirm
    await step(value, event, "2")
    assert not state.confirm and state.path == [2, 1]


@pytest.mark.asyncio
async def test_cancel_while_a_read_is_in_flight_does_not_restore_pending_confirmation(
    setup,
):
    value, _, event = setup
    arrived, release = asyncio.Event(), asyncio.Event()

    async def slow(*_):
        arrived.set()
        await release.wait()
        return "确认踢出 abcdef123456"

    await step(value, event, "菜单")
    await step(value, event, "3")
    value.menu_command = AsyncMock(side_effect=slow)
    task = asyncio.create_task(step(value, event, "1"))
    await arrived.wait()
    cancel = native_event("取消")
    cancel.platform = event.platform
    cancel.message_obj.message_id = "cancel-message"
    cancel.send = AsyncMock()
    assert await value.navigation.handle(cancel, "取消")
    cancel.send.assert_not_awaited()
    assert value.navigation.sessions[value.navigation.key(cancel)].closed
    release.set()
    assert await task == ""
    state = value.navigation.sessions[value.navigation.key(event)]
    assert state.closed and not state.confirm and not state.pending


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "item,path", leaves(), ids=lambda v: v.command if hasattr(v, "command") else str(v)
)
@pytest.mark.parametrize("selection", ["number", "label"])
async def test_every_visible_leaf_is_reachable_through_native_menu_choices(
    setup, item, path, selection
):
    value, _, event = setup
    if item.command in PERSONAL_KNOWLEDGE_COMMANDS | {
        "客服列表",
        "选择客服",
        "退出客服",
    }:
        private = native_event("菜单", private=True)
        private.platform = event.platform
        event = private
    visible = leaves(value.navigation.items_for(event, full=True))
    found = next((choices for leaf, choices in visible if leaf == item), None)
    if found is None:
        from astrbot.builtin_stars.wangshangliao_moderation.command_capabilities import (
            CAPABILITIES,
        )

        assert CAPABILITIES[item.command.split(" ", 1)[0]]["role"] in {
            "server",
            "finance",
            "support_editor",
        }
        return
    path = found
    value.menu_command = AsyncMock(
        return_value="【当前禁言名单】\n1. Fixture"
        if item.command == "解禁"
        else "fixture"
    )
    value.chat = AsyncMock()
    value.group_ai.observe = AsyncMock()
    await step(value, event, "菜单", native=True)
    items = value.navigation.sessions[value.navigation.key(event)].items
    for choice in path:
        selected = items[choice - 1]
        text = str(choice) if selection == "number" else selected.label
        assert value.navigation.accepts(event, text)
        assert await step(value, event, text, native=True)
        items = selected.children
    assert event.get_extra("_component_claimed")
    value.chat.assert_not_awaited()
    value.group_ai.observe.assert_not_awaited()
    state = value.navigation.sessions[value.navigation.key(event)]
    if item.command == "认领":
        assert not state.pending
        value.menu_command.assert_not_awaited()
    elif item.prompt:
        assert state.pending == item
    elif item.command not in READ_COMMANDS | {
        "我的权限",
        "我的套餐",
        "套餐说明",
        "本群额度",
        "能力",
        "群列表",
        "成员列表",
        "下一页",
        "排名",
        "规则",
        "违规计数",
        "开发门禁",
        "管理员列表",
        "热群状态",
        "定时状态",
        "抽奖状态",
        "抽奖设置",
        "中奖名单",
        "抽奖记录",
        "我的邀请",
        "我的邀请明细",
        "邀请奖励",
        "邀请统计说明",
        "邀请统计",
        "邀请记录",
        "邀请奖励状态",
        "/chat",
        "/exit",
    }:
        assert state.confirm == item.command
    else:
        value.menu_command.assert_awaited_with(event, item.command)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", ["accepted", "verified", "rejected", "unknown", "not_sent"]
)
async def test_menu_reply_receipts_are_observable_without_retry(
    setup, monkeypatch, outcome
):
    from astrbot.builtin_stars.wangshangliao_moderation import command_menu

    value, _, event = setup
    trace = Mock()
    monkeypatch.setattr(command_menu, "record_event", trace)
    event.set_extra("_wsl_native_reply_result", outcome)
    assert await step(value, event, "菜单")
    event.send.assert_awaited_once()
    trace.assert_any_call(event, "menu", "claimed")
    trace.assert_any_call(
        event,
        "menu_reply",
        outcome,
        failed=outcome in {"rejected", "unknown"},
        error="result_unknown" if outcome == "unknown" else "",
        aggregate=False,
    )


@pytest.mark.asyncio
async def test_menu_transport_exception_stays_claimed_and_never_retries_or_calls_ai(
    setup, monkeypatch
):
    from astrbot.builtin_stars.wangshangliao_moderation import command_menu

    value, _, event = setup
    trace = Mock()
    monkeypatch.setattr(command_menu, "record_event", trace)
    event.message_str = "菜单"
    event.send = AsyncMock(side_effect=TimeoutError("ambiguous delivery"))
    value.chat = AsyncMock()
    value.group_ai.observe = AsyncMock()
    with pytest.raises(TimeoutError):
        await value.on_event(event)
    assert event.get_extra("_component_claimed")
    event.send.assert_awaited_once()
    value.chat.assert_not_awaited()
    value.group_ai.observe.assert_not_awaited()
    trace.assert_any_call(
        event,
        "menu_reply",
        "unknown",
        failed=True,
        error="result_unknown",
        aggregate=False,
    )


@pytest.mark.asyncio
async def test_menu_duplicate_and_busy_inputs_are_diagnosed_without_resending(
    setup, monkeypatch
):
    from astrbot.builtin_stars.wangshangliao_moderation import command_menu

    value, _, event = setup
    trace = Mock()
    monkeypatch.setattr(command_menu, "record_event", trace)
    await step(value, event, "菜单")
    event.send.reset_mock()
    assert await value.navigation.handle(event, "菜单")
    trace.assert_any_call(event, "menu", "duplicate")
    event.send.assert_not_awaited()
    state = value.navigation.sessions[value.navigation.key(event)]
    state.busy = True
    assert not await step(value, event, "1")
    trace.assert_any_call(event, "menu", "busy")
    event.send.assert_not_awaited()
