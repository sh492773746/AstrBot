"""Individual chat unmute uses existing native identity and authorization gates."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from astrbot.builtin_stars.wangshangliao_moderation.open_service import (
    recognize_integrated,
)
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from tests import test_wangshangliao_command_parity
from tests.test_wangshangliao_command_menu import step

pytestmark = pytest.mark.usefixtures("wsl_active_policy", "wsl_command_classifier")
base_setup = test_wangshangliao_command_parity.setup


@pytest_asyncio.fixture
async def setup(base_setup):
    """Provide a native mute response separate from the complete member roster.

    Args:
        base_setup: Two isolated groups with a verified administrator.

    Returns:
        Service, command engine and native event.
    """
    base_setup[2].platform.nim = SimpleNamespace(
        get_team_muted_members=AsyncMock(return_value={"903"})
    )
    base_setup[2].platform.business = SimpleNamespace(
        request=AsyncMock(
            return_value=base_setup[2].platform.get_moderation_members.return_value
        )
    )
    return base_setup


@pytest.mark.parametrize("prefix", ["", "/", "管理 ", "/管理 ", "群管 "])
@pytest.mark.parametrize("action", ["解禁", "解除禁言"])
@pytest.mark.parametrize("target", [" @Target", "@Target", " 3"])
def test_unmute_spellings_share_the_fixed_action(prefix, action, target):
    assert recognize_integrated(prefix + action + target) == "解禁 " + target.strip()


@pytest.mark.parametrize(
    "text",
    [
        "解除禁言 0",
        "解除禁言 3 5",
        "解除禁言 Target",
        "解除禁言 3\n公告 x",
        "解除禁言 3\u2028公告 x",
    ],
)
def test_unmute_alias_keeps_bounded_member_syntax(text):
    assert not recognize_integrated(text)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["解禁", "解除禁言", "管理 解除禁言"])
@pytest.mark.parametrize("space", ["", " "])
async def test_native_group_unmute_requires_and_resolves_a_real_mention(
    setup, action, space
):
    value, _, event = setup
    command = action + space + "@Target"
    payload = event.get_extra("wangshangliao_payload")
    payload["mentions"] = ["903"]
    start = len((action + space).encode("utf-16-le")) // 2
    payload["mention_spans"] = [
        {"uid": "903", "nick": "Target", "start": start, "end": start + 7}
    ]
    assert "解禁已受理" in await step(value, event, command, native=True)
    request = event.platform.execute_action.call_args.args[0]
    assert request.action == "unmute"
    assert str(request.group) == "5" and str(request.member) == "3"
    event.platform.execute_action.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["typed-name", "missing-span", "extra-text", "two-peers"]
)
async def test_unmute_does_not_guess_a_target_from_text(setup, kind):
    value, _, event = setup
    payload = event.get_extra("wangshangliao_payload")
    command = "解禁 @Target"
    if kind != "typed-name":
        payload["mentions"] = ["903"]
    if kind in {"extra-text", "two-peers"}:
        payload["mention_spans"] = [
            {"uid": "903", "nick": "Target", "start": 3, "end": 10}
        ]
    if kind == "extra-text":
        command += " 顺便踢出"
    elif kind == "two-peers":
        payload["mentions"].append("902")
    assert await step(value, event, command, native=True)
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
async def test_number_menu_unmute_previews_then_submits_only_one_scoped_member(
    setup, private
):
    value, _, event = setup
    if private:
        event.is_private_chat = lambda: True
        event.get_group_id = lambda: ""
        event.eligible = True
        value.verify_actor = AsyncMock()
        await step(value, event, "群列表", native=True)
        await step(value, event, "选择群 1", native=True)
    for text in ("菜单", "4"):
        await step(value, event, text, native=True)
    reply = await step(value, event, "7", native=True)
    assert ("群序号" if private else "成员序号") in reply
    if private:
        assert "当前禁言名单" in await step(value, event, "1", native=True)
    assert "即将执行：解禁 1" in await step(value, event, "1", native=True)
    event.platform.execute_action.assert_not_awaited()
    assert "解禁已受理" in await step(value, event, "1", native=True)
    request = event.platform.execute_action.call_args.args[0]
    assert request.action == "unmute"
    assert str(request.group) == "5" and str(request.member) == "3"
    event.platform.execute_action.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revoke", "cancel", "group", "sender", "version"])
async def test_prepared_unmute_cannot_survive_authority_or_conversation_changes(
    setup, change
):
    value, _, event = setup
    for text in ("菜单", "4", "7", "1"):
        await step(value, event, text, native=True)
    if change == "revoke":
        value.management.assignments[value.management.key(event.platform, "5")] = ()
    elif change == "cancel":
        await step(value, event, "取消", native=True)
    elif change == "group":
        event.message_obj.group_id = "6"
    elif change == "sender":
        event.actor_identity = replace(
            event.actor_identity, peer="903", claimed_uid="3", verified_uid="3"
        )
        event.message_obj.sender.user_id = "3"
    else:
        value.tenants[("1", "5")]["version"] += 1
    await step(value, event, "1", native=True)
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
async def test_unmute_rejects_ordinary_users_and_cross_group_managers(setup, private):
    value, _, event = setup
    value.management.assignments[value.management.key(event.platform, "5")] = ()
    if private:
        event.is_private_chat = lambda: True
        event.get_group_id = lambda: ""
        event.eligible = True
        value.verify_actor = AsyncMock()
        assert "Group 5" not in await step(value, event, "群列表", native=True)
    await step(value, event, "解禁 3", native=True)
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_unmute_rechecks_native_bot_privileges(setup):
    value, _, event = setup
    members = event.platform.get_moderation_members.return_value["groupMemberInfo"]
    members[0]["groupRole"] = "GROUP_ROLE_MEMBER"
    await step(value, event, "解禁 3", native=True)
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_unmute_needs_a_current_member_selection(setup):
    value, _, event = setup
    event.is_private_chat = lambda: True
    event.get_group_id = lambda: ""
    event.eligible = True
    value.verify_actor = AsyncMock()
    await step(value, event, "群列表", native=True)
    await step(value, event, "选择群 1", native=True)
    assert "禁言名单编号无效" in await step(value, event, "解禁 3", native=True)
    await step(value, event, "成员列表", native=True)
    await step(value, event, "群列表", native=True)
    assert "Group 6" in await step(value, event, "选择群 2", native=True)
    assert "禁言名单编号无效" in await step(value, event, "解禁 3", native=True)
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_unmute_revocation_during_member_lookup_prevents_submission(setup):
    value, _, event = setup
    members = event.platform.get_moderation_members.return_value

    async def revoke(_):
        value.management.assignments[value.management.key(event.platform, "5")] = ()
        return members

    event.platform.get_moderation_members.side_effect = revoke
    await step(value, event, "解禁 3", native=True)
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_unmute_transports_the_guard_and_ai_still_cannot_execute(setup):
    value, engine, event = setup
    for text in ("菜单", "4", "7", "1"):
        await step(value, event, text, native=True)

    async def revoke_before_submit(request, *, authorize):
        assert request.action == "unmute"
        value.management.assignments[value.management.key(event.platform, "5")] = ()
        with pytest.raises(ProtocolError):
            await authorize()
        return {"status": "rejected", "submitted": False}

    event.platform.execute_action.side_effect = revoke_before_submit
    assert "解禁已受理" not in await step(value, event, "1", native=True)
    assert await engine.execute(event, "解禁", "3", ai=True) == {
        "status": "rejected",
        "reason": "ai_management_retired",
    }
    event.platform.execute_action.assert_awaited_once()


@pytest.mark.asyncio
async def test_unmute_unknown_receipt_is_not_reported_as_success_or_retried(setup):
    value, _, event = setup
    for text in ("菜单", "4", "7", "1"):
        await step(value, event, text, native=True)
    event.platform.execute_action.return_value = {"status": "unknown"}
    reply = await step(value, event, "1", native=True)
    assert "未知，不自动重试" in reply and "操作ID" in reply
    assert "解禁已受理" not in reply
    await step(value, event, "1", native=True, pace=False)
    event.platform.execute_action.assert_awaited_once()


@pytest.mark.asyncio
async def test_private_wizard_starts_with_only_the_callers_groups(setup):
    value, _, event = setup
    event.is_private_chat = lambda: True
    event.get_group_id = lambda: ""
    event.eligible = True
    value.verify_actor = AsyncMock()
    value.management.assignments[value.management.key(event.platform, "6")] = ()
    for text in ("菜单", "4"):
        await step(value, event, text)
    reply = await step(value, event, "7")
    assert "Group 5" in reply and "Group 6" not in reply
    assert not event.platform.nim.get_team_muted_members.await_count
    reply = await step(value, event, "1")
    assert "1. Target" in reply and ". Manager" not in reply and ". Bot" not in reply
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ["empty", "incomplete", "error"])
async def test_empty_or_failed_mute_queries_do_not_open_a_member_prompt(
    setup, response
):
    value, _, event = setup
    query = event.platform.nim.get_team_muted_members
    if response == "empty":
        query.return_value = set()
    elif response == "incomplete":
        query.return_value = {"904"}
    else:
        query.side_effect = ProtocolError("http_timeout")
    for text in ("菜单", "4"):
        await step(value, event, text)
    reply = await step(value, event, "7")
    assert "成员序号" not in reply
    assert ("没有可解禁" if response == "empty" else "无法获取") in reply
    assert value.navigation.sessions[value.navigation.key(event)].pending is None
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_general_member_list_cannot_be_used_as_a_mute_selection(setup):
    value, _, event = setup
    await step(value, event, "成员列表", native=True)
    assert "禁言名单编号无效" in await step(value, event, "解禁 3", native=True)
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_refresh_invalidates_the_previous_mute_selection(setup):
    value, _, event = setup
    assert "1. Target" in await step(value, event, "禁言名单", native=True)
    event.platform.nim.get_team_muted_members.side_effect = ProtocolError(
        "http_timeout"
    )
    assert "无法获取" in await step(value, event, "禁言名单", native=True)
    assert "禁言名单编号无效" in await step(value, event, "解禁 1", native=True)
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_mute_during_confirmation_is_a_noop(setup):
    value, _, event = setup
    for text in ("菜单", "4", "7", "1"):
        await step(value, event, text)
    event.platform.nim.get_team_muted_members.return_value = set()
    assert "无需解禁" in await step(value, event, "1")
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_unmute_guard_rechecks_platform_status_at_submission(setup):
    value, _, event = setup
    for text in ("菜单", "4", "7", "1"):
        await step(value, event, text)

    async def stop_before_submit(request, *, authorize):
        event.platform.nim.get_team_muted_members.return_value = set()
        with pytest.raises(ProtocolError, match="member_no_longer_muted"):
            await authorize()
        return {"status": "rejected", "submitted": False}

    event.platform.execute_action.side_effect = stop_before_submit
    assert "解禁已受理" not in await step(value, event, "1")
    event.platform.execute_action.assert_awaited_once()
