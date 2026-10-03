"""Group flow shows successful play details without losing-play disclosure."""

# ruff: noqa: F811

import pytest
from test_superbot_community import community  # noqa: F401
from test_superbot_moderation import setup  # noqa: F401
from test_superbot_text_game import request, text_service, update  # noqa: F401


def enter_canada(s):
    s.store.db.execute(
        "INSERT INTO gt_sessions(chat,uid,expires,activated,last_message) "
        "VALUES('-1001','2',?,?,1)",
        (s.clock[0] + 1800, s.clock[0]),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("word", ["流水", "ls", " LS ", "/ls", "/ls@fixture"])
async def test_flow_scope_details_quote_and_lifetime(text_service, word):
    s = text_service
    enter_canada(s)
    for i, status, play, payout, uid, chat in [
        (1, "win", "big", 28, "2", "-1001"),
        (2, "lose", "triple", 0, "2", "-1001"),
        (3, "refund", "small_odd", 10, "2", "-1001"),
        (4, "pending", "pair", 0, "2", "-1001"),
        (5, "win", "dragon", 28, "3", "-1001"),
        (6, "win", "tiger", 28, "2", "-1002"),
    ]:
        s.store.db.execute(
            "INSERT INTO bets(id,uid,room,issue,play,amount,snapshot,status,payout,at,points_chat) "
            "VALUES(?,?,'room28',?,?,10,'{}',?,?,?,?)",
            (str(i), uid, 100 + i, play, status, payout, s.clock[0] + i, chat),
        )
    event = update(s, word)
    event.effective_user.username = "owner"
    await s.group.message(event, word, word)
    await s.text.message(event, word)
    row = request(s)
    assert row["result"] == "flow"
    assert "小单" not in row["text"]
    assert "102 期 · 投注10 · 返还0 · 净变动-10" in row["text"]
    assert all(w not in row["text"] for w in ("豹子", "对子", "龙", "虎"))
    assert "投注30 · 返还38 · 净变动+8" in row["text"]
    assert "第 103 期 · 投注10 · 返还10 · 净变动+0" in row["text"]
    assert s.store.db.execute("SELECT COUNT(*) FROM gt_requests").fetchone()[0] == 1
    await s.text.deliver("-1001")
    args = s.bot.send_message.await_args.kwargs
    assert args["reply_parameters"].message_id == 10
    assert args["text"].startswith("@owner\n🧾")
    folds = [e for e in args["entities"] if e.type == "expandable_blockquote"]
    assert len(folds) == 1
    entity = folds[0]
    folded = (
        args["text"]
        .encode("utf-16-le")[entity.offset * 2 : (entity.offset + entity.length) * 2]
        .decode("utf-16-le")
    )
    assert folded.count(" 期 · ") == 3
    assert "今日统计" not in folded
    assert (
        s.store.db.execute("SELECT due FROM gt_delete").fetchone()[0] == s.clock[0] + 10
    )
    assert s.store.balance("2") == 1000


@pytest.mark.asyncio
async def test_daily_total_not_limited_to_ten_rounds(text_service):
    s = text_service
    enter_canada(s)
    for i in range(12):
        s.store.db.execute(
            "INSERT INTO bets(id,uid,room,issue,play,amount,snapshot,status,payout,at,points_chat) "
            "VALUES(?,'2','room28',?,'big',10,'{}','win',20,?,'-1001')",
            (f"day-{i}", i + 100, s.clock[0]),
        )
    s.store.db.execute(
        "INSERT INTO bets(id,uid,room,issue,play,amount,snapshot,status,payout,at,points_chat) "
        "VALUES('same-round','2','room28',111,'small',10,'{}','lose',0,?,'-1001')",
        (s.clock[0],),
    )
    s.store.db.execute(
        "INSERT INTO bets(id,uid,room,issue,play,amount,snapshot,status,payout,at,points_chat) "
        "VALUES('yesterday','2','room28',99,'big',999,'{}','win',1998,?,'-1001')",
        (s.clock[0] - 86400,),
    )
    await s.text.message(update(s, "流水"), "流水")
    text = request(s)["text"]
    assert text.count(" 期 · ") == 10
    assert "111 期 · 投注20 · 返还20 · 净变动+0" in text
    assert "投注130 · 返还240 · 净变动+110" in text
