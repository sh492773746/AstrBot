"""Offline consent, privacy and rule-review regressions."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import ChatPermissions

from data.plugins.astrbot_plugin_superbot.ad_killer import AdKiller
from data.plugins.astrbot_plugin_superbot.group_identity import group_label
from data.plugins.astrbot_plugin_superbot.moderation import Moderation
from data.plugins.astrbot_plugin_superbot.semantic_review import SemanticReview
from data.plugins.astrbot_plugin_superbot.semantic_review_ui import action, input_text
from data.plugins.astrbot_plugin_superbot.store import Rejected, Store
from data.plugins.astrbot_plugin_superbot.tenant_ui import _chat_label
from data.plugins.astrbot_plugin_superbot.tenant_ui import action as tenant_action
from data.plugins.astrbot_plugin_superbot.ui import UI


@pytest.fixture
def setup(tmp_path):
    now = [2_000_000_000.0]
    store = Store(tmp_path / "semantic.sqlite3", "1", lambda: now[0])
    bot = SimpleNamespace(id=99, send_message=AsyncMock())
    bot.get_chat = AsyncMock(return_value=SimpleNamespace(type="supergroup"))
    members = {
        1: SimpleNamespace(status="administrator"),
        2: SimpleNamespace(status="administrator"),
        3: SimpleNamespace(status="member"),
        99: SimpleNamespace(status="administrator"),
    }
    for member in members.values():
        member.to_dict = lambda member=member: {"status": member.status}
    bot.get_chat_member = AsyncMock(side_effect=lambda chat, uid: members[uid])
    runtime = SimpleNamespace(store=store, bot=bot)
    runtime.application = SimpleNamespace(running=True)
    runtime.moderation = Moderation(runtime)
    store.db.execute(
        "INSERT INTO mod_groups(chat,title,enabled) VALUES('-1001','Test',1)"
    )
    runtime.ad_killer = AdKiller(runtime)
    runtime.semantic_review = SemanticReview(runtime)
    with store.tx() as db:
        store.put(db, "modules", {"moderation": True})
    ui = SimpleNamespace(store=store, runtime=runtime, render=AsyncMock())
    yield SimpleNamespace(store=store, runtime=runtime, ui=ui, now=now, members=members)
    store.close()


def event(text, *, edited=False, message_id=5, sender=None):
    message = SimpleNamespace(
        message_id=message_id,
        text=text,
        caption=None,
        sender_chat=sender,
        from_user=SimpleNamespace(id=3, is_bot=False),
        reply_to_message=None,
    )
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=-1001, type="supergroup"),
        message=None if edited else message,
        edited_message=message if edited else None,
    )


def private(uid=2):
    return SimpleNamespace(
        effective_chat=SimpleNamespace(type="private", id=uid),
        effective_user=SimpleNamespace(id=uid),
    )


@pytest.mark.asyncio
async def test_notice_before_capture_and_permission_recheck(setup):
    review, ui = setup.runtime.semantic_review, setup.ui
    assert not review.capture(event("not recorded"))
    await action(ui, private(), {"action": "semantic_notice", "chat": "-1001"})
    setup.runtime.bot.send_message.side_effect = RuntimeError("unknown send")
    with pytest.raises(Rejected):
        await action(
            ui, private(), {"action": "semantic_start", "chat": "-1001", "version": 0}
        )
    assert not review.capture(event("still not recorded"))
    setup.runtime.bot.send_message.side_effect = None
    setup.members[2].status = "member"
    with pytest.raises(Rejected):
        await action(
            ui, private(), {"action": "semantic_start", "chat": "-1001", "version": 0}
        )
    setup.members[2].status = "administrator"
    await action(
        ui, private(), {"action": "semantic_start", "chat": "-1001", "version": 0}
    )
    assert review.capture(event("one actual message"))
    assert not review.capture(event("one actual message"))
    assert not review.capture(
        SimpleNamespace(
            effective_chat=SimpleNamespace(id=2, type="private"),
            message=event("private").message,
            edited_message=None,
        )
    )


@pytest.mark.asyncio
async def test_notice_sender_revoked_during_delivery_never_enables(setup):
    async def delivered(**kwargs):
        setup.members[2].status = "member"

    setup.runtime.bot.send_message.side_effect = delivered
    with pytest.raises(Rejected):
        await action(
            setup.ui,
            private(),
            {"action": "semantic_start", "chat": "-1001", "version": 0},
        )
    assert setup.runtime.semantic_review.consent("-1001") is None


@pytest.mark.asyncio
async def test_my_groups_lists_only_current_telegram_administrators(setup):
    setup.runtime.tenants = SimpleNamespace()
    setup.runtime.merchant_ads = SimpleNamespace()
    await tenant_action(setup.ui, private(2), {"action": "tenant_home"})
    buttons = setup.ui.render.await_args.args[2]
    assert ("Test", {"action": "semantic_group_menu", "chat": "-1001"}) in buttons
    setup.members[2].status = "member"
    await tenant_action(setup.ui, private(2), {"action": "tenant_home"})
    assert all(label != "Test" for label, _ in setup.ui.render.await_args.args[2])


@pytest.mark.asyncio
async def test_my_groups_checks_only_one_page_of_telegram_groups(setup):
    store = setup.store
    store.db.executemany(
        "INSERT INTO mod_groups(chat,title,enabled) VALUES(?,?,1)",
        [(str(-2000 - n), f"Other {n:02}") for n in range(40)],
    )
    setup.runtime.tenants = SimpleNamespace()
    setup.runtime.merchant_ads = SimpleNamespace()
    await tenant_action(setup.ui, private(2), {"action": "tenant_home"})
    assert setup.runtime.bot.get_chat_member.await_count <= 16
    setup.runtime.bot.get_chat_member.reset_mock()
    await tenant_action(setup.ui, private(2), {"action": "tenant_home", "page": 1})
    assert setup.runtime.bot.get_chat_member.await_count <= 16


@pytest.mark.asyncio
async def test_old_game_group_selector_hides_foreign_groups_from_superadmin(setup):
    from data.plugins.astrbot_plugin_superbot.game_groups_admin import action as games

    setup.store.db.execute(
        "INSERT INTO mod_groups(chat,title,enabled) VALUES('-1002','Other group',1)"
    )

    async def membership(chat, uid):
        if str(chat) == "-1002" and uid == 1:
            return SimpleNamespace(status="member")
        return SimpleNamespace(status="administrator")

    setup.runtime.bot.get_chat_member.side_effect = membership
    await games(setup.ui, private(1), {"action": "games_group_list"})
    buttons = setup.ui.render.await_args.args[2]
    assert all(label != "Other group" for label, _ in buttons)
    assert any(label == "Test" for label, _ in buttons)


def test_edit_versions_capacity_retention_clear(setup):
    review, store = setup.runtime.semantic_review, setup.store
    review.start("-1001", "2", 0)
    assert review.capture(event("initial"))
    assert review.capture(event("edited A", edited=True))
    assert not review.capture(event("edited A", edited=True))
    assert review.capture(event("edited B", edited=True))
    assert [
        r["revision"]
        for r in store.db.execute("SELECT revision FROM sr_messages ORDER BY revision")
    ] == [0, 1, 2]
    review.label("-1001", 5, 1, "advert", "manual", "2")
    with store.tx() as db:
        store.put(db, "semantic_group_bytes", 3)
    assert not review.capture(event("capacity", message_id=6))
    assert review.consent("-1001")["gap"] == "capacity"
    setup.now[0] += 8 * 86400
    review.cleanup()
    assert [
        r["revision"]
        for r in store.db.execute("SELECT revision FROM sr_messages ORDER BY revision")
    ] == [1]
    review.clear("-1001", "2", review.consent("-1001")["version"])
    assert not review.capture(event("after clear", message_id=7))
    assert not store.db.execute("SELECT 1 FROM sr_messages").fetchone()
    assert not store.db.execute("SELECT 1 FROM sr_labels").fetchone()


@pytest.mark.asyncio
async def test_group_confirmation_label_uses_public_handle_or_private_title(setup):
    bot = setup.runtime.bot
    bot.get_chat.return_value = SimpleNamespace(
        type="supergroup", title="目标群", username="target"
    )
    assert await _chat_label(setup.runtime, setup.store, "-1001") == "目标群 (@target)"
    bot.get_chat.return_value.username = None
    assert await _chat_label(setup.runtime, setup.store, "-1001") == "目标群 (私密群)"
    bot.get_chat.side_effect = RuntimeError("temporarily unavailable")
    assert await _chat_label(setup.runtime, setup.store, "-1001") == "Test"


def test_expired_samples_inaccessible_before_scheduled_cleanup(setup):
    review = setup.runtime.semantic_review
    review.start("-1001", "2", 0)
    review.capture(event("expire"))
    setup.now[0] += 7 * 86400 + 1
    assert not review.rows("-1001")
    with pytest.raises(Rejected):
        review.label("-1001", 5, 0, "normal", "late", "2")


@pytest.mark.asyncio
async def test_owner_draft_trial_publish_and_conflict(setup):
    review, store = setup.runtime.semantic_review, setup.store
    review.start("-1001", "2", 0)
    review.capture(event("普通聊天", message_id=5))
    review.label("-1001", 5, 0, "normal", "review", "2")
    review.capture(event("加我咨询推广", message_id=6))
    review.label("-1001", 6, 0, "advert", "review", "2")
    key = review.draft("-1001", "1", "加我+推广")
    result = review.trial(key, "1")
    assert result["hit"] == 1
    assert result["false_positive"] == result["missed"] == 0
    assert not store.db.execute("SELECT 1 FROM ak_hits").fetchone()
    review.submit(key, "1")
    setup.members[2].status = "member"
    with pytest.raises(Rejected):
        await review.adopt(key, "2")
    setup.members[2].status = "administrator"
    # A first-ever policy needs an explicit policy row to adopt against.
    policy = setup.runtime.ad_killer.policy("-1001")
    from data.plugins.astrbot_plugin_superbot.ad_killer import defaults

    setup.runtime.ad_killer.save("-1001", "1", policy["version"], defaults(), False)
    with pytest.raises(Rejected, match="版本"):
        await review.adopt(key, "2")


@pytest.mark.asyncio
async def test_first_policy_adoption_undo_and_stale_confirmation(setup):
    review = setup.runtime.semantic_review
    review.start("-1001", "2", 0)
    review.capture(event("hello world"))
    key = review.draft("-1001", "1", "hello")
    review.trial(key, "1")
    review.submit(key, "1")
    await review.adopt(key, "2")
    policy = setup.runtime.ad_killer.policy("-1001")
    assert policy["version"] == 1
    assert not policy["enabled"]
    assert '"enabled":false' in policy["config"]
    assert '"hello"' in policy["config"]
    await review.undo(key, "2")
    assert setup.runtime.ad_killer.policy("-1001")["version"] == 0
    with pytest.raises(Rejected):
        await review.undo(key, "2")


def test_local_package_is_private_masked_and_expires(setup):
    review, store = setup.runtime.semantic_review, setup.store
    review.start("-1001", "2", 0)
    review.capture(
        event(
            "推广联系 @someone https://t.me/+Invite 微信:secret123 ABC 手机号13812345678"
        )
    )
    key, count = review.package("-1001", "1")
    assert count == 1
    row = store.db.execute("SELECT path FROM sr_packages WHERE id=?", (key,)).fetchone()
    from pathlib import Path

    path = Path(row["path"])
    assert path.stat().st_mode & 0o777 == 0o600
    content = path.read_text()
    assert "@someone" not in content
    assert "13812345678" not in content
    assert "secret123" not in content
    assert "Invite" not in content
    assert "推广联系" in content
    setup.now[0] += 86401
    review.cleanup()
    assert not path.exists()


@pytest.mark.asyncio
async def test_clear_revokes_adopted_undo_without_changing_live_policy(setup):
    review, store = setup.runtime.semantic_review, setup.store
    review.start("-1001", "2", 0)
    review.capture(event("推广 www.sample.org/path bare.example.net 请联系 +1 (202) 555-0123"))
    key = review.draft("-1001", "1", "推广")
    review.trial(key, "1")
    review.submit(key, "1")
    await review.adopt(key, "2")
    live = dict(setup.runtime.ad_killer.policy("-1001"))
    package, _ = review.package("-1001", "1")
    from pathlib import Path

    path = Path(store.db.execute(
        "SELECT path FROM sr_packages WHERE id=?", (package,)
    ).fetchone()["path"])
    content = path.read_text()
    assert "sample.org" not in content
    assert "example.net" not in content
    assert "202" not in content
    review.clear("-1001", "2", review.consent("-1001")["version"])
    assert dict(setup.runtime.ad_killer.policy("-1001")) == live
    assert not path.exists()
    draft = store.db.execute("SELECT * FROM sr_drafts WHERE id=?", (key,)).fetchone()
    assert draft["config"] == "{}" and draft["before_config"] is None
    assert not store.db.execute("SELECT 1 FROM sr_trials").fetchone()
    with pytest.raises(Rejected):
        await review.undo(key, "2")


@pytest.mark.asyncio
async def test_platform_rejoin_requires_original_creator_and_stale_button_fails(setup):
    ui, store = setup.ui, setup.store
    setup.members[1].status = "creator"
    store.suspend_group("-1001")
    store.register_group("-1001", "Test", "1", fresh=True)
    real_ui = UI(setup.runtime)
    real_ui.render = AsyncMock()
    await real_ui.action(private(1), {"action": "mod_group", "chat": "-1001"})
    enable = next(
        p for label, p in real_ui.render.await_args.args[2]
        if label == "开启本群群管服务"
    )
    await real_ui.action(private(1), enable)
    legacy_confirm = real_ui.render.await_args.args[2][0][1]
    with pytest.raises(Rejected, match="确认重新接入"):
        await real_ui.action(private(1), legacy_confirm)
    await action(ui, private(1), {"action": "semantic_group_menu", "chat": "-1001"})
    buttons = ui.render.await_args.args[2]
    preview = next(p for label, p in buttons if label == "确认重新接入")
    await action(ui, private(1), preview)
    confirm = ui.render.await_args.args[2][0][1]
    with pytest.raises(Rejected):
        await action(ui, private(2), confirm)
    setup.members[1].status = "member"
    with pytest.raises(Rejected):
        await action(ui, private(1), confirm)
    setup.members[1].status = "creator"
    await action(ui, private(1), confirm)
    assert store.db.execute(
        "SELECT enabled FROM mod_groups WHERE chat='-1001'"
    ).fetchone()[0] == 1
    with pytest.raises(Rejected):
        await action(ui, private(1), confirm)


def test_cleanup_batches_expired_messages(setup):
    review, store = setup.runtime.semantic_review, setup.store
    review.start("-1001", "2", 0)
    for number in range(210):
        assert review.capture(event("expired", message_id=number + 1))
    setup.now[0] += 8 * 86400
    assert not review.cleanup()
    assert store.db.execute("SELECT count(*) FROM sr_messages").fetchone()[0] == 10
    assert review.cleanup()
    assert not store.db.execute("SELECT 1 FROM sr_messages").fetchone()


def test_cleanup_batches_old_drafts_and_scrubs_all_states(setup):
    review, store = setup.runtime.semantic_review, setup.store
    old = setup.now[0] - 31 * 86400
    store.db.executemany(
        "INSERT INTO sr_drafts(id,chat,actor,base_version,config,state,created,"
        "adopted_version,before_config,before_enabled) "
        "VALUES(?,'-1001','1',1,?,'adopted',?,2,?,1)",
        [(str(n), '{"keywords":"secret"}', old, '{"keywords":"older"}')
        for n in range(205)],
    )
    store.db.execute(
        "INSERT INTO sr_trials(id,draft,result,at) VALUES('late','204',?,?)",
        ('{"evidence":"secret"}', setup.now[0]),
    )
    assert not review.cleanup()
    assert store.db.execute(
        "SELECT count(*) FROM sr_drafts WHERE before_config IS NOT NULL"
    ).fetchone()[0] == 5
    assert review.cleanup()
    assert not store.db.execute("SELECT 1 FROM sr_trials").fetchone()
    assert store.db.execute(
        "SELECT count(*) FROM sr_drafts WHERE state='revoked' AND config='{}' "
        "AND before_config IS NULL"
    ).fetchone()[0] == 205


@pytest.mark.asyncio
async def test_platform_admin_live_permission_without_business_role(setup):
    runtime = setup.runtime
    ui = UI(runtime)
    ui.render = AsyncMock()
    await ui.action(private(), {"action": "mod_group", "chat": "-1001"})
    assert "Test" in ui.render.await_args.args[1]
    setup.members[2].status = "member"
    with pytest.raises(Rejected):
        await ui.action(private(), {"action": "mod_group", "chat": "-1001"})
    setup.members[2].status = "administrator"
    with pytest.raises(Rejected):
        await ui.action(private(3), {"action": "mod_group", "chat": "-1001"})


@pytest.mark.asyncio
async def test_group_identity_and_split_moderation_menus(setup):
    ui = UI(setup.runtime)
    ui.render = AsyncMock()
    setup.runtime.bot.get_chat.return_value = SimpleNamespace(
        id=-1001,
        title="目标测试群",
        type="supergroup",
        username="testgroup",
        permissions=ChatPermissions.all_permissions(),
    )
    await ui.action(private(), {"action": "semantic_group_menu", "chat": "-1001"})
    text, buttons = ui.render.await_args.args[1:3]
    assert "目标测试群 (@testgroup)" in text and "-1001" not in text
    assert ("成员管理", {"action": "mod_group", "chat": "-1001"}) in buttons
    assert ("全群禁言", {"action": "mod_all", "chat": "-1001"}) in buttons

    await ui.action(private(), {"action": "mod_group", "chat": "-1001"})
    text, buttons = ui.render.await_args.args[1:3]
    assert "目标测试群 (@testgroup)" in text
    assert {p["kind"] for _, p in buttons if p["action"] == "mod_form"} == {
        "mute",
        "mute_forever",
        "unmute",
        "kick",
        "ban",
        "unban",
        "delete",
        "disable",
    }
    assert "群虚拟键盘" not in str(buttons)
    assert "加拿大28当前倍率" not in str(buttons)
    assert "广告杀手" not in str(buttons)
    await ui.action(private(), {"action": "mod_all", "chat": "-1001"})
    text, buttons = ui.render.await_args.args[1:3]
    assert "目标测试群 (@testgroup)" in text
    assert {p["kind"] for _, p in buttons if p["action"] == "mod_form"} == {"mute_all"}
    assert "🟢 可发言" in text and "🔴 无" in text
    assert ("定时禁言设置", {"action": "mod_schedule", "chat": "-1001"}) in buttons
    assert "删除消息" not in str(buttons)

    setup.runtime.bot.get_chat.return_value.username = None
    await ui.action(private(), {"action": "mod_group", "chat": "-1001"})
    assert "目标测试群 (私密群)" in ui.render.await_args.args[1]
    assert group_label(None, "目标测试群") == "目标测试群"
    with pytest.raises(Rejected, match="已移除"):
        await ui.action(private(), {"action": "mod_keyboard_on", "chat": "-1001"})
    setup.members[2].status = "member"
    with pytest.raises(Rejected):
        await ui.action(private(), {"action": "mod_all", "chat": "-1001"})


def test_trial_repeat_and_flood_have_no_enforcement_writes(setup):
    review, store = setup.runtime.semantic_review, setup.store
    review.start("-1001", "2", 0)
    for number in range(1, 4):
        review.capture(event("相同的推广推广推广推广推广内容", message_id=number))
        setup.now[0] += 1
    key = review.draft("-1001", "1", "3", "repeat_count")
    report = review.trial(key, "1")
    assert report["hit"] == 1
    assert report["evidence"][0]["rules"] == ["repeat"]
    assert not store.db.execute("SELECT 1 FROM ak_hits").fetchone()
    assert not store.db.execute("SELECT 1 FROM ak_seen").fetchone()
    assert not store.db.execute(
        "SELECT 1 FROM audit WHERE action='moderation_mute'"
    ).fetchone()


@pytest.mark.asyncio
async def test_owner_capacity_form_without_group_and_revoked_access(setup):
    ui = setup.ui
    await action(ui, private(1), {"action": "semantic_limit_input", "field": "group"})
    update = private(1)
    update.message = SimpleNamespace(text="25")
    await input_text(ui, update, {"form": "semantic_limit", "field": "group"})
    assert "25" in ui.render.await_args.args[1]
    with pytest.raises(Rejected):
        await action(ui, private(2), {"action": "semantic_limits"})
    setup.members[2].status = "member"
    with pytest.raises(Rejected):
        await action(
            ui, private(2), {"action": "semantic_clear_preview", "chat": "-1001"}
        )


def test_storage_read_failure_does_not_block_group_messages(setup):
    review = setup.runtime.semantic_review
    review.start("-1001", "2", 0)
    review.consent = lambda chat: (_ for _ in ()).throw(RuntimeError("db read failed"))
    assert not review.capture(event("message"))
