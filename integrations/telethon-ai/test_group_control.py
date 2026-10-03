import asyncio
import copy
import json
import sqlite3
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import ChatPermissions

from astrbot.core.config.default import DEFAULT_CONFIG
from data.plugins.astrbot_plugin_telethon_ai import adapter
from data.plugins.astrbot_plugin_telethon_ai.group_control import (
    PERMISSIONS,
    GroupControl,
    GroupHandler,
)
from data.plugins.astrbot_plugin_telethon_ai.group_store import GroupStore
from data.plugins.astrbot_plugin_telethon_ai.tenants import Denied, Tenants

CHAT = "-100123"


def test_group_cooldown_survives_switch_and_new_binding(tmp_path):
    store = GroupStore(tmp_path / "group.db")
    try:
        store.bind("A", 22, 1, "first", CHAT, "Fixture", 2)
        assert store.admit_reply("A", CHAT, now=100)
        store.bind("B", 33, 1, "second", CHAT, "Fixture", 2)
        assert not store.admit_reply("B", CHAT, now=101)
        assert store.admit_reply("B", CHAT, now=110)
        assert not store.admit_reply("A", CHAT, now=111)
    finally:
        store.close()


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, "state_path", lambda: tmp_path / "gate.db")
    tenants = Tenants(tmp_path / "tenants.db")
    ids = {}
    for platform, bot, owner in (("A", 22, 1), ("B", 33, 1), ("C", 44, 2)):
        ids[platform] = tenants.create_trial(
            "test", owner, bot, "AIClient_" + platform, group_limit=2
        )
        with tenants.db:
            tenants.db.execute(
                "UPDATE tenants SET platform=? WHERE id=?", (platform, ids[platform])
            )
    members = {}
    for uid in (1, 2, 3, 22, 33, 44, 123):
        members[uid] = SimpleNamespace(
            status="administrator", can_delete_messages=True, can_restrict_members=True
        )
    for uid in (100, 200):
        members[uid] = SimpleNamespace(status="member")
    profiles = {}
    platforms = {}
    for platform, uid in (("VIP_DHBot", 123), ("A", 22), ("B", 33), ("C", 44)):
        bot = SimpleNamespace(
            id=uid,
            username=f"bot{uid}",
            send_message=AsyncMock(),
            get_chat_member=AsyncMock(side_effect=lambda chat, user: members[user]),
            get_chat=AsyncMock(
                return_value=SimpleNamespace(
                    id=int(CHAT),
                    type="supergroup",
                    title="Fixture",
                    permissions=ChatPermissions(**dict.fromkeys(PERMISSIONS, True)),
                )
            ),
            restrict_chat_member=AsyncMock(),
            delete_message=AsyncMock(),
        )
        platforms[platform] = SimpleNamespace(
            application=SimpleNamespace(
                bot=bot, bot_data={adapter.NAME + ":ready": True}
            ),
            config={
                "type": "telethon_ai_service",
                "telegram_required_plugin": adapter.NAME,
                "telegram_dedicated_reporting": True,
                "telegram_token": f"{uid}:fixture",
                "service_role": "controller" if platform == "VIP_DHBot" else "customer",
            },
        )
        conf = copy.deepcopy(DEFAULT_CONFIG)
        conf.update(
            admins_id=[],
            disable_builtin_commands=True,
            plugin_set=[adapter.NAME],
            kb_names=[],
        )
        conf["provider_settings"]["enable"] = False
        conf["dashboard"]["enable"] = False
        profiles[platform] = conf
    context = SimpleNamespace(
        get_platform_inst=lambda platform: platforms.get(platform),
        astrbot_config_mgr=SimpleNamespace(
            ucr=SimpleNamespace(
                umop_to_conf_id={p + "::": p for p in profiles},
                get_conf_id_for_umop=lambda origin: origin.split(":")[0],
            ),
            confs=profiles,
        ),
    )
    plugin = SimpleNamespace(
        context=context,
        tenants=tenants,
        closed=False,
        config={"admin_ids": ["1"], "control_platform_id": "VIP_DHBot"},
        control=SimpleNamespace(application=platforms["VIP_DHBot"].application),
    )
    group = GroupControl(plugin)
    plugin.group_control = group
    yield group, tenants, ids, platforms, members, plugin
    group.store.close()
    tenants.close()


def bind(runtime, platform="A", chat=CHAT):
    group, _, ids, platforms, _, _ = runtime
    bot = platforms[platform].application.bot
    scope = group.service(platform, bot)
    group.store.bind(
        platform, bot.id, scope["owner"], ids.get(platform), chat, "Fixture", 2
    )
    return bot


def message(text="hello", mid=10):
    return SimpleNamespace(
        text=text,
        message_id=mid,
        new_chat_members=[],
        sender_chat=None,
        from_user=SimpleNamespace(id=100, is_bot=False),
        reply_text=AsyncMock(),
        reply_to_message=SimpleNamespace(
            message_id=5,
            from_user=SimpleNamespace(id=100, is_bot=False),
            sender_chat=None,
        ),
    )


def update(msg=None, *, actor=1, private=False, query=None):
    return SimpleNamespace(
        effective_chat=SimpleNamespace(
            id=actor if private else int(CHAT),
            type="private" if private else "supergroup",
            title="Fixture",
        ),
        effective_user=SimpleNamespace(id=actor, is_bot=False),
        message=msg,
        callback_query=query,
        my_chat_member=None,
        effective_message=msg,
    )


def test_owner_claim_limits_and_multi_bot_leader(runtime):
    group = runtime[0]
    bind(runtime, "A")
    bind(runtime, "B")
    with pytest.raises(Denied, match="another owner"):
        bind(runtime, "C")
    group.store.set_automatic(1, "A", CHAT)
    group.store.set_automatic(1, "B", CHAT)
    assert group.store.binding("A", CHAT)["automatic"] == "B"
    with pytest.raises(Denied):
        group.store.set_automatic(3, "A", CHAT)
    with pytest.raises(Denied):
        group.store.unbind(3, "A", CHAT)
    group.store.unbind(1, "B", CHAT)
    assert group.store.binding("A", CHAT)["automatic"] is None
    bind(runtime, "A", "-100456")
    with pytest.raises(Denied, match="limit"):
        bind(runtime, "A", "-100789")
    group.store.unbind(1, "A", CHAT)
    bind(runtime, "C")


def test_tokens_are_durable_actor_bot_bound_expire_and_single_use(runtime, monkeypatch):
    group = runtime[0]
    token = group.store.token("A", 22, 1, CHAT, action="bind")
    for platform, bot, actor in (("B", 22, 1), ("A", 33, 1), ("A", 22, 2)):
        with pytest.raises(Denied):
            group.store.consume(token, platform, bot, actor)
    assert group.store.consume(token, "A", 22, 1)[1]["action"] == "bind"
    with pytest.raises(Denied):
        group.store.consume(token, "A", 22, 1)
    token = group.store.token("A", 22, 1, CHAT, action="bind")
    monkeypatch.setattr(time, "time", lambda: 99999999999)
    with pytest.raises(Denied):
        group.store.consume(token, "A", 22, 1)


@pytest.mark.asyncio
async def test_binding_requires_owner_confirmation_and_fresh_admin(runtime):
    group, _, _, platforms, members, _ = runtime
    bot = platforms["A"].application.bot
    await group.handle(
        "A", update(message("/bindgroup@bot22"), actor=3), SimpleNamespace(bot=bot)
    )
    assert group.store.db.execute("SELECT COUNT(*) FROM bindings").fetchone()[0] == 0
    assert bot.send_message.call_args.args[0] == 1
    token = (
        bot.send_message.call_args.kwargs["reply_markup"]
        .inline_keyboard[0][0]
        .callback_data
    )
    query = SimpleNamespace(data=token, answer=AsyncMock())
    with pytest.raises(Denied):
        await group.callback("A", bot, 3, query)
    members[1] = SimpleNamespace(status="member")
    with pytest.raises(Denied):
        await group.callback("A", bot, 1, query)
    assert group.store.db.execute("SELECT COUNT(*) FROM bindings").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_public_admin_self_authorizes_without_platform_admin_in_group(runtime):
    group, tenants, _, platforms, members, _ = runtime
    bot = platforms["VIP_DHBot"].application.bot
    members[1] = SimpleNamespace(status="member")
    await group.handle(
        "VIP_DHBot",
        update(message("/bindgroup@bot123"), actor=3),
        SimpleNamespace(bot=bot),
    )
    assert bot.send_message.call_args.args[0] == 3
    assert group.store.db.execute("SELECT COUNT(*) FROM bindings").fetchone()[0] == 0
    token = (
        bot.send_message.call_args.kwargs["reply_markup"]
        .inline_keyboard[0][0]
        .callback_data
    )
    query = SimpleNamespace(data=token, answer=AsyncMock())
    with pytest.raises(Denied):
        await group.callback("VIP_DHBot", bot, 2, query)
    await group.callback("VIP_DHBot", bot, 3, query)
    binding = group.store.binding("VIP_DHBot", CHAT)
    assert binding["public"] and binding["owner"] == "3" and binding["tenant"] is None
    assert binding["automatic"] is None and not binding["welcome_enabled"]
    assert not tenants.db.execute("SELECT 1 FROM groups").fetchone()
    await group.authorize("VIP_DHBot", bot, CHAT, 2, owner=True)
    await group.callback(
        "VIP_DHBot",
        bot,
        2,
        SimpleNamespace(
            data="gc:" + group.store.token("VIP_DHBot", bot.id, 2, CHAT, action="auto"),
            answer=AsyncMock(),
        ),
    )
    assert group.store.binding("VIP_DHBot", CHAT)["automatic"] == "VIP_DHBot"
    with pytest.raises(Denied):
        await group.authorize("VIP_DHBot", bot, CHAT, 100)
    members[2] = SimpleNamespace(status="member")
    with pytest.raises(Denied):
        await group.authorize("VIP_DHBot", bot, CHAT, 2, owner=True)
    with pytest.raises(Denied):
        await group.callback(
            "VIP_DHBot",
            bot,
            2,
            SimpleNamespace(
                data="gc:"
                + group.store.token("VIP_DHBot", bot.id, 2, CHAT, action="unbind"),
                answer=AsyncMock(),
            ),
        )


@pytest.mark.asyncio
async def test_public_confirm_rechecks_admin_and_rejects_legacy_confirmation(runtime):
    group, _, _, platforms, members, _ = runtime
    bot = platforms["VIP_DHBot"].application.bot
    for payload in ({"action": "bind"}, {"action": "bind", "public": True}):
        token = group.store.token("VIP_DHBot", bot.id, 3, CHAT, **payload)
        members[3] = SimpleNamespace(status="member")
        with pytest.raises(Denied):
            await group.callback(
                "VIP_DHBot",
                bot,
                3,
                SimpleNamespace(data="gc:" + token, answer=AsyncMock()),
            )
    assert group.store.db.execute("SELECT COUNT(*) FROM bindings").fetchone()[0] == 0


def test_public_binding_and_paid_ownership_reservations_are_separate(runtime):
    group = runtime[0]
    group.store.bind("VIP_DHBot", 123, 3, None, CHAT, "Public", None, public=True)
    bind(runtime, "C")
    assert group.store.db.execute("SELECT owner FROM claims").fetchone()[0] == "2"
    with pytest.raises(Denied):
        bind(runtime, "A")
    group.store.unbind(2, "C", CHAT)
    assert group.store.db.execute("SELECT owner FROM claims").fetchone()[0] == ""
    bind(runtime, "A")
    group.store.set_automatic(3, "VIP_DHBot", CHAT)
    assert group.store.binding("A", CHAT)["automatic"] == "VIP_DHBot"
    group.store.unbind(3, "VIP_DHBot", CHAT)
    assert group.store.binding("A", CHAT)["owner"] == "1"
    assert group.store.binding("A", CHAT)["automatic"] is None
    with pytest.raises(Denied):
        group.store.bind("Other", 888, 3, "paid", CHAT, "Invalid", None, public=True)


@pytest.mark.asyncio
async def test_public_group_menu_is_discoverable_without_scanning_other_groups(runtime):
    group, _, _, platforms, members, _ = runtime
    bot = platforms["VIP_DHBot"].application.bot
    group.store.bind("VIP_DHBot", 123, 2, None, CHAT, "Own group", None, public=True)
    group.store.bind(
        "VIP_DHBot", 123, 2, None, "-100999", "Hidden group", None, public=True
    )
    await group.handle(
        "VIP_DHBot",
        update(message("/groups@bot123"), actor=3),
        SimpleNamespace(bot=bot),
    )
    bot.get_chat_member.reset_mock()
    await group.menu("VIP_DHBot", bot, 3)
    assert bot.get_chat_member.await_count == 2
    assert all(call.args[0] == int(CHAT) for call in bot.get_chat_member.call_args_list)
    markup = bot.send_message.call_args.kwargs["reply_markup"]
    assert [b.text for row in markup.inline_keyboard for b in row] == ["Own group"]
    members[3] = SimpleNamespace(status="member")
    await group.menu("VIP_DHBot", bot, 3)
    assert not bot.send_message.call_args.kwargs["reply_markup"].inline_keyboard
    with pytest.raises(Denied):
        await group.menu("VIP_DHBot", bot, 3, "-100999")


def test_migration_preserves_private_binding_audit_and_confirmation(tmp_path):
    path = tmp_path / "group.db"
    store = GroupStore(path)
    store.bind("A", 22, 1, "tenant", CHAT, "Existing", 2)
    token = store.token("A", 22, 1, CHAT, action="bind")
    store.close()
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE access_hints")
        db.execute("ALTER TABLE bindings DROP COLUMN public")
    store = GroupStore(path)
    try:
        assert not store.binding("A", CHAT)["public"]
        assert store.consume(token, "A", 22, 1)[1]["action"] == "bind"
        assert store.db.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_all_admins_can_edit_but_owner_controls_binding(runtime):
    group = runtime[0]
    bot = bind(runtime)
    await group.authorize("A", bot, CHAT, 3)
    with pytest.raises(Denied):
        await group.authorize("A", bot, CHAT, 3, owner=True)
    runtime[4][3] = SimpleNamespace(status="member")
    with pytest.raises(Denied):
        await group.authorize("A", bot, CHAT, 3)
    with pytest.raises(Denied):
        await group.authorize("A", bot, CHAT, 100)


@pytest.mark.asyncio
async def test_expiry_blocks_mutations_but_ai_pause_and_quota_do_not(runtime):
    group, tenants, ids, *_ = runtime
    bot = bind(runtime)
    tenants.set_enabled(1, ids["A"], False)
    with tenants.db:
        tenants.db.execute("UPDATE tenants SET budget=1 WHERE id=?", (ids["A"],))
        tenants.db.execute(
            "INSERT INTO reservations VALUES('spent',?,'fixture',?,'1',?,'sent')",
            (ids["A"], CHAT, time.time()),
        )
    await group.authorize("A", bot, CHAT, 1)
    with tenants.db:
        tenants.db.execute("UPDATE tenants SET expires=0 WHERE id=?", (ids["A"],))
    with pytest.raises(Denied):
        await group.authorize("A", bot, CHAT, 1)
    await group.authorize("A", bot, CHAT, 1, active=False)


@pytest.mark.asyncio
async def test_profile_and_bot_identity_isolation(runtime):
    group = runtime[0]
    bot = bind(runtime)
    with pytest.raises(Denied):
        group.service("A", SimpleNamespace(id=22))
    runtime[5].context.astrbot_config_mgr.confs["A"]["provider_settings"]["enable"] = (
        True
    )
    with pytest.raises(Denied):
        await group.authorize("A", bot, CHAT, 1)


@pytest.mark.asyncio
async def test_keyword_order_plaintext_duplicate_cooldown_and_leader(runtime):
    group = runtime[0]
    bot = bind(runtime)
    other = bind(runtime, "B")
    group.store.set_automatic(1, "A", CHAT)
    group.store.add_keyword(3, "A", CHAT, "contains", "hi", "short")
    group.store.add_keyword(3, "A", CHAT, "contains", "hi there", "<b>long</b>")
    group.store.add_keyword(3, "A", CHAT, "exact", "hi there", "exact")
    group.store.set_config(3, "A", CHAT, "keywords_enabled", True)
    msg = message("hi there")
    await group.automatic("A", bot, group.store.binding("A", CHAT), msg)
    await group.automatic("A", bot, group.store.binding("A", CHAT), msg)
    assert bot.send_message.await_count == 1
    assert bot.send_message.call_args.args[1] == "exact"
    assert bot.send_message.call_args.kwargs["parse_mode"] is None
    await group.automatic(
        "A", bot, group.store.binding("A", CHAT), message("hi there", 11)
    )
    assert bot.send_message.await_count == 1
    await group.automatic(
        "B", other, group.store.binding("B", CHAT), message("hi there", 12)
    )
    other.send_message.assert_not_awaited()
    assert group.store.match("A", CHAT, "oh hi there!") == "<b>long</b>"
    assert group.store.match("B", CHAT, "hi there") is None


@pytest.mark.asyncio
async def test_welcome_default_off_and_ordinary_text_never_welcomes(runtime):
    group = runtime[0]
    bot = bind(runtime)
    group.store.set_automatic(1, "A", CHAT)
    group.store.set_config(3, "A", CHAT, "welcome", "Welcome")
    msg = message("")
    msg.new_chat_members = [SimpleNamespace(is_bot=False)]
    await group.automatic("A", bot, group.store.binding("A", CHAT), msg)
    bot.send_message.assert_not_awaited()
    group.store.set_config(3, "A", CHAT, "welcome_enabled", True)
    await group.automatic("A", bot, group.store.binding("A", CHAT), message("text"))
    bot.send_message.assert_not_awaited()
    await group.automatic("A", bot, group.store.binding("A", CHAT), msg)
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_uncertain_reply_retained_after_reopen_and_no_fallback(runtime, tmp_path):
    group = runtime[0]
    bot = bind(runtime)
    bind(runtime, "B")
    group.store.set_automatic(1, "A", CHAT)
    group.store.add_keyword(1, "A", CHAT, "exact", "hi", "reply")
    group.store.set_config(1, "A", CHAT, "keywords_enabled", True)
    bot.send_message.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await group.automatic("A", bot, group.store.binding("A", CHAT), message("hi"))
    assert group.store.binding("B", CHAT)["automatic"] == "A"
    reopened = GroupStore(tmp_path / "group_control.db")
    assert (
        reopened.db.execute("SELECT state FROM operations").fetchone()[0] == "uncertain"
    )
    reopened.close()
    assert group.store.binding("A", CHAT)["disabled_reason"]


@pytest.mark.asyncio
async def test_health_stops_expired_leader_and_notifies_once(runtime):
    group, tenants, ids, platforms, *_ = runtime
    bind(runtime)
    group.store.set_automatic(1, "A", CHAT)
    with tenants.db:
        tenants.db.execute("UPDATE tenants SET expires=0 WHERE id=?", (ids["A"],))
    await group.tick()
    await group.tick()
    controller = platforms["VIP_DHBot"].application.bot
    controller.send_message.assert_awaited_once()
    assert group.store.binding("A", CHAT)["disabled_reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command,args", [("/mute", "0m"), ("/mute", "8d"), ("/mute", "10"), ("/unmute", "")]
)
async def test_invalid_mute_and_foreign_unmute(runtime, command, args):
    group = runtime[0]
    bot = bind(runtime)
    with pytest.raises(Denied):
        await group.moderate("A", bot, 3, CHAT, message(), command, args)
    bot.restrict_chat_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_mute_unmute_and_external_changes(runtime):
    group = runtime[0]
    bot = bind(runtime)
    members = runtime[4]

    async def restrict(chat, target, permissions, **kwargs):
        if kwargs["until_date"] == 0:
            assert all(permissions.to_dict().values())
            members[target] = SimpleNamespace(status="member")
            return True
        members[target] = SimpleNamespace(
            status="restricted",
            until_date=datetime.fromtimestamp(kwargs["until_date"], timezone.utc),
            is_member=True,
            **{p: getattr(permissions, p) for p in PERMISSIONS},
        )

    bot.restrict_chat_member.side_effect = restrict
    await group.moderate("A", bot, 3, CHAT, message(mid=10), "/mute", "10m")
    assert bot.restrict_chat_member.await_count == 1
    members[100].can_send_photos = True
    with pytest.raises(Denied):
        await group.moderate("A", bot, 3, CHAT, message(mid=11), "/unmute", "")
    members[100].can_send_photos = False
    if "can_react_to_messages" in PERMISSIONS:
        members[100].can_react_to_messages = True
        with pytest.raises(Denied):
            await group.moderate("A", bot, 3, CHAT, message(mid=13), "/unmute", "")
        members[100].can_react_to_messages = False
    await group.moderate("A", bot, 3, CHAT, message(mid=12), "/unmute", "")
    assert bot.restrict_chat_member.await_count == 2
    assert members[100].status == "member"
    await group.moderate("A", bot, 3, CHAT, message(mid=14), "/mute", "1m")
    assert bot.restrict_chat_member.await_count == 3


@pytest.mark.asyncio
async def test_unmute_requires_actual_normal_member_and_retains_uncertain_ledger(
    runtime,
):
    group = runtime[0]
    bot = bind(runtime)
    members = runtime[4]

    async def restrict(chat, target, permissions, **kwargs):
        if kwargs["until_date"]:
            members[target] = SimpleNamespace(
                status="restricted",
                until_date=datetime.fromtimestamp(kwargs["until_date"], timezone.utc),
                is_member=True,
                **dict.fromkeys(PERMISSIONS, False),
            )
        return True

    bot.restrict_chat_member.side_effect = restrict
    await group.moderate("A", bot, 3, CHAT, message(mid=20), "/mute", "10m")
    with pytest.raises(RuntimeError, match="could not be verified"):
        await group.moderate("A", bot, 3, CHAT, message(mid=21), "/unmute", "")
    assert group.store.db.execute("SELECT COUNT(*) FROM mutes").fetchone()[0] == 1
    assert (
        group.store.db.execute(
            "SELECT state FROM operations WHERE kind='/unmute'"
        ).fetchone()[0]
        == "uncertain"
    )
    with pytest.raises(Denied, match="uncertain"):
        await group.moderate("A", bot, 3, CHAT, message(mid=22), "/unmute", "")


@pytest.mark.asyncio
async def test_admin_immunity_delete_dedup_and_unknown_punishment(runtime):
    group = runtime[0]
    bot = bind(runtime)
    runtime[4][100] = SimpleNamespace(status="administrator")
    with pytest.raises(Denied):
        await group.moderate("A", bot, 3, CHAT, message(), "/del", "")
    runtime[4][100] = SimpleNamespace(status="member")
    await group.moderate("A", bot, 3, CHAT, message(), "/del", "")
    await group.moderate("A", bot, 3, CHAT, message(), "/del", "")
    bot.delete_message.assert_awaited_once()
    bot.delete_message.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        await group.moderate("A", bot, 3, CHAT, message(mid=11), "/del", "")
    with pytest.raises(Denied, match="uncertain"):
        await group.moderate("A", bot, 3, CHAT, message(mid=12), "/del", "")


@pytest.mark.asyncio
async def test_no_unqualified_or_other_bot_command_and_no_ai_event(runtime):
    group = runtime[0]
    bot = bind(runtime)
    for text in ("/del", "/del@otherbot"):
        await group.handle(
            "A", update(message(text), actor=3), SimpleNamespace(bot=bot)
        )
    bot.delete_message.assert_not_awaited()
    assert not group.store.db.execute("SELECT 1 FROM operations").fetchone()


@pytest.mark.asyncio
async def test_menu_prompt_is_not_a_pending_input_until_clicked(runtime):
    group = runtime[0]
    bot = bind(runtime)
    await group.menu("A", bot, 3, CHAT)
    handler = GroupHandler(runtime[5], "A")
    assert not handler.check_update(
        update(message("ordinary text"), actor=3, private=True)
    )
    token = group.store.token("A", bot.id, 3, CHAT, action="prompt", field="rules")
    await group.callback(
        "A", bot, 3, SimpleNamespace(data="gc:" + token, answer=AsyncMock())
    )
    assert handler.check_update(update(message("new rules"), actor=3, private=True))
    await group.handle(
        "A",
        update(message("new rules"), actor=3, private=True),
        SimpleNamespace(bot=bot),
    )
    assert group.store.binding("A", CHAT)["rules"] == "new rules"
    assert not group.store.binding("A", CHAT)["welcome_enabled"]


def test_status_has_no_credentials_or_text_content(runtime):
    group = runtime[0]
    bind(runtime)
    group.store.set_config(1, "A", CHAT, "rules", "private group rules")
    encoded = json.dumps(group.status())
    assert "private group rules" not in encoded
    assert "telegram_token" not in encoded


@pytest.mark.asyncio
async def test_unexpected_handler_failure_is_consumed_without_detail(
    runtime, monkeypatch
):
    group = runtime[0]
    bot = bind(runtime)
    monkeypatch.setattr(group, "handle", AsyncMock(side_effect=RuntimeError("secret")))
    await group.dispatch("A", update(message()), SimpleNamespace(bot=bot))
    assert not group.active
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_can_release_local_binding_after_bot_removed(runtime):
    group = runtime[0]
    bot = bind(runtime)
    runtime[4][22] = SimpleNamespace(status="left")
    await group.menu("A", bot, 1, CHAT)
    assert "未核验" in bot.send_message.call_args.args[1]
    token = group.store.token("A", bot.id, 3, CHAT, action="unbind")
    with pytest.raises(Denied):
        await group.callback(
            "A", bot, 3, SimpleNamespace(data="gc:" + token, answer=AsyncMock())
        )
    token = group.store.token("A", bot.id, 1, CHAT, action="unbind")
    await group.callback(
        "A", bot, 1, SimpleNamespace(data="gc:" + token, answer=AsyncMock())
    )
    with pytest.raises(Denied):
        group.store.binding("A", CHAT)
