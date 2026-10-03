"""Global daily schedule acceptance without real Telegram or bets."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_superbot import draw
from test_superbot_community import community, private  # noqa: F401
from test_superbot_game_hardening import flow  # noqa: F401
from test_superbot_moderation import setup  # noqa: F401
from test_superbot_round_broadcast import rounds  # noqa: F401

from data.plugins.astrbot_plugin_superbot.game import Game
from data.plugins.astrbot_plugin_superbot.game_hours import (
    DEFAULT,
    KEY,
    TZ,
    action,
    is_open,
)
from data.plugins.astrbot_plugin_superbot.store import Rejected, Store


@pytest.fixture
def hours(tmp_path):
    now = [datetime(2026, 9, 24, 20, tzinfo=TZ).timestamp()]
    s = Store(tmp_path / "hours.sqlite3", "1", lambda: now[0])
    yield s, now
    s.close()


@pytest.mark.parametrize(
    "start,end,time,expected",
    [
        ("20:00", "02:00", "19:59", False),
        ("20:00", "02:00", "20:00", True),
        ("20:00", "02:00", "23:59", True),
        ("20:00", "02:00", "00:00", True),
        ("20:00", "02:00", "02:00", False),
        ("08:00", "12:00", "08:00", True),
        ("08:00", "12:00", "12:00", False),
    ],
)
def test_boundaries(hours, start, end, time, expected):
    s, now = hours
    with s.tx() as db:
        s.put(db, KEY, {"mode": "daily", "start": start, "end": end})
    h, m = map(int, time.split(":"))
    now[0] = datetime(2026, 9, 24, h, m, tzinfo=TZ).timestamp()
    assert is_open(s) == expected


@pytest.mark.asyncio
async def test_save_authority_version_single_use_and_private(hours):
    s, _ = hours
    ui = SimpleNamespace(store=s, render=AsyncMock())
    assert is_open(s)
    p = {"action": "hours_save", "version": 0, "config": {**DEFAULT, "mode": "paused"}}
    token = s.callback("1", "1", p)[3:]
    with pytest.raises(Rejected):
        await action(ui, private(2), p, token)
    await action(ui, private(), p, token)
    assert not is_open(s)
    with pytest.raises(Rejected):
        await action(ui, private(), p, token)
    p["version"] = 1
    p["config"] = {"mode": "daily", "start": "00:00", "end": "00:00"}
    token = s.callback("1", "1", p)[3:]
    with pytest.raises(Rejected):
        await action(ui, private(), p, token)


def test_bet_gate_and_existing_settlement(hours):
    s, now = hours
    game = Game(s)
    with s.tx() as db:
        s.put(db, "modules", {"game": True})
        rooms = game.rooms()
        rooms["room28"]["enabled"] = True
        s.put(db, "rooms", rooms)
        s.credit(db, "fixture", "2", 100, "fixture")
    game.ingest([draw(100, now[0])])
    issue, _ = game.current()
    game.place("2", "room28", issue, ["big"], 1, "accepted")
    with s.tx() as db:
        s.put(db, KEY, {**DEFAULT, "mode": "paused"})
    with pytest.raises(Rejected, match="休息"):
        game.place("2", "room28", issue, ["big"], 1, "new")
    now[0] += 210
    game.ingest([draw(101, now[0])])
    assert (
        s.db.execute("SELECT status FROM bets WHERE id='accepted/0'").fetchone()[0]
        != "pending"
    )
    assert s.db.execute("SELECT count(*) FROM bets").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_time_button_full_minute_selection(hours):
    s, _ = hours
    ui = SimpleNamespace(store=s, render=AsyncMock())
    p = {
        "action": "hours_minute",
        "field": "start",
        "hour": 20,
        "page": 3,
        "version": 0,
        "config": {**DEFAULT, "mode": "daily"},
    }
    await action(ui, private(), p)
    choices = ui.render.await_args.args[2]
    assert "20:59" in [label for label, _ in choices]
    value = next(data for label, data in choices if label == "20:59")
    assert value["config"]["start"] == "20:59" and value["field"] == "end"


@pytest.mark.asyncio
async def test_closed_queued_announcement_and_reopen(rounds):  # noqa: F811
    s = rounds
    await s.flow.countdown_tick()
    with s.store.tx() as db:
        s.store.put(db, KEY, {**DEFAULT, "mode": "paused"})
    # Use the real admission implementation rather than fixture current().
    s.runtime.game.current = Game(s.store).current
    await s.flow.tick()
    assert s.bot.send_message.await_count == 0
