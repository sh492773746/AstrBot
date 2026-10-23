"""Offline pool-game settlement and Telegram outbox regression tests."""

# ruff: noqa: F811
import asyncio
import json
from itertools import product
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, RetryAfter, TimedOut
from test_superbot_ad_killer import env  # noqa: F401

from data.plugins.astrbot_plugin_superbot.group_game import GroupGame
from data.plugins.astrbot_plugin_superbot.group_points import migrate
from data.plugins.astrbot_plugin_superbot.slots import STAKES, Slots, rank
from data.plugins.astrbot_plugin_superbot.store import Rejected


@pytest.fixture
def slots(env, monkeypatch):
    monkeypatch.setattr("data.plugins.astrbot_plugin_superbot.slots.VERSION", "slots-2")
    runtime = env.runtime
    migrate(runtime.store, "-1001")
    store = runtime.store
    runtime.community = SimpleNamespace(member=AsyncMock())
    runtime.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=100))
    runtime.bot.send_animation = AsyncMock(
        return_value=SimpleNamespace(
            message_id=101, animation=SimpleNamespace(file_id="gif")
        )
    )
    runtime.bot.edit_message_text = AsyncMock()
    runtime.slots = Slots(runtime)
    # Use the real deletion queue migration.
    runtime.group_game = GroupGame(runtime)
    with store.tx() as db:
        store.put(db, "modules", {"game": True, "slots": True, "moderation": True})
        store.put(db, "games_v3_groups", ["-1001", "-1002"])
        db.execute("INSERT INTO slots_groups VALUES('-1001',1,1)")
        for uid in range(2, 15):
            store.credit(db, f"seed{uid}", str(uid), 10000, "test", chat="-1001")
        db.execute(
            "INSERT INTO slots_menus VALUES('menu','-1001','2','source',80,'active',?)",
            (env.now[0] + 600,),
        )
    return SimpleNamespace(
        engine=runtime.slots, store=store, runtime=runtime, now=env.now
    )


def create(slots, stake=100):
    table = slots.engine.create("menu", "2", "-1001", 80, stake, "@owner", 1)
    slots.store.db.execute("UPDATE slots_tables SET message=90 WHERE id=?", (table,))
    slots.store.db.execute(
        "UPDATE slots_outbox SET status='sent',message=90 WHERE table_id=? AND seq=0",
        (table,),
    )
    return table


def join(slots, table, uid="3", action="100", **kwargs):
    return slots.engine.participate(
        table,
        uid,
        kwargs.get("chat", "-1001"),
        kwargs.get("message", 90),
        "@user" + uid,
        action,
        kwargs.get("click", uid + action),
        kwargs.get("version", 1),
    )


def outcomes(monkeypatch, values):
    values = iter(values)
    monkeypatch.setattr(
        "data.plugins.astrbot_plugin_superbot.slots.secrets.randbelow",
        lambda _: next(values),
    )


def test_all_64_outcomes_and_tiers():
    counts = {1: 0, 2: 0, 3: 0}
    for result in product(range(4), repeat=3):
        counts[rank(result)] += 1
    assert counts == {3: 4, 2: 36, 1: 24}
    assert STAKES == (100, 300, 800, 1500, 2000)


@pytest.mark.parametrize("stake", STAKES)
def test_escrow_one_shot_and_refund(slots, stake):
    table = create(slots, stake)
    assert slots.store.balance("2", "-1001") == 10000 - stake
    assert create(slots, 2000) == table
    slots.now[0] += 50
    slots.engine.finish(table)
    slots.engine.finish(table)
    assert slots.store.balance("2", "-1001") == 10000
    assert (
        slots.store.db.execute("SELECT status FROM slots_tables").fetchone()[0]
        == "cancelled"
    )


def test_join_duplicate_withdraw_no_rejoin(slots):
    table = create(slots)
    join(slots, table)
    join(slots, table)
    join(slots, table, action="leave")
    join(slots, table, click="new-click")
    assert slots.store.balance("3", "-1001") == 10000
    assert (
        slots.store.db.execute(
            "SELECT count(*) FROM group_ledger WHERE reason='slots_stake'"
        ).fetchone()[0]
        == 2
    )


@pytest.mark.parametrize("kwargs", [{"chat": "-1002"}, {"message": 91}, {"version": 2}])
def test_wrong_scope_and_version(slots, kwargs):
    table = create(slots)
    with pytest.raises(Rejected):
        join(slots, table, **kwargs)
    assert slots.store.balance("3", "-1001") == 10000


def test_deadline_capacity_and_one_table(slots):
    table = create(slots)
    for uid in range(3, 12):
        join(slots, table, str(uid))
    with pytest.raises(Rejected, match="已满"):
        join(slots, table, "12")
    slots.store.db.execute(
        "INSERT INTO slots_menus VALUES('menu2','-1001','3','s',81,'active',?)",
        (slots.now[0] + 600,),
    )
    with pytest.raises(Rejected, match="已有"):
        slots.engine.create("menu2", "3", "-1001", 81, 100, "x", 1)
    slots.now[0] += 50
    with pytest.raises(Rejected, match="结束"):
        join(slots, table, action="leave")


def test_winner_pool_atomic_repeat_and_fixed_draw(slots, monkeypatch):
    table = create(slots)
    join(slots, table)
    outcomes(monkeypatch, [0, 0, 0, 1, 1, 2])
    slots.now[0] += 50
    slots.engine.finish(table)
    assert slots.store.balance("2", "-1001") == 10520
    assert slots.store.balance("3", "-1001") == 10000
    assert dict(
        slots.store.db.execute("SELECT pool,fee,status FROM slots_tables").fetchone()
    ) == {"pool": 200, "fee": 0, "status": "settled"}
    before = list(slots.store.db.execute("SELECT * FROM group_ledger"))
    slots.engine.finish(table)
    assert list(slots.store.db.execute("SELECT * FROM group_ledger")) == before
    text, markup = slots.engine.render(table, True)
    assert (
        "<blockquote expandable>" in text and "@owner" in text and "净增减 +520" in text
    )
    assert not markup.inline_keyboard


def test_same_rank_tie_remainder_conservation(slots, monkeypatch):
    table = create(slots)
    for uid in range(3, 9):
        join(slots, table, str(uid))
    # Pairs return their own stakes, regardless of other players.
    outcomes(monkeypatch, [0, 0, 1] * 6 + [0, 1, 2])
    slots.now[0] += 50
    slots.engine.finish(table)
    payouts = [
        r[0]
        for r in slots.store.db.execute(
            "SELECT payout FROM slots_players ORDER BY ordinal"
        )
    ]
    assert payouts == [100] * 6 + [0]
    assert sum(payouts) == 600


def test_remainder_order(slots, monkeypatch):
    table = create(slots)
    for uid in range(3, 9):
        join(slots, table, str(uid))
    outcomes(monkeypatch, [0, 0, 1] * 4 + [0, 1, 2] * 3)
    slots.now[0] += 50
    slots.engine.finish(table)
    assert [
        r[0]
        for r in slots.store.db.execute(
            "SELECT payout FROM slots_players ORDER BY ordinal"
        )
    ] == [100, 100, 100, 100, 0, 0, 0]


def test_failed_credit_rolls_back_money_not_saved_randomness(slots, monkeypatch):
    table = create(slots)
    join(slots, table)
    outcomes(monkeypatch, [0, 0, 0, 1, 1, 1])
    slots.now[0] += 50
    credit = slots.store.credit

    def fail(db, op, uid, delta, reason, **kwargs):
        if reason == "slots_payout" and uid == "3":
            raise Rejected("simulated")
        return credit(db, op, uid, delta, reason, **kwargs)

    monkeypatch.setattr(slots.store, "credit", fail)
    with pytest.raises(Rejected):
        slots.engine.finish(table)
    assert slots.store.balance("2", "-1001") == 9900
    assert (
        slots.store.db.execute("SELECT status FROM slots_tables").fetchone()[0]
        == "locked"
    )
    assert json.loads(
        slots.store.db.execute(
            "SELECT result FROM slots_players WHERE uid='2'"
        ).fetchone()[0]
    ) == [0, 0, 0]
    monkeypatch.setattr(slots.store, "credit", credit)
    # No new RNG tickets exist, even after restart and switch closure.
    slots.engine = Slots(slots.runtime)
    slots.engine.finish(table, cancel=True)
    assert slots.store.balance("2", "-1001") == 10520
    assert slots.store.balance("3", "-1001") == 10520


def test_switch_off_and_balance_failure(slots):
    with pytest.raises(Rejected):
        slots.engine.create("menu", "2", "-1001", 80, 10, "x", 1)
    slots.store.db.execute("UPDATE group_wallets SET balance=0 WHERE uid='2'")
    with pytest.raises(Rejected):
        create(slots)
    assert (
        slots.store.db.execute("SELECT count(*) FROM slots_tables").fetchone()[0] == 0
    )
    slots.store.db.execute("UPDATE group_wallets SET balance=10000 WHERE uid='2'")
    table = create(slots)
    join(slots, table)
    slots.engine.finish(table, cancel=True)
    assert slots.store.balance("3", "-1001") == 10000


@pytest.mark.asyncio
async def test_live_member_failure_no_debit(slots):
    slots.runtime.community.member.side_effect = Rejected("left")
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=-1001, type="supergroup"),
        effective_user=SimpleNamespace(
            id=2, is_bot=False, username="u", full_name="User"
        ),
        callback_query=SimpleNamespace(
            data="sl:m:menu:100", message=SimpleNamespace(message_id=80), id="click"
        ),
    )
    with pytest.raises(Rejected):
        await slots.engine.action(update)
    assert slots.store.balance("2", "-1001") == 10000


@pytest.mark.asyncio
async def test_outbox_animation_saved_result_and_cleanup(slots, monkeypatch):
    table = create(slots)
    join(slots, table)
    outcomes(monkeypatch, [0] * 6)
    slots.now[0] += 50
    slots.engine.finish(table)
    with slots.store.tx() as db:
        slots.store.put(db, "slots_animation_gif_v3_000", "cached")
    for i in range(3):
        slots.runtime.bot.send_animation.return_value = SimpleNamespace(
            message_id=101 + i, animation=SimpleNamespace(file_id="cached")
        )
        slots.runtime.bot.send_message.return_value = SimpleNamespace(message_id=110)
        await slots.engine.deliver()
        slots.now[0] += 2
    assert slots.runtime.bot.send_animation.await_count == 2
    assert slots.runtime.bot.send_message.await_count == 1
    rows = slots.store.db.execute("SELECT * FROM gt_delete").fetchall()
    assert {r["message"] for r in rows} == {101, 102, 110}
    assert len({r["due"] for r in rows}) == 1
    assert rows[0]["due"] == slots.now[0] - 2 + 60
    assert 90 not in {r["message"] for r in rows}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,status", [(TimedOut(), "review"), (BadRequest("no permission"), "blocked")]
)
async def test_unknown_panel_refunds_never_resends(slots, error, status):
    table = slots.engine.create("menu", "2", "-1001", 80, 100, "x", 1)
    slots.runtime.bot.send_message.side_effect = error
    with pytest.raises(type(error)):
        await slots.engine.deliver()
    await slots.engine.deliver()
    assert slots.runtime.bot.send_message.await_count == 1
    assert slots.store.balance("2", "-1001") == 10000
    assert (
        slots.store.db.execute("SELECT status FROM slots_outbox").fetchone()[0]
        == status
    )
    assert (
        slots.store.db.execute(
            "SELECT status FROM slots_tables WHERE id=?", (table,)
        ).fetchone()[0]
        == "cancelled"
    )


@pytest.mark.asyncio
async def test_rate_limit_keeps_pending_and_deadline_refunds(slots):
    table = slots.engine.create("menu", "2", "-1001", 80, 100, "x", 1)
    slots.runtime.bot.send_message.side_effect = RetryAfter(60)
    await slots.engine.deliver()
    assert (
        slots.store.db.execute("SELECT status FROM slots_outbox").fetchone()[0]
        == "pending"
    )
    slots.now[0] += 50
    slots.engine.finish(table)
    await slots.engine.deliver()
    assert slots.runtime.bot.send_message.await_count == 1
    assert slots.store.balance("2", "-1001") == 10000


def test_interrupted_panel_on_restart_refunds(slots):
    slots.engine.create("menu", "2", "-1001", 80, 100, "x", 1)
    slots.store.db.execute("UPDATE slots_outbox SET status='sending'")
    Slots(slots.runtime)
    assert slots.store.balance("2", "-1001") == 10000


@pytest.mark.asyncio
async def test_admin_acl_and_version(slots):
    from data.plugins.astrbot_plugin_superbot.slots_admin import action

    ui = slots.runtime.ui
    ui.render = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(type="private"),
    )
    await action(ui, update, {"action": "slots_group", "chat": "-1001"})
    assert "当前托管" in ui.render.call_args.args[1]
    with pytest.raises(Rejected):
        await action(
            ui,
            update,
            {"action": "slots_save", "chat": "-1001", "enabled": False, "version": 0},
        )
    await action(
        ui,
        update,
        {"action": "slots_save", "chat": "-1001", "enabled": False, "version": 1},
    )
    with pytest.raises(Rejected):
        slots.engine.check("-1001")


@pytest.mark.parametrize(
    "uid,chat,message,stake",
    [
        ("3", "-1001", 80, 100),
        ("2", "-1002", 80, 100),
        ("2", "-1001", 99, 100),
        ("2", "-1001", 80, 50),
    ],
)
def test_owner_menu_binding(slots, uid, chat, message, stake):
    with pytest.raises(Rejected):
        slots.engine.create("menu", uid, chat, message, stake, "user", 1)
    assert slots.store.balance("2", "-1001") == 10000


def test_expired_menu_and_disabled_group(slots):
    slots.now[0] += 601
    with pytest.raises(Rejected):
        create(slots)
    slots.store.db.execute("UPDATE slots_menus SET expires=?", (slots.now[0] + 600,))
    slots.store.db.execute("UPDATE slots_groups SET enabled=0")
    with pytest.raises(Rejected):
        create(slots)


def test_wallet_isolation(slots):
    store = slots.store
    store.db.execute(
        "INSERT INTO mod_groups(chat,title,enabled) VALUES('-1002','Other',1)"
    )
    store.db.execute("INSERT INTO slots_groups VALUES('-1002',1,1)")
    with store.tx() as db:
        store.credit(db, "other_seed", "2", 500, "test", chat="-1002")
    create(slots)
    assert store.balance("2", "-1002") == 500
    assert store.balance("2", "-1001") == 9900


@pytest.mark.asyncio
async def test_unknown_animation_continues_summary_without_redraw(slots, monkeypatch):
    table = create(slots)
    join(slots, table)
    outcomes(monkeypatch, [0] * 6)
    slots.now[0] += 50
    slots.engine.finish(table)
    with slots.store.tx() as db:
        slots.store.put(db, "slots_animation_gif_v3_000", "cached")
    slots.runtime.bot.send_animation.side_effect = TimedOut()
    with pytest.raises(TimedOut):
        await slots.engine.deliver()
    with pytest.raises(TimedOut):
        await slots.engine.deliver()
    await slots.engine.deliver()
    assert "未能确认送达" in slots.runtime.bot.send_message.call_args.kwargs["text"]
    assert slots.runtime.bot.send_animation.await_count == 2
    assert slots.store.balance("2", "-1001") == 10520
    await slots.engine.deliver()
    assert slots.runtime.bot.send_animation.await_count == 2


@pytest.mark.asyncio
async def test_slow_send_does_not_block_settlement(slots, monkeypatch):
    table = create(slots)
    join(slots, table)
    entered = asyncio.Event()

    async def slow(**kwargs):
        entered.set()
        await asyncio.Future()

    slots.runtime.bot.edit_message_text.side_effect = slow
    pending = asyncio.create_task(slots.engine.edit_panels())
    await entered.wait()
    outcomes(monkeypatch, [0] * 6)
    slots.now[0] += 50
    worker = asyncio.create_task(slots.engine.loop())
    await asyncio.sleep(0)
    assert (
        slots.store.db.execute(
            "SELECT status FROM slots_tables WHERE id=?", (table,)
        ).fetchone()[0]
        == "settled"
    )
    pending.cancel()
    worker.cancel()
    await asyncio.gather(pending, worker, return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_during_panel_send_refunds(slots):
    slots.engine.create("menu", "2", "-1001", 80, 100, "x", 1)
    entered = asyncio.Event()

    async def slow(**kwargs):
        entered.set()
        await asyncio.Future()

    slots.runtime.bot.send_message.side_effect = slow
    task = asyncio.create_task(slots.engine.deliver())
    await entered.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert slots.store.balance("2", "-1001") == 10000
    assert (
        slots.store.db.execute("SELECT status FROM slots_outbox").fetchone()[0]
        == "review"
    )


@pytest.mark.asyncio
async def test_global_switch_cancels_only_open(slots):
    table = create(slots)
    join(slots, table)
    with slots.store.tx() as db:
        slots.store.put(db, "modules", {"game": True, "slots": False})
    worker = asyncio.create_task(slots.engine.loop())
    await asyncio.sleep(0)
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)
    assert slots.store.balance("2", "-1001") == 10000
    assert slots.store.balance("3", "-1001") == 10000


@pytest.mark.asyncio
async def test_admin_unauthorized(slots):
    from data.plugins.astrbot_plugin_superbot.slots_admin import action

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=2),
        effective_chat=SimpleNamespace(type="private"),
    )
    with pytest.raises(Rejected):
        await action(
            slots.runtime.ui,
            update,
            {"action": "slots_save", "chat": "-1001", "enabled": False, "version": 1},
        )
    assert slots.engine.check("-1001") == 1


def test_all_animation_assets_match_saved_outcomes():
    from PIL import Image, ImageChops, ImageStat

    folder = (
        Path(__file__).parents[1] / "data/plugins/astrbot_plugin_superbot/assets/slots"
    )
    images = {}
    for outcome in product(range(4), repeat=3):
        key = "".join(map(str, outcome))
        with Image.open(folder / (key + ".gif")) as image:
            assert image.n_frames >= 40
            image.seek(image.n_frames - 1)
            images[key] = image.convert("RGB")
    for key, image in images.items():
        for col, symbol in enumerate(key):
            # Ignore the win outlines; identify the actual center fruit artwork.
            box = (85 + col * 184, 291, 189 + col * 184, 399)
            crop = image.crop(box)
            errors = [
                sum(
                    ImageStat.Stat(
                        ImageChops.difference(crop, images[str(n) * 3].crop(box))
                    ).mean
                )
                for n in range(4)
            ]
            assert errors.index(min(errors)) == int(symbol), (key, col, errors)


@pytest.mark.asyncio
async def test_gif_upload_has_filename_and_never_uses_old_cache(slots, monkeypatch):
    table = create(slots)
    join(slots, table)
    outcomes(monkeypatch, [0] * 6)
    slots.now[0] += 50
    slots.engine.finish(table)
    with slots.store.tx() as db:
        slots.store.put(db, "slots_animation_v1_000", "old-document")
    await slots.engine.deliver()
    uploaded = slots.runtime.bot.send_animation.call_args.kwargs["animation"]
    assert uploaded.filename == "slots-000.gif"
    assert uploaded.mimetype == "image/gif"
    assert slots.store.get("slots_animation_gif_v3_000") == "gif"


@pytest.mark.asyncio
async def test_document_response_not_cached_or_resent(slots, monkeypatch):
    table = create(slots)
    join(slots, table)
    outcomes(monkeypatch, [0] * 6)
    slots.now[0] += 50
    slots.engine.finish(table)
    slots.runtime.bot.send_animation.return_value = SimpleNamespace(
        message_id=105, animation=None
    )
    await slots.engine.deliver()
    assert slots.store.get("slots_animation_gif_v3_000") is None
    row = slots.store.db.execute(
        "SELECT * FROM slots_outbox WHERE kind='animation' ORDER BY id LIMIT 1"
    ).fetchone()
    assert row["status"] == "sent" and row["message"] == 105
    assert row["error"] == "AnimationReturnedAsDocument"


def test_idle_selector_refresh_and_expiry(slots):
    idle = slots.engine.idle
    idle.touch("-1001", 80, "slots")
    slots.now[0] += 29
    idle.touch("-1001", 80, "slots")
    slots.now[0] += 29
    idle.expire()
    assert not slots.store.db.execute("SELECT 1 FROM gt_delete").fetchone()
    slots.now[0] += 1
    with pytest.raises(Rejected, match="30秒"):
        create(slots)
    idle.expire()
    idle.expire()
    rows = slots.store.db.execute("SELECT chat,message FROM gt_delete").fetchall()
    assert [tuple(r) for r in rows] == [("-1001", 80)]
    assert slots.store.balance("2", "-1001") == 10000


def test_idle_restart_and_live_table_not_deleted(slots):
    table = create(slots)
    slots.now[0] += 31
    engine = Slots(slots.runtime)
    engine.idle.expire()
    assert slots.store.db.execute("SELECT message FROM gt_delete").fetchone()[0] == 80
    assert (
        slots.store.db.execute(
            "SELECT status FROM slots_tables WHERE id=?", (table,)
        ).fetchone()[0]
        == "open"
    )
    assert not slots.store.db.execute(
        "SELECT 1 FROM gt_delete WHERE message=90"
    ).fetchone()
    with pytest.raises(Rejected):
        engine.idle.touch("-1001", 80, "slots")


@pytest.mark.parametrize("stake", STAKES)
@pytest.mark.parametrize(
    "result,multiplier", [([0, 1, 2], 0), ([0, 0, 1], 10), ([0, 0, 0], 62)]
)
def test_personal_stakes_payouts(slots, monkeypatch, stake, result, multiplier):
    table = create(slots, 100)
    join(slots, table, action=str(stake))
    join(slots, table, action="2000", click="another-click")
    assert slots.store.balance("3", "-1001") == 10000 - stake
    outcomes(monkeypatch, [0, 0, 1] + result)
    slots.now[0] += 50
    slots.engine.finish(table)
    assert slots.store.balance("3", "-1001") == 10000 - stake + stake * multiplier // 10
    assert slots.store.balance("2", "-1001") == 10000
    text, _ = slots.engine.render(table, True)
    assert f"投入 {stake}" in text


def test_different_stakes_cancel_and_withdraw(slots):
    table = create(slots, 300)
    join(slots, table, action="2000")
    join(slots, table, "4", action="800")
    join(slots, table, action="leave")
    assert slots.store.balance("3", "-1001") == 10000
    slots.engine.finish(table, cancel=True)
    assert slots.store.balance("2", "-1001") == 10000
    assert slots.store.balance("4", "-1001") == 10000


def test_legacy_snapshot_keeps_pool_rules(slots, monkeypatch):
    table = create(slots)
    slots.store.db.execute(
        "UPDATE slots_tables SET snapshot=? WHERE id=?",
        (json.dumps({"version": "slots-1"}), table),
    )
    join(slots, table, action="join")
    outcomes(monkeypatch, [0, 0, 0, 1, 1, 2])
    slots.now[0] += 50
    slots.engine.finish(table)
    assert slots.store.balance("2", "-1001") == 10080
    assert slots.store.balance("3", "-1001") == 9900
    text, _ = slots.engine.render(table, True)
    assert "旧版奖池" in text


def test_legacy_stake_migration_preserves_balances(slots):
    table = create(slots, 300)
    join(slots, table, action="300")
    slots.store.db.execute("ALTER TABLE slots_players DROP COLUMN stake")
    Slots(slots.runtime)
    assert [
        row[0]
        for row in slots.store.db.execute(
            "SELECT stake FROM slots_players WHERE table_id=?", (table,)
        )
    ] == [300, 300]
    assert slots.store.balance("2", "-1001") == 9700
    assert slots.store.balance("3", "-1001") == 9700


def test_final_table_has_mentions_and_tied_ranking(slots, monkeypatch):
    table = create(slots)
    join(slots, table)
    join(slots, table, uid="4")
    outcomes(monkeypatch, [0, 1, 2, 1, 1, 1, 2, 2, 2])
    slots.now[0] += 50
    slots.engine.finish(table)
    for summary in (False, True):
        text, _ = slots.engine.render(table, summary)
        assert text.count("第1名") == 2
        assert text.count("第3名") == 1
        assert text.index("user?id=3") < text.index("user?id=2")
        assert "@user3" in text and "@owner" in text
        assert "<blockquote expandable>" in text
        assert "详细结果以开奖汇总为准" not in text
        assert ("60秒后撤回" in text) == summary


def test_switch_messages_and_independence(slots):
    with slots.store.tx() as db:
        slots.store.put(db, "modules", {"game": True, "slots": False})
    with pytest.raises(Rejected, match="老虎机总开关"):
        slots.engine.check("-1001")
    assert slots.store.db.execute("SELECT enabled FROM slots_groups").fetchone()[0] == 1
    with slots.store.tx() as db:
        slots.store.put(db, "modules", {"game": True, "slots": True})
    slots.store.db.execute("UPDATE slots_groups SET enabled=0")
    with pytest.raises(Rejected, match="本群老虎机未启用"):
        slots.engine.check("-1001")
    assert slots.store.get("modules")["slots"]


def test_idle_group_scope_and_unauthorized_click(slots):
    idle = slots.engine.idle
    idle.touch("-1001", 80, "slots")
    idle.touch("-1002", 80, "hub")
    slots.now[0] += 20
    with pytest.raises(Rejected):
        slots.engine.create("menu", "3", "-1001", 80, 100, "other", 1)
    idle.touch("-1002", 80, "hub")
    slots.now[0] += 10
    idle.expire()
    assert [
        tuple(r) for r in slots.store.db.execute("SELECT chat,message FROM gt_delete")
    ] == [("-1001", 80)]
