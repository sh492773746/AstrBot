"""Own-balance queries use quoted replies and the durable ten_second queue."""

# ruff: noqa: F811

import asyncio

import pytest
from telegram.error import BadRequest, TimedOut
from test_superbot_community import community  # noqa: F401
from test_superbot_moderation import Member, setup  # noqa: F401
from test_superbot_text_game import request, text_service, update  # noqa: F401

from data.plugins.astrbot_plugin_superbot.text_game import TextGame


@pytest.mark.asyncio
@pytest.mark.parametrize("word", ["积分", " 积分查询 ", "/points", "/points@fixture"])
async def test_query_without_game_activation_and_ten_second_reply(text_service, word):
    s = text_service
    with s.store.tx() as db:
        s.store.put(db, "modules", {"game": False, "points": True})
    event = update(s, word)
    assert await s.group.message(event, word.split("@")[0], word)
    assert request(s)["result"] == "points"
    assert not s.store.db.execute("SELECT 1 FROM gt_sessions").fetchone()
    assert not s.store.db.execute("SELECT 1 FROM bets").fetchone()
    await s.text.deliver("-1001")
    args = s.bot.send_message.await_args.kwargs
    assert args["text"] == "玩家2\n你的积分：1000"
    assert args["reply_parameters"].message_id == 10
    assert args["reply_parameters"].allow_sending_without_reply is False
    s.clock[0] += 9
    await s.text.cleanup()
    s.bot.delete_message.assert_not_awaited()
    s.clock[0] += 1
    await s.text.cleanup()
    assert s.bot.delete_message.await_args.kwargs["message_id"] == 900
    assert s.store.balance("2") == 1000


@pytest.mark.asyncio
async def test_query_duplicate_cooldown_and_own_user_balance(text_service):
    s = text_service
    event = update(s, "积分")
    await asyncio.gather(s.text.message(event, "积分"), s.text.message(event, "积分"))
    assert s.store.db.execute("SELECT COUNT(*) FROM gt_requests").fetchone()[0] == 1
    before = s.bot.get_chat_member.await_count
    await s.text.message(update(s, "积分查询", 11), "积分查询")
    assert request(s, 11) is None
    assert s.bot.get_chat_member.await_count == before
    s.clock[0] += 5
    await s.text.message(update(s, "积分", 12, uid=3), "积分")
    assert request(s, 12)["text"] == "你的积分：0"
    await s.text.message(event, "积分")
    assert s.store.db.execute("SELECT COUNT(*) FROM gt_requests").fetchone()[0] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["disabled_group", "module", "left", "forward", "bot", "stale"]
)
async def test_unavailable_queries_do_not_send_or_write(text_service, change):
    s = text_service
    event = update(s, "积分", age=61 if change == "stale" else 0)
    if change == "disabled_group":
        s.store.db.execute("UPDATE mod_groups SET enabled=0")
    elif change == "module":
        with s.store.tx() as db:
            s.store.put(db, "modules", {"points": False})
    elif change == "left":
        s.members[2] = Member("left")
    elif change == "forward":
        event.message.forward_origin = object()
    elif change == "bot":
        event.effective_user.is_bot = True
    await s.text.message(event, "积分")
    assert request(s) is None
    s.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_query_refreshes_balance_on_delivery_and_resumes_pending(text_service):
    s = text_service
    await s.text.message(update(s, "积分"), "积分")
    with s.store.tx() as db:
        s.store.credit(db, "fixture-more", "2", 10, "fixture")
    s.text = TextGame(s.group)
    await s.text.deliver("-1001")
    assert s.bot.send_message.await_args.kwargs["text"] == "玩家2\n你的积分：1010"
    assert (
        s.store.db.execute("SELECT due FROM gt_delete").fetchone()[0] == s.clock[0] + 10
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,status",
    [
        (TimedOut(), "review"),
        (BadRequest("reply message not found"), "blocked"),
    ],
)
async def test_query_never_falls_back_or_resends_unknown(text_service, error, status):
    s = text_service
    await s.text.message(update(s, "积分"), "积分")
    s.bot.send_message.side_effect = error
    await s.text.deliver("-1001")
    assert request(s)["status"] == status
    await TextGame(s.group).deliver("-1001")
    assert s.bot.send_message.await_count == 1
    assert s.store.balance("2") == 1000
