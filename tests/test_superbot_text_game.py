"""Offline acceptance for strict text orders and durable short-lived replies."""

import asyncio
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest, Forbidden, RetryAfter, TimedOut
from test_superbot import draw
from test_superbot_community import community  # noqa: F401
from test_superbot_moderation import Member, setup  # noqa: F401

from data.plugins.astrbot_plugin_superbot.bet_text import ALIASES, is_candidate, parse
from data.plugins.astrbot_plugin_superbot.game import Game
from data.plugins.astrbot_plugin_superbot.group_game import GroupGame
from data.plugins.astrbot_plugin_superbot.rules import LABELS
from data.plugins.astrbot_plugin_superbot.store import Rejected
from data.plugins.astrbot_plugin_superbot.text_game import TextGame


@pytest.mark.parametrize("alias,label", ALIASES.items())
def test_aliases(alias, label):
    assert parse(alias.upper() + "123") == [(LABELS[label], 123)]


@pytest.mark.parametrize("number", range(28))
def test_number_plays(number):
    assert parse(f"{number}点2") == [(f"number_{number}", 2)]
    assert parse(f"{number}/2") == [(f"number_{number}", 2)]
    assert parse(f"{number:02d}/01") == [(f"number_{number}", 1)]
    assert parse(f"00{number}/001") == [(f"number_{number}", 1)]
    assert is_candidate(f"{number}/2")


@pytest.mark.parametrize(
    "text",
    [
        "b200",
        "ds0",
        "ds-1",
        "ds1.5",
        "ds+1",
        "ds1闲聊",
        "ds1zz2",
        "大10013点200",
        "大10013点200小1",
        "28点1",
        "01点1",
        "28/10",
        "028/10",
        "01/00",
        "27/0",
        "27/-10",
        "27/1.5",
        "27/",
        "27//10",
        "大10027/10",
        "27/10闲聊",
        "27/9999999999999",
        "ds9999999999999",
        "ds1" * 51,
        "大1" + " " * 511,
        "ds",
        "",
    ],
)
def test_reject_entire_malformed_message(text):
    with pytest.raises(Rejected):
        parse(text)


def test_concatenation_separators_and_duplicates():
    assert parse("ds300xs300bz200") == [
        ("big_even", 300),
        ("small_even", 300),
        ("triple", 200),
    ]
    assert parse(" 大100小单50｜DS 2\n大3 | 13点200 ") == [
        ("big", 100),
        ("small_odd", 50),
        ("big_even", 2),
        ("big", 3),
        ("number_13", 200),
    ]
    assert parse("豹1豹子2") == [("leopard_type", 1), ("triple", 2)]
    assert parse("大1" + " " * 510) == [("big", 1)]
    assert len(parse("大1" * 20)) == 20
    assert is_candidate("b200") and is_candidate("ds-2")
    assert not is_candidate("今天买大100") and not is_candidate("大家好")


@pytest.mark.parametrize("separator", [" ", "\n", "|", "｜"])
def test_slash_number_mixed(separator):
    assert parse(f"大单100{separator}27/10{separator}0/20") == [
        ("big_odd", 100),
        ("number_27", 10),
        ("number_0", 20),
    ]


def test_all_mixed_combinations():
    assert parse("大100小50单20双30大单100小双50ds10xd20 27/10\n0/20｜13点30") == [
        ("big", 100),
        ("small", 50),
        ("odd", 20),
        ("even", 30),
        ("big_odd", 100),
        ("small_even", 50),
        ("big_even", 10),
        ("small_odd", 20),
        ("number_27", 10),
        ("number_0", 20),
        ("number_13", 30),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["大单100 27/10", "大单100\n27/10", "27/10"])
async def test_slash_number_atomic_acceptance(text_service, text):
    s = text_service
    await accept(s, "jnd", 1)
    await accept(s, text)
    assert request(s)["result"] == "accepted"
    assert s.store.balance("2") == (990 if text == "27/10" else 890)
    assert (
        s.store.db.execute(
            "SELECT COUNT(*) FROM bets WHERE play='number_27' AND amount=10"
        ).fetchone()[0]
        == 1
    )
    await accept(s, text)
    assert s.store.balance("2") == (990 if text == "27/10" else 890)


@pytest.fixture
def text_service(community):  # noqa: F811
    s = community
    s.runtime.application = SimpleNamespace(running=True)
    s.runtime.game = Game(s.store)
    s.group = s.runtime.group_game = GroupGame(s.runtime)
    s.text = s.group.text_game
    s.bot.send_message.return_value = SimpleNamespace(message_id=900)
    with s.store.tx() as db:
        s.store.put(db, "modules", {"game": True, "points": True})
        rooms = s.runtime.game.rooms()
        rooms["room28"]["enabled"] = True
        s.store.put(db, "rooms", rooms)
        s.store.credit(db, "fixture", "2", 1000, "fixture")
        db.execute(
            "INSERT INTO draws(issue,at,raw,balls,evidence,received) VALUES(100,?,'[]','[1,2,3]','fixture',?)",
            (s.clock[0] - 10, s.clock[0]),
        )
    return s


def update(s, text, source=10, uid=2, chat=-1001, age=0):
    message = SimpleNamespace(
        text=text,
        message_id=source,
        date=datetime.fromtimestamp(s.clock[0] - age, timezone.utc),
        sender_chat=None,
        forward_origin=None,
    )
    return SimpleNamespace(
        message=message,
        effective_message=message,
        effective_user=SimpleNamespace(id=uid, is_bot=False),
        effective_chat=SimpleNamespace(id=chat, type="supergroup"),
        callback_query=None,
    )


async def accept(s, text="大10", source=10, **kwargs):
    """Set up sessions as already acknowledged for the betting transport tests."""
    result = await s.text.message(update(s, text, source, **kwargs), text)
    if text.strip().lower() in {"jnd", "canada", "加拿大"}:
        s.store.db.execute(
            "UPDATE gt_requests SET status='sent' WHERE chat=? AND source=? AND result='activated'",
            (str(kwargs.get("chat", -1001)), source),
        )
    return result


def request(s, source=10, chat="-1001"):
    return s.store.db.execute(
        "SELECT * FROM gt_requests WHERE chat=? AND source=?", (chat, source)
    ).fetchone()


@pytest.mark.asyncio
async def test_atomic_success_duplicate_and_quote_cleanup(text_service):
    s = text_service
    await accept(s, "Canada", 1)
    assert s.bot.send_message.await_count == 0
    await accept(s, "ds30xs30bz20")
    assert s.store.balance("2") == 920
    assert request(s)["result"] == "accepted"
    await asyncio.gather(accept(s, "ds30xs30bz20"), accept(s, "ds30xs30bz20"))
    assert s.store.balance("2") == 920
    assert s.store.db.execute("SELECT COUNT(*) FROM bets").fetchone()[0] == 3
    await asyncio.gather(s.text.deliver("-1001"), s.text.deliver("-1001"))
    assert s.bot.send_message.await_count == 1
    args = s.bot.send_message.await_args.kwargs
    assert args["reply_parameters"].message_id == 10
    assert args["reply_parameters"].allow_sending_without_reply is False
    assert "豹子 20" in args["text"] and "920" in args["text"]
    job = s.store.db.execute("SELECT * FROM gt_delete").fetchone()
    assert job["due"] == s.clock[0] + 10
    await s.text.cleanup()
    s.bot.delete_message.assert_not_awaited()
    s.clock[0] += 9
    await s.text.cleanup()
    s.bot.delete_message.assert_not_awaited()
    s.clock[0] += 1
    await s.text.cleanup()
    assert s.bot.delete_message.await_args.kwargs["message_id"] == 900
    assert s.store.db.execute("SELECT status FROM gt_delete").fetchone()[0] == "deleted"


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["大10小100", "大10bz10000", "大10大1000"])
async def test_all_or_nothing(text_service, text):
    s = text_service
    await accept(s, "jnd", 1)
    await accept(s, text)
    assert s.store.balance("2") == 1000
    assert s.store.db.execute("SELECT COUNT(*) FROM bets").fetchone()[0] == 0
    assert request(s)["result"] == "rejected"
    assert "未下注、未扣分" in request(s)["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "大家今天聊什么",
        "abc123",
        "b200",
        "ds5垃圾",
        "大-1",
        "大1.5",
        "大10013点200",
        "大10" + " " * 512,
    ],
)
async def test_irrelevant_and_malformed_text_never_touches_betting_backend(
    text_service, text
):
    s = text_service
    await accept(s, "jnd", 1)
    statements = []
    s.store.db.set_trace_callback(statements.append)
    membership_calls = s.bot.get_chat_member.await_count
    try:
        await accept(s, text)
    finally:
        s.store.db.set_trace_callback(None)
    assert statements == []
    assert s.bot.get_chat_member.await_count == membership_calls
    assert request(s) is None
    s.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_sessions_fixed_expiry_exit_restart_and_isolation(text_service):
    s = text_service
    await accept(s)
    assert "玩法大全" in request(s)["text"]
    await accept(s, " jNd ", 1)
    expires = s.clock[0] + 1800
    await accept(s, source=11, uid=3)
    assert "玩法大全" in request(s, 11)["text"]
    s.clock[0] += 5
    await accept(s, source=12)
    assert (
        s.store.db.execute("SELECT expires FROM gt_sessions").fetchone()[0] == expires
    )
    s.text = TextGame(s.group)
    assert (
        s.store.db.execute("SELECT expires FROM gt_sessions").fetchone()[0] == expires
    )
    await accept(s, "加拿大", 2)
    assert (
        s.store.db.execute("SELECT expires FROM gt_sessions").fetchone()[0]
        == s.clock[0] + 1800
    )
    await accept(s, "退出加拿大", 3)
    await accept(s, source=13)
    assert "玩法大全" in request(s, 13)["text"]
    await accept(s, "jnd", 4)
    s.clock[0] += 1800
    await accept(s, source=14)
    assert "玩法大全" in request(s, 14)["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["bot", "forward", "anonymous", "edited", "caption", "chat"]
)
async def test_ignore_non_plain_user_messages(text_service, kind):
    s = text_service
    event = update(s, "大10")
    if kind == "bot":
        event.effective_user.is_bot = True
    elif kind == "forward":
        event.message.forward_origin = object()
    elif kind == "anonymous":
        event.message.sender_chat = object()
    elif kind == "edited":
        event.message = None
    elif kind == "caption":
        event.message.text = None
    else:
        event.message.text = "今天聊聊天"
    assert not await s.text.message(event, event.effective_message.text or "")
    assert request(s) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["issue", "disabled", "room", "member", "stale", "before_round", "closed"]
)
async def test_revalidate_after_network_and_message_age(text_service, change):
    s = text_service
    await accept(s, "jnd", 1)

    async def verify(*args, **kwargs):
        if change == "issue":
            s.store.db.execute("UPDATE draws SET issue=101")
        elif change == "disabled":
            s.store.db.execute("UPDATE mod_groups SET enabled=0")
        elif change == "room":
            s.store.db.execute("UPDATE gg_group_room SET room='room27'")
        elif change == "member":
            raise Rejected("已离群")
        elif change == "closed":
            s.clock[0] += 190

    s.runtime.community.member = verify
    await accept(
        s, age=61 if change == "stale" else (11 if change == "before_round" else 0)
    )
    assert request(s)["result"] == "rejected"
    assert s.store.balance("2") == 1000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,status",
    [
        (TimedOut(), "review"),
        (BadRequest("reply message not found"), "blocked"),
        (Forbidden("no access"), "blocked"),
        (RetryAfter(7), "pending"),
    ],
)
async def test_feedback_failures_never_refund_or_unquoted_resend(
    text_service, error, status
):
    s = text_service
    await accept(s, "jnd", 1)
    await accept(s)
    s.bot.send_message.side_effect = error
    await s.text.deliver("-1001")
    assert request(s)["status"] == status
    assert s.store.balance("2") == 990
    await s.text.deliver("-1001")
    assert s.bot.send_message.await_count == 1
    assert s.store.db.execute("SELECT COUNT(*) FROM gt_delete").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_delete_budget_rate_limit_restart_and_known_ids(text_service):
    s = text_service
    await accept(s, "jnd", 1)
    await accept(s)
    await s.text.deliver("-1001")
    s.clock[0] += 10
    s.bot.delete_message.side_effect = RetryAfter(7)
    await s.text.cleanup()
    row = s.store.db.execute("SELECT * FROM gt_delete").fetchone()
    assert row["attempts"] == 0 and row["next"] == s.clock[0] + 7
    s.clock[0] += 7
    s.bot.delete_message.side_effect = TimedOut()
    for attempt, delay in enumerate((5, 15, 45, 120, 0), 1):
        await s.text.cleanup()
        row = s.store.db.execute("SELECT * FROM gt_delete").fetchone()
        assert row["attempts"] == attempt
        assert row["status"] == ("review" if attempt == 5 else "pending")
        if delay:
            assert row["next"] == s.clock[0] + delay
            s.clock[0] += delay
    await s.text.cleanup()
    assert s.bot.delete_message.await_count == 6
    s.store.db.execute("UPDATE gt_delete SET status='sending',attempts=2")
    TextGame(s.group)
    row = s.store.db.execute("SELECT * FROM gt_delete").fetchone()
    assert row["attempts"] == 2 and row["next"] == s.clock[0] + 15
    assert {c.kwargs["message_id"] for c in s.bot.delete_message.await_args_list} == {
        900
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,status",
    [
        (BadRequest("message to delete not found"), "deleted"),
        (Forbidden("permission"), "blocked"),
        (ValueError("unknown"), "review"),
    ],
)
async def test_delete_terminal_results(text_service, error, status):
    s = text_service
    await accept(s, "jnd", 1)
    await accept(s)
    await s.text.deliver("-1001")
    s.clock[0] += 10
    s.bot.delete_message.side_effect = error
    await s.text.cleanup()
    assert s.store.db.execute("SELECT status FROM gt_delete").fetchone()[0] == status


@pytest.mark.asyncio
async def test_old_actions_commands_and_announcements_disabled(text_service):
    s = text_service
    event = update(s, "/bet 大1")
    for action in ("game", "room", "numbers", "pick", "preview", "confirm"):
        with pytest.raises(Rejected, match="停用"):
            await s.group.action(event, {"action": action})
    for command in ("/bet", "/game"):
        assert await s.group.message(event, command, command + " 大1")
    await s.group.tick()
    await s.group.countdown_tick()
    await s.group.broadcast.tick()
    assert s.store.db.execute("SELECT COUNT(*) FROM bets").fetchone()[0] == 0
    assert s.store.db.execute("SELECT COUNT(*) FROM gg_dispatch").fetchone()[0] == 0
    s.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_chats_limit_slow_chat_and_shutdown(text_service):
    s = text_service
    entered, release = [], asyncio.Event()
    for number in range(1, 7):
        chat = str(-1000 - number)
        s.store.db.execute(
            "INSERT OR IGNORE INTO mod_groups(chat,title,enabled) VALUES(?,'Fixture',1)",
            (chat,),
        )
        for source in (10, 11):
            s.store.db.execute(
                "INSERT INTO gt_requests(chat,source,uid,items,result,text,at) "
                "VALUES(?,?,'2','[]','rejected','fixture',?)",
                (chat, source, s.clock[0]),
            )

    async def send(**kwargs):
        entered.append((kwargs["chat_id"], kwargs["reply_parameters"].message_id))
        await release.wait()
        return SimpleNamespace(message_id=900)

    s.bot.send_message.side_effect = send
    task = asyncio.create_task(s.text.delivery_loop())
    try:
        for _ in range(20):
            await asyncio.sleep(0.02)
            if len(entered) == 4:
                break
        assert len(entered) == 4
        assert len({chat for chat, _ in entered}) == 4
        assert {source for _, source in entered} == {10}
        # Acceptance does not await blocked sends.
        await accept(s, "jnd", 1)
        await accept(s, "大1", 20)
        assert request(s, 20)["result"] == "accepted"
        release.set()
        await asyncio.sleep(0.3)
        assert len(entered) == 6
        assert all(source == 10 for _, source in entered)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert not s.text.workers


@pytest.mark.asyncio
async def test_cross_group_balance_race_and_session_isolation(text_service):
    s = text_service
    s.store.db.execute(
        "INSERT INTO mod_groups(chat,title,enabled) VALUES('-1002','Other',1)"
    )
    await accept(s, "jnd", 1)
    await accept(s, source=20, chat=-1002)
    assert "玩法大全" in request(s, 20, "-1002")["text"]
    await accept(s, "jnd", 1, chat=-1002)
    entered, release = 0, asyncio.Event()

    async def verify(*args, **kwargs):
        nonlocal entered
        entered += 1
        if entered == 2:
            release.set()
        await release.wait()

    s.runtime.community.member = verify
    await asyncio.gather(accept(s, "大600", 21), accept(s, "大600", 21, chat=-1002))
    rows = s.store.db.execute(
        "SELECT result FROM gt_requests WHERE source=21"
    ).fetchall()
    assert sorted(r[0] for r in rows) == ["accepted", "rejected"]
    assert s.store.balance("2") == 400
    assert s.store.db.execute("SELECT COUNT(*) FROM bets").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_duplicate_during_member_lookup_and_database_rollback(text_service):
    s = text_service
    await accept(s, "jnd", 1)
    entered, release = 0, asyncio.Event()

    async def verify(*args, **kwargs):
        nonlocal entered
        entered += 1
        if entered == 2:
            release.set()
        await release.wait()

    s.runtime.community.member = verify
    await asyncio.gather(accept(s), accept(s))
    assert s.store.balance("2") == 990
    assert (
        s.store.db.execute(
            "SELECT COUNT(*) FROM gt_requests WHERE result<>'activated'"
        ).fetchone()[0]
        == 1
    )
    s.store.db.execute(
        "CREATE TRIGGER fail_feedback BEFORE INSERT ON gt_requests "
        "BEGIN SELECT RAISE(ABORT,'fixture'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        await accept(s, "大10豹子5", 11)
    assert s.store.balance("2") == 990
    assert s.store.db.execute("SELECT COUNT(*) FROM bets").fetchone()[0] == 1
    assert s.store.db.execute("SELECT COUNT(*) FROM gg_receipts").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_same_chat_pacing_persists_across_restart(text_service):
    s = text_service
    await accept(s, "jnd", 1)
    await accept(s, source=10)
    await accept(s, source=11)
    await s.text.deliver("-1001")
    s.text = TextGame(s.group)
    await s.text.deliver("-1001")
    assert s.bot.send_message.await_count == 1
    s.clock[0] += 1
    s.bot.send_message.return_value = SimpleNamespace(message_id=901)
    await s.text.deliver("-1001")
    assert s.bot.send_message.await_count == 2
    assert s.bot.send_message.await_args.kwargs["reply_parameters"].message_id == 11
    assert s.store.balance("2") == 980


@pytest.mark.asyncio
async def test_cancelled_send_review_and_pending_delete_restarts(text_service):
    s = text_service
    await accept(s, "jnd", 1)
    await accept(s)
    entered = asyncio.Event()

    async def send(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    s.bot.send_message.side_effect = send
    task = asyncio.create_task(s.text.deliver("-1001"))
    await entered.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert request(s)["status"] == "review"
    TextGame(s.group)
    await s.text.deliver("-1001")
    assert s.bot.send_message.await_count == 1
    s.store.db.execute(
        "INSERT INTO gt_delete(chat,message,source,due,next) VALUES('-1001',900,10,?,?)",
        (s.clock[0] + 2, s.clock[0] + 2),
    )
    s.clock[0] += 10
    await TextGame(s.group).cleanup()
    assert s.bot.delete_message.await_args.kwargs["message_id"] == 900


@pytest.mark.asyncio
async def test_replayed_activation_cannot_undo_exit_or_extend_session(text_service):
    s = text_service
    original = update(s, "jnd", 1)
    await s.text.message(original, "jnd")
    expiry = s.store.db.execute("SELECT expires FROM gt_sessions").fetchone()[0]
    s.clock[0] += 5
    await s.text.message(original, "jnd")
    assert s.store.db.execute("SELECT expires FROM gt_sessions").fetchone()[0] == expiry
    await accept(s, "退出加拿大", 2)
    s.text = TextGame(s.group)
    await s.text.message(original, "jnd")
    await accept(s, source=10)
    assert "玩法大全" in request(s)["text"]


@pytest.mark.asyncio
async def test_deleted_original_does_not_cancel_order_or_settlement(text_service):
    s = text_service
    await accept(s, "jnd", 1)
    await accept(s)
    s.bot.send_message.side_effect = BadRequest("reply message not found")
    await s.text.deliver("-1001")
    assert request(s)["status"] == "blocked"
    s.clock[0] += 210
    s.runtime.game.ingest([draw(101, s.clock[0])])
    assert s.store.db.execute("SELECT status FROM bets").fetchone()[0] != "pending"
    balance = s.store.balance("2")
    s.runtime.game.settle()
    await s.group.tick()
    await s.group.broadcast.tick()
    assert s.store.balance("2") == balance
    assert not s.store.db.execute(
        "SELECT 1 FROM notices WHERE id LIKE 'settle/gg:%'"
    ).fetchone()
    assert not s.store.db.execute("SELECT 1 FROM gg_dispatch").fetchone()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["module", "room", "member", "rollback_switch"])
async def test_disabled_or_invalid_orders_do_not_debit(text_service, change):
    s = text_service
    await accept(s, "jnd", 1)
    with s.store.tx() as db:
        if change == "module":
            s.store.put(db, "modules", {"game": False})
        elif change == "room":
            rooms = s.runtime.game.rooms()
            rooms["room28"]["enabled"] = False
            s.store.put(db, "rooms", rooms)
        elif change == "member":
            s.members[2] = Member("left")
        elif change == "rollback_switch":
            s.store.put(db, "text_betting_enabled", False)
    text = "大10"
    event = update(s, text)
    await s.text.message(event, text.strip())
    assert request(s)["result"] == "rejected"
    assert s.store.balance("2") == 1000


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["大100", "xs10da10", "和值10 20", "9 90 10 90"])
async def test_wager_without_active_room_prompts_room_selection(text_service, text):
    s = text_service
    assert await accept(s, text, source=801)
    row = request(s, 801)
    assert row["result"] == "room_required"
    assert "玩法大全" in row["text"]
    assert "未下注、未扣分" in row["text"]
    assert s.store.balance("2") == 1000


@pytest.mark.asyncio
async def test_normal_chat_without_active_room_stays_silent(text_service):
    s = text_service
    assert not await accept(s, "今天大家吃什么", source=802)
    assert request(s, 802) is None
