"""Text shortcuts, durable mentions, atomic check-in and reply lifetimes."""

# ruff: noqa: F811

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
from telegram import MessageEntity
from telegram.error import TimedOut
from test_superbot_community import community  # noqa: F401
from test_superbot_moderation import Member, setup  # noqa: F401
from test_superbot_text_game import accept, request, text_service, update  # noqa: F401

from data.plugins.astrbot_plugin_superbot.bet_text import parse
from data.plugins.astrbot_plugin_superbot.group_points import migrate
from data.plugins.astrbot_plugin_superbot.points import DEFAULT, Points
from data.plugins.astrbot_plugin_superbot.text_game import TextGame


def enter_canada(s):
    s.store.db.execute(
        "INSERT INTO gt_sessions(chat,uid,expires,activated,last_message) "
        "VALUES('-1001','2',?,?,1)",
        (s.clock[0] + 1800, s.clock[0]),
    )


@pytest.mark.parametrize("word", ["dd100", "DD100", "dd100xs20 27/10"])
def test_dd_alias(word):
    assert parse(word)[0] == ("big_odd", 100)


@pytest.mark.asyncio
async def test_history_latest_ten_verified_results_quote_mention_and_sixty_seconds(
    text_service,
):
    s = text_service
    enter_canada(s)
    for issue in range(101, 115):
        s.store.db.execute(
            "INSERT INTO draws(issue,at,raw,balls,evidence,received,conflict) VALUES(?,?,'[]','[1,2,3]','fixture',?,?)",
            (issue, s.clock[0], s.clock[0], int(issue == 114)),
        )
    event = update(s, "历史")
    event.effective_user.username = "fixture_user"
    await asyncio.gather(s.text.message(event, "历史"), s.text.message(event, "历史"))
    row = request(s)
    assert row["result"] == "history"
    assert row["text"].count("期：") == 10
    assert "113 期" in row["text"] and "104 期" in row["text"]
    assert "114 期" not in row["text"] and "103 期" not in row["text"]
    assert s.store.db.execute("SELECT 1 FROM gt_sessions").fetchone()
    s.text = TextGame(s.group)
    await s.text.deliver("-1001")
    args = s.bot.send_message.await_args.kwargs
    assert args["text"].startswith("@fixture_user\n")
    assert args["reply_parameters"].message_id == 10
    assert args["entities"][0].type == MessageEntity.MENTION
    folds = [
        entity for entity in args["entities"] if entity.type == "expandable_blockquote"
    ]
    assert len(folds) == 1
    encoded = args["text"].encode("utf-16-le")
    folded = encoded[
        folds[0].offset * 2 : (folds[0].offset + folds[0].length) * 2
    ].decode("utf-16-le")
    assert folded.count("期：") == 10
    assert folded.startswith("第 113 期：")
    assert "@fixture_user" not in folded and "最近开奖记录" not in folded
    assert (
        s.store.db.execute("SELECT due FROM gt_delete").fetchone()[0] == s.clock[0] + 60
    )
    s.clock[0] += 59
    await s.text.cleanup()
    s.bot.delete_message.assert_not_awaited()
    s.clock[0] += 1
    await TextGame(s.group).cleanup()
    assert s.bot.delete_message.await_args.kwargs["message_id"] == 900


@pytest.mark.asyncio
async def test_history_empty_and_disabled_group(text_service):
    s = text_service
    enter_canada(s)
    s.store.db.execute("DELETE FROM draws")
    await s.text.message(update(s, "历史"), "历史")
    assert "暂无可靠开奖记录" in request(s)["text"]
    s.store.db.execute("UPDATE mod_groups SET enabled=0")
    await s.text.message(update(s, "历史", 20, uid=3), "历史")
    assert request(s, 20) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("word", ["签到", "/checkin", "/checkin@fixture"])
async def test_checkin_atomic_group_reward_quoted_ten_seconds(text_service, word):
    s = text_service
    s.runtime.points = Points(s.store)
    s.runtime.points.configure("1", {**DEFAULT, "enabled": True, "checkin": 10})
    migrate(s.store, "-1001")
    event = update(s, word)
    event.effective_user.username = "checkin_user"
    await asyncio.gather(
        s.group.message(event, word, word), s.group.message(event, word, word)
    )
    assert s.store.balance("2", "-1001") == 1010
    assert "签到成功" in request(s)["text"]
    assert s.store.db.execute("SELECT COUNT(*) FROM gt_requests").fetchone()[0] == 1
    await s.text.deliver("-1001")
    args = s.bot.send_message.await_args.kwargs
    assert args["text"].startswith("@checkin_user\n签到成功")
    assert args["reply_parameters"].message_id == 10
    s.clock[0] += 9
    await s.text.cleanup()
    s.bot.delete_message.assert_not_awaited()
    s.clock[0] += 1
    await s.text.cleanup()
    assert s.bot.delete_message.await_args.kwargs["message_id"] == 900
    await s.text.message(update(s, "签到", 11), "签到")
    assert "今天已签到" in request(s, 11)["text"]
    assert s.store.balance("2", "-1001") == 1010


@pytest.mark.asyncio
async def test_checkin_outbox_failure_rolls_back_reward(text_service):
    s = text_service
    s.runtime.points = Points(s.store)
    s.runtime.points.configure("1", {**DEFAULT, "enabled": True, "checkin": 10})
    migrate(s.store, "-1001")
    s.store.db.execute(
        "CREATE TRIGGER fail_checkin BEFORE INSERT ON gt_requests "
        "WHEN NEW.result='checkin' BEGIN SELECT RAISE(ABORT,'fixture failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        await s.text.message(update(s, "签到"), "签到")
    assert s.store.balance("2", "-1001") == 1000
    assert not s.store.db.execute(
        "SELECT 1 FROM group_ledger WHERE op LIKE 'checkin/%'"
    ).fetchone()


@pytest.mark.asyncio
async def test_checkin_send_unknown_does_not_regrant_or_resend(text_service):
    s = text_service
    s.runtime.points = Points(s.store)
    s.runtime.points.configure("1", {**DEFAULT, "enabled": True, "checkin": 10})
    event = update(s, "签到")
    await s.text.message(event, "签到")
    s.bot.send_message.side_effect = TimedOut()
    await s.text.deliver("-1001")
    assert request(s)["status"] == "review"
    await s.text.message(event, "签到")
    await TextGame(s.group).deliver("-1001")
    assert s.store.balance("2") == 1010
    assert s.bot.send_message.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("word", ["历史", "签到"])
@pytest.mark.parametrize("change", ["forward", "bot", "left", "old"])
async def test_shortcuts_reject_nonoriginal_or_ineligible_messages(
    text_service, word, change
):
    s = text_service
    event = update(s, word, age=61 if change == "old" else 0)
    if change == "forward":
        event.message.forward_origin = object()
    elif change == "bot":
        event.effective_user.is_bot = True
    elif change == "left":
        s.members[2] = Member("left")
    await s.text.message(event, word)
    assert request(s) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("word", ["jnd", "积分", "dd10", "历史", "签到"])
async def test_mentions_use_actual_author_and_utf16_fallback(text_service, word):
    s = text_service
    s.runtime.points = Points(s.store)
    s.runtime.points.configure("1", {**DEFAULT, "enabled": True, "checkin": 10})
    if word == "dd10":
        await accept(s, "jnd", 1)
    event = update(s, word)
    event.effective_user.full_name = "😀<真实用户>"
    await s.text.message(event, word)
    await TextGame(s.group).deliver("-1001")
    args = s.bot.send_message.await_args.kwargs
    assert args["text"].startswith("😀<真实用户>\n")
    entity = args["entities"][0]
    assert entity.type == MessageEntity.TEXT_LINK
    assert entity.url == "tg://user?id=2"
    assert entity.length == len("😀<真实用户>".encode("utf-16-le")) // 2
    assert args["parse_mode"] is None


@pytest.mark.asyncio
async def test_private_bets_remains_private_with_username(text_service):
    s = text_service
    event = update(s, "/bets")
    event.effective_user.username = "history_owner"
    s.bot.send_message.return_value = SimpleNamespace(
        message_id=0, ephemeral_message_id=77
    )
    await s.group.message(event, "/bets", "/bets")
    await s.text.deliver("-1001")
    args = s.bot.send_message.await_args.kwargs
    assert args["text"].startswith("@history_owner\n")
    assert args["api_kwargs"]["ephemeral_message_parameters"]["receiver_user_id"] == 2
    assert (
        s.store.db.execute("SELECT due FROM gt_delete").fetchone()[0] == s.clock[0] + 10
    )
