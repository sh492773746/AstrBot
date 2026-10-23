"""Private game navigation, scoped back paths and migrated switch acceptance."""

# ruff: noqa: F811

import pytest
from test_superbot_group_ux import form  # noqa: F401
from test_superbot_tenant_settings_matrix import configured  # noqa: F401
from test_superbot_tenants import merchant  # noqa: F401

from data.plugins.astrbot_plugin_superbot.group_game import GroupGame
from data.plugins.astrbot_plugin_superbot.store import Rejected


@pytest.mark.parametrize("uid", [10, 40])
@pytest.mark.parametrize("game", ["canada", "k3", "wheel", "slots", "mines", "duel"])
@pytest.mark.asyncio
async def test_game_menu_details_and_back_paths(form, uid, game):
    ui, event, *_ = form
    ui.runtime.group_game = GroupGame(ui.runtime)
    event.effective_user.id = uid
    await ui.action(event, {"action": "tenant_games", "chat": "-1001"})
    choices = ui.render.await_args.args[2]
    assert {p["game"] for _, p in choices if p["action"] == "tenant_game"} == {
        "canada",
        "k3",
        "wheel",
        "slots",
        "mines",
        "duel",
    }
    assert len(choices) == 8
    chosen = next(p for _, p in choices if p.get("game") == game)
    await ui.action(event, chosen)
    buttons = ui.render.await_args.args[2]
    assert not any(
        "本群玩法总览" in label or "开启" in label or "关闭" in label or "时段" in label
        for label, _ in buttons
    )
    for _, p in buttons:
        assert p["action"] not in {"games_group_view", "admin_game", "schedule_home"}
        if p["action"] == "tenant_edit":
            assert p["game"] == game
        if p["action"].startswith(("wheel_", "k3_", "slots_", "mines_")):
            assert p["action"].startswith(game + "_")
    parent = next(p for label, p in buttons if label == "返回玩法设置")
    select = next(p for label, p in buttons if label == "返回选群")
    await ui.action(event, parent)
    assert "玩法设置" in ui.render.await_args.args[1]
    await ui.action(event, select)
    assert "我的群" in ui.render.await_args.args[1]


@pytest.mark.parametrize("uid", [10, 40])
@pytest.mark.parametrize(
    "legacy",
    ["wheel_groups", "slots_groups", "k3_groups", "mines_groups", "games_group_list"],
)
@pytest.mark.asyncio
async def test_legacy_back_button_avoids_global_permission_gate(form, uid, legacy):
    ui, event, *_ = form
    event.effective_user.id = uid
    await ui.action(event, {"action": legacy})
    assert "我的群" in ui.render.await_args.args[1]


@pytest.mark.parametrize(
    "name",
    [
        "wheel_group",
        "slots_group",
        "k3_group",
        "mines_group",
        "tenant_game",
        "games_group_view",
    ],
)
@pytest.mark.asyncio
async def test_revoked_admin_and_foreign_group_still_rejected(form, name):
    ui, event, _, _, admin = form
    event.effective_user.id = 40
    with pytest.raises(Rejected):
        await ui.action(event, {"action": name, "chat": "-1002", "game": "wheel"})
    admin.status = "member"
    with pytest.raises(Rejected):
        await ui.action(event, {"action": name, "chat": "-1001", "game": "wheel"})


@pytest.mark.parametrize(
    "name",
    [
        "games_group_view",
        "wheel_preview",
        "k3_preview",
        "slots_preview",
        "mines_preview",
    ],
)
@pytest.mark.asyncio
async def test_obsolete_overview_and_switch_redirect_without_mutation(form, name):
    ui, event, *_ = form
    before = list(ui.store.db.execute("SELECT * FROM tenant_settings"))
    await ui.action(
        event,
        {
            "action": name,
            "chat": "-1001",
            "field": "enabled",
            "value": True,
            "enabled": True,
            "version": 0,
        },
    )
    assert "功能状态" in ui.render.await_args.args[1]
    assert [tuple(r) for r in before] == [
        tuple(r) for r in ui.store.db.execute("SELECT * FROM tenant_settings")
    ]


@pytest.mark.asyncio
async def test_admin_wheel_edit_input_save_returns_to_wheel(form):
    ui, event, *_ = form
    event.effective_user.id = 40
    await ui.action(event, {"action": "wheel_edit", "chat": "-1001", "field": "stakes"})
    for text in ("50", "100", "完成"):
        event.message.text = text
        await ui.input(event, ui.store.dialog("40"))
    confirm = ui.render.await_args.args[2][0][1]
    await ui.action(event, confirm)
    assert ui.runtime.wheel.config("-1001")[0]["stakes"] == [50, 100]
    assert "本群积分转盘" in ui.render.await_args.args[1]
    assert any(label == "返回玩法设置" for label, _ in ui.render.await_args.args[2])


@pytest.mark.parametrize(
    "game,values",
    [
        ("slots", ["100", "300", "完成"]),
        ("mines", ["100", "300", "完成"]),
        ("k3", ["5", "3", "2", "10"]),
    ],
)
@pytest.mark.asyncio
async def test_game_specific_setting_roundtrip_stays_on_same_game(form, game, values):
    ui, event, *_ = form
    event.effective_user.id = 40
    await ui.action(event, {"action": "tenant_game", "chat": "-1001", "game": game})
    edit = next(
        p for _, p in ui.render.await_args.args[2] if p["action"] == "tenant_edit"
    )
    await ui.action(event, edit)
    for text in values:
        event.message.text = text
        await ui.input(event, ui.store.dialog("40"))
    save = ui.render.await_args.args[2][0][1]
    await ui.action(event, save)
    assert any(p.get("field") == edit["field"] for _, p in ui.render.await_args.args[2])


@pytest.mark.asyncio
async def test_native_callback_navigation_never_hits_global_permissions(
    form, monkeypatch
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from telegram.ext import ApplicationHandlerStop
    from test_superbot_private_security import incoming

    from data.plugins.astrbot_plugin_superbot.main import Main
    from data.plugins.astrbot_plugin_superbot.ui import UI

    ui, *_ = form
    ticks = [1000.0]
    monkeypatch.setattr(
        "data.plugins.astrbot_plugin_superbot.main.time.monotonic", lambda: ticks[0]
    )
    plugin = Main(MagicMock(), {"platform_id": "fixture"})
    plugin.store = ui.store
    plugin.application = SimpleNamespace(running=True, bot=ui.runtime.bot)
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
        "moderation",
    ):
        setattr(plugin, attr, getattr(ui.runtime, attr))
    plugin.ui = UI(plugin)
    plugin.ui.render = AsyncMock()
    plugin.keyboard_versions["40"] = str(plugin.ui.keyboard("40").to_dict())
    payload = {"action": "tenant_games", "chat": "-1001"}
    for selection in ("wheel", "返回玩法设置", "返回选群", None):
        ticks[0] += 1
        callback = ui.store.callback("40", "40", payload)
        with pytest.raises(ApplicationHandlerStop):
            await plugin.receive(incoming(uid=40, callback=callback), None)
        if selection:
            payload = next(
                p
                for label, p in plugin.ui.render.await_args.args[2]
                if p.get("game") == selection or label == selection
            )
    assert "我的群" in plugin.ui.render.await_args.args[1]
    assert not plugin.failure
    plugin.bot.send_message.assert_not_awaited()
