"""Isolated new-version economics, compatibility and immutable result tests."""

# ruff: noqa: F811
import json
from unittest.mock import AsyncMock

import pytest
from telegram.error import TimedOut
from test_superbot_ad_killer import env  # noqa: F401
from test_superbot_mines import click, mines, table  # noqa: F401
from test_superbot_slots import create, join, slots  # noqa: F401

from data.plugins.astrbot_plugin_superbot.mines import Mines
from data.plugins.astrbot_plugin_superbot.slots import Slots
from data.plugins.astrbot_plugin_superbot.store import Rejected


@pytest.fixture
def pool(slots, monkeypatch):
    monkeypatch.setattr("data.plugins.astrbot_plugin_superbot.slots.VERSION", "slots-3")
    return slots


@pytest.mark.parametrize("category_wins", [False, True])
def test_v5_luck_persisted_and_category_priority(pool, monkeypatch, category_wins):
    monkeypatch.setattr("data.plugins.astrbot_plugin_superbot.slots.VERSION", "slots-5")
    identity = create(pool)
    join(pool, identity, "3", "join")
    fruits = iter([0, 0, 0, 0, 1, 2] if category_wins else [0, 1, 2] * 2)
    points = iter([0, 999999])
    monkeypatch.setattr(
        "data.plugins.astrbot_plugin_superbot.slots_pool.secrets.randbelow",
        lambda n: next(fruits if n == 4 else points),
    )
    pool.now[0] += 50
    pool.engine.finish(identity)
    pool.engine.finish(identity)
    rows = pool.store.db.execute(
        "SELECT * FROM slots_v3_rounds ORDER BY seq"
    ).fetchall()
    assert [r["luck"] for r in rows] == [1, 1000000]
    winner = "2" if category_wins else "3"
    assert (
        pool.store.db.execute(
            "SELECT uid FROM slots_players WHERE payout=180"
        ).fetchone()[0]
        == winner
    )
    text, _ = pool.engine.render(identity, True)
    assert "1,000,000" in text
    assert pool.store.db.execute("SELECT fee FROM slots_tables").fetchone()[0] == 20


def test_v5_full_tie_refund_and_display_bound(pool, monkeypatch):
    from data.plugins.astrbot_plugin_superbot.rich_text import PlainHTML

    monkeypatch.setattr("data.plugins.astrbot_plugin_superbot.slots.VERSION", "slots-5")
    identity = create(pool)
    for uid in range(3, 12):
        join(pool, identity, str(uid), "join")
    pool.store.db.execute("UPDATE slots_players SET label=?", ("😀<&" * 20,))
    monkeypatch.setattr(
        "data.plugins.astrbot_plugin_superbot.slots_pool.secrets.randbelow",
        lambda n: 0 if n == 4 else 999999,
    )
    pool.now[0] += 50
    pool.engine.finish(identity)
    assert (
        pool.store.db.execute("SELECT COUNT(*) FROM slots_v3_rounds").fetchone()[0]
        == 60
    )
    assert (
        pool.store.db.execute("SELECT SUM(payout) FROM slots_players").fetchone()[0]
        == 1000
    )
    assert pool.store.db.execute("SELECT fee FROM slots_tables").fetchone()[0] == 0
    text, _ = pool.engine.render(identity, True)
    parser = PlainHTML()
    parser.feed(text)
    assert len("".join(parser.parts).encode("utf-16-le")) // 2 < 4096


@pytest.mark.asyncio
async def test_v4_equal_stake_and_separate_delayed_summary(pool, monkeypatch):
    monkeypatch.setattr("data.plugins.astrbot_plugin_superbot.slots.VERSION", "slots-4")
    identity = create(pool, 300)
    with pytest.raises(Rejected, match="与发起者相同"):
        join(pool, identity, "3", "100")
    assert pool.store.balance("3", "-1001") == 10000
    join(pool, identity, "3", "join")
    text, buttons = pool.engine.render(identity)
    assert "每人投入300" in text
    assert buttons.inline_keyboard[0][0].callback_data.endswith(":join")
    values = iter([0, 0, 0, 0, 1, 2])
    monkeypatch.setattr(
        "data.plugins.astrbot_plugin_superbot.slots_pool.secrets.randbelow",
        lambda n: next(values),
    )
    pool.now[0] += 50
    pool.engine.finish(identity)
    await pool.engine.edit_panels()
    assert pool.runtime.bot.edit_message_text.await_count == 0
    await pool.engine.deliver()
    pool.now[0] += 1
    await pool.engine.deliver()
    pool.now[0] += 7
    await pool.engine.deliver()
    assert pool.runtime.bot.send_message.await_count == 0
    pool.now[0] += 1
    await pool.engine.deliver()
    assert pool.runtime.bot.send_message.await_count == 1
    assert "540" in pool.runtime.bot.send_message.await_args.kwargs["text"]
    assert pool.store.db.execute("SELECT 1 FROM gt_delete WHERE message=90").fetchone()


@pytest.fixture
def personal_mines(mines, monkeypatch):
    monkeypatch.setattr("data.plugins.astrbot_plugin_superbot.mines.VERSION", "mines-2")
    return mines


def test_pool_single_and_duplicate(pool, monkeypatch):
    identity = create(pool)
    monkeypatch.setattr(
        "data.plugins.astrbot_plugin_superbot.slots_pool.secrets.randbelow", lambda n: 0
    )
    pool.now[0] += 50
    pool.engine.finish(identity)
    pool.engine.finish(identity)
    row = pool.store.db.execute("SELECT * FROM slots_tables").fetchone()
    player = pool.store.db.execute("SELECT * FROM slots_players").fetchone()
    assert row["status"] == "settled" and row["fee"] == 0
    assert player["payout"] == 620
    assert (
        pool.store.db.execute("SELECT COUNT(*) FROM slots_v3_rounds").fetchone()[0] == 1
    )


@pytest.mark.parametrize(
    "result,payout", [([0, 1, 2], 0), ([0, 0, 1], 100), ([0, 0, 0], 620)]
)
def test_solo_all_tiers(pool, monkeypatch, result, payout):
    identity = create(pool)
    values = iter(result)
    monkeypatch.setattr(
        "data.plugins.astrbot_plugin_superbot.slots_pool.secrets.randbelow",
        lambda n: next(values),
    )
    pool.now[0] += 50
    pool.engine.finish(identity)
    assert (
        pool.store.db.execute("SELECT payout FROM slots_players").fetchone()[0]
        == payout
    )
    assert pool.store.db.execute("SELECT fee FROM slots_tables").fetchone()[0] == 0


def test_pool_ten_players_tie_message_bound(pool, monkeypatch):
    from data.plugins.astrbot_plugin_superbot.rich_text import PlainHTML

    identity = create(pool)
    for uid in range(3, 12):
        join(pool, identity, str(uid), "100")
    pool.store.db.execute("UPDATE slots_players SET label=?", ("😀<&" * 20,))
    monkeypatch.setattr(
        "data.plugins.astrbot_plugin_superbot.slots_pool.secrets.randbelow", lambda n: 0
    )
    pool.now[0] += 50
    pool.engine.finish(identity)
    text, _ = pool.engine.render(identity, True)
    parser = PlainHTML()
    parser.feed(text)
    assert len("".join(parser.parts).encode("utf-16-le")) // 2 < 3800
    assert (
        pool.store.db.execute("SELECT COUNT(*) FROM slots_v3_rounds").fetchone()[0]
        == 60
    )


@pytest.mark.asyncio
async def test_pool_round_gifs_unknown_send_and_final_cleanup(pool, monkeypatch):
    identity = create(pool)
    join(pool, identity, "3", "300")
    values = iter([0, 0, 1, 1, 1, 2, 3, 3, 3, 0, 1, 2])
    monkeypatch.setattr(
        "data.plugins.astrbot_plugin_superbot.slots_pool.secrets.randbelow",
        lambda n: next(values),
    )
    pool.now[0] += 50
    pool.engine.finish(identity)
    pool.runtime.bot.send_animation.side_effect = TimedOut()
    assert not pool.store.db.execute(
        "SELECT 1 FROM slots_outbox WHERE kind='summary'"
    ).fetchone()
    pool.runtime.bot.edit_message_text.reset_mock()
    await pool.engine.edit_panels()
    assert pool.runtime.bot.edit_message_text.await_count == 0
    with pytest.raises(TimedOut):
        await pool.engine.deliver()
    assert (
        pool.store.db.execute(
            "SELECT COUNT(*) FROM slots_outbox WHERE status='review'"
        ).fetchone()[0]
        == 1
    )
    pool.runtime.bot.send_animation.side_effect = None
    for _ in range(6):
        pool.now[0] += 5
        await pool.engine.deliver()
    assert pool.runtime.bot.send_animation.await_count == 4
    captions = [
        call.kwargs["caption"]
        for call in pool.runtime.bot.send_animation.await_args_list
    ]
    assert sum("免费加赛1" in caption for caption in captions) == 2
    await pool.engine.edit_panels()
    assert pool.store.db.execute("SELECT 1 FROM gt_delete WHERE message=90").fetchone()
    assert pool.runtime.bot.send_message.await_count == 0


def test_pool_rollout_and_lock_boundary(pool, monkeypatch):
    with pool.store.tx() as db:
        pool.store.put(db, "games_v3_groups", [])
    with pytest.raises(Rejected, match="尚未"):
        create(pool)
    with pool.store.tx() as db:
        pool.store.put(db, "games_v3_groups", ["-1001"])
    identity = create(pool)
    pool.now[0] += 50
    with pytest.raises(Rejected):
        join(pool, identity, "3", "300")
    monkeypatch.setattr(
        "data.plugins.astrbot_plugin_superbot.slots_pool.secrets.randbelow", lambda n: 0
    )
    pool.engine.finish(identity, cancel=True)
    assert (
        pool.store.db.execute("SELECT payout FROM slots_players").fetchone()[0] == 100
    )


def test_pool_tie_elimination_and_prize(pool, monkeypatch):
    identity = create(pool)
    join(pool, identity, "3", "300")
    join(pool, identity, "4", "800")
    # Initial: A pair, B pair, C scatter. Tie-break: A scatter, B triple.
    values = iter([0, 0, 1, 1, 1, 2, 0, 1, 2, 0, 1, 2, 3, 3, 3])
    monkeypatch.setattr(
        "data.plugins.astrbot_plugin_superbot.slots_pool.secrets.randbelow",
        lambda n: next(values),
    )
    pool.now[0] += 50
    pool.engine.finish(identity)
    row = pool.store.db.execute("SELECT * FROM slots_tables").fetchone()
    people = pool.store.db.execute(
        "SELECT uid,payout FROM slots_players ORDER BY uid"
    ).fetchall()
    assert row["pool"] == 1200 and row["fee"] == 120
    assert [p["payout"] for p in people] == [0, 1080, 0]
    assert (
        pool.store.db.execute(
            "SELECT uid FROM slots_v3_rounds WHERE round=1 ORDER BY uid"
        ).fetchall()[0][0]
        == "2"
    )
    assert (
        pool.store.db.execute(
            "SELECT COUNT(*) FROM slots_v3_rounds WHERE uid='4'"
        ).fetchone()[0]
        == 1
    )
    text, _ = pool.engine.render(identity, True)
    assert "1080" in text and text.index("id=3") < text.index("id=2")


def test_pool_five_extra_rounds_refund(pool, monkeypatch):
    identity = create(pool)
    join(pool, identity, "3", "300")
    monkeypatch.setattr(
        "data.plugins.astrbot_plugin_superbot.slots_pool.secrets.randbelow", lambda n: 0
    )
    pool.now[0] += 50
    pool.engine.finish(identity)
    row = pool.store.db.execute("SELECT * FROM slots_tables").fetchone()
    assert row["status"] == "cancelled" and row["fee"] == 0
    assert (
        pool.store.db.execute("SELECT COUNT(*) FROM slots_v3_rounds").fetchone()[0]
        == 12
    )
    assert [
        p[0]
        for p in pool.store.db.execute("SELECT payout FROM slots_players ORDER BY uid")
    ] == [100, 300]
    pool.engine.finish(identity)
    assert (
        pool.store.db.execute("SELECT COUNT(*) FROM slots_v3_rounds").fetchone()[0]
        == 12
    )


@pytest.mark.parametrize("version", ["slots-3", "slots-5"])
def test_pool_failed_payout_restart_does_not_redraw(pool, monkeypatch, version):
    monkeypatch.setattr("data.plugins.astrbot_plugin_superbot.slots.VERSION", version)
    identity = create(pool)
    join(pool, identity, "3", "300" if version == "slots-3" else "join")
    values = iter([0, 0, 0, 0, 1, 2])
    monkeypatch.setattr(
        "data.plugins.astrbot_plugin_superbot.slots_pool.secrets.randbelow",
        lambda n: next(values) if n == 4 else 999999,
    )
    original = pool.store.credit

    def fail(db, op, *args, **kwargs):
        if op.endswith(":payout"):
            raise RuntimeError("simulated")
        return original(db, op, *args, **kwargs)

    monkeypatch.setattr(pool.store, "credit", fail)
    pool.now[0] += 50
    with pytest.raises(RuntimeError):
        pool.engine.finish(identity)
    assert (
        pool.store.db.execute("SELECT status FROM slots_tables").fetchone()[0]
        == "locked"
    )
    monkeypatch.setattr(pool.store, "credit", original)
    restored = Slots(pool.runtime)
    restored.finish(identity, cancel=True)
    assert (
        pool.store.db.execute(
            "SELECT payout FROM slots_players WHERE uid='2'"
        ).fetchone()[0]
        == (360 if version == "slots-3" else 180)
    )


@pytest.mark.asyncio
async def test_personal_mines_prize_effects_and_duplicate(personal_mines):
    s = personal_mines
    await s.engine.action(click())
    row = table(s)
    await s.engine.tick(row["id"])
    with pytest.raises(Rejected, match="档位"):
        s.engine.transition(row["id"], "join", "3", "@user3")
    for uid, stake in [("3", 300), ("4", 800)]:
        s.engine.transition(row["id"], "join", uid, f"@user{uid}", stake=stake)
    s.now[0] += 50
    await s.engine.tick(row["id"])
    row = table(s)
    while row["status"] == "active":
        state = json.loads(row["state"])
        s.engine.transition(
            row["id"], "pick", state["turn"], version=row["version"], cell=state["mine"]
        )
        await s.engine.tick(row["id"])
        row = table(s)
    state = json.loads(row["state"])
    winner = next(p for p in state["people"] if p["uid"] == state["winner"])
    assert state["pool"] == 1200 and state["fee"] == 120
    assert winner["payout"] == 1080
    effects = s.store.db.execute("SELECT * FROM mines_events ORDER BY rowid").fetchall()
    assert [e["kind"] for e in effects] == ["explosion", "explosion", "trophy"]
    assert effects[-1]["uid"] == winner["uid"]
    s.engine.transition(row["id"], "timeout")
    assert s.store.db.execute("SELECT COUNT(*) FROM mines_events").fetchone()[0] == 3
    for event in effects:
        await s.engine.effect(event["id"], modern=True)
    assert s.runtime.bot.send_animation.await_count == 3
    assert "1080" in s.runtime.bot.send_animation.await_args.kwargs["caption"]


@pytest.mark.asyncio
async def test_personal_mines_exit_cancel_and_rollout(personal_mines):
    s = personal_mines
    with s.store.tx() as db:
        s.store.put(db, "games_v3_groups", [])
    with pytest.raises(Rejected, match="尚未"):
        await s.engine.action(click())
    with s.store.tx() as db:
        s.store.put(db, "games_v3_groups", ["-1001"])
    await s.engine.action(click())
    row = table(s)
    await s.engine.tick(row["id"])
    s.engine.transition(row["id"], "join", "3", "@user3", stake=300)
    s.engine.transition(row["id"], "leave", "3")
    s.engine.transition(row["id"], "join", "4", "@user4", stake=800)
    s.engine.transition(row["id"], "cancel")
    state = json.loads(table(s)["state"])
    assert [p["payout"] for p in state["people"]] == [100, 300, 800]
    assert not s.store.db.execute("SELECT 1 FROM mines_events").fetchone()


@pytest.mark.asyncio
async def test_mines_unknown_effect_never_replays(personal_mines):
    s = personal_mines
    s.store.db.execute(
        "INSERT INTO mines_events(id,table_id,chat,uid,label,kind,caption) VALUES('e','t','-1001','2','@user2','explosion','test')"
    )
    s.runtime.bot.send_animation = AsyncMock(side_effect=TimedOut())
    await s.engine.effect("e", modern=True)
    restored = Mines(s.runtime)
    await restored.effect("e", modern=True)
    assert s.runtime.bot.send_animation.await_count == 1
    assert (
        s.store.db.execute("SELECT status FROM mines_events").fetchone()[0] == "review"
    )


@pytest.mark.asyncio
async def test_mines_each_turn_uses_new_panel(personal_mines):
    from types import SimpleNamespace

    s = personal_mines
    s.runtime.bot.send_message = AsyncMock(
        side_effect=[SimpleNamespace(message_id=n) for n in (100, 101, 102)]
    )
    await s.engine.action(click())
    row = table(s)
    await s.engine.tick(row["id"])
    for uid in ("3", "4"):
        s.engine.transition(row["id"], "join", uid, "@user" + uid, stake=100)
    s.now[0] += 50
    await s.engine.tick(row["id"])
    row = table(s)
    assert row["message"] == 101
    state = json.loads(row["state"])
    s.engine.transition(
        row["id"],
        "pick",
        state["turn"],
        version=row["version"],
        cell=(state["mine"] + 1) % 6,
    )
    assert table(s)["message"] is None
    assert table(s)["deadline"] == 0
    await s.engine.tick(row["id"])
    assert table(s)["message"] == 102
    assert table(s)["deadline"] == s.now[0] + 15
    assert s.runtime.bot.send_message.await_count == 3
    assert s.runtime.bot.edit_message_text.await_count == 0
    assert s.store.db.execute("SELECT 1 FROM gt_delete WHERE message=101").fetchone()
    with pytest.raises(Rejected, match="对应桌次"):
        await s.engine.action(
            click(
                row["id"],
                kind="t",
                action="pick0",
                version=table(s)["version"],
                message=101,
            )
        )
