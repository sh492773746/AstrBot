"""Offline race, delivery, authorization and historical recovery acceptance."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_superbot import draw, env  # noqa: F401
from test_superbot_community import community  # noqa: F401
from test_superbot_group_flows import group_update
from test_superbot_moderation import setup  # noqa: F401

from data.plugins.astrbot_plugin_superbot.game import Game
from data.plugins.astrbot_plugin_superbot.game_maintenance import action
from data.plugins.astrbot_plugin_superbot.group_game import GroupGame
from data.plugins.astrbot_plugin_superbot.main import Main
from data.plugins.astrbot_plugin_superbot.store import Rejected


@pytest.fixture
def flow(community):  # noqa: F811
    s = community
    s.runtime.game = Game(s.store)
    s.runtime.game.current = lambda db=None: (101, s.clock[0] + 60)
    s.flow = GroupGame(s.runtime)
    s.bot.send_message.return_value = SimpleNamespace(message_id=700)
    s.bot.edit_message_text = AsyncMock()
    with s.store.tx() as db:
        s.store.put(db, "modules", {"game": True, "points": True, "moderation": False})
        rooms = s.runtime.game.rooms()
        rooms["room28"]["enabled"] = True
        s.store.put(db, "rooms", rooms)
        s.store.credit(db, "fixture", "2", 100, "fixture")
    return s


async def preview(s):
    """Seed a historical confirmation without exposing a new betting control."""
    s.flow.group_room("-1001")
    token = s.store.callback(
        "2",
        "-1001",
        {
            "action": "confirm",
            "room": "room28",
            "issue": 101,
            "plays": ["big"],
            "amount": 1,
        },
    )[3:]
    return token, s.store.resolve(token, "2", "-1001")


@pytest.mark.asyncio
async def test_public_queries_throttled_but_community_permissions_unchanged(flow):
    s = flow
    await s.flow.message(
        group_update(text="/points", at=s.clock[0]), "/points", "/points"
    )
    await s.flow.text_game.deliver("-1001")
    calls = s.bot.send_message.await_count
    await s.flow.message(
        group_update(text="/points", at=s.clock[0], source=101), "/points", "/points"
    )
    assert s.bot.send_message.await_count == calls
    with pytest.raises(Rejected, match="未启用"):
        await s.runtime.community.member("2", "-1001")
    s.store.db.execute("UPDATE mod_groups SET enabled=0")
    assert await s.flow.message(group_update(), "/game", "/game")
    assert s.bot.send_message.await_count == calls


@pytest.mark.asyncio
async def test_delivery_review_requires_admin_fresh_single_use_confirmation(flow):
    s = flow
    s.store.db.execute(
        "INSERT INTO gg_dispatch(id,chat,op,kind,text,status) VALUES('fixture','-1001','op','accepted','fixture','review')"
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=2),
        effective_chat=SimpleNamespace(id=2),
        callback_query=None,
    )
    with pytest.raises(Rejected):
        await action(s.runtime.ui, update, {"action": "game_delivery"}, "")
    update.effective_user.id = update.effective_chat.id = 1
    payload = {
        "action": "game_notice_resolve",
        "id": "fixture",
        "version": 0,
        "result": "sent",
    }
    token = s.store.callback("1", "1", payload)[3:]
    await action(s.runtime.ui, update, payload, token)
    assert s.store.db.execute("SELECT status FROM gg_dispatch").fetchone()[0] == "sent"
    with pytest.raises(Rejected):
        await action(s.runtime.ui, update, payload, token)
    assert s.store.balance("2") == 100


@pytest.mark.asyncio
async def test_round_missing_id_rejects_old_manual_sent_button(flow):
    s = flow
    s.store.db.execute(
        "INSERT INTO gg_dispatch(id,chat,op,kind,text,status) "
        "VALUES('missing','-1001','op','round_open','fixture','review')"
    )
    event = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=1),
        callback_query=None,
    )
    s.runtime.ui.render = AsyncMock()
    await action(
        s.runtime.ui, event, {"action": "game_notice_view", "id": "missing"}, ""
    )
    rendered = s.runtime.ui.render.await_args.args
    assert "/recoverbroadcast" in rendered[1]
    assert all(p.get("result") != "sent" for _, p in rendered[2])
    payload = {
        "action": "game_notice_resolve",
        "id": "missing",
        "version": 0,
        "result": "sent",
    }
    token = s.store.callback("1", "1", payload)[3:]
    with pytest.raises(Rejected, match="recoverbroadcast"):
        await action(s.runtime.ui, event, payload, token)
    assert (
        s.store.db.execute("SELECT status FROM gg_dispatch").fetchone()[0] == "review"
    )


@pytest.mark.asyncio
async def test_historical_gap_recovery_preserves_live_health_and_settles_once(env):  # noqa: F811
    store, game, clock = env
    original = clock[0]
    game.place("2", "room18", 101, ["big"], 1, "fixture")
    clock[0] += 63000
    game.ingest([draw(400, clock[0])])
    assert (
        store.db.execute("SELECT status FROM game_backfill").fetchone()[0] == "pending"
    )
    with store.tx() as db:
        store.put(db, "keno_error", "LiveUnavailable")
    runtime = SimpleNamespace(
        store=store,
        game=game,
        keno=SimpleNamespace(fetch=AsyncMock(return_value=[draw(101, original + 210)])),
        report=lambda *args: None,
    )
    await Main.backfill_once(runtime)
    runtime.keno.fetch.assert_awaited_once_with(offset=250)
    assert store.get("keno_error") == "LiveUnavailable"
    assert (
        store.db.execute("SELECT status FROM game_backfill").fetchone()[0] == "complete"
    )
    assert store.db.execute("SELECT status FROM bets").fetchone()[0] != "pending"
    balance = store.balance("2")
    await Main.backfill_once(runtime)
    assert store.balance("2") == balance


@pytest.mark.asyncio
async def test_missing_target_and_network_failure_backoff_without_fabrication(env):  # noqa: F811
    store, game, clock = env
    store.db.execute("INSERT INTO game_backfill(issue) VALUES(50)")
    runtime = SimpleNamespace(
        store=store,
        game=game,
        keno=SimpleNamespace(fetch=AsyncMock(side_effect=TimeoutError)),
        report=lambda *args: None,
    )
    await Main.backfill_once(runtime)
    row = store.db.execute("SELECT * FROM game_backfill").fetchone()
    assert row["status"] == "pending" and row["next"] > clock[0]
    assert not store.db.execute("SELECT 1 FROM draws WHERE issue=50").fetchone()
    clock[0] = row["next"]
    runtime.keno.fetch.side_effect = None
    runtime.keno.fetch.return_value = [draw(99, clock[0] - 210)]
    await Main.backfill_once(runtime)
    assert (
        store.db.execute("SELECT status FROM game_backfill").fetchone()[0] == "pending"
    )
    assert not store.db.execute("SELECT 1 FROM draws WHERE issue=99").fetchone()
