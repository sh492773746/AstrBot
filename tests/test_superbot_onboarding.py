"""Offline onboarding, scoped support and menu regression tests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import test_superbot_tenants as tenant_tests

from data.plugins.astrbot_plugin_superbot.availability import refresh, snapshot
from data.plugins.astrbot_plugin_superbot.onboarding import invitation
from data.plugins.astrbot_plugin_superbot.store import Rejected
from data.plugins.astrbot_plugin_superbot.support_menu import action, contact_url
from data.plugins.astrbot_plugin_superbot.tenant_ui import action as tenant_action

merchant = tenant_tests.merchant


def event(uid=10, chat=10, kind="private"):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=uid),
        effective_chat=SimpleNamespace(id=chat, type=kind),
        callback_query=None,
    )


@pytest.mark.parametrize("public", [False, True])
def test_invitation_reflects_switch_without_mutation(merchant, public):
    runtime, _, _ = merchant
    runtime.bot.username = "example_fixture_bot"
    policy = {**runtime.tenants.policy(), "public": public}
    runtime.tenants.configure_policy("1", policy)
    before = runtime.store.db.total_changes
    text, buttons = invitation(runtime)
    assert ("分批开放" in text) is not public
    assert "AI、制图及付费广告另行开通" in text
    assert buttons[0][1]["url"] == "https://t.me/example_fixture_bot?startgroup"
    assert runtime.store.db.total_changes == before


@pytest.mark.asyncio
async def test_closed_entry_has_contact_and_no_challenge(merchant):
    runtime, _, _ = merchant
    ui = SimpleNamespace(runtime=runtime, store=runtime.store, render=AsyncMock())
    before = runtime.store.db.execute(
        "SELECT count(*) FROM tenant_bindings"
    ).fetchone()[0]
    await tenant_action(ui, event(30), {"action": "tenant_join"})
    assert "尚未对公众开放" in ui.render.call_args.args[1]
    assert (
        runtime.store.db.execute("SELECT count(*) FROM tenant_bindings").fetchone()[0]
        == before
    )
    await tenant_action(ui, event(10), {"action": "tenant_join"})
    assert "/bindgroup" in ui.render.call_args.args[1]


def test_group_snapshot_is_independent_and_suspension_hides_everything(merchant):
    runtime, _, _ = merchant
    runtime.navigation_permissions = {
        chat: {"ready": True, "expires": runtime.store.clock() + 30}
        for chat in ("-1001", "-1002")
    }
    runtime.store.db.execute(
        "INSERT INTO tenant_settings(chat,key,value) VALUES('-1001','canada_enabled','true')"
    )
    assert snapshot(runtime, "-1001")["activate"]
    assert not snapshot(runtime, "-1002")["activate"]
    runtime.store.db.execute(
        "UPDATE tenant_groups SET status='suspended' WHERE chat='-1001'"
    )
    assert not any(snapshot(runtime, "-1001").values())
    assert snapshot(runtime, "-1002")["points"]


@pytest.mark.parametrize(
    "value",
    [
        "+123456789",
        "https://evil.test/user",
        "tg://user?id=42",
        "https://t.me/+1234567",
    ],
)
def test_contact_rejects_phone_and_untrusted_links(value):
    with pytest.raises(Rejected):
        contact_url(value)


@pytest.mark.asyncio
async def test_navigation_unknown_expired_or_failed_permissions_hide_actions(merchant):
    from telegram.error import TimedOut

    runtime, clock, _ = merchant
    assert not any(snapshot(runtime, "-1001").values())
    runtime.bot.get_chat_member = AsyncMock(
        return_value=SimpleNamespace(status="administrator", can_delete_messages=True)
    )
    await refresh(runtime, "-1001")
    assert snapshot(runtime, "-1001")["points"]
    runtime.navigation_permissions["-1001"]["expires"] = 0
    assert not any(snapshot(runtime, "-1001").values())
    runtime.bot.get_chat_member.side_effect = TimedOut()
    state = await refresh(runtime, "-1001")
    assert "失败" in state["reason"]
    assert not any(snapshot(runtime, "-1001").values())


@pytest.mark.asyncio
async def test_contact_context_and_unauthorized_ai_never_calls_model(merchant):
    runtime, _, _ = merchant
    runtime.community = SimpleNamespace(member=AsyncMock())
    runtime.chat_sessions = {("10", "10"): (1, "member", True)}
    ui = SimpleNamespace(runtime=runtime, store=runtime.store, render=AsyncMock())
    await action(ui, event(), {"action": "support_contact", "chat": "-1001"})
    assert ui.render.call_args.args[2][0][1]["url"] == "tg://user?id=10"
    assert not runtime.chat_sessions
    await action(ui, event(), {"action": "support_contact", "chat": "-1002"})
    assert ui.render.call_args.args[2][0][1]["url"] == "tg://user?id=20"
    with pytest.raises(Rejected):
        await action(ui, event(), {"action": "support_contact_edit", "chat": "-1002"})
    with pytest.raises(Rejected):
        await action(ui, event(), {"action": "support_ai", "chat": "-1001"})
    assert runtime.tenants.resource_context("10", "chat") is None


@pytest.mark.asyncio
async def test_contact_save_version_owner_and_group_isolation(merchant):
    runtime, _, _ = merchant
    runtime.community = SimpleNamespace(member=AsyncMock())
    ui = SimpleNamespace(runtime=runtime, store=runtime.store, render=AsyncMock())
    await action(ui, event(), {"action": "support_contact_edit", "chat": "-1001"})
    from data.plugins.astrbot_plugin_superbot.tenants import binding, local_config

    payload = {
        "action": "support_contact_save",
        "chat": "-1001",
        "value": "@example_contact",
        "version": 0,
        "owner_version": binding(runtime.store, "-1001")["version"],
    }
    await action(ui, event(), payload)
    assert (
        local_config(runtime.store, "-1001", "business_contact", "")
        == "https://t.me/example_contact"
    )
    assert local_config(runtime.store, "-1002", "business_contact", "") == ""
    with pytest.raises(Rejected):
        await action(ui, event(), payload)
    with pytest.raises(Rejected):
        await action(ui, event(20), {**payload, "version": 1})
