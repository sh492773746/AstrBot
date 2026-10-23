"""Regression coverage for uppercase routing and per-bet settlement details."""

# ruff: noqa: F811

import pytest
from test_superbot import draw
from test_superbot_community import community  # noqa: F401
from test_superbot_moderation import setup  # noqa: F401
from test_superbot_text_game import accept, request, text_service  # noqa: F401

from data.plugins.astrbot_plugin_superbot.bet_text import is_candidate, parse_partial


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,count,total",
    [
        ("A大双50 A小单50 B大50 B单50\nC小单20 C小双20", 6, 240),
        ("A0 10\nA3 10\nA4 10\nA5 10\nA6 10\nA9 10", 6, 60),
        ("a0/10 B3押10 c4.10", 3, 30),
    ],
)
async def test_actual_message_route(text_service, text, count, total):
    s = text_service
    assert is_candidate(text)
    assert len(parse_partial(text)[0]) == count
    await accept(s, "jnd", 1)
    await accept(s, text, 10)
    assert request(s)["result"] == "accepted"
    assert s.store.balance("2") == 1000 - total
    await accept(s, text, 10)
    assert s.store.balance("2") == 1000 - total
    s.clock[0] += 210
    s.runtime.game.ingest([draw(101, s.clock[0])])
    s.text.results.tick()
    output = "\n".join(
        r[0]
        for r in s.store.db.execute(
            "SELECT text FROM gt_requests WHERE result='settlement'"
        )
    )
    assert output.count(" · 返还") == count + 2
    assert any(word in output for word in ("中奖", "未中奖", "回本"))
    for row in s.store.db.execute(
        "SELECT text FROM gt_requests WHERE result='settlement'"
    ):
        assert len(row[0].encode("utf-16-le")) // 2 < 4096
    s.store.db.execute(
        "UPDATE gt_requests SET status='sent' WHERE result<>'settlement'"
    )
    await s.text.deliver("-1001")
    args = s.bot.send_message.await_args.kwargs
    folds = [e for e in args["entities"] if e.type == "expandable_blockquote"]
    assert len(folds) == 1
    entity = folds[0]
    folded = (
        args["text"]
        .encode("utf-16-le")[entity.offset * 2 : (entity.offset + entity.length) * 2]
        .decode("utf-16-le")
    )
    assert folded.count(" · 返还") == count
    assert "净变动" not in folded


def test_ambiguous_position_numbers_not_guessed():
    assert parse_partial("A10 10 大10") == ([("big", 10)], 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [23, 50, 51])
async def test_large_batch_limit_feedback(text_service, count):
    s = text_service
    await accept(s, "jnd", 1)
    text = "a大1 " * count
    await accept(s, text, 10)
    if count <= 50:
        assert request(s)["result"] == "accepted"
        assert s.store.balance("2") == 1000 - count
    else:
        assert request(s)["result"] == "rejected"
        assert "最多50项" in request(s)["text"]
        assert "未下注、未扣分" in request(s)["text"]
        assert s.store.balance("2") == 1000
        await s.text.deliver("-1001")
        assert s.bot.send_message.await_args.kwargs["reply_parameters"].message_id == 10
        assert (
            s.store.db.execute("SELECT due FROM gt_delete").fetchone()[0]
            == s.clock[0] + 10
        )


@pytest.mark.asyncio
async def test_user_full_twenty_three_items(text_service):
    s = text_service
    text = "\n".join(
        [f"A{n} 10" for n in (0, 3, 4, 5, 6, 9)]
        + [f"B{n} 10" for n in (1, 2, 3, 6, 8)]
        + [f"C{n} 10" for n in (0, 2, 5, 7, 8)]
        + ["A大单50", "A小双50", "B大双50", "B小双50", "C大单50", "C大双50", "大边400"]
    )
    await accept(s, "jnd", 1)
    await accept(s, text, 10)
    assert request(s)["result"] == "accepted"
    assert s.store.balance("2") == 140
    assert s.store.db.execute("SELECT COUNT(*) FROM bets").fetchone()[0] == 23
