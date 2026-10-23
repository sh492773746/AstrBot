"""Regression coverage for issues found by real-account acceptance."""

# ruff: noqa: F811
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import NetworkError, RetryAfter, TimedOut
from test_superbot_ad_killer import env  # noqa: F401
from test_superbot_community import community  # noqa: F401
from test_superbot_k3 import k3  # noqa: F401
from test_superbot_moderation import setup  # noqa: F401
from test_superbot_round_results import settled
from test_superbot_slots import slots  # noqa: F401
from test_superbot_text_game import request, text_service, update  # noqa: F401

from data.plugins.astrbot_plugin_superbot.k3 import K3, ODDS


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["flow", "profit"])
@pytest.mark.parametrize("game", ["canada", "k3"])
async def test_refunds_never_become_losses(text_service, action, game):
    s = text_service
    K3(s.runtime)
    balance = s.store.balance("2")
    entries = [
        ("won", 10, 20, s.clock[0], "2", "-1001"),
        ("lost", 10, 0, s.clock[0], "2", "-1001"),
        ("refunded", 11, 0, s.clock[0], "2", "-1001"),
        ("refunded", 111, 0, s.clock[0] - 86400, "2", "-1001"),
        ("pending", 999, 0, s.clock[0], "2", "-1001"),
        ("won", 999, 9999, s.clock[0], "3", "-1001"),
        ("won", 999, 9999, s.clock[0], "2", "-1002"),
    ]
    for index, (status, amount, payout, at, uid, chat) in enumerate(entries, 1):
        if game == "k3":
            s.store.db.execute(
                "INSERT INTO k3_bets VALUES(?,?,?,?,?,?,?,?,?)",
                (str(index), chat, index, uid, "big", amount, payout, status, at),
            )
        else:
            status = {"won": "win", "lost": "lose", "refunded": "refund"}.get(
                status, status
            )
            if status == "refund":
                status = "cancelled"
            s.store.db.execute(
                "INSERT INTO bets(id,uid,room,issue,play,amount,snapshot,status,payout,at,points_chat) "
                "VALUES(?,?,'room28',?,'big',?,'{}',?,?,?,?)",
                (str(index), uid, index, amount, status, payout, at, chat),
            )
    if game == "canada":
        s.store.db.execute(
            "INSERT INTO bets(id,uid,room,issue,play,amount,snapshot,status,payout,at,points_chat) "
            "VALUES('normal-return','2','room28',8,'big_odd',5,'{}','refund',5,?,'-1001')",
            (s.clock[0],),
        )
    await s.text.query_points(update(s, action), action, game_kind=game)
    text = request(s)["text"]
    if action == "flow":
        assert (
            "投注20 · 返还20 · 净变动+0"
            if game == "k3"
            else "投注25 · 返还25 · 净变动+0"
        ) in text
        assert text.count(" 期 · ") == (2 if game == "k3" else 3)
        assert "第 3 期 · " not in text
    else:
        assert "今日输赢：+0 积分" in text
        assert "昨日输赢：+0 积分" in text
    assert "异常退款" in text
    assert s.store.balance("2") == balance


@pytest.mark.asyncio
async def test_bets_alias_uses_k3_room_and_shared_flow_cooldown(k3):
    s = k3
    text = s.runtime.group_game.text_game
    text.k3 = s.engine
    s.store.db.execute(
        "INSERT INTO k3_bets VALUES('settled','-1001',7,'2','big',10,19,'won',?)",
        (s.now[0],),
    )
    event = update(SimpleNamespace(clock=s.now), "/bets", 10)
    event.effective_user.username = "tester"
    await s.runtime.group_game.message(event, "/bets", "/bets")
    await text.query_points(update(SimpleNamespace(clock=s.now), "流水", 11), "flow")
    row = s.store.db.execute("SELECT * FROM gt_requests").fetchone()
    assert row["result"] == "bets"
    assert "积分快三" in row["text"] and "第 7 期 · 投注10 · 返还19" in row["text"]
    assert s.store.db.execute("SELECT count(*) FROM gt_requests").fetchone()[0] == 1
    s.runtime.bot.send_message.return_value = SimpleNamespace(
        message_id=0, ephemeral_message_id=99
    )
    await text.deliver("-1001")
    sent = s.runtime.bot.send_message.await_args.kwargs
    assert sent["text"].startswith("@tester\n🧾")
    assert sent["reply_parameters"].message_id == 10
    assert any(e.type == "expandable_blockquote" for e in sent["entities"])
    assert sent["api_kwargs"]["ephemeral_message_parameters"]["receiver_user_id"] == 2
    assert (
        s.store.db.execute("SELECT due FROM gt_delete").fetchone()[0] == s.now[0] + 10
    )


@pytest.mark.asyncio
async def test_old_bets_callback_deduplicates_without_quoting_other_user(text_service):
    s = text_service
    event = update(s, "/bets")
    event.message = None
    event.callback_query = SimpleNamespace(id="legacy-bets-click")
    await s.group.action(event, {"action": "bets"})
    s.clock[0] += 6
    await s.group.action(event, {"action": "bets"})
    row = s.store.db.execute("SELECT * FROM gt_requests").fetchone()
    assert row["source"] < 0 and row["result"] == "bets"
    assert s.store.db.execute("SELECT count(*) FROM gt_requests").fetchone()[0] == 1
    s.bot.send_message.return_value = SimpleNamespace(
        message_id=0, ephemeral_message_id=99
    )
    await s.text.deliver("-1001")
    assert "reply_parameters" not in s.bot.send_message.await_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [None, NetworkError("connect failed")])
async def test_bets_membership_check_is_single_and_fails_closed(text_service, error):
    s = text_service
    s.runtime.community.member = AsyncMock(side_effect=error)
    await s.group.message(update(s, "/bets"), "/bets", "/bets")
    assert s.runtime.community.member.await_count == 1
    assert bool(request(s)) == (error is None)
    s.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("username", ["", "tester"])
async def test_canada_mentions_duplicate_names_with_unicode(text_service, username):
    s = text_service
    await settled(s)
    s.store.db.execute("UPDATE user_labels SET username=? WHERE uid='2'", (username,))
    s.group.broadcast.roster = lambda chat, issue: [
        {"uid": str(uid), "name": "同名🙂", "n": 1, "stake": 10, "payout": 20}
        for uid in (2, 3)
    ]
    s.text.results.tick()
    await s.text.deliver("-1001")
    sent = s.bot.send_message.await_args.kwargs
    mentions = [e for e in sent["entities"] if e.type in {"mention", "text_link"}]
    assert len(mentions) == 2 and mentions[0].offset != mentions[1].offset
    labels = [
        sent["text"]
        .encode("utf-16-le")[e.offset * 2 : (e.offset + e.length) * 2]
        .decode("utf-16-le")
        for e in mentions
    ]
    assert labels == ["@tester" if username else "同名🙂", "同名🙂"]
    assert mentions[1].url == "tg://user?id=3"
    if not username:
        assert mentions[0].url == "tg://user?id=2"


def seed_result(s, players=1):
    """Seed a bounded settled result without Telegram or wallet mutations.

    Args:
        s: Isolated K3 fixture.
        players: Number of independent participants.

    Returns:
        Saved round row.
    """
    db = s.store.db
    db.execute(
        "INSERT INTO k3_rounds VALUES('-1001',9,'settled',0,?,1,100)",
        ('{"values":[1,2,3],"sum":6}',),
    )
    for uid in range(2, players + 2):
        db.execute("INSERT INTO gg_players VALUES(?,?)", (str(uid), "同名🙂<&>"))
        db.execute("INSERT INTO user_labels VALUES(?,?)", (str(uid), f"tester{uid}"))
        for index, play in enumerate(ODDS):
            db.execute(
                "INSERT INTO k3_bets VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    f"{uid}:{index}",
                    "-1001",
                    9,
                    str(uid),
                    play,
                    1,
                    8 if play == "straight" else 0,
                    "won" if play == "straight" else "lost",
                    s.now[0],
                ),
            )
    db.execute(
        "INSERT INTO k3_results(chat,issue,due) VALUES('-1001',9,?)", (s.now[0],)
    )
    return db.execute("SELECT * FROM k3_rounds WHERE issue=9").fetchone()


def test_k3_per_play_details_mentions_merge_and_complete_pages(k3):
    row = seed_result(k3, players=8)
    k3.store.db.execute(
        "INSERT INTO k3_bets VALUES('extra','-1001',9,'2','straight',1,9,'won',?)",
        (k3.now[0],),
    )
    pages = k3.engine._panel_text(row, paged=True)
    assert len(pages) > 1
    assert all(len(page.encode("utf-16-le")) // 2 < 4096 for page in pages)
    text = "\n".join(pages)
    assert "顺子 · 投入2 · ✅ 中奖 · 返还<b>17</b>" in text
    assert "大单 · 投入1 · ❌ 未中" in text
    for uid in range(2, 10):
        assert text.count(f">@tester{uid}</a>") == 1
        assert f"tg://user?id={uid}" in text
    assert text.count("<blockquote expandable>") == 8


@pytest.mark.asyncio
async def test_k3_paginated_retry_resumes_unsent_pages_only(k3):
    row = seed_result(k3, players=8)
    page_count = len(k3.engine._panel_text(row, paged=True))
    balance = k3.store.balance("2", "-1001")
    k3.runtime.bot.send_message = AsyncMock(
        side_effect=[SimpleNamespace(message_id=101), RetryAfter(3)]
    )
    await k3.engine.deliver_result("-1001", 9)
    assert (
        k3.store.db.execute(
            "SELECT status FROM k3_result_pages ORDER BY page"
        ).fetchall()[0][0]
        == "sent"
    )
    k3.now[0] += 3
    restored = K3(k3.runtime)
    k3.runtime.bot.send_message.side_effect = [
        SimpleNamespace(message_id=102 + index) for index in range(page_count - 1)
    ]
    await restored.deliver_result("-1001", 9)
    await restored.deliver_result("-1001", 9)
    assert k3.runtime.bot.send_message.await_count == page_count + 1
    assert k3.store.db.execute("SELECT status FROM k3_results").fetchone()[0] == "sent"
    assert (
        k3.store.db.execute("SELECT count(*) FROM gt_delete").fetchone()[0]
        == page_count + 1
    )
    assert k3.store.balance("2", "-1001") == balance


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimedOut(), asyncio.CancelledError()])
async def test_k3_partial_unknown_page_never_replays(k3, error):
    seed_result(k3, players=8)
    k3.runtime.bot.send_message = AsyncMock(
        side_effect=[SimpleNamespace(message_id=101), error]
    )
    if isinstance(error, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await k3.engine.deliver_result("-1001", 9)
    else:
        await k3.engine.deliver_result("-1001", 9)
    restored = K3(k3.runtime)
    await restored.deliver_result("-1001", 9)
    rows = k3.store.db.execute(
        "SELECT status,message FROM k3_result_pages ORDER BY page"
    ).fetchall()
    assert rows[0]["status"] == "sent" and rows[0]["message"] == 101
    assert rows[1]["status"] == "review" and rows[1]["message"] is None
    assert (
        k3.store.db.execute("SELECT status FROM k3_results").fetchone()[0] == "review"
    )
    assert k3.runtime.bot.send_message.await_count == 2
    assert k3.store.db.execute("SELECT count(*) FROM gt_delete").fetchone()[0] == 1
