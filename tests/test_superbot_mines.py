"""Offline atomicity, turn ownership and failure recovery for mines."""

# ruff: noqa: F811
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, Forbidden, RetryAfter, TimedOut
from test_superbot_ad_killer import env  # noqa: F401
from test_superbot_slots import slots  # noqa: F401

from data.plugins.astrbot_plugin_superbot.mines import Mines
from data.plugins.astrbot_plugin_superbot.mines_admin import action as admin_action
from data.plugins.astrbot_plugin_superbot.store import Rejected, encode


@pytest.fixture
def mines(slots, monkeypatch):
    monkeypatch.setattr("data.plugins.astrbot_plugin_superbot.mines.VERSION", "mines-1")
    s = slots
    s.runtime.report = lambda *args: None
    s.runtime.mines = Mines(s.runtime)
    s.engine = s.runtime.mines
    with s.store.tx() as db:
        s.store.put(db, "modules", {"game": True, "mines": True})
        db.execute("INSERT INTO mines_groups VALUES('-1001',1,1)")
        db.execute(
            "INSERT INTO mines_menus VALUES('menu','-1001','2','source',80,'active',?)",
            (s.now[0],),
        )
    return s


def click(
    identity="menu",
    kind="m",
    action="100",
    uid=2,
    version=0,
    chat="-1001",
    message=None,
):
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=int(chat), type="supergroup"),
        effective_user=SimpleNamespace(
            id=int(uid), username=f"user{uid}", full_name="User", is_bot=False
        ),
        callback_query=SimpleNamespace(
            data=f"mn:{kind}:{identity}:{version}:{action}",
            message=SimpleNamespace(message_id=message or (80 if kind == "m" else 100)),
        ),
    )


def table(s, identity=None):
    return s.store.db.execute(
        "SELECT * FROM mines_tables WHERE id=coalesce(?,id) ORDER BY created DESC LIMIT 1",
        (identity,),
    ).fetchone()


async def start(s, count=3):
    await s.engine.action(click())
    row = table(s)
    await s.engine.tick(row["id"])
    for uid in range(3, 2 + count):
        s.engine.transition(row["id"], "join", str(uid), f"@user{uid}")
    s.now[0] += 50
    await s.engine.tick(row["id"])
    return table(s)


@pytest.mark.asyncio
async def test_start_turn_board_and_full_pool(mines):
    s = mines
    row = await start(s)
    state = json.loads(row["state"])
    assert sorted(state["seats"]) == ["2", "3", "4"]
    assert row["status"] == "active" and row["deadline"] == s.now[0] + 15
    assert state["mine"] in range(6) and state["opened"] == []
    while row["status"] == "active":
        state = json.loads(row["state"])
        s.engine.transition(
            row["id"], "pick", state["turn"], version=row["version"], cell=state["mine"]
        )
        await s.engine.tick(row["id"])
        row = table(s)
    assert row["status"] == "settled"
    state = json.loads(row["state"])
    assert s.store.balance(state["winner"], "-1001") == 10200
    assert sum(s.store.balance(str(uid), "-1001") for uid in (2, 3, 4)) == 30000
    before = list(s.store.db.execute("SELECT * FROM group_ledger"))
    s.engine.transition(row["id"], "cancel")
    s.engine.transition(row["id"], "timeout")
    assert list(s.store.db.execute("SELECT * FROM group_ledger")) == before
    text, buttons = s.engine.render(row)
    assert (
        "@user" in text
        and "<blockquote expandable>" in text
        and not buttons.inline_keyboard
    )
    assert (
        s.store.db.execute("SELECT due FROM gt_delete WHERE message=100").fetchone()[0]
        == s.now[0] + 60
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 2])
async def test_underfilled_refund(mines, count):
    row = await start(mines, count)
    assert row["status"] == "cancelled"
    assert mines.store.balance("2", "-1001") == 10000
    assert mines.store.balance("3", "-1001") == 10000


@pytest.mark.asyncio
async def test_signup_duplicates_limits_withdraw(mines):
    s = mines
    await s.engine.action(click())
    await s.engine.action(click(action="2000"))
    assert s.store.balance("2", "-1001") == 9900
    row = table(s)
    await s.engine.tick(row["id"])
    for uid in range(3, 12):
        s.engine.transition(row["id"], "join", str(uid), f"@user{uid}")
    with pytest.raises(Rejected, match="已满"):
        s.engine.transition(row["id"], "join", "12")
    s.engine.transition(row["id"], "leave", "3")
    assert s.store.balance("3", "-1001") == 10000
    with pytest.raises(Rejected, match="重复"):
        s.engine.transition(row["id"], "join", "3")
    s.engine.transition(row["id"], "cancel")
    assert all(s.store.balance(str(uid), "-1001") == 10000 for uid in range(2, 12))


@pytest.mark.asyncio
async def test_safe_cell_and_stale_race(mines):
    s = mines
    row = await start(s)
    state = json.loads(row["state"])
    safe = (state["mine"] + 1) % 6
    with pytest.raises(Rejected, match="未轮到"):
        s.engine.transition(row["id"], "pick", "999", version=row["version"], cell=safe)
    s.engine.transition(
        row["id"], "pick", state["turn"], version=row["version"], cell=safe
    )
    with pytest.raises(Rejected, match="已更新"):
        s.engine.transition(row["id"], "timeout", version=row["version"])
    await s.engine.tick(row["id"])
    updated = table(s)
    after = json.loads(updated["state"])
    assert after["mine"] == state["mine"] and after["opened"] == [safe]
    assert after["turn"] != state["turn"]
    with pytest.raises(Rejected, match="已翻开"):
        s.engine.transition(
            row["id"], "pick", after["turn"], version=updated["version"], cell=safe
        )


@pytest.mark.asyncio
async def test_timeout_does_not_replay(mines, monkeypatch):
    s = mines
    row = await start(s)
    monkeypatch.setattr(
        "data.plugins.astrbot_plugin_superbot.mines.secrets.choice",
        lambda values: values[0],
    )
    s.now[0] += 15
    await asyncio.gather(s.engine.tick(row["id"]), s.engine.tick(row["id"]))
    assert table(s)["version"] == row["version"] + 1
    assert table(s)["deadline"] == s.now[0] + 15


@pytest.mark.asyncio
async def test_ten_players_full_auto_and_no_reroll(mines):
    s = mines
    row = await start(s, 10)
    steps = 0
    while row["status"] == "active":
        s.now[0] += 15
        await s.engine.tick(row["id"])
        row = table(s)
        steps += 1
        assert steps <= 54
    assert row["status"] == "settled"
    state = json.loads(row["state"])
    assert len(state["eliminated"]) == 9
    assert sum(p["payout"] for p in state["people"]) == 1000
    assert sum(s.store.balance(str(uid), "-1001") for uid in range(2, 12)) == 100000
    assert len(s.engine.render(row)[0].encode("utf-16-le")) // 2 < 4096


@pytest.mark.asyncio
async def test_open_race_only_one_table_and_cross_group_callback(mines):
    s = mines
    s.store.db.execute(
        "INSERT INTO mines_menus VALUES('other','-1001','3','other-source',81,'active',?)",
        (s.now[0],),
    )
    results = await asyncio.gather(
        s.engine.action(click()),
        s.engine.action(click(identity="other", uid=3, message=81)),
        return_exceptions=True,
    )
    assert sum(isinstance(result, Rejected) for result in results) == 1
    assert s.store.db.execute("SELECT count(*) FROM mines_tables").fetchone()[0] == 1
    assert s.store.balance("2", "-1001") + s.store.balance("3", "-1001") == 19900
    row = table(s)
    await s.engine.tick(row["id"])
    with pytest.raises(Rejected):
        await s.engine.action(
            click(identity=row["id"], kind="t", chat="-1002", action="join", version=1)
        )


@pytest.mark.asyncio
async def test_restart_unknown_initial_send_refunds(mines):
    s = mines
    await s.engine.action(click())
    row = table(s)
    s.store.db.execute("UPDATE mines_tables SET delivery='sending'")
    s.engine = Mines(s.runtime)
    await s.engine.tick(row["id"])
    assert table(s)["status"] == "cancelled"
    assert s.store.balance("2", "-1001") == 10000
    s.runtime.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_settled_display_failure_cannot_refund_winner(mines):
    s = mines
    row = await start(s)
    while row["status"] == "active":
        state = json.loads(row["state"])
        s.engine.transition(
            row["id"], "pick", state["turn"], version=row["version"], cell=state["mine"]
        )
        row = table(s)
        if row["status"] == "active":
            await s.engine.tick(row["id"])
            row = table(s)
    s.runtime.bot.edit_message_text.side_effect = TimedOut()
    await s.engine.tick(row["id"])
    s.now[0] += 301
    await s.engine.tick(row["id"])
    state = json.loads(table(s)["state"])
    assert table(s)["status"] == "settled" and table(s)["delivery"] == "review"
    assert s.store.balance(state["winner"], "-1001") == 10200


@pytest.mark.asyncio
async def test_restart_preserves_board_and_fresh_timer(mines):
    s = mines
    row = await start(s)
    s.now[0] += 100
    s.engine = Mines(s.runtime)
    assert table(s)["state"] == row["state"] and table(s)["deadline"] == 0
    await s.engine.tick(row["id"])
    assert (
        table(s)["deadline"] == s.now[0] + 15 and table(s)["version"] == row["version"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [TimedOut(), Forbidden("no permission"), RetryAfter(6)]
)
async def test_edit_failure_pauses_then_recovers(mines, error):
    s = mines
    row = await start(s)
    s.now[0] += 3
    s.runtime.bot.edit_message_text.side_effect = error
    await s.engine.tick(row["id"])
    assert table(s)["deadline"] == 0
    with pytest.raises(Rejected, match="暂停"):
        s.engine.transition(row["id"], "timeout")
    s.now[0] += 7
    s.runtime.bot.edit_message_text.side_effect = None
    await s.engine.tick(row["id"])
    assert table(s)["deadline"] == s.now[0] + 15 and table(s)["state"] == row["state"]


@pytest.mark.asyncio
async def test_long_limit_refunds_without_violating_retry_after(mines):
    s = mines
    row = await start(s)
    s.now[0] += 3
    s.runtime.bot.edit_message_text.side_effect = RetryAfter(600)
    await s.engine.tick(row["id"])
    calls = s.runtime.bot.edit_message_text.await_count
    s.now[0] += 300
    await s.engine.tick(row["id"])
    assert table(s)["status"] == "cancelled"
    assert s.runtime.bot.edit_message_text.await_count == calls
    assert all(s.store.balance(str(uid), "-1001") == 10000 for uid in (2, 3, 4))


@pytest.mark.asyncio
async def test_missing_panel_refunds_eliminated_players(mines):
    s = mines
    row = await start(s)
    state = json.loads(row["state"])
    s.engine.transition(
        row["id"], "pick", state["turn"], version=row["version"], cell=state["mine"]
    )
    s.runtime.bot.edit_message_text.side_effect = BadRequest(
        "Message to edit not found"
    )
    await s.engine.tick(row["id"])
    assert table(s)["status"] == "cancelled"
    assert all(s.store.balance(str(uid), "-1001") == 10000 for uid in (2, 3, 4))


@pytest.mark.asyncio
async def test_send_unknown_refunds_and_never_resends(mines):
    s = mines
    await s.engine.action(click())
    row = table(s)
    s.runtime.bot.send_message.side_effect = TimedOut()
    await s.engine.tick(row["id"])
    await s.engine.tick(row["id"])
    assert table(s)["status"] == "cancelled" and table(s)["delivery"] == "review"
    assert s.runtime.bot.send_message.await_count == 1
    assert s.store.balance("2", "-1001") == 10000


@pytest.mark.asyncio
async def test_initial_rate_limit_safe_retry(mines):
    s = mines
    await s.engine.action(click())
    row = table(s)
    s.runtime.bot.send_message.side_effect = RetryAfter(5)
    await s.engine.tick(row["id"])
    assert table(s)["delivery"] == "pending" and table(s)["status"] == "open"
    s.now[0] += 5
    s.runtime.bot.send_message.side_effect = None
    await s.engine.tick(row["id"])
    assert table(s)["deadline"] == s.now[0] + 50


@pytest.mark.asyncio
@pytest.mark.parametrize("module", ["game", "mines", "group"])
async def test_switch_cancel_open_not_started(mines, module):
    s = mines
    await s.engine.action(click())
    if module == "group":
        s.store.db.execute("UPDATE mines_groups SET enabled=0")
    else:
        with s.store.tx() as db:
            s.store.put(db, "modules", {"game": True, "mines": True, module: False})
    await s.engine.tick(table(s)["id"])
    assert table(s)["status"] == "cancelled"
    assert s.store.balance("2", "-1001") == 10000


@pytest.mark.asyncio
async def test_started_game_continues_after_disabled(mines):
    row = await start(mines)
    mines.store.db.execute("UPDATE mines_groups SET enabled=0")
    state = json.loads(row["state"])
    mines.engine.transition(
        row["id"], "pick", state["turn"], version=row["version"], cell=state["mine"]
    )
    assert table(mines)["status"] == "active"


@pytest.mark.asyncio
async def test_owner_chat_expiry_and_private_guards(mines):
    s = mines
    for kwargs in ({"uid": 3}, {"chat": "-1002"}, {"message": 9}):
        with pytest.raises(Rejected):
            await s.engine.action(click(**kwargs))
    update = click()
    update.effective_chat.type = "private"
    with pytest.raises(Rejected):
        await s.engine.action(update)
    s.engine.idle.touch("-1001", 80, "mines")
    s.now[0] += 31
    with pytest.raises(Rejected, match="30秒"):
        await s.engine.action(click())
    assert s.store.balance("2", "-1001") == 10000


@pytest.mark.asyncio
async def test_insufficient_balance_no_table(mines):
    s = mines
    with s.store.tx() as db:
        s.store.credit(db, "reduce", "2", -9999, "test", chat="-1001")
    with pytest.raises(Rejected):
        await s.engine.action(click())
    assert not table(s) and s.store.balance("2", "-1001") == 1


@pytest.mark.asyncio
async def test_payout_failure_rolls_back_and_no_redraw(mines, monkeypatch):
    s = mines
    row = await start(s)
    state = json.loads(row["state"])
    s.engine.transition(
        row["id"], "pick", state["turn"], version=row["version"], cell=state["mine"]
    )
    await s.engine.tick(row["id"])
    row = table(s)
    state = json.loads(row["state"])
    original = s.store.credit

    def fail(db, op, *args, **kwargs):
        if op.endswith(":payout"):
            raise RuntimeError("ledger failure")
        return original(db, op, *args, **kwargs)

    monkeypatch.setattr(s.store, "credit", fail)
    with pytest.raises(RuntimeError):
        s.engine.transition(
            row["id"], "pick", state["turn"], version=row["version"], cell=state["mine"]
        )
    assert table(s)["state"] == row["state"] and table(s)["version"] == row["version"]


@pytest.mark.asyncio
async def test_admin_scope_and_preview(mines):
    s = mines
    s.runtime.moderation = SimpleNamespace(check=AsyncMock())
    ui = SimpleNamespace(store=s.store, runtime=s.runtime, render=AsyncMock())
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=int(s.store.owner)),
        effective_chat=SimpleNamespace(type="private"),
    )
    await admin_action(ui, update, {"action": "mines_group", "chat": "-1001"})
    assert "扫雷总开关" in ui.render.await_args.args[1]
    await admin_action(
        ui,
        update,
        {"action": "mines_save", "chat": "-1001", "version": 1, "enabled": False},
    )
    assert s.store.get("modules")["mines"]
    assert not s.store.db.execute("SELECT enabled FROM mines_groups").fetchone()[0]
    with pytest.raises(Rejected):
        await admin_action(
            ui,
            update,
            {"action": "mines_save", "chat": "-1001", "version": 1, "enabled": True},
        )
    update.effective_user.id = 12345
    with pytest.raises(Rejected):
        await admin_action(ui, update, {"action": "mines_groups"})


def test_new_group_disabled_and_snapshot_safe(mines):
    with pytest.raises(Rejected):
        mines.engine.check("-999")
    state = {
        "people": [{"uid": "2", "label": "<script>", "status": "alive", "payout": 0}]
    }
    text, _ = mines.engine.render(
        {
            "id": "abc",
            "state": encode(state),
            "status": "open",
            "version": 1,
            "stake": 100,
            "deadline": 0,
            "fault": 0,
        }
    )
    assert "&lt;script&gt;" in text and "<script>" not in text


@pytest.mark.asyncio
async def test_signup_allows_same_displayed_version(mines):
    s = mines
    await s.engine.action(click())
    row = table(s)
    await s.engine.tick(row["id"])
    s.engine.transition(row["id"], "join", "3", "@three", row["version"])
    s.engine.transition(row["id"], "join", "4", "@four", row["version"])
    assert len(json.loads(table(s)["state"])["people"]) == 3
    with pytest.raises(Rejected):
        s.engine.transition(row["id"], "join", "3", "@three", row["version"])


@pytest.mark.asyncio
async def test_slow_success_does_not_consume_turn(mines):
    s = mines
    row = await start(s)
    s.now[0] += 3

    async def slow(**kwargs):
        s.now[0] += 12

    s.runtime.bot.edit_message_text.side_effect = slow
    await s.engine.tick(row["id"])
    assert table(s)["deadline"] - s.now[0] == 12


@pytest.mark.asyncio
async def test_scheduler_does_not_wait_for_slow_table(mines, monkeypatch):
    s = mines
    await s.engine.action(click())
    row = table(s)
    for index in range(5):
        s.store.db.execute(
            "INSERT INTO mines_tables(id,chat,stake,status,version,state,created,menu) "
            "VALUES(?,?,100,'cancelled',1,?,0,?)",
            (f"extra{index}", f"-200{index}", row["state"], f"menu{index}"),
        )
    started = []
    blocked = asyncio.Event()
    progressed = asyncio.Event()

    async def tick(identity):
        started.append(identity)
        s.store.db.execute("UPDATE mines_tables SET next=1e20 WHERE id=?", (identity,))
        if len(started) == 1:
            await blocked.wait()
        elif len(started) >= 5:
            progressed.set()

    monkeypatch.setattr(s.engine, "tick", tick)
    task = asyncio.create_task(s.engine.loop())
    try:
        await asyncio.wait_for(progressed.wait(), 2)
        assert not blocked.is_set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert not s.engine.jobs


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [False, True])
async def test_final_explosion_identity_and_no_replay(mines, unknown):
    s = mines
    row = await start(s)
    loser = None
    while row["status"] == "active":
        state = json.loads(row["state"])
        loser = state["turn"]
        s.engine.transition(
            row["id"], "pick", loser, version=row["version"], cell=state["mine"]
        )
        await s.engine.tick(row["id"])
        row = table(s)
    before = list(s.store.db.execute("SELECT * FROM group_ledger"))
    if unknown:
        s.runtime.bot.send_animation.side_effect = TimedOut()
    await s.engine.effect(row["id"])
    await s.engine.effect(row["id"])
    assert s.runtime.bot.send_animation.await_count == 1
    args = s.runtime.bot.send_animation.await_args.kwargs
    assert f"user?id={loser}" in args["caption"]
    assert args["animation"].filename.endswith(".gif")
    assert args["animation"].mimetype == "image/gif"
    assert list(s.store.db.execute("SELECT * FROM group_ledger")) == before
    effect = s.store.db.execute("SELECT * FROM mines_effects").fetchone()
    assert effect["status"] == ("review" if unknown else "sent")
    if not unknown:
        assert (
            s.store.db.execute(
                "SELECT due FROM gt_delete WHERE message=101"
            ).fetchone()[0]
            == s.now[0] + 60
        )
