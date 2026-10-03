"""Offline automatic moderation: no real Telegram writes."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.ext import ApplicationHandlerStop

from data.plugins.astrbot_plugin_superbot.ad_killer import AdKiller, defaults
from data.plugins.astrbot_plugin_superbot.store import Rejected


class Member:
    def __init__(self, status):
        self.status = status
        self.can_delete_messages = True
        self.can_restrict_members = True

    def to_dict(self):
        return {"status": self.status}


@pytest.fixture
def env(tmp_path):
    from data.plugins.astrbot_plugin_superbot.moderation import Moderation
    from data.plugins.astrbot_plugin_superbot.store import Store
    from data.plugins.astrbot_plugin_superbot.ui import UI

    now = [1790140000.0]
    store = Store(tmp_path / "bot.sqlite3", "1", lambda: now[0])
    with store.tx() as db:
        store.put(db, "modules", {"moderation": True, "points": True})
    members = {1: Member("creator"), 9: Member("administrator"), 2: Member("member")}
    bot = SimpleNamespace(
        id=9,
        get_chat=AsyncMock(return_value=SimpleNamespace(type="supergroup")),
        get_chat_member=AsyncMock(side_effect=lambda chat, uid: members[uid]),
        delete_message=AsyncMock(),
        restrict_chat_member=AsyncMock(),
        ban_chat_member=AsyncMock(),
        unban_chat_member=AsyncMock(),
        send_message=AsyncMock(),
    )
    runtime = SimpleNamespace(
        store=store,
        bot=bot,
        application=SimpleNamespace(running=True),
        report=lambda *args: None,
    )
    runtime.moderation = Moderation(runtime)
    runtime.ad_killer = AdKiller(runtime)
    runtime.ui = UI(runtime)
    store.db.execute(
        "INSERT INTO mod_groups(chat,title,enabled) VALUES('-1001','Test',1)"
    )
    yield SimpleNamespace(runtime=runtime, now=now, members=members)
    store.close()


def message(text, number=1, user=2, *, caption=False, entities=()):
    msg = SimpleNamespace(
        message_id=number,
        from_user=SimpleNamespace(id=user, is_bot=False),
        sender_chat=None,
        text=None if caption else text,
        caption=text if caption else None,
        entities=entities if not caption else (),
        caption_entities=entities if caption else (),
        new_chat_members=(),
        reply_to_message=None,
        migrate_to_chat_id=None,
        forward_origin=None,
    )
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=-1001, type="supergroup", title="Test"),
        message=msg,
        edited_message=None,
        effective_user=msg.from_user,
        my_chat_member=None,
        callback_query=None,
    )


def enable(env, *rules, action="delete", **changes):
    config = defaults()
    for key in rules:
        config["rules"][key] = {"enabled": True, "action": action}
    config.update(changes)
    env.runtime.ad_killer.save("-1001", "1", 0, config, True)
    return config


@pytest.mark.asyncio
async def test_defaults_off_own_ad_and_protected_administrator(env):
    killer = env.runtime.ad_killer
    assert not killer.policy("-1001")["enabled"]
    assert not await killer.inspect(message("https://spam.example"))
    enable(env, "links")
    own = message("https://sponsored.example", user=9)
    assert not await killer.inspect(own)
    env.members[2] = Member("administrator")
    assert not await killer.inspect(message("https://spam.example", 2))
    env.runtime.bot.delete_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_links_caption_hidden_link_and_domain_boundary(env):
    from telegram import MessageEntity

    config = enable(env, "links")
    config["domains"] = ["safe.example"]
    env.runtime.ad_killer.save("-1001", "1", 1, config, True)
    killer = env.runtime.ad_killer
    assert not await killer.inspect(message("https://safe.example/page"))
    assert not await killer.inspect(message("https://sub.safe.example", 2))
    assert await killer.inspect(message("https://notsafe.example", 3, caption=True))
    hidden = MessageEntity("text_link", 0, 4, url="https://bad.example")
    assert await killer.inspect(message("点击这里", 4, entities=(hidden,)))
    assert env.runtime.bot.delete_message.await_count == 2
    assert not await killer.inspect(message("https://safe.example/page", 5))


@pytest.mark.asyncio
async def test_public_group_identity_own_links_and_cached_resolution(env):
    from data.plugins.astrbot_plugin_superbot.ad_link_rules import classify

    config = defaults()
    config["rules"]["group_links"]["enabled"] = True
    event = message("https://t.me/foreign_group/12")
    bot = env.runtime.bot
    bot.get_chat.return_value = SimpleNamespace(id=-10099, type="supergroup")
    cache = {}
    first = await classify(
        event.message, event.effective_chat, config, bot, cache, env.now[0]
    )
    assert first["group_links"]
    await classify(event.message, event.effective_chat, config, bot, cache, env.now[0])
    assert bot.get_chat.await_count == 1
    event.effective_chat.username = "foreign_group"
    assert not (
        await classify(
            event.message, event.effective_chat, config, bot, cache, env.now[0]
        )
    )["links"]
    event.effective_chat.username = None
    config["telegram_allow"] = ["-10099"]
    assert not (
        await classify(
            event.message, event.effective_chat, config, bot, cache, env.now[0]
        )
    )["group_links"]


@pytest.mark.asyncio
async def test_invite_deep_link_whitelist_preserves_hash_and_own_private_link(env):
    from data.plugins.astrbot_plugin_superbot.ad_link_rules import classify

    config = defaults()
    bot = env.runtime.bot
    event = message("https://t.me/+AbCdE123")
    flags = await classify(
        event.message, event.effective_chat, config, bot, {}, env.now[0]
    )
    assert flags["group_links"]
    config["telegram_allow"] = ["https://t.me/+AbCdE123"]
    event.message.text = "tg://join?invite=AbCdE123"
    assert not (
        await classify(event.message, event.effective_chat, config, bot, {}, env.now[0])
    )["links"]
    event.message.text = "tg://join?invite=abcde123"
    assert (
        await classify(event.message, event.effective_chat, config, bot, {}, env.now[0])
    )["group_links"]
    event.message.text = "https://t.me/c/1/15"
    assert not (
        await classify(event.message, event.effective_chat, config, bot, {}, env.now[0])
    )["links"]


@pytest.mark.asyncio
async def test_channel_forward_without_text_and_source_allowlist(env):
    config = enable(env, "channel_forward")
    event = message("")
    event.message.forward_origin = SimpleNamespace(
        type="channel", chat=SimpleNamespace(id=-10099, username="news")
    )
    assert await env.runtime.ad_killer.inspect(event)
    env.runtime.bot.delete_message.assert_awaited_once()
    config["telegram_allow"] = ["-10099"]
    env.runtime.ad_killer.save("-1001", "1", 1, config, True)
    event.message.message_id = 2
    assert not await env.runtime.ad_killer.inspect(event)
    env.runtime.bot.delete_message.assert_awaited_once()
    config["telegram_allow"] = []
    env.runtime.ad_killer.save("-1001", "1", 2, config, True)
    event.message.is_automatic_forward = True
    assert not await env.runtime.ad_killer.inspect(event)
    event.message.is_automatic_forward = False
    event.message.forward_origin = SimpleNamespace(type="user")
    assert not await env.runtime.ad_killer.inspect(event)


@pytest.mark.asyncio
async def test_platform_hidden_link_zero_width_and_telegram_separation(env):
    from telegram import MessageEntity

    enable(env, "platform_links")
    hidden = MessageEntity("text_link", 0, 2, url="https://discord.gg/offer")
    assert await env.runtime.ad_killer.inspect(message("点击", 1, entities=(hidden,)))
    assert await env.runtime.ad_killer.inspect(message("https://evil.exa\u200bmple", 2))
    assert not await env.runtime.ad_killer.inspect(
        message("https://t.me/other_group", 3)
    )
    assert env.runtime.bot.delete_message.await_count == 2


def test_link_rule_migration_preserves_existing_settings_and_invalidates_buttons(env):
    import json

    old = enable(env, "contact")
    for key in ("group_links", "platform_links", "channel_forward"):
        del old["rules"][key]
    del old["telegram_allow"]
    old["keywords"] = ["代理+私信"]
    env.runtime.store.db.execute(
        "UPDATE ak_policies SET config=? WHERE chat='-1001'", (json.dumps(old),)
    )
    killer = AdKiller(env.runtime)
    policy = killer.policy("-1001")
    new = json.loads(policy["config"])
    assert policy["version"] == 2
    assert policy["enabled"] == 1
    assert new["rules"]["contact"]["enabled"]
    assert new["keywords"] == ["代理+私信"]
    for key in ("group_links", "platform_links", "channel_forward"):
        assert new["rules"][key] == {"enabled": False, "action": "delete"}
    assert not new["telegram_allow"]
    assert AdKiller(env.runtime).policy("-1001")["version"] == 2


@pytest.mark.asyncio
async def test_unknown_public_target_not_classified_as_group_and_rpc_bounded(env):
    from data.plugins.astrbot_plugin_superbot.ad_link_rules import classify

    config = defaults()
    config["rules"]["group_links"]["enabled"] = True
    env.runtime.bot.get_chat.side_effect = TimeoutError()
    event = message(" ".join(f"https://t.me/target_{n}" for n in range(8)))
    result = await classify(
        event.message, event.effective_chat, config, env.runtime.bot, {}, env.now[0]
    )
    assert not result["group_links"]
    assert result["telegram"]
    assert env.runtime.bot.get_chat.await_count == 3


@pytest.mark.asyncio
async def test_repeat_flood_edit_idempotence_and_no_content_duplication(env):
    config = enable(env, "repeat", "flood")
    config.update(flood_count=6, repeat_count=3)
    killer = env.runtime.ad_killer
    text = "长期招商计划欢迎大家了解一下"
    assert not await killer.inspect(message(text, 1))
    assert not await killer.inspect(message(text, 2))
    assert await killer.inspect(message(text, 3))
    assert await killer.inspect(message(text, 3))
    assert env.runtime.bot.delete_message.await_count == 1
    for n in (4, 5):
        assert not await killer.inspect(message("short" + str(n), n))
    assert await killer.inspect(message("short6", 6))
    assert env.runtime.bot.delete_message.await_count == 2
    # The original message was accepted; editing it to an external link can
    # still trigger, but a replay of the edit cannot trigger twice.
    config["rules"]["links"]["enabled"] = True
    killer.save("-1001", "1", 1, config, True)
    changed = message("https://promoted.example", 7)
    changed.edited_message, changed.message = changed.message, None
    assert await killer.inspect(changed)
    assert await killer.inspect(changed)
    assert env.runtime.bot.delete_message.await_count == 3


@pytest.mark.asyncio
async def test_repeat_similar_text_and_contact_email(env):
    enable(env, "repeat", "contact")
    killer = env.runtime.ad_killer
    assert not await killer.inspect(message("本周继续招收主播和相关运营人员", 1))
    assert not await killer.inspect(message("本周继续招收主播及相关运营人员", 2))
    assert await killer.inspect(message("本周继续招收主播与相关运营人员", 3))
    assert await killer.inspect(message("请联系 promotions@example.com", 4))
    assert env.runtime.bot.delete_message.await_count == 2


@pytest.mark.asyncio
async def test_timeout_freezes_group_and_restart_never_replays(env):
    enable(env, "links")
    env.runtime.bot.delete_message.side_effect = TimeoutError("simulated")
    assert await env.runtime.ad_killer.inspect(message("https://bad.example"))
    row = env.runtime.store.db.execute(
        "SELECT status FROM ak_hits WHERE chat='-1001'"
    ).fetchone()
    assert row[0] == "review"
    assert not env.runtime.ad_killer.policy("-1001")["enabled"]
    assert not await env.runtime.ad_killer.inspect(message("https://bad.example", 2))
    assert env.runtime.bot.delete_message.await_count == 1
    AdKiller(env.runtime)
    assert not env.runtime.ad_killer.policy("-1001")["enabled"]


@pytest.mark.asyncio
async def test_failed_identity_is_audited_without_releasing_message(env):
    enable(env, "links")
    env.runtime.bot.get_chat_member.side_effect = TimeoutError("private data")
    assert await env.runtime.ad_killer.inspect(message("https://bad.example"))
    row = env.runtime.store.db.execute(
        "SELECT status,step,error FROM ak_hits WHERE chat='-1001'"
    ).fetchone()
    assert tuple(row) == ("skipped", "identity_unknown", "member_query_failed")
    env.runtime.bot.delete_message.assert_not_awaited()
    assert await env.runtime.ad_killer.inspect(message("https://bad.example"))
    assert env.runtime.bot.get_chat_member.await_count == 1


@pytest.mark.asyncio
async def test_manual_recovery_keeps_rules_off_and_never_replays(env):
    enable(env, "links")
    env.runtime.bot.delete_message.side_effect = TimeoutError("simulated")
    assert await env.runtime.ad_killer.inspect(message("https://bad.example"))
    env.runtime.bot.delete_message.reset_mock()
    ui = env.runtime.ui.moderation_ui
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=1),
        callback_query=None,
    )
    await ui.action(update, {"action": "mod_ak_recovery", "chat": "-1001"})
    assert "不会自动恢复规则" in env.runtime.bot.send_message.await_args.kwargs["text"]
    with pytest.raises(Rejected, match="变化"):
        await ui.action(
            update,
            {
                "action": "mod_ak_recovery_confirm",
                "chat": "-1001",
                "version": 1,
                "count": 0,
            },
        )
    await ui.action(
        update,
        {
            "action": "mod_ak_recovery_preview",
            "chat": "-1001",
            "version": 1,
            "count": 1,
        },
    )
    await ui.action(
        update,
        {
            "action": "mod_ak_recovery_confirm",
            "chat": "-1001",
            "version": 1,
            "count": 1,
        },
    )
    assert not env.runtime.ad_killer.policy("-1001")["enabled"]
    assert not env.runtime.ad_killer.policy("-1001")["error"]
    assert (
        env.runtime.store.db.execute(
            "SELECT status FROM ak_hits WHERE chat='-1001'"
        ).fetchone()[0]
        == "reviewed"
    )
    env.runtime.bot.delete_message.assert_not_awaited()
    with pytest.raises(Rejected, match="变化"):
        await ui.action(
            update,
            {
                "action": "mod_ak_recovery_confirm",
                "chat": "-1001",
                "version": 1,
                "count": 1,
            },
        )


@pytest.mark.asyncio
async def test_mute_kick_and_revocation_before_network_write(env):
    config = enable(env, "links", action="mute")
    assert await env.runtime.ad_killer.inspect(message("https://bad.example"))
    env.runtime.bot.restrict_chat_member.assert_awaited_once()
    # No automatic overwrite of a pre-existing personal restriction.
    env.members[2] = Member("restricted")
    assert await env.runtime.ad_killer.inspect(message("https://other.example", 2))
    assert env.runtime.bot.restrict_chat_member.await_count == 1
    env.members[2] = Member("member")
    config["rules"]["links"]["action"] = "kick"
    env.runtime.ad_killer.save("-1001", "1", 1, config, True)
    assert await env.runtime.ad_killer.inspect(message("https://third.example", 3))
    env.runtime.bot.ban_chat_member.assert_awaited_once()
    env.runtime.bot.unban_chat_member.assert_awaited_once()
    env.runtime.store.db.execute("UPDATE mod_groups SET enabled=0 WHERE chat='-1001'")
    assert not await env.runtime.ad_killer.inspect(message("https://fourth.example", 4))
    assert env.runtime.bot.delete_message.await_count == 3


@pytest.mark.asyncio
async def test_private_buttons_version_and_role(env):
    env.runtime.ad_killer
    env.runtime.store.db.execute(
        "INSERT INTO roles(uid,scopes,expires) VALUES('2','[\"moderation\"]',?)",
        (env.now[0] + 600,),
    )
    env.runtime.store.db.execute("INSERT INTO mod_acl VALUES('2','-1001')")
    env.members[2] = Member("administrator")
    ui = env.runtime.ui.moderation_ui
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=2),
        effective_chat=SimpleNamespace(id=2),
        callback_query=None,
    )
    await ui.action(update, {"action": "mod_ak_home", "chat": "-1001"})
    assert "广告杀手" in env.runtime.bot.send_message.await_args.kwargs["text"]
    await ui.action(
        update,
        {
            "action": "mod_ak_preview",
            "chat": "-1001",
            "version": 0,
            "change": {"rule": "links", "enabled": True},
        },
    )
    markup = env.runtime.bot.send_message.await_args.kwargs["reply_markup"]
    token = markup.inline_keyboard[0][0].callback_data[3:]
    saved = env.runtime.store.resolve(token, "2", "2")
    assert saved["action"] == "mod_ak_save"
    await ui.action(update, saved, token)
    assert env.runtime.ad_killer.policy("-1001")["version"] == 1
    with pytest.raises(Rejected, match="修改"):
        await ui.action(update, saved, token)
    env.runtime.store.revoke("1", "2")
    with pytest.raises(Rejected):
        await ui.action(update, {"action": "mod_ak_home", "chat": "-1001"})


def test_expiry_cleans_terminal_body_but_keeps_audit(env):
    config = defaults()
    env.runtime.ad_killer.save("-1001", "1", 0, config, True)
    env.runtime.store.db.execute(
        "INSERT INTO ak_hits(chat,message,uid,rules,action,version,body,status,step,at) VALUES('-1001',1,'2','[]','delete',1,'private','accepted','done',?)",
        (env.now[0] - 31 * 86400,),
    )
    env.runtime.ad_killer.cleanup()
    row = env.runtime.store.db.execute(
        "SELECT body,status FROM ak_hits WHERE chat='-1001'"
    ).fetchone()
    assert tuple(row) == (None, "accepted")


@pytest.mark.asyncio
async def test_group_gate_blocks_points_and_model_before_detected_message(env):
    from data.plugins.astrbot_plugin_superbot.main import Main

    enable(env, "links")
    plugin = Main(SimpleNamespace(), {"platform_id": "test"})
    plugin.store = env.runtime.store
    plugin.application = SimpleNamespace(bot=env.runtime.bot, running=True)
    plugin.moderation = env.runtime.moderation
    plugin.ad_killer = AdKiller(plugin)
    plugin.points = SimpleNamespace(chat=MagicMock())
    plugin.report = MagicMock()
    plugin.chat_sessions[("-1001", "2")] = (float("inf"), (), False)
    event = message("https://bad.example", 31)
    with pytest.raises(ApplicationHandlerStop):
        await plugin.receive(event, None)
    plugin.points.chat.assert_not_called()
    env.runtime.bot.delete_message.assert_awaited_once_with("-1001", 31)
    assert plugin.chat_sessions[("-1001", "2")]
    event = message("https://another.example", 32)
    event.edited_message, event.message = event.message, None
    with pytest.raises(ApplicationHandlerStop):
        await plugin.receive(event, None)
    plugin.points.chat.assert_not_called()
    assert env.runtime.bot.delete_message.await_count == 2


def test_policy_validation_rejects_unsafe_inputs(env):
    killer = env.runtime.ad_killer
    policy = defaults()
    policy["keywords"] = ["(?=a).*"]
    # Literal text is safe; it is never evaluated as a regular expression.
    killer.save("-1001", "1", 0, policy, False)
    policy["domains"] = ["example.com/bad"]
    with pytest.raises(Rejected, match="域名"):
        killer.save("-1001", "1", 1, policy, True)
    policy["domains"] = []
    policy["mute_minutes"] = 525601
    with pytest.raises(Rejected, match="阈值"):
        killer.save("-1001", "1", 1, policy, True)
