"""Group-scoped funds, reward limits, migration and settlement acceptance."""

# ruff: noqa: F811

import asyncio
import sqlite3

import pytest
from test_superbot import draw
from test_superbot_community import community, private  # noqa: F401
from test_superbot_moderation import setup  # noqa: F401
from test_superbot_text_game import accept, request, text_service, update  # noqa: F401

from data.plugins.astrbot_plugin_superbot.group_points import migrate
from data.plugins.astrbot_plugin_superbot.points import DEFAULT, Points
from data.plugins.astrbot_plugin_superbot.store import Rejected, Store
from data.plugins.astrbot_plugin_superbot.ui import parse_form


@pytest.fixture
def scoped(text_service):
    s = text_service
    migrate(s.store, "-1001")
    s.store.db.execute(
        "INSERT INTO mod_groups(chat,title,enabled) VALUES('-1002','Other',1)"
    )
    s.runtime.points = Points(s.store)
    return s


def test_migration_conserves_funds_idempotent_and_archive_is_frozen(scoped):
    s = scoped
    assert s.store.balance("2", "-1001") == 1000
    assert s.store.balance("2", "-1002") == 0
    migrate(s.store, "-1002")
    assert s.store.balance("2", "-1002") == 0
    for query in (
        "UPDATE wallets SET balance=balance+1",
        "DELETE FROM ledger",
        "INSERT INTO wallets VALUES('999',100)",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            s.store.db.execute(query)
    before = s.store.db.execute("SELECT SUM(delta) FROM ledger").fetchone()[0]
    assert (
        s.store.db.execute("SELECT SUM(delta) FROM group_ledger").fetchone()[0]
        == before
    )
    with pytest.raises(Rejected):
        s.store.balance("2")
    with pytest.raises(Rejected), s.store.tx() as db:
        s.store.credit(db, "unscoped", "2", 100, "bad")
    reopened = Store(s.store.path, "1", s.store.clock)
    try:
        migrate(reopened, "-1001")
        assert reopened.balance("2", "-1001") == 1000
    finally:
        reopened.close()


@pytest.mark.parametrize("target", [None, "2", "-999", "-1002"])
def test_migration_requires_approved_enabled_destination(text_service, target):
    with pytest.raises(Rejected):
        migrate(text_service.store, target)
    assert not text_service.store.get("group_points_enabled", False)
    assert text_service.store.balance("2") == 1000


def test_migration_rejects_unbalanced_archive(text_service):
    text_service.store.db.execute("UPDATE wallets SET balance=balance+1")
    with pytest.raises(Rejected, match="账本"):
        migrate(text_service.store, "-1001")
    assert not text_service.store.get("group_points_enabled", False)


def test_fresh_store_can_enable_without_legacy_target(tmp_path):
    store = Store(tmp_path / "fresh.db", "1")
    try:
        migrate(store)
        assert store.balance("2", "-1001") == 0
        with pytest.raises(Rejected), store.tx() as db:
            store.credit(db, "missing", "2", 10, "test")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_two_groups_same_message_id_independent_debit_and_settlement(scoped):
    s = scoped
    with s.store.tx() as db:
        s.store.credit(db, "fund", "2", 100, "test", chat="-1002")
    await accept(s, "jnd", 1)
    await accept(s, "jnd", 1, chat=-1002)
    await asyncio.gather(
        accept(s, "大10", 10),
        accept(s, "小20", 10, chat=-1002),
    )
    assert request(s)["result"] == request(s, chat="-1002")["result"] == "accepted"
    assert s.store.balance("2", "-1001") == 990
    assert s.store.balance("2", "-1002") == 80
    await accept(s, "大10", 10)
    assert s.store.balance("2", "-1001") == 990
    s.clock[0] += 210
    s.runtime.game.ingest([draw(101, s.clock[0])])
    for group, before in (("-1001", 990), ("-1002", 80)):
        payout = s.store.db.execute(
            "SELECT SUM(payout) FROM bets WHERE points_chat=?", (group,)
        ).fetchone()[0]
        assert s.store.balance("2", group) == before + payout
    before = list(s.store.db.execute("SELECT * FROM group_wallets"))
    s.runtime.game.settle()
    assert [tuple(r) for r in before] == [
        tuple(r) for r in s.store.db.execute("SELECT * FROM group_wallets")
    ]


@pytest.mark.asyncio
async def test_empty_group_cannot_spend_other_group_funds(scoped):
    s = scoped
    await accept(s, "jnd", 1, chat=-1002)
    await accept(s, "大10", 10, chat=-1002)
    assert request(s, chat="-1002")["result"] == "rejected"
    assert s.store.balance("2", "-1001") == 1000
    assert s.store.balance("2", "-1002") == 0
    assert not s.store.db.execute("SELECT 1 FROM bets").fetchone()


@pytest.mark.asyncio
async def test_batch_failure_rolls_back_only_target_group(scoped):
    s = scoped
    await accept(s, "jnd", 1)
    await accept(s, "大10小100", 10)
    assert request(s)["result"] == "rejected"
    assert s.store.balance("2", "-1001") == 1000
    assert s.store.balance("2", "-1002") == 0


@pytest.mark.asyncio
async def test_legacy_pending_order_returns_to_migration_wallet(text_service):
    s = text_service
    await accept(s, "jnd", 1)
    await accept(s, "大10", 10)
    migrate(s.store, "-1001")
    assert s.store.balance("2", "-1001") == 990
    s.clock[0] += 210
    s.runtime.game.ingest([draw(101, s.clock[0])])
    payout = s.store.db.execute("SELECT SUM(payout) FROM bets").fetchone()[0]
    assert s.store.balance("2", "-1001") == 990 + payout
    assert s.store.balance("2", "-1002") == 0


def test_checkin_chat_caps_and_duplicates_are_per_group(scoped):
    s = scoped
    points = s.runtime.points
    points.configure(
        "1",
        {
            **DEFAULT,
            "enabled": True,
            "checkin": 10,
            "chat": 3,
            "cap": 3,
            "groups": ["-1001", "-1002"],
        },
    )
    for group in ("-1001", "-1002"):
        assert points.checkin("2", group)
        assert not points.checkin("2", group)
        assert points.chat("2", group, 30, "今天大家一起聊聊")
        assert not points.chat("2", group, 31, "今天大家一起聊聊")
    assert s.store.balance("2", "-1001") == 1013
    assert s.store.balance("2", "-1002") == 13
    with pytest.raises(Rejected):
        points.checkin("2")
    s.store.db.execute("UPDATE mod_groups SET enabled=0 WHERE chat='-1002'")
    with pytest.raises(Rejected):
        points.checkin("3", "-1002")
    assert not points.chat("3", "-1002", 35, "今天还会有什么活动")


def test_adjust_requires_group_and_is_idempotent(scoped):
    s = scoped
    p = s.runtime.points
    with pytest.raises(Rejected):
        p.adjust("1", "2", 100, "test", "old-button")
    with pytest.raises(Rejected):
        p.adjust("2", "2", 100, "test", "unauthorized", chat="-1001")
    for group in ("-1001", "-1002"):
        p.adjust("1", "2", 100, "test", "adjust-test", chat=group)
        p.adjust("1", "2", 100, "test", "adjust-test", chat=group)
    assert s.store.balance("2", "-1001") == 1100
    assert s.store.balance("2", "-1002") == 100
    assert parse_form("adjust", "-1002 2 100 测试奖励", {}) == {
        "chat": "-1002",
        "target": "2",
        "amount": 100,
        "reason": "测试奖励",
    }


@pytest.mark.asyncio
async def test_private_balance_pages_and_checkin_do_not_default_to_legacy(scoped):
    s = scoped
    await s.runtime.ui.action(private(2), {"action": "account"})
    assert "按群独立" in s.bot.send_message.await_args.kwargs["text"]
    await s.runtime.ui.action(
        private(2), {"action": "points_history", "points_chat": "-1002"}
    )
    assert "本群积分：0" in s.bot.send_message.await_args.kwargs["text"]
    await s.runtime.ui.action(private(2), {"action": "checkin"})
    assert "请到需要领取积分的群" in s.bot.send_message.await_args.kwargs["text"]
    assert s.store.balance("2", "-1001") == 1000
    assert s.store.balance("2", "-1002") == 0


@pytest.mark.asyncio
async def test_group_query_and_history_do_not_leak_other_group(scoped):
    s = scoped
    await s.text.message(update(s, "积分", chat=-1002), "积分")
    await s.text.deliver("-1002")
    assert s.bot.send_message.await_args.kwargs["text"] == "玩家2\n你的积分：0"
    await accept(s, "jnd", 1)
    await accept(s, "大10", 10)
    await accept(s, "jnd", 1, chat=-1002)
    await s.text.queue_bets(update(s, "/bets", 20, chat=-1002))
    assert "暂无已结算期次" in request(s, 20, "-1002")["text"]
    assert "第 101 期 · " not in request(s, 20, "-1002")["text"]


def test_same_operation_cannot_be_rebound_within_group(scoped):
    s = scoped
    with s.store.tx() as db:
        assert s.store.credit(db, "same", "2", 1, "test", chat="-1001")
    with pytest.raises(Rejected), s.store.tx() as db:
        s.store.credit(db, "same", "3", 1, "test", chat="-1001")
    assert s.store.balance("3", "-1001") == 0


@pytest.mark.asyncio
async def test_per_period_limit_is_independent_in_other_group(scoped):
    s = scoped
    with s.store.tx() as db:
        s.store.credit(db, "fund", "2", 1000, "test", chat="-1002")
        rooms = s.runtime.game.rooms(db)
        rooms["room28"]["maximum"] = 100
        rooms["room28"]["total"] = 100
        s.store.put(db, "rooms", rooms)
    for chat in (-1001, -1002):
        await accept(s, "jnd", 1, chat=chat)
        await accept(s, "大100", 10, chat=chat)
        assert request(s, 10, str(chat))["result"] == "accepted"
        await accept(s, "大1", 11, chat=chat)
        assert request(s, 11, str(chat))["result"] == "rejected"
        assert s.store.balance("2", str(chat)) == 900


def test_migration_preserves_existing_daily_checkin(text_service):
    s = text_service
    points = Points(s.store)
    points.configure("1", {**DEFAULT, "enabled": True, "checkin": 10})
    assert points.checkin("2")
    migrate(s.store, "-1001")
    assert not points.checkin("2", "-1001")
    assert s.store.balance("2", "-1001") == 1010


def test_legacy_chase_keeps_destination(text_service):
    s = text_service
    s.runtime.game.create_chase(
        "2", "room28", 101, ["big"], 10, 2, "flat", False, "old"
    )
    migrate(s.store, "-1001")
    assert s.store.db.execute("SELECT points_chat FROM chases").fetchone()[0] == "-1001"
    s.clock[0] += 210
    s.runtime.game.ingest([draw(101, s.clock[0])])
    s.runtime.game.recovered = True
    s.runtime.game.tick_chases()
    assert s.store.db.execute("SELECT COUNT(*) FROM bets").fetchone()[0] == 2
    assert all(
        r[0] == "-1001" for r in s.store.db.execute("SELECT points_chat FROM bets")
    )


@pytest.mark.asyncio
async def test_disabled_group_still_receives_settlement(scoped):
    s = scoped
    await accept(s, "jnd", 1)
    await accept(s, "大10", 10)
    s.store.db.execute("UPDATE mod_groups SET enabled=0 WHERE chat='-1001'")
    s.clock[0] += 210
    s.runtime.game.ingest([draw(101, s.clock[0])])
    payout = s.store.db.execute("SELECT SUM(payout) FROM bets").fetchone()[0]
    assert s.store.balance("2", "-1001") == 990 + payout
