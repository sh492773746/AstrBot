"""Offline acceptance of scoped community workflows and durable notifications."""

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
from telegram.error import Forbidden, RetryAfter
from test_superbot_ad_killer import message
from test_superbot_moderation import Member, setup  # noqa: F401

from data.plugins.astrbot_plugin_superbot import community_ui
from data.plugins.astrbot_plugin_superbot.ad_killer import AdKiller, defaults
from data.plugins.astrbot_plugin_superbot.community import Community
from data.plugins.astrbot_plugin_superbot.community_logs import CommunityLogs
from data.plugins.astrbot_plugin_superbot.join_verify import JoinVerify
from data.plugins.astrbot_plugin_superbot.store import Rejected


@pytest.fixture
def community(setup):  # noqa: F811
    s = setup
    s.runtime.report = lambda *args: None
    s.runtime.ad_killer = AdKiller(s.runtime)
    s.runtime.join_verify = JoinVerify(s.runtime)
    s.runtime.community = Community(s.runtime)
    s.runtime.community_logs = CommunityLogs(s.runtime)
    s.cm = s.runtime.community
    for uid in (3, 4):
        s.members[uid] = Member("member")
    return s


def private(uid=1):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=uid),
        effective_chat=SimpleNamespace(id=uid, type="private"),
        callback_query=None,
    )


def report_update(uid=2, number=10):
    update = message("/report", number=100 + number, user=uid)
    update.message.reply_to_message = SimpleNamespace(
        from_user=SimpleNamespace(id=4, is_bot=False),
        sender_chat=None,
        message_id=number,
        text="PRIVATE EVIDENCE",
        caption=None,
    )
    return update


@pytest.mark.asyncio
async def test_defaults_migration_and_view_permission(community):
    s = community
    assert not any(s.cm.policy("-1001")["config"]["enabled"].values())
    assert not s.cm.policy("-1001")["config"]["log"]["enabled"]
    s.members[1] = Member("administrator")
    s.members[1].can_restrict_members = False
    s.members[9].can_restrict_members = False
    cfg = s.cm.policy("-1001")["config"]
    cfg["rules"] = "Hello <b>plain</b>"
    cfg["enabled"]["rules"] = True
    await s.cm.save("1", "-1001", 0, cfg)
    assert Community(s.runtime).policy("-1001")["version"] == 1
    with pytest.raises(Rejected):
        await s.mod.check("1", "-1001", "mute")
    await s.runtime.ui.action(private(), {"action": "mod_group", "chat": "-1001"})
    assert "举报处理" in str(s.bot.send_message.await_args.kwargs["reply_markup"])
    with pytest.raises(Rejected):
        await s.cm.save("2", "-1001", 1, cfg)
    with pytest.raises(Rejected):
        await s.cm.save("1", "-1001", 0, cfg)
    s.members[2] = Member("left")
    with pytest.raises(Rejected):
        await s.runtime.ui.action(private(2), {"action": "cm_rules", "chat": "-1001"})


@pytest.mark.asyncio
async def test_scoped_admin_and_old_button_revoked(community):
    s = community
    s.members[2] = Member("administrator")
    with pytest.raises(Rejected):
        await s.runtime.ui.action(private(2), {"action": "cm_cases", "chat": "-1001"})
    s.store.db.execute(
        "INSERT INTO roles VALUES('2','[\"moderation\"]',?)", (s.clock[0] + 10,)
    )
    s.store.db.execute("INSERT INTO mod_acl VALUES('2','-1001')")
    await s.runtime.ui.action(private(2), {"action": "cm_cases", "chat": "-1001"})
    s.store.db.execute("DELETE FROM mod_acl WHERE uid='2'")
    with pytest.raises(Rejected):
        await s.runtime.ui.action(private(2), {"action": "cm_cases", "chat": "-1001"})
    s.store.db.execute("INSERT INTO mod_acl VALUES('2','-1001')")
    s.clock[0] += 11
    with pytest.raises(Rejected):
        await s.runtime.ui.action(private(2), {"action": "cm_cases", "chat": "-1001"})
    assert "群管理" not in str(s.runtime.ui.keyboard("2"))


@pytest.mark.asyncio
async def test_report_merge_binding_limits_and_privacy(community):
    s = community
    cfg = s.cm.policy("-1001")["config"]
    cfg["enabled"]["reports"] = True
    await s.cm.save("1", "-1001", 0, cfg)
    s.store.db.execute("INSERT INTO cm_private VALUES('1',?)", (s.clock[0],))
    token = await s.cm.ticket(report_update())
    with pytest.raises(Rejected):
        await s.cm.submit("3", token, "ads")
    case = await s.cm.submit("2", token, "ads", "PRIVATE DETAIL")
    with pytest.raises(Rejected):
        await s.cm.submit("2", token, "ads")
    token2 = await s.cm.ticket(report_update(3))
    assert case == await s.cm.submit("3", token2, "fraud")
    assert s.cm.count("-1001", "4") == 0
    assert s.store.db.execute("SELECT COUNT(*) FROM cm_cases").fetchone()[0] == 1
    assert s.store.db.execute("SELECT COUNT(*) FROM cm_reports").fetchone()[0] == 2
    s.bot.delete_message.assert_not_awaited()
    await s.runtime.community_logs.tick()
    sent = str(s.bot.send_message.await_args.kwargs)
    assert "PRIVATE" not in sent and "举报人" not in sent
    await s.runtime.ui.action(private(2), {"action": "cm_mine"})
    assert "PRIVATE" not in s.bot.send_message.await_args.kwargs["text"]
    for number in (11, 12):
        await s.cm.ticket(report_update(2, number))
    with pytest.raises(Rejected, match="频繁"):
        await s.cm.ticket(report_update(2, 13))
    s.clock[0] += 601
    with pytest.raises(Rejected, match="过期"):
        await s.cm.submit("2", token, "ads")


@pytest.mark.asyncio
async def test_case_confirm_competition_and_unknown(community):
    s = community
    cfg = s.cm.policy("-1001")["config"]
    cfg["enabled"]["reports"] = True
    await s.cm.save("1", "-1001", 0, cfg)
    case = await s.cm.submit("2", await s.cm.ticket(report_update()), "ads")
    prepared = await s.mod.preview("1", "-1001", "delete", "10")
    results = await asyncio.gather(
        s.cm.decide("1", case, 1, "delete", "ads", "one", prepared),
        s.cm.decide("1", case, 1, "delete", "ads", "two", prepared),
        return_exceptions=True,
    )
    assert sum(isinstance(r, Rejected) for r in results) == 1
    s.bot.delete_message.assert_awaited_once()
    case2 = await s.cm.submit("3", await s.cm.ticket(report_update(3, 11)), "ads")
    s.bot.delete_message.side_effect = TimeoutError
    prepared = await s.mod.preview("1", "-1001", "delete", "11")
    with pytest.raises(TimeoutError):
        await s.cm.decide("1", case2, 1, "delete", "ads", "unknown", prepared)
    assert (
        s.store.db.execute(
            "SELECT status FROM cm_cases WHERE id=?", (case2,)
        ).fetchone()[0]
        == "review"
    )
    Community(s.runtime)
    with pytest.raises(Rejected):
        await s.cm.decide("1", case2, 2, "delete", "ads", "again", prepared)
    assert s.bot.delete_message.await_count == 2


@pytest.mark.asyncio
async def test_shared_ledger_warning_ad_duplicate_expiry_and_revoke(community):
    s = community
    cfg = s.cm.policy("-1001")["config"]
    cfg["enabled"]["warnings"] = True
    await s.cm.save("1", "-1001", 0, cfg)
    await s.cm.warn("1", "-1001", "2", "other", "warn1", 20)
    ak = defaults()
    ak["rules"]["links"]["enabled"] = True
    s.runtime.ad_killer.save("-1001", "1", 0, ak, True)
    await s.runtime.ad_killer.inspect(message("https://spam.example", number=20))
    assert s.cm.count("-1001", "2") == 1
    await s.runtime.ad_killer.inspect(message("https://spam.example", number=21))
    assert s.cm.count("-1001", "2") == 2
    await s.runtime.ui.action(
        private(),
        {"action": "cm_revoke", "chat": "-1001", "id": "warn1", "version": 1},
        "revoke",
    )
    assert s.cm.count("-1001", "2") == 1
    assert s.store.db.execute(
        "SELECT false_positive FROM ak_hits WHERE message=20"
    ).fetchone()[0]
    s.clock[0] += 86401
    assert s.cm.count("-1001", "2") == 0
    assert s.store.db.execute("SELECT COUNT(*) FROM cm_violations").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_manual_threshold_once_and_unknown_does_not_replay(community):
    s = community
    cfg = s.cm.policy("-1001")["config"]
    cfg["enabled"]["warnings"] = True
    await s.cm.save("1", "-1001", 0, cfg)
    ak = defaults()
    ak["escalation"] = {"enabled": True, "count": 2, "action": "mute"}
    s.runtime.ad_killer.save("-1001", "1", 0, ak, True)
    await s.cm.warn("1", "-1001", "2", "other", "one")
    assert "受理" in await s.cm.warn("1", "-1001", "2", "other", "two")
    await s.cm.warn("1", "-1001", "2", "other", "two")
    s.bot.restrict_chat_member.assert_awaited_once()
    await s.cm.warn("1", "-1001", "3", "other", "three")
    s.bot.restrict_chat_member.side_effect = TimeoutError
    assert "核查" in await s.cm.warn("1", "-1001", "3", "other", "four")
    assert s.runtime.ad_killer.policy("-1001")["error"]
    Community(s.runtime)
    await s.cm.warn("1", "-1001", "3", "other", "four")
    assert s.bot.restrict_chat_member.await_count == 2
    s.bot.unban_chat_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_notes_form_limits_and_no_command_urls(community):
    s = community
    ui = s.runtime.ui
    await ui.action(private(), {"action": "cm_note_edit", "chat": "-1001", "index": 0})
    for value in ("FAQ", "Safe answer"):
        await community_ui.input_text(ui, private(), s.store.dialog("1"), value)
    payload = s.store.resolve(
        s.bot.send_message.await_args.kwargs["reply_markup"]
        .inline_keyboard[0][0]
        .callback_data[3:],
        "1",
        "1",
    )
    await ui.action(private(), payload)
    confirm = s.store.resolve(
        s.bot.send_message.await_args.kwargs["reply_markup"]
        .inline_keyboard[0][0]
        .callback_data[3:],
        "1",
        "1",
    )
    await ui.action(private(), confirm, "save")
    cfg = s.cm.policy("-1001")["config"]
    assert cfg["notes"] == [{"title": "FAQ", "body": "Safe answer", "url": ""}]
    cfg["notes"][0]["url"] = "tg://resolve?domain=internal"
    with pytest.raises(Rejected):
        await s.cm.save("1", "-1001", 1, cfg)
    cfg["notes"][0]["url"] = ""
    cfg["notes"] *= 11
    with pytest.raises(Rejected):
        await s.cm.save("1", "-1001", 1, cfg)
    await ui.action(private(), {"action": "cm_edit", "chat": "-1001", "field": "rules"})
    await ui.action(private(), {"action": "cm_settings", "chat": "-1001"})
    assert s.store.dialog("1") is None


@pytest.mark.asyncio
async def test_welcome_dedup_and_timeout_no_replay(community):
    s = community
    cfg = s.cm.policy("-1001")["config"]
    cfg["enabled"]["welcome"] = True
    cfg["welcome"] = "Hello {name}, {group}"
    await s.cm.save("1", "-1001", 0, cfg)
    user = SimpleNamespace(id=2, is_bot=False, full_name="<b>Person</b>")
    await s.cm.welcome("-1001", user, 99, "<Test>")
    await s.cm.welcome("-1001", user, 99, "<Test>")
    s.bot.send_message.assert_awaited_once()
    assert s.bot.send_message.await_args.kwargs["parse_mode"] is None
    s.bot.send_message.side_effect = TimeoutError
    with pytest.raises(TimeoutError):
        await s.cm.welcome("-1001", user, 100, "<Test>")
    Community(s.runtime)
    await s.cm.welcome("-1001", user, 100, "<Test>")
    assert s.bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_log_privacy_retry_unknown_and_restart(community):
    s = community
    logs = s.runtime.community_logs
    channel = SimpleNamespace(
        id=-1009, title="Private audit", type="channel", username=None
    )
    s.bot.get_chat.side_effect = lambda chat: (
        channel if str(chat) == "-1009" else s.group
    )
    original = s.bot.get_chat_member.side_effect
    own = Member("administrator")
    own.can_post_messages = True
    s.bot.get_chat_member.side_effect = lambda chat, uid: (
        own if str(chat) == "-1009" else original(chat, uid)
    )
    cfg = s.cm.policy("-1001")["config"]
    cfg["log"] = {"enabled": True, "channel": "-1009", "title": "Private audit"}
    await s.cm.save("1", "-1001", 0, cfg)
    with s.store.tx() as db:
        s.store.audit(
            db, "2", "community_report", {"chat": "-1001", "secret": "DO NOT EXPORT"}
        )
        s.store.audit(
            db,
            "1",
            "community_case_closed",
            {"chat": "-1001", "case": 9, "reporter": "2", "body": "DO NOT EXPORT"},
        )
    logs.project()
    assert "DO NOT EXPORT" not in str(
        [tuple(r) for r in s.store.db.execute("SELECT * FROM cm_delivery")]
    )
    s.bot.send_message.side_effect = RetryAfter(12)
    await logs.tick()
    assert (
        s.store.db.execute(
            "SELECT status FROM cm_delivery ORDER BY rowid LIMIT 1"
        ).fetchone()[0]
        == "pending"
    )
    s.clock[0] += 13
    s.bot.send_message.side_effect = TimeoutError
    await logs.tick()
    assert (
        s.store.db.execute(
            "SELECT status FROM cm_delivery ORDER BY rowid LIMIT 1"
        ).fetchone()[0]
        == "review"
    )
    logs = CommunityLogs(s.runtime)
    s.bot.send_message.side_effect = None
    await logs.tick()
    assert s.bot.send_message.await_count == 3
    await logs.tick()
    assert s.bot.send_message.await_count == 3
    channel.username = "public"
    with pytest.raises(Rejected):
        await logs.check_channel("1", "-1009")
    channel.username = None
    logs.enqueue("blocked", "-1001", "-1009", "fixture", "Minimal", "1")
    s.bot.send_message.side_effect = Forbidden("no")
    await logs.tick()
    assert (
        s.store.db.execute(
            "SELECT status FROM cm_delivery WHERE id='blocked'"
        ).fetchone()[0]
        == "blocked"
    )


@pytest.mark.asyncio
async def test_retention_closed_only_and_idempotence(community):
    s = community
    cfg = s.cm.policy("-1001")["config"]
    cfg["enabled"]["reports"] = True
    await s.cm.save("1", "-1001", 0, cfg)
    closed = await s.cm.submit("2", await s.cm.ticket(report_update()), "ads", "secret")
    opened = await s.cm.submit(
        "3", await s.cm.ticket(report_update(3, 11)), "ads", "keep"
    )
    await s.cm.decide("1", closed, 1, "ignore", "other", "ignore")
    s.clock[0] += 30 * 86400 + 1
    s.cm.cleanup()
    assert (
        s.store.db.execute(
            "SELECT body FROM cm_cases WHERE id=?", (closed,)
        ).fetchone()[0]
        is None
    )
    assert (
        s.store.db.execute(
            "SELECT detail FROM cm_reports WHERE case_id=?", (closed,)
        ).fetchone()[0]
        is None
    )
    assert (
        s.store.db.execute(
            "SELECT body FROM cm_cases WHERE id=?", (opened,)
        ).fetchone()[0]
        == "PRIVATE EVIDENCE"
    )
    assert s.store.db.execute("SELECT COUNT(*) FROM cm_reports").fetchone()[0] == 2
    s.cm.cleanup()


@pytest.mark.asyncio
async def test_rule_changes_while_warning_waits_for_lock(community):
    s = community
    cfg = s.cm.policy("-1001")["config"]
    cfg["enabled"]["warnings"] = True
    await s.cm.save("1", "-1001", 0, cfg)
    lock = s.mod.locks.setdefault("-1001", asyncio.Lock())
    await lock.acquire()
    task = asyncio.create_task(
        s.cm.warn(
            "1",
            "-1001",
            "2",
            "other",
            "stale",
            expected={"version": 1, "ak_version": 0},
        )
    )
    await asyncio.sleep(0)
    s.store.db.execute("UPDATE cm_config SET version=version+1")
    lock.release()
    with pytest.raises(Rejected):
        await task
    assert s.cm.count("-1001", "2") == 0


@pytest.mark.asyncio
async def test_report_write_failure_rolls_back_claim_and_case(community):
    s = community
    cfg = s.cm.policy("-1001")["config"]
    cfg["enabled"]["reports"] = True
    await s.cm.save("1", "-1001", 0, cfg)
    token = await s.cm.ticket(report_update())

    def deny_report_insert(operation, table, *_):
        return (
            sqlite3.SQLITE_DENY
            if operation == sqlite3.SQLITE_INSERT and table == "cm_reports"
            else sqlite3.SQLITE_OK
        )

    s.store.db.set_authorizer(deny_report_insert)
    with pytest.raises(sqlite3.DatabaseError):
        await s.cm.submit("2", token, "other")
    s.store.db.set_authorizer(None)
    assert (
        s.store.db.execute(
            "SELECT used FROM cm_tickets WHERE token=?", (token,)
        ).fetchone()[0]
        == 0
    )
    assert s.store.db.execute("SELECT COUNT(*) FROM cm_cases").fetchone()[0] == 0
    await s.cm.submit("2", token, "other")


@pytest.mark.asyncio
async def test_log_preflight_failure_backoff_and_revocation(community):
    s = community
    logs = s.runtime.community_logs
    s.store.db.execute("INSERT INTO cm_private VALUES('1',?)", (s.clock[0],))
    logs.enqueue(
        "private", "-1001", "1", "report_notice", "Pending case", "1", private=True
    )
    original = s.bot.get_chat_member.side_effect
    s.bot.get_chat_member.side_effect = TimeoutError
    await logs.tick()
    assert (
        s.store.db.execute("SELECT status FROM cm_delivery").fetchone()[0] == "pending"
    )
    s.bot.send_message.assert_not_awaited()
    s.clock[0] += 31
    s.bot.get_chat_member.side_effect = original
    s.members[1] = Member("member")
    await logs.tick()
    assert (
        s.store.db.execute("SELECT status FROM cm_delivery").fetchone()[0] == "blocked"
    )
    s.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_command_links_and_foreign_command(community):
    from unittest.mock import MagicMock

    from telegram.ext import ApplicationHandlerStop

    from data.plugins.astrbot_plugin_superbot.main import Main

    s = community
    cfg = s.cm.policy("-1001")["config"]
    cfg["enabled"]["reports"] = True
    await s.cm.save("1", "-1001", 0, cfg)
    update = report_update()
    await community_ui.group_command(s.runtime, update, "/report")
    sent = s.bot.send_message.await_args.kwargs
    assert "举报已受理" in sent["text"]
    assert s.store.db.execute("SELECT COUNT(*) FROM cm_reports").fetchone()[0] == 1
    assert "PRIVATE EVIDENCE" not in sent["text"]
    plugin = Main(MagicMock(), {})
    plugin.store, plugin.moderation = s.store, s.mod
    plugin.ad_killer, plugin.community = s.runtime.ad_killer, s.cm
    plugin.application = SimpleNamespace(bot=s.bot, running=True)
    update.message.text = "/report@someone_else"
    s.bot.send_message.reset_mock()
    with pytest.raises(ApplicationHandlerStop):
        await plugin.receive(update, None)
    s.bot.send_message.assert_not_awaited()
    assert not plugin.active_updates


@pytest.mark.asyncio
async def test_upgrade_does_not_backfill_historic_ad_hits(community):
    s = community
    s.store.db.execute(
        "INSERT INTO ak_hits(chat,message,uid,rules,action,version,body,status,step,at) VALUES('-1001',5,'2','[]','delete',1,'old','accepted','done',?)",
        (s.clock[0],),
    )
    Community(s.runtime)
    assert s.cm.count("-1001", "2") == 0


@pytest.mark.asyncio
async def test_single_use_and_cross_private_confirmation(community):
    s = community
    cfg = s.cm.policy("-1001")["config"]
    payload = {"action": "cm_save", "chat": "-1001", "config": cfg, "version": 0}
    token = s.store.callback("1", "1", payload)[3:]
    assert s.store.resolve(token, "1", "1") == payload
    with pytest.raises(Rejected):
        s.store.resolve(token, "2", "2")
    with pytest.raises(Rejected):
        s.store.resolve(token, "1", "-1001")
    s.store.consume(token)
    await s.runtime.ui.action(private(), payload, token)
    with pytest.raises(Rejected):
        s.store.consume(token)
    expired = s.store.callback("1", "1", payload)[3:]
    s.clock[0] += 601
    with pytest.raises(Rejected):
        s.store.resolve(expired, "1", "1")


@pytest.mark.asyncio
async def test_unload_waits_without_cancelling_transport():
    from unittest.mock import MagicMock

    from data.plugins.astrbot_plugin_superbot.main import Main

    plugin = Main(MagicMock(), {})
    plugin.store = MagicMock()
    release = asyncio.Event()
    transport = asyncio.create_task(release.wait())
    plugin.active_updates.add(transport)
    stopping = asyncio.create_task(plugin.terminate())
    await asyncio.sleep(0)
    assert not stopping.done()
    plugin.store.close.assert_not_called()
    assert not transport.cancelled()
    release.set()
    await stopping
    plugin.store.close.assert_called_once()
    assert not transport.cancelled()


@pytest.mark.asyncio
async def test_reusable_template_keeps_penalties_and_log_unchanged(community):
    import json
    from pathlib import Path

    s = community
    before = s.runtime.ad_killer.policy("-1001")
    template = json.loads(
        (Path(community_ui.__file__).parent / "community_defaults.json").read_text()
    )
    await s.cm.save("1", "-1001", 0, template)
    assert all(s.cm.policy("-1001")["config"]["enabled"].values())
    assert not s.cm.policy("-1001")["config"]["log"]["enabled"]
    assert s.runtime.ad_killer.policy("-1001") == before
    await s.runtime.ui.action(private(), {"action": "cm_template", "chat": "-1001"})
    markup = s.bot.send_message.await_args.kwargs["reply_markup"]
    token = markup.inline_keyboard[0][0].callback_data[3:]
    payload = s.store.resolve(token, "1", "1")
    assert payload["action"] == "cm_save"
    assert payload["version"] == 1
    assert payload["config"]["log"] == template["log"]
    with pytest.raises(Rejected):
        await s.runtime.ui.action(
            private(2), {"action": "cm_template", "chat": "-1001"}
        )
