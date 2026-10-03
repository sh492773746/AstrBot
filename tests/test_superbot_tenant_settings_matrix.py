"""Isolated settings acceptance using the actual tenant services."""

# ruff: noqa: F811
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_superbot_tenants import merchant  # noqa: F401

from data.plugins.astrbot_plugin_superbot.duel import Duel
from data.plugins.astrbot_plugin_superbot.game import Game
from data.plugins.astrbot_plugin_superbot.k3 import K3
from data.plugins.astrbot_plugin_superbot.mines import Mines
from data.plugins.astrbot_plugin_superbot.points import DEFAULT
from data.plugins.astrbot_plugin_superbot.slots import Slots, group_stakes
from data.plugins.astrbot_plugin_superbot.store import Rejected
from data.plugins.astrbot_plugin_superbot.tenants import binding, local_config
from data.plugins.astrbot_plugin_superbot.ui import UI
from data.plugins.astrbot_plugin_superbot.wheel import Wheel
from data.plugins.astrbot_plugin_superbot.wheel_admin import action as wheel_action


@pytest.fixture
def configured(merchant):
    runtime, clock, owners = merchant
    runtime.game = Game(runtime.store)
    runtime.wheel = Wheel(runtime)
    runtime.slots = Slots(runtime)
    runtime.mines = Mines(runtime)
    runtime.k3 = K3(runtime)
    runtime.duel = Duel(SimpleNamespace(store=runtime.store, runtime=runtime))
    with runtime.store.tx() as db:
        runtime.store.put(
            db,
            "modules",
            {
                **runtime.store.get("modules"),
                "slots": True,
                "mines": True,
                "duel": True,
            },
        )
    return runtime, clock, owners


@pytest.mark.parametrize(
    "feature", ["group", "ads", "canada", "k3", "duel", "wheel", "slots", "mines"]
)
def test_all_local_switches_isolated_and_versioned(configured, feature):
    runtime, _, _ = configured
    store = runtime.store
    modules = store.get("modules")
    other = dict(binding(store, "-1002"))
    for enabled in (False, True):
        version = binding(store, "-1001")["version"]
        runtime.tenants.switch("10", "-1001", feature, enabled, version)
        with pytest.raises(Rejected):
            runtime.tenants.switch("10", "-1001", feature, enabled, version)
        with pytest.raises(Rejected):
            runtime.tenants.switch(
                "20", "-1001", feature, enabled, binding(store, "-1001")["version"]
            )
        if feature == "group":
            assert bool(runtime.moderation.row("-1001")["enabled"]) == enabled
        elif feature in {"ads", "canada"}:
            assert local_config(store, "-1001", feature + "_enabled", None) == enabled
        elif feature == "wheel":
            assert runtime.wheel.config("-1001")[0]["enabled"] == enabled
        else:
            assert (
                bool(
                    store.db.execute(
                        f"SELECT enabled FROM {feature}_groups WHERE chat='-1001'"
                    ).fetchone()[0]
                )
                == enabled
            )
        assert store.get("modules") == modules
        assert dict(binding(store, "-1002")) == other


@pytest.mark.parametrize(
    "key,value",
    [
        ("slots_stakes", [100, 300]),
        ("mines_stakes", [300, 800]),
        ("k3_limits", {"ordinary": 500, "special": 50, "number": 10, "total": 1000}),
    ],
)
def test_local_operating_values_and_replay(configured, key, value):
    runtime, _, _ = configured
    runtime.tenants.operating("10", "-1001", key, value, 0)
    assert local_config(runtime.store, "-1001", key, None) == value
    assert local_config(runtime.store, "-1002", key, None) is None
    if key.endswith("_stakes"):
        assert group_stakes(runtime.store, "-1001", key.split("_")[0]) == value
    with pytest.raises(Rejected):
        runtime.tenants.operating("10", "-1001", key, value, 0)
    with pytest.raises(Rejected):
        runtime.tenants.operating("20", "-1001", key, value, 1)


def test_canada_reader_uses_group_limits_without_changing_odds(configured):
    runtime, _, _ = configured
    original = runtime.game.rooms()
    for room_id, room in original.items():
        value = {
            "minimum": room["minimum"],
            "maximum": room["minimum"],
            "total": room["minimum"],
        }
        runtime.tenants.operating("10", "-1001", "canada_limits:" + room_id, value, 0)
        current = runtime.game.rooms(chat="-1001")[room_id]
        for field in value:
            assert current[field] == value[field]
        assert current["odds"] == room["odds"]
    assert runtime.game.rooms(chat="-1002") == original


@pytest.mark.parametrize(
    "field,value",
    [
        ("stakes", [50, 100]),
        ("limit", 7),
        ("cooldown", 6),
    ],
)
@pytest.mark.asyncio
async def test_wheel_owner_save_and_effective_reader(configured, field, value):
    runtime, _, _ = configured
    ui = UI(runtime)
    ui.render = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=10),
        effective_chat=SimpleNamespace(type="private", id=10),
    )
    payload = {
        "action": "wheel_save",
        "chat": "-1001",
        "field": field,
        "value": value,
        "version": 0,
    }
    await wheel_action(ui, update, payload)
    assert runtime.wheel.config("-1001")[0][field] == value
    assert runtime.wheel.config("-1002")[1] == 0
    with pytest.raises(Rejected):
        await wheel_action(ui, update, payload)
    update.effective_user.id = 20
    with pytest.raises(Rejected):
        await wheel_action(ui, update, {**payload, "version": 1})


def test_reward_values_drive_actual_wallet_and_daily_cap(configured):
    runtime, clock, _ = configured
    config = {
        **DEFAULT,
        "enabled": True,
        "checkin": 13,
        "chat": 2,
        "interval": 60,
        "cap": 3,
        "groups": ["-1001"],
    }
    runtime.tenants.settings("10", "-1001", "points", config, 1)
    runtime.points.checkin("30", "-1001")
    assert runtime.store.balance("30", "-1001") == 13
    runtime.points.chat("30", "-1001", "100", "这是第一条测试发言")
    assert runtime.store.balance("30", "-1001") == 15
    runtime.points.chat("30", "-1001", "101", "这是第二条测试发言")
    assert runtime.store.balance("30", "-1001") == 15
    clock[0] += 61
    runtime.points.chat("30", "-1001", "102", "这是第三条测试发言")
    assert runtime.store.balance("30", "-1001") == 16
    assert runtime.store.balance("30", "-1002") == 0
    runtime.points.adjust("10", "30", 5, "isolated test", "adjust-test", "-1001")
    assert runtime.store.balance("30", "-1001") == 21
    with pytest.raises(Rejected):
        runtime.points.adjust("20", "30", 5, "foreign owner", "bad", "-1001")


@pytest.mark.parametrize(
    "key,value",
    [
        ("slots_stakes", [99]),
        ("mines_stakes", [100, 100]),
        ("k3_limits", {"ordinary": 1001, "special": 50, "number": 10, "total": 1000}),
        ("odds", {"big": 999}),
        ("probability", 1),
        ("fee", 0),
    ],
)
def test_invalid_or_platform_only_settings_rejected(configured, key, value):
    runtime, _, _ = configured
    with pytest.raises(Rejected):
        runtime.tenants.operating("10", "-1001", key, value, 0)
    assert local_config(runtime.store, "-1001", key, None) is None


@pytest.mark.parametrize("kind", ["normal", "pinned"])
@pytest.mark.parametrize("duration", ["30days", "month"])
@pytest.mark.parametrize("capacity", [1, 2, 3, 5, 10])
def test_ad_settings_used_in_order_snapshot(merchant, kind, duration, capacity):
    runtime, _, _ = merchant
    config = {
        "name": "isolated setting",
        "price": "12.34",
        "kind": kind,
        "duration": duration,
        "slots": capacity,
        "enabled": True,
    }
    runtime.merchant_ads.configure("10", "-1001", "matrix", config)
    runtime.merchant_ads.submit("30", "matrix", "synthetic", "test", 1, "matrix-order")
    order = runtime.merchant_ads.order("30", "matrix-order")
    snapshot = json.loads(order["package"])
    for key in ("kind", "duration", "price"):
        assert snapshot[key] == config[key]
    if kind == "pinned":
        assert (
            runtime.store.db.execute(
                "SELECT capacity FROM ad_targets WHERE chat='-1001'"
            ).fetchone()[0]
            == capacity
        )
    runtime.merchant_ads.configure(
        "10", "-1001", "matrix", {**config, "price": "23.45", "enabled": False}, 1
    )
    assert (
        runtime.merchant_ads.order("30", "matrix-order")["package"] == order["package"]
    )
    with pytest.raises(Rejected):
        runtime.merchant_ads.submit("30", "matrix", "synthetic", "test", 2, "disabled")
    with pytest.raises(Rejected):
        runtime.merchant_ads.configure("20", "-1001", "matrix", config, 2)


def test_k3_config_changes_actual_limit_and_freezes_existing_round(configured):
    runtime, clock, _ = configured
    store = runtime.store
    runtime.tenants.switch(
        "10", "-1001", "k3", True, binding(store, "-1001")["version"]
    )
    limits = {"ordinary": 5, "special": 3, "number": 2, "total": 10}
    runtime.tenants.operating("10", "-1001", "k3_limits", limits, 0)
    with store.tx() as db:
        store.credit(db, "matrix-seed", "30", 100, "isolated", chat="-1001")
        db.execute(
            "INSERT INTO k3_sessions(chat,uid,expires,activated,last_message) VALUES(?,?,?,?,?)",
            ("-1001", "30", clock[0] + 1800, clock[0], 1),
        )
    with pytest.raises(Rejected):
        runtime.k3.place("-1001", "30", 2, [("big", 6)], clock[0])
    assert store.balance("30", "-1001") == 100
    runtime.k3.place("-1001", "30", 3, [("big", 5)], clock[0])
    assert store.balance("30", "-1001") == 95
    runtime.tenants.operating(
        "10",
        "-1001",
        "k3_limits",
        {"ordinary": 10, "special": 5, "number": 3, "total": 20},
        1,
    )
    with pytest.raises(Rejected):
        runtime.k3.place("-1001", "30", 4, [("big", 1)], clock[0])
    assert store.balance("30", "-1001") == 95


@pytest.mark.parametrize("game", ["wheel", "k3", "slots", "mines"])
@pytest.mark.asyncio
async def test_disabled_group_admin_access_does_not_open_game(configured, game):
    import importlib

    from data.plugins.astrbot_plugin_superbot.game_switches import blocker

    runtime, _, _ = configured
    store = runtime.store
    runtime.tenants.switch(
        "10", "-1001", "group", False, binding(store, "-1001")["version"]
    )
    ui = UI(runtime)
    ui.render = AsyncMock()
    event = SimpleNamespace(
        effective_user=SimpleNamespace(id=10),
        effective_chat=SimpleNamespace(type="private", id=10),
    )
    action = importlib.import_module(
        f"data.plugins.astrbot_plugin_superbot.{game}_admin"
    ).action
    await action(ui, event, {"action": game + "_group", "chat": "-1001"})
    assert ui.render.await_count == 1
    assert "停用" in ui.render.await_args.args[1]
    assert not runtime.moderation.row("-1001")["enabled"]
    assert blocker(store, "-1001", game, True)
    for uid in (20, 30):
        event.effective_user.id = uid
        with pytest.raises(Rejected):
            await action(ui, event, {"action": game + "_group", "chat": "-1001"})


@pytest.mark.parametrize("game", ["slots", "mines"])
@pytest.mark.asyncio
async def test_admin_stakes_match_local_reader(configured, game):
    import importlib

    runtime, _, _ = configured
    runtime.tenants.operating("10", "-1001", game + "_stakes", [100, 300], 0)
    ui = UI(runtime)
    ui.render = AsyncMock()
    event = SimpleNamespace(
        effective_user=SimpleNamespace(id=10),
        effective_chat=SimpleNamespace(type="private", id=10),
    )
    action = importlib.import_module(
        f"data.plugins.astrbot_plugin_superbot.{game}_admin"
    ).action
    await action(ui, event, {"action": game + "_group", "chat": "-1001"})
    text = ui.render.await_args.args[1]
    assert "100 / 300" in text
    assert "100 / 300 / 800 / 1500 / 2000" not in text


@pytest.mark.asyncio
async def test_wheel_save_while_group_disabled(configured):
    runtime, _, _ = configured
    runtime.tenants.switch(
        "10", "-1001", "group", False, binding(runtime.store, "-1001")["version"]
    )
    ui = UI(runtime)
    ui.render = AsyncMock()
    event = SimpleNamespace(
        effective_user=SimpleNamespace(id=10),
        effective_chat=SimpleNamespace(type="private", id=10),
    )
    await wheel_action(
        ui,
        event,
        {
            "action": "wheel_save",
            "chat": "-1001",
            "field": "limit",
            "value": 7,
            "version": 0,
        },
    )
    assert runtime.wheel.config("-1001")[0]["limit"] == 7
    with pytest.raises(Rejected):
        runtime.wheel.check("-1001")


@pytest.mark.parametrize("game", ["slots", "mines"])
@pytest.mark.asyncio
async def test_game_subpage_switch_accepts_scoped_owner(configured, game):
    import importlib

    runtime, _, _ = configured
    ui = UI(runtime)
    ui.render = AsyncMock()
    event = SimpleNamespace(
        effective_user=SimpleNamespace(id=10),
        effective_chat=SimpleNamespace(type="private", id=10),
    )
    action = importlib.import_module(
        f"data.plugins.astrbot_plugin_superbot.{game}_admin"
    ).action
    await action(
        ui,
        event,
        {
            "action": game + "_preview",
            "chat": "-1001",
            "enabled": True,
            "version": 0,
        },
    )
    payload = ui.render.await_args.args[2][0][1]
    await action(ui, event, payload)
    assert (
        runtime.store.db.execute(
            f"SELECT enabled FROM {game}_groups WHERE chat='-1001'"
        ).fetchone()[0]
        == 1
    )
    event.effective_user.id = 20
    with pytest.raises(Rejected):
        await action(ui, event, {**payload, "enabled": False, "version": 1})
