"""Group billing confirmations and shared native group command helpers."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.command_menu import (
    CommandMenu,
    Session,
)
from astrbot.builtin_stars.wangshangliao_moderation.open_service import (
    OPEN_COMMANDS,
    recognize_integrated,
)
from astrbot.core.platform.sources.wangshangliao.group_scope import group_revision
from astrbot.core.platform.sources.wangshangliao.identity import ActorIdentity
from tests.test_wangshangliao_command_parity import setup as setup

pytestmark = pytest.mark.usefixtures("wsl_active_policy", "wsl_command_classifier")


@pytest.fixture
def consent():
    """Provide one isolated group billing preview with verified identity."""
    extras = {}
    event = SimpleNamespace(
        platform=SimpleNamespace(account="1", config={"_business_revision": 1}),
        actor_identity=ActorIdentity("1", "2", "902", "2"),
        is_private_chat=lambda: False,
        get_group_id=lambda: "5",
        get_sender_id=lambda: "2",
        unified_msg_origin="wsl:GroupMessage:1/5",
        policy_epoch=1,
        message_obj=SimpleNamespace(message_id="preview"),
        set_extra=lambda name, value: extras.update({name: value}),
        send=AsyncMock(),
        plain_result=lambda text: text,
    )
    service = SimpleNamespace(
        tenants={("1", "5"): {"version": 1, "epoch": 1, "native_role": "ADMIN"}},
        suspended_groups=set(),
        membership_receiving={},
        settings={"version": 1},
        group_ai=SimpleNamespace(configs={}),
        plans=SimpleNamespace(confirm_stamp=Mock(return_value=("payer", 1))),
        admit_command=Mock(return_value=True),
        manager_command=AsyncMock(return_value="已确认本群扣费"),
        member_whitelist=SimpleNamespace(cancel=Mock()),
        member_blacklist=SimpleNamespace(cancel=Mock()),
    )
    menu = CommandMenu(service)
    state = Session(
        expires=time.monotonic() + 60,
        confirm="确认本群扣费",
        stamp=menu.stamp(event),
        preview_message="preview",
    )
    menu.sessions[menu.key(event)] = state
    return menu, service, event, state, extras


@pytest.mark.parametrize(
    "change", ["private", "group", "actor", "account", "sender", "origin", "peer"]
)
def test_group_confirmation_requires_authenticated_current_identity(consent, change):
    menu, _, event, _, _ = consent
    if change == "private":
        event.is_private_chat = lambda: True
    elif change == "group":
        event.get_group_id = lambda: ""
    elif change == "actor":
        event.actor_identity = None
    elif change == "account":
        event.platform.account = "9"
    elif change == "sender":
        event.get_sender_id = lambda: "3"
    elif change == "origin":
        event.unified_msg_origin = ""
    else:
        event.actor_identity = ActorIdentity("1", "2", "")
    assert menu.key(event) is None
    assert not menu.accepts(event, "1")
    assert not menu.accepts(event, "取消")


@pytest.mark.parametrize("change", ["actor", "account", "group", "conversation"])
def test_group_confirmation_cannot_cross_scope(consent, change):
    menu, _, event, state, _ = consent
    original = menu.key(event)
    if change == "actor":
        event.actor_identity = ActorIdentity("1", "3", "903", "3")
        event.get_sender_id = lambda: "3"
    elif change == "account":
        event.platform.account = "9"
        event.actor_identity = ActorIdentity("9", "2", "902", "2")
    elif change == "group":
        event.get_group_id = lambda: "6"
    else:
        event.unified_msg_origin += "/other"
    assert menu.key(event) != original
    assert not menu.accepts(event, "1")
    assert menu.sessions[original] is state


@pytest.mark.parametrize(
    "text", ["菜单", "完整菜单", "帮助", "2", "确认", "确认本群扣费"]
)
@pytest.mark.asyncio
async def test_group_confirmation_never_claims_old_menu_inputs(consent, text):
    menu, service, event, _, extras = consent
    assert not await menu.handle(event, text)
    service.manager_command.assert_not_awaited()
    event.send.assert_not_awaited()
    assert not extras


@pytest.mark.asyncio
async def test_group_billing_confirmation_commits_once_from_a_fresh_message(consent):
    menu, service, event, state, extras = consent
    event.message_obj.message_id = "confirmation"
    assert await menu.handle(event, "1")
    service.manager_command.assert_awaited_once_with(event, "确认本群扣费")
    event.send.assert_awaited_once_with("已确认本群扣费")
    assert extras["_wsl_billing_consent_stamp"] == state.stamp
    assert extras["_component_claimed"] and extras["wsl_plain_command"]
    assert state.closed and state.busy
    assert not menu.sessions
    assert not await menu.handle(event, "1")
    assert service.manager_command.await_count == 1


@pytest.mark.parametrize(
    "condition", ["preview_message", "busy", "expired", "closed", "rate"]
)
@pytest.mark.asyncio
async def test_uncommittable_group_confirmation_has_no_business_action(
    consent, condition
):
    menu, service, event, state, extras = consent
    if condition != "preview_message":
        event.message_obj.message_id = "confirmation"
    if condition == "busy":
        state.busy = True
    elif condition == "expired":
        state.expires = time.monotonic() - 1
    elif condition == "closed":
        state.closed = True
    elif condition == "rate":
        service.admit_command.return_value = False
    assert await menu.handle(event, "1") is (condition not in {"expired", "closed"})
    service.manager_command.assert_not_awaited()
    event.send.assert_not_awaited()
    assert "_wsl_billing_consent_stamp" not in extras


@pytest.mark.parametrize(
    "change",
    [
        "tenant",
        "epoch",
        "role",
        "policy",
        "business",
        "settings",
        "ai",
        "suspended",
        "receiving",
        "payer",
    ],
)
@pytest.mark.asyncio
async def test_changed_group_authority_invalidates_billing_confirmation(
    consent, change
):
    menu, service, event, state, extras = consent
    event.message_obj.message_id = "confirmation"
    if change in {"tenant", "epoch", "role"}:
        field = {"tenant": "version", "epoch": "epoch", "role": "native_role"}[change]
        service.tenants[("1", "5")][field] = "changed"
    elif change == "policy":
        event.policy_epoch += 1
    elif change == "business":
        event.platform.config["_business_revision"] += 1
    elif change == "settings":
        service.settings["version"] += 1
    elif change == "ai":
        service.group_ai.configs[("1", "5")] = {"version": 1}
    elif change == "suspended":
        service.suspended_groups.add(("1", "5"))
    elif change == "receiving":
        service.membership_receiving[("1", "5")] = True
    else:
        service.plans.confirm_stamp.return_value = ("payer", 2)
    assert await menu.handle(event, "1")
    service.manager_command.assert_not_awaited()
    event.send.assert_awaited_once_with("本群设置或授权已变化，本次未确认。")
    assert state.closed and not state.confirm
    assert not menu.sessions
    assert "_wsl_billing_consent_stamp" not in extras


@pytest.mark.parametrize("text", ["取消", "0"])
@pytest.mark.asyncio
async def test_group_cancellation_dismisses_only_its_pending_operations(consent, text):
    menu, service, event, state, _ = consent
    other = (*menu.key(event)[:-1], "6")
    menu.sessions[other] = Session(expires=time.monotonic() + 60)
    assert await menu.handle(event, text)
    assert set(menu.sessions) == {other}
    assert state.closed and not state.confirm
    service.member_whitelist.cancel.assert_called_once_with(event)
    service.member_blacklist.cancel.assert_called_once_with(event)
    service.manager_command.assert_not_awaited()
    event.send.assert_awaited_once_with("已取消尚未提交的操作。")


@pytest.mark.asyncio
async def test_failed_group_billing_confirmation_is_not_retried(consent):
    menu, service, event, state, _ = consent
    event.message_obj.message_id = "confirmation"
    service.manager_command.side_effect = RuntimeError("unknown result")
    with pytest.raises(RuntimeError, match="unknown result"):
        await menu.handle(event, "1")
    assert state.closed and not menu.sessions
    assert not await menu.handle(event, "1")
    service.manager_command.assert_awaited_once()
    event.send.assert_not_awaited()


async def step(value, event, text, *, native=False, pace=True):
    """Deliver a fresh group command without sleeping through its cooldown.

    Args:
        value: Isolated service.
        event: Native fixture whose sender and conversation remain unchanged.
        text: Actual user input.
        native: Exercise the full plugin event route, not only navigation.
        pace: Advance only the command cooldown for routine test steps.

    Returns:
        Last emitted text, or an empty string for a consumed duplicate.
    """
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


@pytest.mark.parametrize("command", ["开启自主接话", "关闭自主接话"])
def test_autonomous_commands_use_the_native_classifier(command):
    from astrbot.builtin_stars.wangshangliao_moderation.syntax import recognize_command

    assert recognize_command(command) == command
    assert recognize_integrated(command) == command


def test_only_member_unmute_is_restored_and_other_manual_mutes_stay_retired():
    from astrbot.builtin_stars.wangshangliao_moderation.command_capabilities import (
        COMMAND_SPECS,
    )
    from astrbot.builtin_stars.wangshangliao_moderation.syntax import (
        RETIRED_CHAT_ACTIONS,
    )

    assert not OPEN_COMMANDS & RETIRED_CHAT_ACTIONS
    assert "解禁" in OPEN_COMMANDS
    assert COMMAND_SPECS["解禁"].role == "group_manager"
    assert COMMAND_SPECS["禁言"].role == "mute_delegate"
    assert not {"全员禁言", "解除全员禁言"} & COMMAND_SPECS.keys()
    assert {"定时禁言", "确认定时", "定时状态", "恢复定时"} <= COMMAND_SPECS.keys()


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["", "管理 ", "/管理 ", "群管 "])
@pytest.mark.parametrize("command", ["全员禁言", "解除全员禁言"])
async def test_retired_chat_mutes_only_explain_app_without_executing(
    setup, prefix, command
):
    value, engine, event = setup
    engine.run = AsyncMock(return_value="should never execute")
    reply = await step(value, event, prefix + command, native=True)
    assert "旺商聊App" in reply
    assert "定时解除" in reply
    engine.run.assert_not_awaited()
    event.platform.execute_action.assert_not_awaited()


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
