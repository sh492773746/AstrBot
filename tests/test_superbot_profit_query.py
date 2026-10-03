"""Own-group settled profit query with durable quoted ten-second replies."""

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
@pytest.mark.parametrize("word", ["输赢", "sy", " SY ", "/sy", "/sy@fixture"])
async def test_profit_quote_mention_and_expiry(text_service, word):
    s = text_service
    enter_canada(s)
    for i in range(14):
        s.store.db.execute(
            "INSERT INTO bets(id,uid,room,issue,play,amount,snapshot,status,payout,at,points_chat) "
            "VALUES(?,'2','room28',?,'big',10,'{}',?,20,?,'-1001')",
            (f"fixture-{i}", 100 + i, "pending" if i == 13 else "win", s.clock[0] + i),
        )
    s.store.db.execute(
        "INSERT INTO bets(id,uid,room,issue,play,amount,snapshot,status,payout,at,points_chat) "
        "VALUES('foreign','2','room28',999,'big',10,'{}','win',999,9999999999,'-1002')"
    )
    event = update(s, word)
    event.effective_user.username = "owner"
    await s.group.message(event, word.strip().split("@")[0], word)
    row = request(s)
    assert row["result"] == "profit"
    assert "今日输赢：+130 积分" in row["text"]
    assert "昨日输赢：+0 积分" in row["text"]
    assert "CNY 输赢\n未接入" in row["text"]
    assert "USDT 输赢\n未接入" in row["text"]
    await s.text.deliver("-1001")
    args = s.bot.send_message.await_args.kwargs
    assert args["text"].startswith("@owner\n💎")
    assert len([e for e in args["entities"] if e.type == "expandable_blockquote"]) == 3
    assert args["reply_parameters"].message_id == 10
    s.clock[0] += 9
    await s.text.cleanup()
    s.bot.delete_message.assert_not_awaited()
    s.clock[0] += 1
    await s.text.cleanup()
    assert s.bot.delete_message.await_args.kwargs["message_id"] == 900
    assert s.store.balance("2") == 1000


@pytest.mark.asyncio
async def test_empty_duplicate_and_disabled(text_service):
    s = text_service
    enter_canada(s)
    event = update(s, "输赢")
    await s.text.message(event, "输赢")
    await s.text.message(event, "输赢")
    assert "今日输赢：+0 积分" in request(s)["text"]
    assert s.store.db.execute("SELECT COUNT(*) FROM gt_requests").fetchone()[0] == 1
    s.store.db.execute("UPDATE mod_groups SET enabled=0")
    await s.text.message(update(s, "sy", 20, uid=3), "sy")
    assert request(s, 20) is None
