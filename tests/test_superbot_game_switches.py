"""Offline group activation, admission diagnosis and scope isolation."""

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_superbot_ad_killer import env  # noqa: F401

from data.plugins.astrbot_plugin_superbot.game import Game
from data.plugins.astrbot_plugin_superbot.game_groups_admin import action
from data.plugins.astrbot_plugin_superbot.game_switches import (
    FEATURES,
    blocker,
    enable_group,
    snapshot,
)
from data.plugins.astrbot_plugin_superbot.group_game import GroupGame
from data.plugins.astrbot_plugin_superbot.group_points import migrate
from data.plugins.astrbot_plugin_superbot.k3 import K3
from data.plugins.astrbot_plugin_superbot.mines import Mines
from data.plugins.astrbot_plugin_superbot.mines_admin import action as mines_action
from data.plugins.astrbot_plugin_superbot.slots import Slots
from data.plugins.astrbot_plugin_superbot.slots_admin import action as slots_action
from data.plugins.astrbot_plugin_superbot.store import Rejected, encode
from data.plugins.astrbot_plugin_superbot.wheel import DEFAULT, Wheel


@pytest.fixture
def games(env):  # noqa: F811
    runtime = env.runtime
    migrate(runtime.store, "-1001")
    runtime.game = Game(runtime.store)
    runtime.k3 = K3(runtime)
    runtime.wheel = Wheel(runtime)
    runtime.slots = Slots(runtime)
    runtime.mines = Mines(runtime)
    runtime.group_game = GroupGame(runtime)
    runtime.ui.render = AsyncMock()
    with runtime.store.tx() as db:
        runtime.store.put(
            db,
            "modules",
            {"game": True, "moderation": True, **dict.fromkeys(FEATURES, True)},
        )
        runtime.store.put(db, "games_v3_groups", ["-1002"])
        db.execute("INSERT INTO mod_groups VALUES('-1002','Other',1,1)")
        for key in ("slots", "mines", "k3", "duel"):
            db.execute(f"INSERT OR REPLACE INTO {key}_groups VALUES('-1001',0,1)")
            db.execute(f"INSERT OR REPLACE INTO {key}_groups VALUES('-1002',0,1)")
        cfg = {**DEFAULT, "stakes": [5, 250], "limit": 7, "cooldown": 9}
        db.execute("INSERT INTO wheel_groups VALUES('-1001',?,1)", (encode(cfg),))
        db.execute("INSERT INTO wheel_groups VALUES('-1002',?,1)", (encode(DEFAULT),))
    return env


def private(uid=1):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=uid),
        effective_chat=SimpleNamespace(id=uid, type="private"),
    )


def test_bulk_enable_preserves_globals_other_groups_limits_and_wallets(games):
    store = games.runtime.store
    before = snapshot(store, "-1001")
    other = snapshot(store, "-1002")
    modules = store.get("modules")
    wallets = list(store.db.execute("SELECT * FROM group_wallets"))
    enable_group(store, "1", "-1001", before)
    after = snapshot(store, "-1001")
    assert all(setting["enabled"] for setting in after["features"].values())
    assert after["rollout"]
    assert after["features"]["wheel"]["config"] == {
        **before["features"]["wheel"]["config"],
        "enabled": True,
    }
    assert snapshot(store, "-1002") == other
    assert store.get("modules") == modules
    assert list(store.db.execute("SELECT * FROM group_wallets")) == wallets
    assert store.get("games_v3_groups") == ["-1002", "-1001"]
    assert games.runtime.wheel.check("-1001")
    assert games.runtime.slots.check("-1001")
    assert games.runtime.mines.check("-1001")
    assert games.runtime.k3.enabled("-1001")
    with pytest.raises(Rejected, match="已变化"):
        enable_group(store, "1", "-1001", before)
    assert store.get("games_v3_groups") == ["-1002", "-1001"]


@pytest.mark.parametrize("changed", ["modules", "local", "group", "rollout"])
def test_stale_confirmation_does_not_override_changes(games, changed):
    store = games.runtime.store
    expected = snapshot(store, "-1001")
    with store.tx() as db:
        if changed == "modules":
            store.put(db, "modules", {**store.get("modules"), "wheel": False})
        elif changed == "local":
            db.execute("UPDATE mines_groups SET version=version+1 WHERE chat='-1001'")
        elif changed == "group":
            db.execute("UPDATE mod_groups SET version=version+1 WHERE chat='-1001'")
        else:
            store.put(db, "games_v3_groups", ["-1002", "-1001"])
    current = snapshot(store, "-1001")
    with pytest.raises(Rejected, match="已变化"):
        enable_group(store, "1", "-1001", expected)
    assert snapshot(store, "-1001") == current


def test_global_disable_is_not_overridden_and_new_groups_remain_off(games):
    store = games.runtime.store
    with store.tx() as db:
        store.put(db, "modules", {**store.get("modules"), "game": False})
    enable_group(store, "1", "-1001", snapshot(store, "-1001"))
    assert not store.get("modules")["game"]
    assert "玩法中心总开关已关闭" in blocker(store, "-1001", "wheel", True)
    store.db.execute("INSERT INTO mod_groups VALUES('-1003','New',1,1)")
    assert not any(
        config["enabled"] for config in snapshot(store, "-1003")["features"].values()
    )
    assert "-1003" not in store.get("games_v3_groups")


def test_other_group_rollout_change_is_preserved(games):
    store = games.runtime.store
    expected = snapshot(store, "-1001")
    with store.tx() as db:
        store.put(db, "games_v3_groups", ["-1002", "-1003"])
    enable_group(store, "1", "-1001", expected)
    assert store.get("games_v3_groups") == ["-1002", "-1003", "-1001"]


def test_group_points_missing_does_not_enable_anything(games):
    store = games.runtime.store
    with store.tx() as db:
        store.put(db, "group_points_enabled", False)
    expected = snapshot(store, "-1001")
    with pytest.raises(Rejected, match="逐群积分"):
        enable_group(store, "1", "-1001", expected)
    assert snapshot(store, "-1001") == expected


def test_bulk_failure_rolls_back_partial_activation(games):
    store = games.runtime.store
    before = snapshot(store, "-1001")
    store.db.execute(
        "CREATE TRIGGER fail_group BEFORE UPDATE ON mines_groups "
        "WHEN NEW.chat='-1001' BEGIN SELECT RAISE(ABORT,'test failure'); END"
    )
    with pytest.raises(Exception, match="test failure"):
        enable_group(store, "1", "-1001", before)
    assert snapshot(store, "-1001") == before
    assert not store.db.execute(
        "SELECT 1 FROM audit WHERE action='game_group_rollout'"
    ).fetchone()


def test_diagnostics_distinguish_each_gate_and_do_not_mutate(games):
    store = games.runtime.store
    before = snapshot(store, "-1001")
    assert "本群积分转盘未启用" in blocker(store, "-1001", "wheel", False)
    assert "新版开放尚未确认" in blocker(store, "-1001", "slots", True, rollout=True)
    assert not blocker(store, "-1001", "wheel", True)
    with store.tx() as db:
        store.put(db, "modules", {**store.get("modules"), "wheel": False})
    assert "转盘总开关已关闭" in blocker(store, "-1001", "wheel", False)
    with store.tx() as db:
        store.put(db, "modules", before["modules"])
    assert "未登记或已停用" in blocker(store, "-999", "wheel", True)
    assert before["features"] == snapshot(store, "-1001")["features"]


@pytest.mark.asyncio
async def test_admin_overview_preview_and_explicit_confirmation(games):
    ui = games.runtime.ui
    await ui.admin(private(), {"action": "admin_game"}, "")
    assert any(
        p["action"] == "games_group_list" for _, p in ui.render.await_args.args[2]
    )
    await action(ui, private(), {"action": "games_group_view", "chat": "-1001"})
    text = ui.render.await_args.args[1]
    assert "本群关闭" in text and "总开关开启" in text and "阻塞" in text
    assert ui.render.await_args.kwargs["fold_sections"]
    before = snapshot(ui.store, "-1001")
    await action(
        ui, private(), {"action": "games_group_enable_preview", "chat": "-1001"}
    )
    assert snapshot(ui.store, "-1001") == before
    payload = ui.render.await_args.args[2][0][1]
    assert "其他群" in ui.render.await_args.args[1]
    await action(ui, private(), payload)
    assert "配置就绪" in ui.render.await_args.args[1]
    assert all(
        cfg["enabled"] for cfg in snapshot(ui.store, "-1001")["features"].values()
    )


@pytest.mark.asyncio
async def test_permission_scope_group_identity_and_private_only(games):
    ui = games.runtime.ui
    with pytest.raises(Rejected, match="无权限"):
        await action(
            ui, private(2), {"action": "games_group_enable_preview", "chat": "-1001"}
        )
    token = ui.store.grant("1", "2", ["game", "moderation"], 1)
    ui.store.db.execute(
        "INSERT INTO mod_grants VALUES(?,?)",
        (hashlib.sha256(token.encode()).hexdigest(), encode(["-1002"])),
    )
    ui.store.accept_grant("2", token)
    with pytest.raises(Rejected, match="没有此群"):
        await action(
            ui, private(2), {"action": "games_group_enable_preview", "chat": "-1001"}
        )
    ui.store.db.execute("INSERT INTO mod_acl VALUES('2','-1001')")
    with pytest.raises(Rejected, match="Telegram"):
        await action(
            ui, private(2), {"action": "games_group_enable_preview", "chat": "-1001"}
        )
    event = private()
    event.effective_chat.type = "supergroup"
    with pytest.raises(Rejected, match="私聊"):
        await action(ui, event, {"action": "games_group_view", "chat": "-1001"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key,handler", [("slots", slots_action), ("mines", mines_action)]
)
async def test_individual_enable_confirms_rollout_and_rejects_legacy_button(
    games, key, handler
):
    ui = games.runtime.ui
    payload = {"action": key + "_save", "chat": "-1001", "version": 1, "enabled": True}
    with pytest.raises(Rejected, match="重新预览"):
        await handler(ui, private(), payload)
    await handler(ui, private(), {**payload, "action": key + "_preview"})
    assert "新版准入" in ui.render.await_args.args[1]
    confirm = ui.render.await_args.args[2][0][1]
    await handler(ui, private(), confirm)
    assert ui.store.get("games_v3_groups") == ["-1002", "-1001"]
    assert snapshot(ui.store, "-1001")["features"][key]["enabled"]
    other_key = "mines" if key == "slots" else "slots"
    assert not snapshot(ui.store, "-1001")["features"][other_key]["enabled"]
    assert not snapshot(ui.store, "-1002")["features"][key]["enabled"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key,handler", [("slots", slots_action), ("mines", mines_action)]
)
async def test_individual_confirmation_rechecks_rollout_state(games, key, handler):
    ui = games.runtime.ui
    await handler(
        ui,
        private(),
        {"action": key + "_preview", "chat": "-1001", "version": 1, "enabled": True},
    )
    confirm = ui.render.await_args.args[2][0][1]
    with ui.store.tx() as db:
        ui.store.put(db, "games_v3_groups", ["-1002", "-1001"])
    with pytest.raises(Rejected, match="新版开放状态已变化"):
        await handler(ui, private(), confirm)
    assert not snapshot(ui.store, "-1001")["features"][key]["enabled"]
