"""Offline safety tests for the unreleased three-dice service."""

# ruff: noqa: F811
import asyncio
from itertools import product
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, RetryAfter, TimedOut
from test_superbot_ad_killer import env  # noqa: F401
from test_superbot_slots import slots  # noqa: F401

from data.plugins.astrbot_plugin_superbot.k3 import (
    K3,
    ODDS,
    parse,
    parse_partial,
    play_name,
)
from data.plugins.astrbot_plugin_superbot.store import Rejected


@pytest.fixture
def k3(slots):
    slots.engine = K3(slots.runtime)
    with slots.store.tx() as db:
        slots.store.put(db, "modules", {"game": True, "k3": True})
        db.execute("INSERT INTO k3_groups VALUES('-1001',1,1)")
    slots.engine.activate("-1001", "2", 1, slots.now[0])
    return slots


@pytest.mark.asyncio
async def test_admin_group_status_and_stale_switch(k3):
    with k3.store.tx() as db:
        k3.store.put(db, "modules", {"game": True, "k3": True, "moderation": True})
    ui = k3.runtime.ui
    ui.render = AsyncMock()
    event = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(type="private"),
    )
    await ui.admin(event, {"action": "k3_groups"}, "")
    assert "快三总开关：🟢 已开启" in ui.render.await_args.args[1]
    assert ui.render.await_args.args[2][-1][1]["action"] == "admin_game"
    await ui.admin(event, {"action": "k3_group", "chat": "-1001"}, "")
    assert "本群开关：🟢 开启" in ui.render.await_args.args[1]
    assert ui.render.await_args.kwargs["fold_sections"]
    preview = ui.render.await_args.args[2][0][1]
    await ui.admin(event, preview, "")
    save = ui.render.await_args.args[2][0][1]
    await ui.admin(event, save, "")
    assert "本群开关：⚪ 关闭" in ui.render.await_args.args[1]
    assert k3.store.get("modules")["k3"]
    with pytest.raises(Rejected, match="配置已变更"):
        await ui.admin(event, save, "")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("大100 单50", [("big", 100), ("odd", 50)]),
        ("da10x10d10s10", [("big", 10), ("small", 10), ("odd", 10), ("even", 10)]),
        (
            "dd10ds20xd30xs40",
            [
                ("big_odd", 10),
                ("big_even", 20),
                ("small_odd", 30),
                ("small_even", 40),
            ],
        ),
        (
            "大单10 大双20 小单30 小双40",
            [
                ("big_odd", 10),
                ("big_even", 20),
                ("small_odd", 30),
                ("small_even", 40),
            ],
        ),
        ("bz5 dz5 sz5", [("triple", 5), ("pair", 5), ("straight", 5)]),
        ("和值10 20", [("sum10", 20)]),
        ("三同666 5", [("triple666", 5)]),
        ("顺子20\n对子50", [("straight", 20), ("pair", 50)]),
    ],
)
def test_parse(text, expected):
    assert parse(text) == expected


def test_mixed_canada_style_number_pairs_keep_valid_k3_sums():
    text = "大单100 27/10 5/1 xs10bz10sz1 14/1 小1"
    items, skipped = parse_partial(text)
    assert items == [
        ("big_odd", 100),
        ("sum5", 1),
        ("small_even", 10),
        ("triple", 10),
        ("straight", 1),
        ("sum14", 1),
        ("small", 1),
    ]
    assert skipped == 1


@pytest.mark.parametrize(
    "play,label",
    [
        ("big_odd", "大单"),
        ("sum14", "和值14"),
        ("triple666", "三同666"),
    ],
)
def test_player_facing_play_names(play, label):
    assert play_name(play) == label


@pytest.mark.parametrize(
    "text",
    [
        "和值1020",
        "三同123 5",
        "和值19 1",
        "大0",
        "大-1",
        "大1.2",
        "dx100",
        "大10闲聊",
        "大1 " * 21,
    ],
)
def test_parse_rejects_entire_message(text):
    with pytest.raises(Rejected):
        parse(text)


def test_duplicate_message_and_accumulated_cap(k3):
    now = k3.now[0]
    first = k3.engine.place("-1001", "2", 2, [("big", 600)], now)
    assert k3.engine.place("-1001", "2", 2, [("big", 600)], now) == first
    assert k3.store.balance("2", "-1001") == 9400
    with pytest.raises(Rejected):
        k3.engine.place("-1001", "2", 3, [("big", 401)], now)
    assert k3.store.balance("2", "-1001") == 9400


def test_limit_feedback_names_amounts_and_whole_message_rejection(k3):
    items, _ = parse_partial("大90大双45大单45 15/70 顺子30豹子20")
    with pytest.raises(Rejected) as exc:
        k3.engine.place("-1001", "2", 2, items, k3.now[0])
    message = str(exc.value)
    assert "【和值15】超限" in message
    assert "本期已下注 0积分 ＋ 本条再投 70积分 ＝ 70积分" in message
    assert "本期累计上限：20积分" in message
    assert "最多还能投：20积分" in message
    assert "所有项目均未下注、未扣分" in message
    assert k3.store.balance("2", "-1001") == 10000
    assert not k3.store.db.execute("SELECT 1 FROM k3_rounds").fetchone()


def test_limit_feedback_accumulates_duplicates_and_prior_bets(k3):
    k3.engine.place("-1001", "2", 2, [("sum15", 12)], k3.now[0])
    with pytest.raises(Rejected) as exc:
        k3.engine.place("-1001", "2", 3, [("sum15", 5), ("sum15", 5)], k3.now[0])
    message = str(exc.value)
    assert message.count("【和值15】") == 1
    assert "本期已下注 12积分 ＋ 本条再投 10积分 ＝ 22积分" in message
    assert "最多还能投：8积分" in message
    assert k3.store.balance("2", "-1001") == 9988


def test_limit_feedback_reports_total_and_item_limits_together(k3):
    k3.engine.place("-1001", "2", 2, [("big", 1000), ("odd", 900)], k3.now[0])
    with pytest.raises(Rejected) as exc:
        k3.engine.place("-1001", "2", 3, [("big", 50), ("sum15", 70)], k3.now[0])
    message = str(exc.value)
    assert "【大】超限" in message and "【和值15】超限" in message
    assert "【本期所有玩法合计】超限" in message
    assert "本期已下注 1900积分 ＋ 本条再投 120积分 ＝ 2020积分" in message
    assert "本期合计最多还能投：100积分" in message
    assert k3.store.balance("2", "-1001") == 8100


def test_failed_first_order_does_not_create_round(k3):
    with pytest.raises(Rejected):
        k3.engine.place("-1001", "2", 2, [("big", 1), ("sum3", 21)], k3.now[0])
    assert k3.store.db.execute("SELECT count(*) FROM k3_rounds").fetchone()[0] == 0
    assert k3.store.balance("2", "-1001") == 10000


def test_closed_or_review_round_blocks_new_order(k3):
    k3.engine.place("-1001", "2", 2, [("big", 1)], k3.now[0])
    with pytest.raises(Rejected):
        k3.engine.place("-1001", "2", 3, [("big", 1)], k3.now[0] + 50)
    k3.store.db.execute("UPDATE k3_rounds SET status='review'")
    with pytest.raises(Rejected):
        k3.engine.place("-1001", "2", 4, [("big", 1)], k3.now[0])


def test_all_216_results(k3):
    for issue, values in enumerate(product(range(1, 7), repeat=3), 1):
        total = sum(values)
        triple = len(set(values)) == 1
        expected = {
            "big": not triple and total >= 11,
            "small": not triple and total <= 10,
            "odd": not triple and total % 2 == 1,
            "even": not triple and total % 2 == 0,
            "big_odd": not triple and total >= 11 and total % 2 == 1,
            "big_even": not triple and total >= 11 and total % 2 == 0,
            "small_odd": not triple and total <= 10 and total % 2 == 1,
            "small_even": not triple and total <= 10 and total % 2 == 0,
            "triple": triple,
            "pair": len(set(values)) == 2,
            "straight": sorted(values) in ([1, 2, 3], [2, 3, 4], [3, 4, 5], [4, 5, 6]),
        }
        db = k3.store.db
        db.execute(
            "INSERT INTO k3_rounds VALUES('-1001',?,'closing',0,'{}',1,NULL)", (issue,)
        )
        for seq, value in enumerate(values):
            db.execute(
                "INSERT INTO k3_dice(chat,issue,seq,message,value,status) "
                "VALUES('-1001',?,?,?,?, 'sent')",
                (issue, seq, seq + 1, value),
            )
        for key in ODDS:
            db.execute(
                "INSERT INTO k3_bets VALUES(?,'-1001',?,'2',?,1,0,'pending',0)",
                (f"{issue}:{key}", issue, key),
            )
        row = db.execute("SELECT * FROM k3_rounds WHERE issue=?", (issue,)).fetchone()
        k3.engine.settle(row, values)
        for bet in db.execute("SELECT * FROM k3_bets WHERE issue=?", (issue,)):
            key = bet["play"]
            win = expected.get(
                key,
                key == f"sum{total}"
                or (triple and key == f"triple{values[0]}" + str(values[0]) * 2),
            )
            assert bet["payout"] == (ODDS[key] // 100 if win else 0)
        balance = k3.store.balance("2", "-1001")
        k3.engine.settle(row, values)
        assert k3.store.balance("2", "-1001") == balance


@pytest.mark.asyncio
async def test_unknown_dice_stops_and_blocks_round(k3):
    k3.engine.place("-1001", "2", 2, [("big", 1)], k3.now[0])
    k3.now[0] += 51
    k3.runtime.bot.send_dice = AsyncMock(side_effect=TimedOut())
    row = k3.store.db.execute("SELECT * FROM k3_rounds").fetchone()
    await k3.engine.draw(row)
    assert k3.runtime.bot.send_dice.await_count == 1
    assert k3.store.db.execute("SELECT status FROM k3_rounds").fetchone()[0] == "review"
    await k3.engine.draw(row)
    assert k3.runtime.bot.send_dice.await_count == 1


@pytest.mark.asyncio
async def test_sequential_receipts_and_settlement(k3):
    k3.engine.place("-1001", "2", 2, [("sum3", 1)], k3.now[0])
    k3.now[0] += 51
    k3.runtime.bot.send_dice = AsyncMock(
        side_effect=[
            SimpleNamespace(message_id=i, dice=SimpleNamespace(value=1))
            for i in (10, 11, 12)
        ]
    )
    await k3.engine.draw(k3.store.db.execute("SELECT * FROM k3_rounds").fetchone())
    assert k3.store.balance("2", "-1001") == 10204
    assert (
        k3.store.db.execute("SELECT status FROM k3_rounds").fetchone()[0] == "settled"
    )
    before = k3.runtime.bot.send_message.await_count
    await k3.engine.deliver_result("-1001", 1)
    assert k3.runtime.bot.send_message.await_count == before
    k3.now[0] += 7.9
    await k3.engine.deliver_result("-1001", 1)
    assert k3.runtime.bot.send_message.await_count == before
    restored = K3(k3.runtime)
    k3.now[0] += 0.1
    await restored.deliver_result("-1001", 1)
    await restored.deliver_result("-1001", 1)
    assert k3.runtime.bot.send_message.await_count == before + 1
    assert "已结算" in k3.runtime.bot.send_message.await_args.kwargs["text"]
    assert not any(
        "已结算" in c.kwargs.get("text", "")
        for c in k3.runtime.bot.edit_message_text.await_args_list
    )
    assert k3.store.db.execute("SELECT COUNT(*) FROM gt_delete").fetchone()[0] >= 4
    assert k3.runtime.bot.send_dice.await_count == 3


@pytest.mark.asyncio
async def test_result_unknown_send_no_replay(k3):
    k3.engine.place("-1001", "2", 2, [("sum3", 1)], k3.now[0])
    k3.now[0] += 51
    k3.runtime.bot.send_dice = AsyncMock(
        side_effect=[
            SimpleNamespace(message_id=i, dice=SimpleNamespace(value=1))
            for i in (10, 11, 12)
        ]
    )
    await k3.engine.draw(k3.store.db.execute("SELECT * FROM k3_rounds").fetchone())
    balance = k3.store.balance("2", "-1001")
    k3.now[0] += 8
    k3.runtime.bot.send_message = AsyncMock(side_effect=TimedOut())
    await k3.engine.deliver_result("-1001", 1)
    restored = K3(k3.runtime)
    await restored.deliver_result("-1001", 1)
    assert k3.runtime.bot.send_message.await_count == 1
    assert (
        k3.store.db.execute("SELECT status FROM k3_results").fetchone()[0] == "review"
    )
    assert k3.store.balance("2", "-1001") == balance


@pytest.mark.asyncio
async def test_result_rate_limit_defers_without_resettling(k3):
    k3.engine.place("-1001", "2", 2, [("sum3", 1)], k3.now[0])
    k3.now[0] += 51
    k3.runtime.bot.send_dice = AsyncMock(
        side_effect=[
            SimpleNamespace(message_id=i, dice=SimpleNamespace(value=1))
            for i in (10, 11, 12)
        ]
    )
    await k3.engine.draw(k3.store.db.execute("SELECT * FROM k3_rounds").fetchone())
    balance = k3.store.balance("2", "-1001")
    k3.now[0] += 8
    k3.runtime.bot.send_message = AsyncMock(
        side_effect=[RetryAfter(3), SimpleNamespace(message_id=200)]
    )
    await k3.engine.deliver_result("-1001", 1)
    await k3.engine.deliver_result("-1001", 1)
    assert k3.runtime.bot.send_message.await_count == 1
    k3.now[0] += 3
    await k3.engine.deliver_result("-1001", 1)
    assert k3.runtime.bot.send_message.await_count == 2
    assert k3.store.balance("2", "-1001") == balance


@pytest.mark.asyncio
async def test_rate_limit_resumes_without_redrawing_confirmed_dice(k3):
    k3.engine.place("-1001", "2", 2, [("sum3", 1)], k3.now[0])
    k3.now[0] += 51
    k3.runtime.bot.send_dice = AsyncMock(
        side_effect=[
            SimpleNamespace(message_id=10, dice=SimpleNamespace(value=1)),
            RetryAfter(5),
            SimpleNamespace(message_id=11, dice=SimpleNamespace(value=1)),
            SimpleNamespace(message_id=12, dice=SimpleNamespace(value=1)),
        ]
    )
    row = k3.store.db.execute("SELECT * FROM k3_rounds").fetchone()
    await k3.engine.draw(row)
    assert (
        k3.store.db.execute("SELECT status FROM k3_rounds").fetchone()[0] == "closing"
    )
    assert [
        tuple(item)
        for item in k3.store.db.execute(
            "SELECT seq,status,message FROM k3_dice ORDER BY seq"
        )
    ] == [(0, "sent", 10), (1, "retry", None)]
    k3.now[0] += 5
    await k3.engine.draw(row)
    assert k3.runtime.bot.send_dice.await_count == 4
    assert [
        tuple(item)
        for item in k3.store.db.execute(
            "SELECT seq,value,message,status FROM k3_dice ORDER BY seq"
        )
    ] == [(0, 1, 10, "sent"), (1, 1, 11, "sent"), (2, 1, 12, "sent")]
    assert (
        k3.store.db.execute("SELECT status FROM k3_rounds").fetchone()[0] == "settled"
    )


def test_restart_preserves_retry_but_reviews_unknown_send(k3):
    db = k3.store.db
    db.execute("INSERT INTO k3_rounds VALUES('-1001',1,'closing',0,'{}',1,NULL)")
    db.execute(
        "INSERT INTO k3_dice(chat,issue,seq,message,value,status,next_retry,error) "
        "VALUES('-1001',1,0,NULL,0,'retry',123,'RetryAfter')"
    )
    db.execute("INSERT INTO k3_rounds VALUES('-1002',1,'closing',0,'{}',1,NULL)")
    db.execute(
        "INSERT INTO k3_dice(chat,issue,seq,message,value,status) "
        "VALUES('-1002',1,0,NULL,0,'sending')"
    )
    K3(k3.runtime)
    assert (
        db.execute("SELECT status FROM k3_rounds WHERE chat='-1001'").fetchone()[0]
        == "closing"
    )
    assert (
        db.execute("SELECT status FROM k3_dice WHERE chat='-1001'").fetchone()[0]
        == "retry"
    )
    assert (
        db.execute("SELECT status FROM k3_rounds WHERE chat='-1002'").fetchone()[0]
        == "review"
    )
    interrupted = db.execute(
        "SELECT status,error FROM k3_dice WHERE chat='-1002'"
    ).fetchone()
    assert tuple(interrupted) == ("review", "InterruptedUnknown")


def test_refund_only_accepts_review_round(k3):
    k3.engine.place("-1001", "2", 2, [("big", 10)], k3.now[0])
    with pytest.raises(Rejected, match="仅待核查"):
        k3.engine.refund_round("-1001", 1, "1", "test")
    k3.store.db.execute(
        "UPDATE k3_rounds SET status='review' WHERE chat='-1001' AND issue=1"
    )
    k3.engine.refund_round("-1001", 1, "1", "test")
    assert k3.store.balance("2", "-1001") == 10000
    assert (
        k3.store.db.execute("SELECT status FROM k3_rounds").fetchone()[0] == "refunded"
    )


@pytest.mark.asyncio
async def test_multiple_groups_draw_concurrently(k3):
    db = k3.store.db
    db.execute("INSERT INTO k3_groups VALUES('-1002',1,1)")
    with k3.store.tx() as transaction:
        k3.store.credit(
            transaction, "test:second-group", "2", 100, "test", chat="-1002"
        )
    k3.engine.activate("-1002", "2", 1, k3.now[0])
    k3.engine.place("-1001", "2", 2, [("sum3", 1)], k3.now[0])
    k3.engine.place("-1002", "2", 2, [("sum3", 1)], k3.now[0])
    k3.now[0] += 51
    fast_finished = asyncio.Event()

    async def send_dice(chat_id, emoji):
        if chat_id == "-1001":
            await asyncio.sleep(0.05)
        else:
            fast_finished.set()
        send_dice.counter += 1
        return SimpleNamespace(
            message_id=send_dice.counter, dice=SimpleNamespace(value=1)
        )

    send_dice.counter = 20
    k3.runtime.bot.send_dice = AsyncMock(side_effect=send_dice)
    rows = db.execute(
        "SELECT * FROM k3_rounds WHERE status='open' ORDER BY chat"
    ).fetchall()
    tasks = [asyncio.create_task(k3.engine.draw(row)) for row in rows]
    await asyncio.wait_for(fast_finished.wait(), 0.03)
    await asyncio.gather(*tasks)
    assert sorted(
        tuple(row)
        for row in db.execute("SELECT chat,status FROM k3_rounds ORDER BY chat")
    ) == [("-1001", "settled"), ("-1002", "settled")]


def test_bet_at_exact_close_is_rejected_atomically(k3):
    k3.engine.place("-1001", "2", 2, [("big", 1)], k3.now[0])
    before = k3.store.balance("2", "-1001")
    k3.now[0] += 50
    with pytest.raises(Rejected, match="已封盘"):
        k3.engine.place("-1001", "2", 3, [("small", 1)], k3.now[0])
    assert k3.store.balance("2", "-1001") == before
    assert k3.store.db.execute("SELECT count(*) FROM k3_requests").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_unchanged_close_panel_does_not_block_dice(k3):
    k3.engine.place("-1001", "2", 2, [("sum3", 1)], k3.now[0])
    k3.store.db.execute("UPDATE k3_rounds SET message=99")
    k3.now[0] += 51
    k3.runtime.bot.edit_message_text = AsyncMock(
        side_effect=BadRequest("Message is not modified")
    )
    k3.runtime.bot.send_dice = AsyncMock(
        side_effect=[
            SimpleNamespace(message_id=i, dice=SimpleNamespace(value=1))
            for i in (10, 11, 12)
        ]
    )
    await k3.engine.draw(k3.store.db.execute("SELECT * FROM k3_rounds").fetchone())
    assert (
        k3.store.db.execute("SELECT status FROM k3_rounds").fetchone()[0] == "settled"
    )
    assert k3.runtime.bot.send_dice.await_count == 3


@pytest.mark.asyncio
async def test_active_k3_room_accepts_compatible_combination_aliases(k3):
    """Compatible aliases must remain owned and accepted by the active K3 room."""
    text = k3.runtime.group_game.text_game
    text.k3 = k3.engine
    event = SimpleNamespace(
        message=SimpleNamespace(
            text="xs10da10",
            message_id=88,
            date=__import__("datetime").datetime.fromtimestamp(
                k3.now[0], __import__("datetime").timezone.utc
            ),
            sender_chat=None,
            forward_origin=None,
            forward_date=None,
            is_automatic_forward=False,
        ),
        effective_message=None,
        effective_user=SimpleNamespace(
            id=2, is_bot=False, first_name="tester", username="tester_name"
        ),
        effective_chat=SimpleNamespace(id=-1001, type="supergroup"),
        callback_query=None,
    )
    assert await text.message(event, event.message.text)
    assert k3.store.db.execute("SELECT count(*) FROM gt_requests").fetchone()[0] == 0
    assert k3.store.db.execute("SELECT count(*) FROM k3_requests").fetchone()[0] == 1
    assert (
        k3.store.db.execute(
            "SELECT count(*) FROM k3_bets WHERE play IN ('small_even','big')"
        ).fetchone()[0]
        == 2
    )
    assert (
        k3.store.db.execute(
            "SELECT username FROM user_labels WHERE uid='2'"
        ).fetchone()[0]
        == "tester_name"
    )


def test_settled_panel_ranks_and_mentions_players(k3):
    db = k3.store.db
    db.execute(
        "INSERT INTO k3_rounds VALUES('-1001',9,'settled',0,?,1,NULL)",
        ('{"values":[6,5,3],"sum":14}',),
    )
    db.execute("INSERT INTO gg_players VALUES('2','阿海')")
    db.execute("INSERT INTO gg_players VALUES('3','小明')")
    db.execute("INSERT INTO k3_bets VALUES('a','-1001',9,'2','big',100,195,'won',0)")
    db.execute("INSERT INTO k3_bets VALUES('b','-1001',9,'3','small',100,0,'lost',0)")
    row = db.execute("SELECT * FROM k3_rounds WHERE issue=9").fetchone()
    text = k3.engine._panel_text(row)
    assert "<blockquote expandable>" in text
    assert "tg://user?id=2" in text
    assert ">阿海</a>" in text and "净赢 <b>+95</b>" in text
    assert "大 · 投入100 · ✅ 中奖 · 返还<b>195</b>" in text
    assert "小 · 投入100 · ❌ 未中 · 返还<b>0</b>" in text
    assert "净输 <b>100</b>" in text
