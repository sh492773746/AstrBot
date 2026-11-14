"""Isolated acceptance of group-scoped forms, permissions and daily admission."""

# ruff: noqa: F811

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from telegram import ChatPermissions, User
from test_superbot_tenant_settings_matrix import configured  # noqa: F401
from test_superbot_tenants import merchant  # noqa: F401

from data.plugins.astrbot_plugin_superbot.game_schedule import (
    GAMES,
    reason,
    save,
    snapshot,
)
from data.plugins.astrbot_plugin_superbot.semantic_review import SemanticReview
from data.plugins.astrbot_plugin_superbot.store import Rejected
from data.plugins.astrbot_plugin_superbot.tenant_ui import FEATURES, find_ad_groups
from data.plugins.astrbot_plugin_superbot.tenants import (
    ADMIN_CONTEXT,
    binding,
    local_config,
)
from data.plugins.astrbot_plugin_superbot.ui import UI


@pytest.fixture
def form(configured):
    runtime, clock, _ = configured
    original = runtime.bot.get_chat_member.side_effect
    member = SimpleNamespace(
        status="member", user=User(30, "群员 <测试>", False, username="member30")
    )
    admin = SimpleNamespace(status="administrator")

    async def lookup(chat, uid):
        if str(chat) == "-1001" and int(uid) == 30:
            return member
        if str(chat) == "-1001" and int(uid) == 40:
            return admin
        return await original(chat, uid)

    runtime.bot.get_chat_member.side_effect = lookup
    runtime.bot.get_chat.return_value = SimpleNamespace(
        id=-1001, type="supergroup", title="目标测试群"
    )
    runtime.store.db.execute(
        "INSERT INTO mod_members VALUES('-1001','30','member','member30',0)"
    )
    runtime.store.db.execute(
        "INSERT INTO mod_members VALUES('-1001','40','admin','admin40',0)"
    )
    with runtime.store.tx() as db:
        runtime.store.put(db, "brand_unified", True)
        runtime.store.credit(db, "seed", "30", 100, "fixture", chat="-1001")
    ui = UI(runtime)
    ui.render = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=10),
        effective_chat=SimpleNamespace(type="private", id=10),
        message=SimpleNamespace(text=""),
    )
    return ui, update, clock, member, admin


@pytest.mark.parametrize("value", ["14", "86401", "abc", "15.5"])
@pytest.mark.asyncio
async def test_interval_prompt_and_invalid_input_preserve_step(form, value):
    ui, event, *_ = form
    await ui.action(
        event, {"action": "tenant_edit", "chat": "-1001", "field": "rewards"}
    )
    for text in ("10", "2"):
        event.message.text = text
        await ui.input(event, ui.store.dialog("10"))
    assert "15—86400" in ui.render.await_args.args[1]
    before = ui.store.dialog("10")
    event.message.text = value
    with pytest.raises(Rejected):
        await ui.input(event, before)
    assert ui.store.dialog("10") == before
    event.message.text = "15"
    await ui.input(event, before)
    assert ui.store.dialog("10")["values"] == [10, 2, 15]


@pytest.mark.parametrize("target", ["@member30", "https://t.me/member30"])
@pytest.mark.parametrize("sign,quantity", [(1, "50"), (-1, "-50"), (-1, "50")])
@pytest.mark.asyncio
async def test_gift_steps_idempotent_ledger_and_group_notification(
    form, target, sign, quantity
):
    ui, event, *_ = form
    await ui.action(
        event, {"action": "tenant_edit", "chat": "-1001", "field": "points"}
    )
    assert "@用户名" in ui.render.await_args.args[1]
    event.message.text = target
    await ui.input(event, ui.store.dialog("10"))
    payload = next(p for _, p in ui.render.await_args.args[2] if p["sign"] == sign)
    assert payload["target"] == "30"
    await ui.action(event, payload)
    event.message.text = quantity
    await ui.input(event, ui.store.dialog("10"))
    assert "原因" not in ui.render.await_args.args[1]
    confirm = ui.render.await_args.args[2][0][1]
    assert ui.store.balance("30", "-1001") == 100
    await ui.action(event, confirm)
    await ui.action(event, confirm)
    assert ui.store.balance("30", "-1001") == 100 + sign * 50
    assert ui.store.balance("30", "-1002") == 0
    rows = ui.store.db.execute(
        "SELECT * FROM notices WHERE id LIKE 'points-gift/%'"
    ).fetchall()
    assert len(rows) == 1 and rows[0]["chat"] == "-1001"
    assert "@member30" in rows[0]["text"]
    assert "本群当前积分" in rows[0]["text"]
    ui.runtime.bot.send_message.assert_not_awaited()


@pytest.mark.parametrize(
    "target", ["30", "@unknown", "https://evil.test/member30", "https://t.me/+invite"]
)
@pytest.mark.asyncio
async def test_gift_unknown_identity_never_changes_points(form, target):
    ui, event, *_ = form
    await ui.action(
        event, {"action": "tenant_edit", "chat": "-1001", "field": "points"}
    )
    event.message.text = target
    with pytest.raises(Rejected):
        await ui.input(event, ui.store.dialog("10"))
    assert ui.store.balance("30", "-1001") == 100
    assert ui.store.dialog("10")["stage"] == "member"


@pytest.mark.parametrize("change", ["left", "renamed", "insufficient"])
@pytest.mark.asyncio
async def test_gift_rechecks_member_and_balance_at_confirmation(form, change):
    ui, event, _, member, _ = form
    await ui.action(
        event, {"action": "tenant_edit", "chat": "-1001", "field": "points"}
    )
    event.message.text = "@member30"
    await ui.input(event, ui.store.dialog("10"))
    await ui.action(event, ui.render.await_args.args[2][1][1])
    event.message.text = "-100"
    await ui.input(event, ui.store.dialog("10"))
    confirm = ui.render.await_args.args[2][0][1]
    if change == "left":
        member.status = "left"
    elif change == "renamed":
        member.user = User(30, "Renamed", False, username="changed30")
    else:
        ui.runtime.points.adjust("10", "30", -1, "fixture", "other", "-1001")
    before = ui.store.balance("30", "-1001")
    with pytest.raises(Rejected):
        await ui.action(event, confirm)
    assert ui.store.balance("30", "-1001") == before
    assert ui.store.db.execute("SELECT COUNT(*) FROM notices").fetchone()[0] == 0


@pytest.mark.parametrize("feature", list(FEATURES))
@pytest.mark.asyncio
async def test_group_admin_can_toggle_only_local_feature_with_brand_mode(form, feature):
    ui, event, _, _, admin = form
    event.effective_user.id = 40
    modules = ui.store.get("modules")
    other = dict(binding(ui.store, "-1002"))
    await ui.action(event, {"action": "tenant_switches", "chat": "-1001"})
    assert any(p.get("feature") == feature for _, p in ui.render.await_args.args[2])
    await ui.action(
        event,
        {
            "action": "tenant_switch_preview",
            "chat": "-1001",
            "feature": feature,
            "enabled": False,
        },
    )
    confirm = ui.render.await_args.args[2][0][1]
    await ui.action(event, confirm)
    assert ui.store.get("modules") == modules
    assert dict(binding(ui.store, "-1002")) == other
    assert ADMIN_CONTEXT.get() is None
    with pytest.raises(Rejected):
        ui.store.require("40", "game", chat="-1001")
    with pytest.raises(Rejected):
        await ui.action(event, confirm)
    await ui.action(
        event,
        {
            "action": "tenant_switch_preview",
            "chat": "-1001",
            "feature": feature,
            "enabled": True,
        },
    )
    await ui.action(event, ui.render.await_args.args[2][0][1])
    if feature in {"ads", "canada", "points"}:
        assert local_config(ui.store, "-1001", feature + "_enabled", False)
    assert ui.store.get("modules") == modules
    admin.status = "member"
    with pytest.raises(Rejected):
        await ui.action(event, {"action": "tenant_switches", "chat": "-1001"})


@pytest.mark.asyncio
async def test_admin_has_no_financial_or_cross_group_authority(form):
    ui, event, *_ = form
    event.effective_user.id = 40
    for data in (
        {"action": "tenant_switches", "chat": "-1002"},
        {"action": "tenant_edit", "chat": "-1001", "field": "address"},
        {"action": "tenant_ads", "chat": "-1001"},
    ):
        with pytest.raises(Rejected):
            await ui.action(event, data)
        assert ADMIN_CONTEXT.get() is None


@pytest.mark.parametrize("uid", [10, 40])
@pytest.mark.asyncio
async def test_tenant_group_owner_and_admin_can_confirm_only_own_moderation(form, uid):
    ui, event, _, _, admin = form
    event.effective_user.id = uid
    runtime = ui.runtime
    runtime.semantic_review = SemanticReview(runtime)
    runtime.bot.get_chat.return_value.permissions = ChatPermissions.all_permissions()
    original = runtime.bot.get_chat_member.side_effect

    async def member(chat, who):
        result = await original(chat, who)
        result.can_restrict_members = True
        return result

    runtime.bot.get_chat_member.side_effect = member
    runtime.bot.set_chat_permissions = AsyncMock()
    await ui.action(event, {"action": "mod_form", "kind": "mute_all", "chat": "-1001"})
    confirm = ui.render.await_args.args[2][0][1]
    await ui.action(event, confirm, "tenant-mute")
    runtime.bot.set_chat_permissions.assert_awaited_once()
    with pytest.raises(Rejected):
        await ui.action(
            event, {"action": "mod_form", "kind": "mute_all", "chat": "-1002"}
        )
    runtime.bot.set_chat_permissions.assert_awaited_once()


@pytest.mark.asyncio
async def test_tenant_admin_without_restrict_permission_denied(form):
    ui, event, _, _, admin = form
    event.effective_user.id = 40
    ui.runtime.semantic_review = SemanticReview(ui.runtime)
    ui.runtime.bot.get_chat.return_value.permissions = ChatPermissions.all_permissions()
    admin.can_restrict_members = False
    original = ui.runtime.bot.get_chat_member.side_effect

    async def member(chat, who):
        result = await original(chat, who)
        if who == ui.runtime.bot.id:
            result.can_restrict_members = True
        return result

    ui.runtime.bot.get_chat_member.side_effect = member
    ui.runtime.bot.set_chat_permissions = AsyncMock()
    with pytest.raises(Rejected, match="对应 Telegram"):
        await ui.action(
            event, {"action": "mod_form", "kind": "mute_all", "chat": "-1001"}
        )
    ui.runtime.bot.set_chat_permissions.assert_not_awaited()


@pytest.mark.asyncio
async def test_tenant_admin_can_enable_disabled_group_from_moderation_page(form):
    ui, event, _, _, _ = form
    event.effective_user.id = 40
    ui.runtime.semantic_review = SemanticReview(ui.runtime)
    ui.runtime.ad_killer = SimpleNamespace(pause=lambda *args: None)
    original = ui.runtime.bot.get_chat_member.side_effect

    async def member(chat, who):
        result = await original(chat, who)
        if who == ui.runtime.bot.id:
            result.can_restrict_members = True
        return result

    ui.runtime.bot.get_chat_member.side_effect = member
    ui.store.db.execute("UPDATE mod_groups SET enabled=0 WHERE chat='-1001'")
    await ui.action(event, {"action": "mod_group", "chat": "-1001"})
    buttons = ui.render.await_args.args[2]
    enable = next(p for label, p in buttons if label == "开启本群群管服务")
    assert enable["action"] == "mod_form"
    await ui.action(event, enable)
    confirm = ui.render.await_args.args[2][0][1]
    await ui.action(event, confirm, "tenant-enable")
    assert ui.runtime.moderation.row("-1001")["enabled"] == 1
    assert binding(ui.store, "-1001")["version"] > 1
    await ui.action(event, {"action": "mod_group", "chat": "-1001"})
    disable = next(
        p for label, p in ui.render.await_args.args[2]
        if label == "关闭本群群管服务"
    )
    await ui.action(event, disable)
    disable_confirm = ui.render.await_args.args[2][0][1]
    await ui.action(event, disable_confirm, "tenant-disable")
    assert ui.runtime.moderation.row("-1001")["enabled"] == 0
    with pytest.raises(Rejected):
        await ui.action(event, disable_confirm, "tenant-disable-stale")


@pytest.mark.asyncio
async def test_my_groups_lists_only_current_admin_groups(form):
    ui, event, _, _, admin = form
    event.effective_user.id = 40
    await ui.action(event, {"action": "tenant_home"})
    assert any(p.get("chat") == "-1001" for _, p in ui.render.await_args.args[2])
    admin.status = "member"
    await ui.action(event, {"action": "tenant_home"})
    assert not any(p.get("chat") == "-1001" for _, p in ui.render.await_args.args[2])


@pytest.mark.asyncio
async def test_superadmin_cannot_browse_or_change_foreign_group_with_old_buttons(form):
    ui, event, _, _, admin = form
    ui.store.db.execute(
        "INSERT INTO roles(uid,scopes,expires) VALUES(?,?,?)",
        ("40", '["superadmin"]', ui.store.clock() + 3600),
    )
    event.effective_user.id = 40
    admin.status = "member"
    await ui.action(event, {"action": "tenant_home"})
    assert not any(p.get("chat") == "-1001" for _, p in ui.render.await_args.args[2])
    for payload in (
        {"action": "tenant_group", "chat": "-1001"},
        {"action": "tenant_switches", "chat": "-1001"},
        {"action": "tenant_ads", "chat": "-1001"},
        {"action": "tenant_resources", "chat": "-1001"},
        {"action": "tenant_status_preview", "chat": "-1001"},
    ):
        with pytest.raises(Rejected):
            await ui.action(event, payload, token="old-button")
    with pytest.raises(Rejected):
        ui.store.require("40", "ads", chat="-1001")
    assert not any(
        payload["action"] == "bot_control"
        for _, payload in ui.render.await_args.args[2]
    )


@pytest.mark.asyncio
async def test_superadmin_who_is_group_owner_keeps_own_group_controls(form):
    ui, event, *_ = form
    ui.store.db.execute(
        "INSERT INTO roles(uid,scopes,expires) VALUES(?,?,?)",
        ("10", '["superadmin"]', ui.store.clock() + 3600),
    )
    await ui.action(event, {"action": "tenant_home"})
    assert any(p.get("chat") == "-1001" for _, p in ui.render.await_args.args[2])
    await ui.action(event, {"action": "tenant_group", "chat": "-1001"})
    actions = {p["action"] for _, p in ui.render.await_args.args[2]}
    assert {"tenant_ads", "tenant_address", "tenant_resources", "tenant_status_preview"} <= actions


@pytest.mark.asyncio
async def test_group_admin_revocation_before_final_save_rejects(form):
    ui, event, _, _, admin = form
    event.effective_user.id = 40
    await ui.action(event, {"action": "tenant_switches", "chat": "-1001"})
    payload = next(
        p for _, p in ui.render.await_args.args[2]
        if p.get("feature") == "chat"
    )
    await ui.action(event, payload)
    confirm = ui.render.await_args.args[2][0][1]
    admin.status = "member"
    with pytest.raises(Rejected):
        await ui.action(event, confirm)


@pytest.mark.asyncio
async def test_old_admin_and_group_select_buttons_allow_current_group_admin(form):
    ui, event, _, _, admin = form
    event.effective_user.id = 40
    for payload in (
        {"action": "admin"},
        {"action": "mod_home", "menu_version": 2},
        {"action": "admin_ads"},
    ):
        await ui.action(event, payload, token="old-button")
        assert any(p.get("chat") == "-1001" for _, p in ui.render.await_args.args[2])
    await ui.action(event, {"action": "tenant_group", "chat": "-1001"})
    buttons = ui.render.await_args.args[2]
    assert {p["action"] for _, p in buttons} >= {
        "semantic_group_menu",
        "tenant_switches",
        "tenant_rewards",
        "tenant_games",
    }
    assert not {p["action"] for _, p in buttons} & {
        "tenant_ads",
        "tenant_address",
        "support_contact_edit",
        "tenant_errors",
    }
    admin.status = "member"
    await ui.action(event, {"action": "mod_home"}, token="old-button")
    assert not any(p.get("chat") == "-1001" for _, p in ui.render.await_args.args[2])
    with pytest.raises(Rejected):
        await ui.action(event, {"action": "tenant_group", "chat": "-1001"})


@pytest.mark.parametrize("game", list(GAMES))
@pytest.mark.asyncio
async def test_daily_schedule_steps_admin_save_and_boundaries(form, game):
    ui, event, clock, *_ = form
    event.effective_user.id = 40
    await ui.action(event, {"action": "schedule_edit", "chat": "-1001", "game": game})
    assert "00:00—23:59" in ui.render.await_args.args[1]
    for value in ("09:00", "18:00"):
        event.message.text = value
        await ui.input(event, ui.store.dialog("40"))
    confirm = ui.render.await_args.args[2][0][1]
    await ui.action(event, confirm)
    assert snapshot(ui.store, "-1001")[0][game] == [540, 1080]
    assert snapshot(ui.store, "-1002")[0] == {}
    for hour, minute, closed in (
        (8, 59, True),
        (9, 0, False),
        (17, 59, False),
        (18, 0, True),
    ):
        clock[0] = datetime(
            2026, 10, 4, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai")
        ).timestamp()
        assert bool(reason(ui.store, "-1001", game)) is closed
    with pytest.raises(Rejected):
        await ui.action(event, confirm)
    assert ADMIN_CONTEXT.get() is None


@pytest.mark.parametrize(
    "window,hour,closed",
    [
        ([0, 1440], 23, False),
        ([1320, 120], 23, False),
        ([1320, 120], 1, False),
        ([1320, 120], 2, True),
    ],
)
def test_full_day_and_overnight_schedule(form, window, hour, closed):
    ui, _, clock, *_ = form
    save(ui.store, "10", "-1001", "canada", *window, 0)
    clock[0] = datetime(2026, 10, 4, hour, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    assert bool(reason(ui.store, "-1001", "canada")) is closed


def test_retired_group_schedule_does_not_block_rewards_or_ads(form):
    ui, _, clock, *_ = form
    clock[0] = datetime(2026, 10, 4, 8, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    with ui.store.tx() as db:
        ui.store.put(
            db,
            "game_schedule:-1001",
            {"group": [540, 1080], "points": [540, 1080], "ads": [540, 1080]},
        )
    assert not reason(ui.store, "-1001", "canada")
    assert not reason(ui.store, "-1001", "points")
    ui.runtime.merchant_ads.submit("30", "pkg1", "test ad", "contact", 1, "new")
    assert ui.store.db.execute("SELECT COUNT(*) FROM ads").fetchone()[0] == 1


def test_disabled_rewards_do_not_accrue_but_manual_gift_still_works(form):
    ui, *_ = form
    ui.runtime.tenants.switch(
        "10", "-1001", "points", False, binding(ui.store, "-1001")["version"]
    )
    with pytest.raises(Rejected, match="奖励已关闭"):
        ui.runtime.points.checkin("30", "-1001")
    assert not ui.runtime.points.chat("30", "-1001", 1, "这是新的有效群消息")
    ui.runtime.points.adjust(
        "10", "30", 5, "群员积分派送", "manual", "-1001", notify="@member30"
    )
    assert ui.store.balance("30", "-1001") == 105


@pytest.mark.parametrize("resource", ["chat", "avatar"])
def test_default_shared_service_preserves_switch_and_ignores_retired_schedule(
    form, resource
):
    ui, _, clock, *_ = form
    runtime = ui.runtime
    runtime.tenants.switch(
        "10", "-1001", resource, True, binding(ui.store, "-1001")["version"]
    )
    assert runtime.tenants.resource_allowed("30", "-1001", resource)
    runtime.store.db.execute(
        "INSERT INTO roles(uid,scopes,expires) VALUES(?,?,?)",
        ("10", '["superadmin"]', runtime.store.clock() + 7200),
    )
    runtime.tenants.resource_grant("10", "-1001", resource, 2, 0)
    assert runtime.tenants.resource_allowed("30", "-1001", resource)
    runtime.tenants.switch(
        "10", "-1001", resource, False, binding(ui.store, "-1001")["version"]
    )
    assert not runtime.tenants.resource_allowed("30", "-1001", resource)
    runtime.tenants.switch(
        "10", "-1001", resource, True, binding(ui.store, "-1001")["version"]
    )
    with pytest.raises(Rejected):
        save(ui.store, "10", "-1001", resource, 540, 1080, 0)
    clock[0] = datetime(2026, 10, 4, 8, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    assert runtime.tenants.resource_allowed("30", "-1001", resource)
    clock[0] += 3600
    assert runtime.tenants.resource_allowed("30", "-1001", resource)
    assert runtime.tenants.resource_allowed("30", "-1002", resource)


def test_notice_failure_rolls_back_gift_and_audit(form):
    import sqlite3

    ui, *_ = form
    ui.store.db.execute(
        "CREATE TRIGGER reject_notice BEFORE INSERT ON notices BEGIN SELECT RAISE(ABORT,'fixture'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        ui.runtime.points.adjust(
            "10", "30", 10, "群员积分派送", "atomic", "-1001", notify="@member30"
        )
    assert ui.store.balance("30", "-1001") == 100
    assert ui.store.db.execute("SELECT COUNT(*) FROM notices").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_revoked_admin_cannot_save_prepared_gift(form):
    ui, event, _, _, admin = form
    event.effective_user.id = 40
    payload = {
        "action": "tenant_gift_preview",
        "chat": "-1001",
        "username": "member30",
        "target": "30",
        "amount": 10,
        "op": "revoked",
    }
    await ui.action(event, payload)
    confirm = ui.render.await_args.args[2][0][1]
    admin.status = "member"
    with pytest.raises(Rejected):
        await ui.action(event, confirm)
    assert ui.store.balance("30", "-1001") == 100
    assert ADMIN_CONTEXT.get() is None


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.asyncio
async def test_gift_notification_worker_sends_once_without_recredit(
    form, monkeypatch, failed
):
    import asyncio

    from data.plugins.astrbot_plugin_superbot.main import Main

    ui, *_ = form
    ui.runtime.points.adjust(
        "10", "30", 10, "群员积分派送", "notify", "-1001", notify="@member30"
    )
    worker = object.__new__(Main)
    worker.store = ui.store
    worker.application = SimpleNamespace(running=True, bot=ui.runtime.bot)
    worker.report = lambda *args: None
    if failed:
        ui.runtime.bot.send_message.side_effect = TimeoutError("fixture")
    with monkeypatch.context() as patch:
        patch.setattr(
            "data.plugins.astrbot_plugin_superbot.main.asyncio.sleep",
            AsyncMock(side_effect=asyncio.CancelledError),
        )
        for _ in range(2):
            with pytest.raises(asyncio.CancelledError):
                await worker.notifications()
    assert ui.runtime.bot.send_message.await_count == 1
    sent = ui.runtime.bot.send_message.await_args.kwargs
    assert sent["chat_id"] == "-1001" and "@member30" in sent["text"]
    assert ui.store.balance("30", "-1001") == 110
    assert ui.store.db.execute("SELECT status FROM notices").fetchone()[0] == (
        "unknown" if failed else "sent"
    )


@pytest.mark.parametrize(
    "missing,phrase",
    [
        ("package", "尚未配置广告位"),
        ("disabled", "所有广告位均已停用"),
        ("address", "尚未配置收款地址"),
    ],
)
@pytest.mark.asyncio
async def test_ad_diagnostics_name_registered_group_and_exact_missing_step(
    form, missing, phrase
):
    ui, *_ = form
    if missing == "package":
        ui.store.db.execute("DELETE FROM merchant_packages WHERE chat='-1001'")
    elif missing == "disabled":
        ui.store.db.execute(
            "UPDATE merchant_packages SET config=json_set(config,'$.enabled',json('false'))"
        )
    else:
        ui.store.db.execute("DELETE FROM merchant_addresses WHERE chat='-1001'")
    with pytest.raises(Rejected) as error:
        await find_ad_groups(ui, "@testgroup")
    assert "目标测试群" in str(error.value) and "已接入本BOT" in str(error.value)
    assert phrase in str(error.value)


@pytest.mark.asyncio
async def test_native_private_callback_and_text_gift_flow_never_enters_support(
    form, monkeypatch
):
    from unittest.mock import MagicMock

    from telegram.ext import ApplicationHandlerStop
    from test_superbot_private_security import incoming

    from data.plugins.astrbot_plugin_superbot.main import Main

    ui, *_ = form
    ticks = [1000.0]
    monkeypatch.setattr(
        "data.plugins.astrbot_plugin_superbot.main.time.monotonic", lambda: ticks[0]
    )
    plugin = Main(MagicMock(), {"platform_id": "fixture"})
    runtime = ui.runtime
    plugin.store = ui.store
    plugin.application = SimpleNamespace(running=True, bot=runtime.bot)
    for attr in (
        "tenants",
        "merchant_ads",
        "points",
        "ads",
        "game",
        "wheel",
        "slots",
        "mines",
        "k3",
        "duel",
    ):
        setattr(plugin, attr, getattr(runtime, attr))
    plugin.ui = UI(plugin)
    plugin.ui.render = AsyncMock()
    plugin.keyboard_versions["10"] = str(plugin.ui.keyboard("10").to_dict())
    plugin.chat_sessions[("10", "10")] = (2000, "user")
    callback = ui.store.callback(
        "10", "10", {"action": "tenant_edit", "chat": "-1001", "field": "points"}
    )
    with pytest.raises(ApplicationHandlerStop):
        await plugin.receive(incoming(uid=10, callback=callback), None)
    assert ui.store.dialog("10")["form"] == "tenant_gift"
    ticks[0] += 1
    with pytest.raises(ApplicationHandlerStop):
        await plugin.receive(incoming("@member30", uid=10), None)
    assert "第2/3步" in plugin.ui.render.await_args.args[1]
    payload = plugin.ui.render.await_args.args[2][0][1]
    ticks[0] += 1
    with pytest.raises(ApplicationHandlerStop):
        await plugin.receive(
            incoming(uid=10, callback=ui.store.callback("10", "10", payload)), None
        )
    ticks[0] += 1
    with pytest.raises(ApplicationHandlerStop):
        await plugin.receive(incoming("10", uid=10), None)
    assert "确认派送积分给群员" in plugin.ui.render.await_args.args[1]
    payload = plugin.ui.render.await_args.args[2][0][1]
    for _ in range(2):
        ticks[0] += 1
        with pytest.raises(ApplicationHandlerStop):
            await plugin.receive(
                incoming(uid=10, callback=ui.store.callback("10", "10", payload)), None
            )
    assert ui.store.balance("30", "-1001") == 110
    assert ui.store.db.execute("SELECT COUNT(*) FROM notices").fetchone()[0] == 1
    assert not plugin.chat_sessions
    assert not plugin.failure
