"""Offline onboarding, scoped support and menu regression tests."""

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import test_superbot_tenants as tenant_tests
from telegram.error import TimedOut

from data.plugins.astrbot_plugin_superbot.availability import refresh, snapshot
from data.plugins.astrbot_plugin_superbot.group_authority import CURRENT, GroupAuthority
from data.plugins.astrbot_plugin_superbot.onboarding import invitation
from data.plugins.astrbot_plugin_superbot.store import Rejected
from data.plugins.astrbot_plugin_superbot.support_menu import action, contact_url
from data.plugins.astrbot_plugin_superbot.tenant_ui import action as tenant_action
from data.plugins.astrbot_plugin_superbot.tenants import binding, local_config

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
    assert "默认授权AI客服与制图" in text
    assert "功能开关及服务限额" in text
    assert "广告投递须先配置广告位" in text
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
    assert "新群不需要凭证" in ui.render.call_args.args[1]
    assert (
        runtime.store.db.execute("SELECT count(*) FROM tenant_bindings").fetchone()[0]
        == before
    )


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
    runtime.store.db.execute(
        "INSERT INTO tenant_resource_grants(chat,resource,budget,enabled) "
        "VALUES('-1001','chat',0,0)"
    )
    with pytest.raises(Rejected, match="显式限额"):
        await action(ui, event(), {"action": "support_ai", "chat": "-1001"})
    assert runtime.tenants.resource_context("10", "chat") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", [False, True])
async def test_contact_save_version_owner_and_group_isolation(
    merchant, monkeypatch, platform
):
    runtime, _, owners = merchant
    store = runtime.store
    uid = 1 if platform else 10
    if platform:
        owners["-1001"] = str(uid)
        store.db.execute(
            "UPDATE tenant_groups SET tenant='platform',version=version+1 "
            "WHERE chat='-1001'"
        )
    transaction = store.tx
    committed = []

    @contextmanager
    def scoped_transaction():
        proof = CURRENT.get()
        assert proof is not None
        assert proof.store is store and proof.task is asyncio.current_task()
        assert (proof.uid, proof.chat) == (str(uid), "-1001")
        with transaction() as db:
            try:
                yield db
            finally:
                assert CURRENT.get() is proof
        assert CURRENT.get() is proof
        committed.append(proof)

    monkeypatch.setattr(store, "tx", scoped_transaction)
    get_chat = runtime.bot.get_chat
    get_member = runtime.bot.get_chat_member

    async def chat_lookup(*args, **kwargs):
        assert not store.db.in_transaction
        return await get_chat(*args, **kwargs)

    async def member_lookup(*args, **kwargs):
        assert not store.db.in_transaction
        return await get_member(*args, **kwargs)

    async def render(*args):
        assert not store.db.in_transaction

    monkeypatch.setattr(runtime.bot, "get_chat", AsyncMock(side_effect=chat_lookup))
    monkeypatch.setattr(
        runtime.bot, "get_chat_member", AsyncMock(side_effect=member_lookup)
    )
    runtime.community = SimpleNamespace(member=AsyncMock())
    ui = SimpleNamespace(
        runtime=runtime, store=store, render=AsyncMock(side_effect=render)
    )
    assert CURRENT.get() is None
    await action(ui, event(uid), {"action": "support_contact_edit", "chat": "-1001"})
    assert CURRENT.get() is None

    payload = {
        "action": "support_contact_save",
        "chat": "-1001",
        "value": "@example_contact",
        "version": 0,
        "owner_version": binding(store, "-1001")["version"],
    }
    await action(ui, event(uid), payload)
    assert CURRENT.get() is None
    assert len(committed) == 1
    assert (
        local_config(store, "-1001", "business_contact", "")
        == "https://t.me/example_contact"
    )
    assert local_config(store, "-1002", "business_contact", "") == ""
    with pytest.raises(Rejected, match="群归属或配置已变化"):
        await action(ui, event(uid), payload)
    assert CURRENT.get() is None
    store.db.execute("UPDATE tenant_groups SET version=version+1 WHERE chat='-1001'")
    with pytest.raises(Rejected, match="群归属或配置已变化"):
        await action(ui, event(uid), {**payload, "version": 1})
    assert CURRENT.get() is None
    with pytest.raises(Rejected):
        await action(ui, event(20), {**payload, "version": 1})
    assert CURRENT.get() is None
    assert len(committed) == 1
    assert (
        store.db.execute(
            "SELECT count(*) FROM audit WHERE action='business_contact'"
        ).fetchone()[0]
        == 1
    )
    assert (
        store.db.execute(
            "SELECT version FROM tenant_settings "
            "WHERE chat='-1001' AND key='business_contact'"
        ).fetchone()[0]
        == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["owner_changed", "bot_removed", "group_revision_race", "timeout", "paused"],
)
async def test_contact_save_rechecks_live_authority(merchant, failure):
    runtime, _, owners = merchant
    store = runtime.store
    ui = SimpleNamespace(runtime=runtime, store=store, render=AsyncMock())
    await action(ui, event(), {"action": "support_contact_edit", "chat": "-1001"})
    payload = {
        "action": "support_contact_save",
        "chat": "-1001",
        "value": "@example_contact",
        "version": 0,
        "owner_version": binding(store, "-1001")["version"],
    }
    original = runtime.bot.get_chat_member.side_effect

    async def changed_member(chat, uid):
        assert not store.db.in_transaction
        if uid == runtime.bot.id:
            if failure == "bot_removed":
                return SimpleNamespace(status="left")
            if failure == "group_revision_race":
                store.db.execute(
                    "UPDATE mod_groups SET version=version+1 WHERE chat=?", (chat,)
                )
        return await original(chat, uid)

    runtime.bot.get_chat_member.side_effect = changed_member
    if failure == "owner_changed":
        owners["-1001"] = "30"
    elif failure == "timeout":
        runtime.bot.get_chat.side_effect = TimedOut()
    elif failure == "paused":
        store.suspend_group("-1001")
    assert CURRENT.get() is None
    with pytest.raises(Rejected):
        await action(ui, event(), payload)
    assert CURRENT.get() is None
    assert not store.db.in_transaction
    assert local_config(store, "-1001", "business_contact", "") == ""
    assert local_config(store, "-1002", "business_contact", "") == ""
    assert (
        store.db.execute(
            "SELECT count(*) FROM audit WHERE action='business_contact'"
        ).fetchone()[0]
        == 0
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["support_contact_edit", "support_contact_save"])
async def test_contact_management_remains_owner_only_for_designated_admin(
    merchant, name
):
    runtime, _, _ = merchant
    original = runtime.bot.get_chat_member.side_effect

    async def designated_admin(chat, uid):
        if uid == 30:
            return SimpleNamespace(status="administrator")
        return await original(chat, uid)

    runtime.bot.get_chat_member.side_effect = designated_admin
    await runtime.tenants.designate(
        "10", "-1001", "30", True, runtime.moderation.row("-1001")["version"]
    )
    async with GroupAuthority(runtime).session("30", "-1001"):
        runtime.store.require("30", "moderation", chat="-1001")
    assert CURRENT.get() is None
    ui = SimpleNamespace(runtime=runtime, store=runtime.store, render=AsyncMock())
    with pytest.raises(Rejected, match="仅本群经营群主"):
        await action(
            ui,
            event(30),
            {
                "action": name,
                "chat": "-1001",
                "value": "@example_contact",
                "version": 0,
                "owner_version": binding(runtime.store, "-1001")["version"],
            },
        )
    assert CURRENT.get() is None
    assert local_config(runtime.store, "-1001", "business_contact", "") == ""
