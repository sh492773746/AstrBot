"""Offline superbot acceptance: no real Telegram writes or legacy DB access."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from data.plugins.astrbot_plugin_superbot.ads import Ads, expiry
from data.plugins.astrbot_plugin_superbot.game import Game
from data.plugins.astrbot_plugin_superbot.keno import parse
from data.plugins.astrbot_plugin_superbot.points import DEFAULT, Points
from data.plugins.astrbot_plugin_superbot.rules import (
    canada_balls,
    evaluate,
    room_defaults,
)
from data.plugins.astrbot_plugin_superbot.store import Rejected, Store


@pytest.fixture
def env(tmp_path):
    clock = [datetime(2026, 9, 22, 12, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()]
    store = Store(tmp_path / "db.sqlite3", "1", lambda: clock[0])
    with store.tx() as db:
        store.put(db, "modules", {"ads": True, "game": True, "points": True})
        store.credit(db, "seed", "2", 1000000, "test fixture")
    game = Game(store)
    game.configure("1", "room18", True, 1, 200000, 200000)
    game.configure("1", "special", True, 1, 200000, 200000)
    game.ingest([draw(100, clock[0])])
    game.tick_chases()
    yield store, game, clock
    store.close()


def draw(issue, stamp, raw=None):
    return {
        "issue": issue,
        "at": stamp,
        "raw": raw or list(range(1, 21)),
        "evidence": {"source": "fixture", "issue": issue},
    }


def package(**kwargs):
    return dict(
        name="测试置顶",
        kind="pinned",
        price="10.00",
        currency="CNY",
        target="-100123",
        duration="30days",
        slots=1,
        payment="仅测试",
        enabled=True,
        **kwargs,
    )


@pytest.mark.parametrize(
    "room,balls,play,amount,kind,payout",
    [
        ("room18", [5, 5, 5], "big", 100, "win", 200),
        ("room18", [5, 5, 5], "small", 100, "lose", 0),
        ("room18", [4, 4, 5], "number_13", 3, "win", 33),
        ("room18", [8, 8, 8], "extreme_big", 100, "win", 1500),
        ("room18", [2, 2, 5], "pair", 100, "win", 320),
        ("room18", [0, 1, 9], "straight", 100, "win", 1400),
        ("room18", [0, 8, 9], "straight", 100, "win", 1400),
        ("room18", [4, 4, 4], "triple", 100, "win", 6600),
        ("room18", [1, 2, 3], "dragon", 100, "win", 285),
        ("room18", [4, 5, 5], "big", 100, "win", 180),
        ("special", [4, 5, 5], "even", 50000, "win", 94000),
        ("special", [4, 5, 5], "even", 50001, "win", 90001),
        ("double", [4, 5, 5], "big_even", 100, "refund", 100),
        ("double", [4, 5, 5], "big", 100, "win", 160),
        ("room27", [4, 4, 7], "big_odd", 100, "refund", 100),
        ("room28", [4, 5, 6], "big_odd", 100, "refund", 100),
        ("room32", [0, 7, 8], "big", 100, "refund", 100),
        ("room32", [9, 4, 5], "big_even", 100, "refund", 100),
        ("room32", [0, 7, 7], "pair", 100, "win", 320),
        ("room18", [4, 4, 5], "odd", 5, "win", 9),
        ("room18", [4, 4, 4], "pair", 100, "lose", 0),
    ],
)
def test_go_settlement_golden(room, balls, play, amount, kind, payout):
    assert evaluate(room_defaults()[room], play, amount, balls) == (kind, payout)


def test_go_algorithm_and_profile_units():
    assert canada_balls(list(range(1, 21))) == [7, 3, 9]
    rooms = room_defaults()
    assert len(rooms) == 6
    assert rooms["room18"]["maximum"] == 200000
    assert rooms["room27"]["limits"]["pair"] == 20000
    assert rooms["double"]["odds"]["big_odd"] == 420
    assert rooms["room32"]["odds"]["small_odd"] == 700
    with pytest.raises(Rejected):
        canada_balls([1] * 20)


def test_grants_scopes_expiry_and_owner(env):
    store, _, clock = env
    token = store.grant("1", "3", ["ads"], 1)
    with pytest.raises(Rejected):
        store.accept_grant("4", token)
    store.accept_grant("3", token)
    assert store.allowed("3", "ads") and not store.allowed("3", "points")
    with pytest.raises(Rejected):
        store.accept_grant("3", token)
    with pytest.raises(Rejected):
        store.grant("3", "4", ["ads"], 1)
    token = store.grant("1", "4", ["game"], 1)
    clock[0] += 600
    with pytest.raises(Rejected):
        store.accept_grant("4", token)
    store.revoke("1", "3")
    assert not store.allowed("3")
    with pytest.raises(Rejected):
        Store(store.path, "5")


def test_callbacks_private_binding_and_single_use(env):
    store, _, clock = env
    token = store.callback("2", "2", {"action": "save_form"})[3:]
    with pytest.raises(Rejected):
        store.resolve(token, "3", "2")
    with pytest.raises(Rejected):
        store.resolve(token, "2", "-100")
    assert store.resolve(token, "2", "2")["action"] == "save_form"
    store.consume(token)
    with pytest.raises(Rejected):
        store.consume(token)
    clock[0] += 601
    with pytest.raises(Rejected):
        store.resolve(token, "2", "2")


def test_reward_limits_repeats_and_day_boundary(env):
    store, _, clock = env
    points = Points(store)
    points.configure(
        "1",
        {
            **DEFAULT,
            "enabled": True,
            "checkin": 10,
            "chat": 3,
            "cap": 5,
            "groups": ["-100"],
        },
    )
    assert points.checkin("3")
    assert not points.checkin("3")
    assert points.chat("3", "-100", 1, "今日活动在哪里查看")
    clock[0] += 60
    assert not points.chat("3", "-100", 2, "今日活动在哪里查看")
    assert points.chat("3", "-100", 3, "请问今天开始时间是什么")
    clock[0] += 60
    assert not points.chat("3", "-100", 4, "每日签到可以获取什么")
    assert store.balance("3") == 15
    clock[0] += 86400
    assert points.checkin("3")
    assert store.balance("3") == 25
    with pytest.raises(Rejected):
        points.configure("3", DEFAULT)


@pytest.mark.parametrize(
    "body,eligible",
    [
        ("今天开心", False),
        ("今天真开心", True),
        ("abcde", True),
        ("今 天 开 心 !!!", False),
        ("哈哈哈哈哈", False),
    ],
)
def test_chat_reward_minimum_five_effective_characters(env, body, eligible):
    store, _, _ = env
    points = Points(store)
    points.configure(
        "1",
        {
            **DEFAULT,
            "enabled": True,
            "chat": 3,
            "cap": 30,
            "groups": ["-100"],
        },
    )
    before = store.balance("3")
    assert points.chat("3", "-100", 999, body) is eligible
    assert store.balance("3") == before + (3 if eligible else 0)
    assert store.db.execute("SELECT COUNT(*) FROM chat_seen").fetchone()[0] == int(
        eligible
    )


def test_atomic_dedup_rollback_and_room_coverage(env):
    store, game, clock = env
    game.place("2", "room18", 101, ["big"], 100, "one")
    game.place("2", "room18", 101, ["big"], 100, "one")
    assert store.balance("2") == 999900
    with pytest.raises(Rejected):
        game.place("2", "room18", 101, ["big"], 101, "one")
    with pytest.raises(Rejected):
        game.place("2", "special", 101, ["small"], 100, "two")
    assert store.balance("2") == 999900
    with pytest.raises(Rejected):
        game.place("4", "room18", 101, ["big"], 100, "low")
    assert (
        store.db.execute("SELECT COUNT(*) FROM bets WHERE uid='4'").fetchone()[0] == 0
    )
    clock[0] += 190
    with pytest.raises(Rejected):
        game.place("2", "room18", 101, ["odd"], 100, "late")


def test_settlement_restart_conflict_retains_facts(env):
    store, game, clock = env
    game.place("2", "room18", 101, ["big"], 100, "one")
    clock[0] += 210
    game.ingest([draw(101, clock[0])])
    before = store.balance("2")
    assert before == 1000100
    game.ingest([draw(101, clock[0])])
    game.settle()
    assert store.balance("2") == before
    game.ingest([draw(101, clock[0], list(range(2, 22)))])
    assert store.balance("2") == before
    with pytest.raises(Rejected):
        game.current()
    assert (
        store.db.execute(
            "SELECT COUNT(*) FROM ledger WHERE op LIKE ?", ("settle/%",)
        ).fetchone()[0]
        == 1
    )


def test_chase_cycles_cancel_and_recovery(env):
    store, game, clock = env
    game.create_chase("2", "room18", 101, ["big"], 10, 3, "double", False, "plan")
    assert store.balance("2") == 999990
    clock[0] += 210
    game.ingest([draw(101, clock[0])])
    game.tick_chases()
    game.tick_chases()
    assert store.db.execute("SELECT COUNT(*) FROM bets").fetchone()[0] == 2
    game.cancel_chase("2", "plan")
    clock[0] += 210
    game.ingest([draw(102, clock[0])])
    game.tick_chases()
    assert store.db.execute("SELECT COUNT(*) FROM bets").fetchone()[0] == 2
    game.create_chase("2", "room18", 103, ["big"], 10, 3, "flat", False, "second")
    clock[0] += 210
    game.ingest([draw(103, clock[0])])
    restored = Game(store)
    restored.tick_chases()
    assert (
        store.db.execute("SELECT status FROM chases WHERE id='second'").fetchone()[0]
        == "paused"
    )


def test_chase_stop_win_and_skipped_issue(env):
    store, game, clock = env
    game.create_chase("2", "room18", 101, ["big"], 10, 3, "flat", True, "plan")
    clock[0] += 210
    game.ingest([draw(101, clock[0])])
    game.tick_chases()
    assert (
        store.db.execute("SELECT status FROM chases WHERE id='plan'").fetchone()[0]
        == "complete"
    )
    game.create_chase("2", "room18", 102, ["big"], 10, 3, "flat", False, "skip")
    clock[0] += 420
    game.ingest([draw(103, clock[0])])
    game.tick_chases()
    assert (
        store.db.execute("SELECT status FROM chases WHERE id='skip'").fetchone()[0]
        == "paused"
    )


def test_concurrent_wallet_no_overdraft(env):
    store, _, clock = env
    with store.tx() as db:
        store.credit(db, "fund", "8", 10, "fixture")

    def debit(i):
        local = Store(store.path, "1", lambda: clock[0])
        try:
            with local.tx() as db:
                local.credit(db, f"debit{i}", "8", -7, "fixture")
            return True
        except Rejected:
            return False
        finally:
            local.close()

    with ThreadPoolExecutor(2) as pool:
        result = list(pool.map(debit, range(2)))
    assert sum(result) == 1
    assert store.balance("8") == 3


@pytest.mark.asyncio
async def test_permission_notice_destination_throttle_and_recovery(env):
    from data.plugins.astrbot_plugin_superbot.permission_notice import notify

    store, _, clock = env
    runtime = SimpleNamespace(
        store=store, bot=SimpleNamespace(send_message=AsyncMock()), logger=MagicMock()
    )
    chat = SimpleNamespace(id=-100, title="Test", type="supergroup")
    member = SimpleNamespace(status="member")
    await notify(runtime, chat, member)
    assert runtime.bot.send_message.call_args.kwargs["chat_id"] == -100
    assert "请把我设置为管理员" in runtime.bot.send_message.call_args.kwargs["text"]
    await notify(runtime, chat, member)
    assert runtime.bot.send_message.await_count == 1
    await notify(
        runtime,
        chat,
        SimpleNamespace(
            status="administrator",
            can_pin_messages=True,
            can_delete_messages=True,
            can_restrict_members=True,
        ),
    )
    clock[0] += 601
    await notify(runtime, chat, member)
    assert runtime.bot.send_message.await_count == 2
    chat.id = -200
    chat.type = "channel"
    await notify(runtime, chat, member)
    assert runtime.bot.send_message.call_args.kwargs["chat_id"] == store.owner
    assert "发布消息" in runtime.bot.send_message.call_args.kwargs["text"]
    await notify(runtime, chat, SimpleNamespace(status="left"))
    assert runtime.bot.send_message.await_count == 3


@pytest.mark.asyncio
async def test_channel_enable_permissions_and_target_picker(env):
    from data.plugins.astrbot_plugin_superbot.channels import action, target
    from data.plugins.astrbot_plugin_superbot.ui import UI
    from data.plugins.astrbot_plugin_superbot.wizard import run

    store, _, _ = env
    store.db.execute(
        "CREATE TABLE mod_groups(chat TEXT PRIMARY KEY,title TEXT,enabled INTEGER)"
    )
    member = SimpleNamespace(
        status="administrator", can_post_messages=True, can_edit_messages=True
    )
    bot = SimpleNamespace(
        id=9,
        get_chat=AsyncMock(
            return_value=SimpleNamespace(
                id=-10088, title="Test channel", type="channel"
            )
        ),
        get_chat_member=AsyncMock(return_value=member),
    )
    ui = UI(SimpleNamespace(store=store, bot=bot))
    ui.render = AsyncMock()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1))
    assert target("https://t.me/test_channel") == "@test_channel"
    assert target("-10088") == -10088
    await action(
        ui, update, {"action": "channel_preview", "chat": "-10088", "enabled": True}
    )
    assert not store.db.execute("SELECT * FROM ad_channels").fetchall()
    await action(ui, update, ui.render.call_args.args[2][0][1])
    await run(ui, update, "package", ["p", "test", "置顶", "10", "USDT"])
    assert any("Test channel" in label for label, _ in ui.render.call_args.args[2])
    member.can_edit_messages = False
    with pytest.raises(Rejected):
        await action(
            ui, update, {"action": "channel_save", "chat": "-10088", "enabled": True}
        )
    update.effective_user.id = 2
    with pytest.raises(Rejected):
        await action(ui, update, {"action": "channels"})


@pytest.mark.asyncio
async def test_channel_board_permission_loss_blocks_publish(env):
    store, _, _ = env
    ads = Ads(store)
    with store.tx() as db:
        store.put(db, "ad_board_enabled", True)
        db.execute("INSERT INTO ad_channels VALUES('-100123','Channel',1)")
    ads.configure("1", "p", package())
    ads.submit("2", "广告", "body", "contact", "p", store.get("packages")["p"], "a")
    ads.moderate("1", "a", "paid", "verified")
    ads.moderate("1", "a", "approve")
    member = SimpleNamespace(
        status="administrator", can_post_messages=True, can_edit_messages=False
    )
    bot = SimpleNamespace(
        id=9,
        get_chat=AsyncMock(return_value=SimpleNamespace(type="channel", id=-100123)),
        get_chat_member=AsyncMock(return_value=member),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=55)),
        pin_chat_message=AsyncMock(),
    )
    await ads.tick(bot)
    bot.send_message.assert_not_awaited()
    member.can_edit_messages = True
    await ads.tick(bot)
    bot.send_message.assert_awaited_once()
    bot.pin_chat_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_ad_review_paid_filters_and_user_labels(env):
    from data.plugins.astrbot_plugin_superbot.ui import UI

    store, _, _ = env
    ads = Ads(store)
    ads.configure("1", "p", package())
    cfg = store.get("packages")["p"]
    for key in ("paid-order", "unpaid-order"):
        ads.submit("2", "广告", "test", "contact", "p", cfg, key)
    ads.moderate("1", "paid-order", "paid", "verified")
    store.db.execute("INSERT INTO user_labels VALUES('2','tester')")
    ui = UI(SimpleNamespace(store=store, ads=ads))
    ui.render = AsyncMock()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1))
    await ui.admin(update, {"action": "ad_queue"}, "")
    buttons = ui.render.call_args.args[2]
    assert any(
        label == "查看订单 1" and action["id"] == "paid-order"
        for label, action in buttons
        if "id" in action
    )
    assert not any(action.get("id") == "unpaid-order" for _, action in buttons)
    assert "@tester · 已付款" in ui.render.call_args.args[1]
    await ui.admin(update, {"action": "ad_queue", "paid": False}, "")
    assert "@tester · 未付款" in ui.render.call_args.args[1]
    assert any(
        action.get("id") == "unpaid-order" for _, action in ui.render.call_args.args[2]
    )


@pytest.mark.asyncio
async def test_shared_ad_board_payment_edit_expiry_and_restart(env):
    store, _, clock = env
    ads = Ads(store)
    with store.tx() as db:
        store.put(db, "ad_board_enabled", True)
    ads.configure("1", "p", {**package(), "slots": 3})
    cfg = store.get("packages")["p"]
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=88)),
        edit_message_text=AsyncMock(),
        pin_chat_message=AsyncMock(),
        unpin_chat_message=AsyncMock(),
    )
    ads.submit("2", "广告", "first", "contact", "p", cfg, "a")
    ads.moderate("1", "a", "approve")
    await ads.tick(bot)
    bot.send_message.assert_not_awaited()
    ads.edit("1", "a", "edited", "first")
    with pytest.raises(Rejected):
        ads.moderate("1", "a", "approve", expected="first")
    ads.moderate("1", "a", "paid", "verified")
    await ads.tick(bot)
    bot.send_message.assert_not_awaited()
    ads.moderate("1", "a", "approve", expected="edited")
    await ads.tick(bot)
    assert "edited" in bot.send_message.call_args.kwargs["text"]
    ads.submit("2", "广告", "second", "contact", "p", cfg, "b")
    ads.moderate("1", "b", "paid", "verified")
    ads.moderate("1", "b", "approve")
    await ads.tick(bot)
    bot.send_message.assert_awaited_once()
    assert "【广告位 2】" in bot.edit_message_text.call_args.kwargs["text"]
    before = tuple(
        store.db.execute("SELECT paid,expires,message FROM ads WHERE id='b'").fetchone()
    )
    await ads.edit_published("1", "b", "second revised", "second", bot)
    await ads.edit_published("1", "b", "second", "second revised", bot)
    assert (
        tuple(
            store.db.execute(
                "SELECT paid,expires,message FROM ads WHERE id='b'"
            ).fetchone()
        )
        == before
    )
    with pytest.raises(Rejected):
        await ads.edit_published("1", "b", "stale", "second revised", bot)
    with pytest.raises(Rejected):
        await ads.edit_published("2", "b", "unauthorized", "second", bot)
    ads.moderate("1", "a", "stop", "test")
    await ads.tick(bot)
    assert "edited" not in bot.edit_message_text.call_args.kwargs["text"]
    assert "second" in bot.edit_message_text.call_args.kwargs["text"]
    bot.unpin_chat_message.assert_not_awaited()
    ads = Ads(store)
    await ads.tick(bot)
    assert bot.send_message.await_count == 1
    clock[0] += 31 * 86400
    await ads.tick(bot)
    bot.unpin_chat_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_shared_board_unknown_never_retries(env):
    store, _, _ = env
    ads = Ads(store)
    with store.tx() as db:
        store.put(db, "ad_board_enabled", True)
    ads.configure("1", "p", package())
    ads.submit("2", "广告", "test", "contact", "p", store.get("packages")["p"], "a")
    ads.moderate("1", "a", "paid", "verified")
    ads.moderate("1", "a", "approve")
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=TimeoutError()))
    await ads.tick(bot)
    await Ads(store).tick(bot)
    bot.send_message.assert_awaited_once()
    assert store.db.execute("SELECT state FROM ad_boards").fetchone()[0] == "review"


@pytest.mark.asyncio
async def test_ads_dual_review_snapshot_publish_pin_expire(env):
    store, _, clock = env
    ads = Ads(store)
    cfg = package()
    ads.configure("1", "p", cfg)
    cfg = store.get("packages")["p"]
    ads.submit("2", "供应", "服务说明", "contact", "p", cfg, "a")
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=8)),
        pin_chat_message=AsyncMock(),
        unpin_chat_message=AsyncMock(),
    )
    ads.moderate("1", "a", "approve")
    await ads.tick(bot)
    bot.send_message.assert_not_awaited()
    ads.moderate("1", "a", "paid", "到账记录")
    ads.configure("1", "p", {**cfg, "price": "20"})
    await ads.tick(bot)
    await ads.tick(bot)
    await ads.tick(bot)
    bot.send_message.assert_awaited_once()
    bot.pin_chat_message.assert_awaited_once()
    assert (
        json.loads(store.db.execute("SELECT package FROM ads").fetchone()[0])["price"]
        == "10.00"
    )
    clock[0] += 30 * 86400
    await ads.tick(bot)
    await ads.tick(bot)
    bot.unpin_chat_message.assert_awaited_once_with(chat_id="-100123", message_id=8)


@pytest.mark.asyncio
async def test_unknown_ad_no_retry_and_revoked_approver(env):
    store, _, _ = env
    ads = Ads(store)
    ads.configure("1", "p", package())
    cfg = store.get("packages")["p"]
    ads.submit("2", "需求", "需要服务", "contact", "p", cfg, "a")
    grant = store.grant("1", "3", ["ads"], 1)
    store.accept_grant("3", grant)
    ads.moderate("3", "a", "approve")
    ads.moderate("3", "a", "paid", "checked")
    store.revoke("1", "3")
    bot = SimpleNamespace(
        send_message=AsyncMock(side_effect=TimeoutError),
        pin_chat_message=AsyncMock(),
        unpin_chat_message=AsyncMock(),
    )
    await ads.tick(bot)
    bot.send_message.assert_not_awaited()
    ads.moderate("1", "a", "approve")
    ads.moderate("1", "a", "paid", "checked")
    await ads.tick(bot)
    await ads.tick(bot)
    assert store.db.execute("SELECT status FROM ads").fetchone()[0] == "review"
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_renewal_extends_without_resending(env):
    store, _, clock = env
    ads = Ads(store)
    ads.configure("1", "p", package())
    cfg = store.get("packages")["p"]
    ads.submit("2", "供应", "service", "contact", "p", cfg, "a")
    ads.moderate("1", "a", "approve")
    ads.moderate("1", "a", "paid", "checked")
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=8)),
        pin_chat_message=AsyncMock(),
        unpin_chat_message=AsyncMock(),
    )
    await ads.tick(bot)
    await ads.tick(bot)
    ads.renew("2", "a", "p", cfg, "renew")
    ads.moderate("1", "renew", "approve")
    ads.moderate("1", "renew", "paid", "checked")
    await ads.tick(bot)
    bot.send_message.assert_awaited_once()
    assert (
        store.db.execute("SELECT expires FROM ads WHERE id='renew'").fetchone()[0]
        == clock[0] + 60 * 86400
    )
    clock[0] += 30 * 86400
    await ads.tick(bot)
    bot.unpin_chat_message.assert_not_awaited()


def test_source_strict_time_and_dst():
    row = {
        "drawNbr": 123,
        "drawDate": "Sep 22, 2026",
        "drawTime": "01:05:00 AM",
        "drawNbrs": list(range(1, 21)),
    }
    assert parse({"draws": [row]})[0]["issue"] == 123
    with pytest.raises(Rejected):
        parse({"draws": [{**row, "drawTime": "unknown"}]})
    with pytest.raises(Rejected):
        parse(
            {"draws": [{**row, "drawDate": "Nov 2, 2025", "drawTime": "01:30:00 AM"}]}
        )
    now = datetime(2026, 1, 31, 12, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    assert (
        datetime.fromtimestamp(expiry(now, "month"), ZoneInfo("Asia/Shanghai")).day
        == 28
    )


@pytest.mark.asyncio
async def test_disabled_plugin_is_inert():
    from data.plugins.astrbot_plugin_superbot.main import Main

    plugin = Main(MagicMock(), {"enabled": False})
    await plugin.initialize()
    assert not plugin.tasks and plugin.store is None
    await plugin.terminate()


def test_admin_menu_hidden_and_unbound_callbacks(env):
    from data.plugins.astrbot_plugin_superbot.ui import UI

    store, game, _ = env
    ui = UI(SimpleNamespace(store=store, game=game))
    assert "⚙️ 管理" not in str(ui.keyboard("2"))
    assert "⚙️ 管理" in str(ui.keyboard("1"))
    assert "更多" not in str(ui.keyboard("1"))


@pytest.mark.asyncio
async def test_ad_slot_group_picker_and_disabled_group(env):
    from data.plugins.astrbot_plugin_superbot.ui import UI
    from data.plugins.astrbot_plugin_superbot.wizard import run

    store, game, _ = env
    store.db.execute(
        "CREATE TABLE mod_groups(chat TEXT PRIMARY KEY,title TEXT,enabled INTEGER)"
    )
    store.db.executemany(
        "INSERT INTO mod_groups VALUES(?,?,?)",
        [(f"-{i}", f"Group {i:02}", 1) for i in range(1, 10)]
        + [("-99", "Disabled", 0)],
    )
    ui = UI(SimpleNamespace(store=store, game=game))
    ui.render = AsyncMock()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1))
    values = ["slot1", "Test", "置顶", "30", "USDT"]
    await run(ui, update, "package", values)
    buttons = ui.render.call_args.args[2]
    assert sum("Group" in title for title, _ in buttons) == 8
    assert "Disabled" not in str(buttons)
    assert any(title == "下一页" for title, _ in buttons)
    await run(ui, update, "package", values, 1)
    assert sum("Group" in title for title, _ in ui.render.call_args.args[2]) == 1
    with pytest.raises(Rejected, match="未启用管理"):
        await run(ui, update, "package", values + ["-99"])
    with pytest.raises(Rejected, match="发布群"):
        await ui.admin(
            update,
            {
                "action": "save_form",
                "form": "package",
                "data": {"key": "slot1", "package": {"target": "-99"}},
            },
            "test",
        )


@pytest.mark.asyncio
async def test_admin_wizard_choices_confirmation_and_permissions(env):
    from data.plugins.astrbot_plugin_superbot.ui import UI
    from data.plugins.astrbot_plugin_superbot.wizard import run

    store, game, _ = env
    ui = UI(SimpleNamespace(store=store, game=game, points=Points(store)))
    ui.render = AsyncMock()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1))
    await ui.admin(update, {"action": "form", "form": "points"}, "")
    assert ui.render.call_args.args[2][0][0] == "开"
    await run(ui, update, "points", ["开", "10", "1", "60", "20", "-"])
    assert store.get("points", DEFAULT) == DEFAULT
    payload = ui.render.call_args.args[2][0][1]
    await ui.admin(update, payload, "test")
    assert store.get("points")["checkin"] == 10
    with pytest.raises(Rejected):
        await run(ui, update, "points", ["错误"])
    update.effective_user.id = 2
    with pytest.raises(Rejected):
        await run(ui, update, "grant", [])


@pytest.mark.asyncio
async def test_reward_group_buttons_require_enabled_group_and_allow_15_seconds(env):
    from data.plugins.astrbot_plugin_superbot.ui import UI
    from data.plugins.astrbot_plugin_superbot.wizard import run

    store, game, _ = env
    store.db.execute(
        "CREATE TABLE mod_groups(chat TEXT PRIMARY KEY,title TEXT,enabled INTEGER)"
    )
    store.db.executemany(
        "INSERT INTO mod_groups VALUES(?,?,?)",
        [("-123", "Managed", 1), ("-456", "Discovered", 0)],
    )
    ui = UI(SimpleNamespace(store=store, game=game, points=Points(store)))
    ui.render = AsyncMock()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1))
    await run(ui, update, "points", ["开", "10", "1", "15", "20"])
    buttons = ui.render.call_args.args[2]
    assert any("Managed" in title for title, _ in buttons)
    assert not any("Discovered" in title for title, _ in buttons)
    choice = next(payload for title, payload in buttons if "Managed" in title)
    await ui.admin(update, choice, "")
    confirmation = ui.render.call_args.args[2][0][1]
    await ui.admin(update, confirmation, "save")
    assert store.get("points")["groups"] == ["-123"]
    assert store.get("points")["interval"] == 15
    store.db.execute("UPDATE mod_groups SET enabled=0 WHERE chat='-123'")
    with pytest.raises(Rejected, match="尚未启用管理"):
        await ui.admin(update, confirmation, "stale")
    with pytest.raises(Rejected, match="15"):
        Points(store).configure("1", {**DEFAULT, "interval": 14})


@pytest.mark.asyncio
async def test_room_button_confirmation_and_stale_settings(env):
    from data.plugins.astrbot_plugin_superbot.ui import UI

    store, game, _ = env
    ui = UI(SimpleNamespace(store=store, game=game))
    ui.render = AsyncMock()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1))
    original = game.rooms()["room28"]
    await ui.admin(
        update, {"action": "room_preview", "room": "room28", "expected": original}, ""
    )
    assert not game.rooms()["room28"]["enabled"]
    confirmation = ui.render.call_args.args[2][0][1]
    await ui.admin(update, confirmation, "test")
    assert game.rooms()["room28"]["enabled"]
    with pytest.raises(Rejected, match="已变更"):
        await ui.admin(update, confirmation, "test")
    update.effective_user.id = 2
    with pytest.raises(Rejected):
        await ui.admin(update, {"action": "room_manage", "room": "room28"}, "")


@pytest.mark.asyncio
async def test_room_single_limit_preserves_other_values(env):
    from data.plugins.astrbot_plugin_superbot.ui import UI

    store, game, _ = env
    ui = UI(SimpleNamespace(store=store, game=game))
    ui.render = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1), message=SimpleNamespace(text="5")
    )
    original = game.rooms()["room28"]
    await ui.input(
        update,
        {
            "form": "room_limit",
            "room": "room28",
            "field": "minimum",
            "expected": original,
        },
    )
    assert game.rooms()["room28"] == original
    await ui.admin(update, ui.render.call_args.args[2][0][1], "test")
    assert game.rooms()["room28"] == {**original, "minimum": 5}


@pytest.mark.asyncio
async def test_separate_points_and_quick_bet(env):
    from data.plugins.astrbot_plugin_superbot.ui import UI

    store, game, _ = env
    runtime = SimpleNamespace(
        store=store,
        game=game,
        bot=SimpleNamespace(send_message=AsyncMock()),
        points=Points(store),
    )
    ui = UI(runtime)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=2),
        effective_chat=SimpleNamespace(id=2),
        callback_query=None,
        message=SimpleNamespace(text="10"),
    )
    await ui.action(update, {"action": "points"})
    assert "积分记录" in str(runtime.bot.send_message.await_args.kwargs["reply_markup"])
    await ui.action(update, {"action": "game"})
    assert "玩法大全" in runtime.bot.send_message.await_args.kwargs["text"]
    assert (
        "私聊不能下注、开桌或抽奖" in runtime.bot.send_message.await_args.kwargs["text"]
    )
    assert "积分获取" not in str(
        runtime.bot.send_message.await_args.kwargs["reply_markup"]
    )
    before = store.balance("2")
    with pytest.raises(Rejected, match="群里"):
        await ui.action(update, {"action": "bet_pick", "room": "room18", "play": "大"})
    assert store.balance("2") == before
    assert store.dialog("2") is None


@pytest.mark.asyncio
async def test_adapter_hook_rebuild_and_detach():
    from astrbot.core.platform.sources.telegram.tg_adapter import (
        TelegramPlatformAdapter,
    )
    from data.plugins.astrbot_plugin_superbot.main import Main

    plugin = Main(MagicMock(), {"enabled": False})
    one = MagicMock()
    two = MagicMock()
    adapter = SimpleNamespace(_application_hooks={}, application=one)
    TelegramPlatformAdapter.register_application_hook(
        adapter, "superbot", plugin.attach
    )
    plugin.attach(one)
    one.add_handler.assert_called_once()
    for hook in adapter._application_hooks.values():
        hook(two)
    one.remove_handler.assert_called_once()
    two.add_handler.assert_called_once()
    await plugin.terminate()
    two.remove_handler.assert_called_once()


@pytest.mark.asyncio
async def test_native_update_gate_and_revoked_admin(env, monkeypatch):
    from telegram.ext import ApplicationHandlerStop

    from data.plugins.astrbot_plugin_superbot.main import Main
    from data.plugins.astrbot_plugin_superbot.ui import UI

    store, game, _ = env
    plugin = Main(MagicMock(), {"platform_id": "new"})
    plugin.store, plugin.game, plugin.points, plugin.ads = (
        store,
        game,
        Points(store),
        Ads(store),
    )
    bot = SimpleNamespace(send_message=AsyncMock(), username="fixture_bot")
    plugin.application = SimpleNamespace(bot=bot)
    plugin.ui = UI(plugin)
    ticks = [1000.0]
    monkeypatch.setattr(
        "data.plugins.astrbot_plugin_superbot.main.time.monotonic", lambda: ticks[0]
    )

    def update(text, uid="2"):
        ticks[0] += 1
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=int(uid), is_bot=False),
            effective_chat=SimpleNamespace(id=int(uid), type="private"),
            callback_query=None,
            message=SimpleNamespace(text=text, caption=None, photo=[]),
        )

    with pytest.raises(ApplicationHandlerStop):
        await plugin.receive(update("⚙️ 管理"), None)
    assert bot.send_message.await_args.kwargs["text"] == "无权限"
    bot.send_message.reset_mock()
    with pytest.raises(ApplicationHandlerStop):
        await plugin.receive(update("你好，介绍一下自己"), None)
    assert "客服" in bot.send_message.await_args.kwargs["text"]
    with pytest.raises(ApplicationHandlerStop):
        await plugin.receive(update("客服"), None)
    assert not plugin.chat_sessions
    with pytest.raises(ApplicationHandlerStop):
        await plugin.receive(update("/chat"), None)
    bot.send_message.reset_mock()
    await plugin.receive(update("你好，介绍一下自己"), None)
    bot.send_message.assert_not_awaited()
    with pytest.raises(ApplicationHandlerStop):
        await plugin.receive(update("/exit"), None)
    grant = store.grant("1", "3", ["points"], 1)
    store.accept_grant("3", grant)
    token = store.callback(
        "3",
        "3",
        {
            "action": "save_form",
            "form": "adjust",
            "data": {"target": "2", "amount": 100, "reason": "test"},
        },
    )[3:]
    store.revoke("1", "3")
    query = SimpleNamespace(
        data="sb:" + token, answer=AsyncMock(), edit_message_text=AsyncMock()
    )
    incoming = update("", "3")
    incoming.callback_query = query
    with pytest.raises(ApplicationHandlerStop):
        await plugin.receive(incoming, None)
    assert store.balance("2") == 1000000
    assert bot.send_message.await_args.kwargs["text"] == "无权限"


@pytest.mark.asyncio
async def test_private_form_and_duplicate_confirmation(env):
    from telegram.ext import ApplicationHandlerStop

    from data.plugins.astrbot_plugin_superbot.main import Main
    from data.plugins.astrbot_plugin_superbot.ui import UI

    store, game, _ = env
    plugin = Main(MagicMock(), {})
    plugin.store, plugin.game = store, game
    plugin.ui = UI(plugin)
    plugin.application = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    message = SimpleNamespace(text="大 10", caption=None, photo=[])
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=2, is_bot=False),
        effective_chat=SimpleNamespace(id=2, type="private"),
        message=message,
        callback_query=None,
    )
    with pytest.raises(Rejected, match="群"):
        await plugin.ui.input(update, {"form": "bet", "room": "room18", "issue": 101})
    token = store.callback(
        "2",
        "2",
        {
            "action": "bet_submit",
            "room": "room18",
            "issue": 101,
            "plays": ["big"],
            "amount": 10,
            "rules": game.rooms()["room18"],
        },
    )
    query = SimpleNamespace(
        data=token, answer=AsyncMock(), edit_message_text=AsyncMock()
    )
    update.callback_query = query
    for _ in range(2):
        with pytest.raises(ApplicationHandlerStop):
            await plugin.receive(update, None)
    assert store.balance("2") == 1000000
    assert store.db.execute("SELECT count(*) FROM bets").fetchone()[0] == 0


def test_disk_failure_rolls_back_debit(env):
    store, game, _ = env
    store.db.execute(
        "CREATE TRIGGER test_failure BEFORE INSERT ON bets BEGIN SELECT RAISE(ABORT,'test disk failure'); END"
    )
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        game.place("2", "room18", 101, ["big"], 100, "fault")
    assert store.balance("2") == 1000000
    assert (
        store.db.execute("SELECT count(*) FROM ledger WHERE op='bet/fault'").fetchone()[
            0
        ]
        == 0
    )


def test_backup_restoration_and_unknown_tasks(env, tmp_path, monkeypatch, capsys):
    import sqlite3
    import sys

    from data.plugins.astrbot_plugin_superbot.maintenance import main

    store, _, clock = env
    ads = Ads(store)
    ads.configure("1", "p", package())
    ads.submit("2", "供应", "service", "contact", "p", store.get("packages")["p"], "a")
    store.db.execute("UPDATE ads SET status='sending' WHERE id='a'")
    destination = tmp_path / "backup.sqlite3"
    monkeypatch.setattr(
        sys,
        "argv",
        ["maintenance", "backup", str(store.path), "--destination", str(destination)],
    )
    main()
    with sqlite3.connect(destination) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT SUM(delta) FROM ledger").fetchone()[0] == 1000000
    restored = Store(destination, "1", lambda: clock[0])
    assert restored.db.execute("SELECT status FROM ads").fetchone()[0] == "review"
    restored.close()
    monkeypatch.setattr(sys, "argv", ["maintenance", "verify", str(destination)])
    main()
    assert "wallet_mismatches" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_profile_isolated_and_repeated_setup(env):
    from astrbot.core.provider.provider import EmbeddingProvider, Provider
    from data.plugins.astrbot_plugin_superbot.provision import prepare

    store, _, _ = env
    providers = {
        "chat": MagicMock(spec=Provider),
        "embed": MagicMock(spec=EmbeddingProvider),
    }
    personas = {}
    documents = []

    async def create_persona(key, prompt, **kwargs):
        assert kwargs == {"tools": [], "skills": []}
        personas[key] = prompt

    async def upload(name, *args):
        documents.append(SimpleNamespace(doc_name=name))

    kb = SimpleNamespace(
        kb=SimpleNamespace(kb_id="kb-test"),
        kb_db=SimpleNamespace(
            list_documents_by_kb=AsyncMock(side_effect=lambda _: documents)
        ),
        upload_document=AsyncMock(side_effect=upload),
        delete_document=AsyncMock(),
    )
    routing = {}
    manager = SimpleNamespace(confs={}, ucr=SimpleNamespace(umop_to_conf_id=routing))

    async def create_conf(config, name):
        manager.confs["new"] = config
        return "new"

    async def update_route(route, profile):
        routing[route] = profile

    manager.ucr.update_route = AsyncMock(side_effect=update_route)
    manager.create_conf = AsyncMock(side_effect=create_conf)
    manager.get_conf_list = lambda: []
    context = SimpleNamespace(
        get_provider_by_id=lambda key: providers.get(key),
        persona_manager=SimpleNamespace(
            get_persona_v3_by_id=lambda key: personas.get(key),
            create_persona=AsyncMock(side_effect=create_persona),
        ),
        kb_manager=SimpleNamespace(get_kb_by_name=AsyncMock(return_value=kb)),
        astrbot_config_mgr=manager,
    )
    assert await prepare(context, store, "dedicated", "chat", "embed") == "new"
    assert await prepare(context, store, "dedicated", "chat", "embed") == "new"
    manager.create_conf.assert_awaited_once()
    assert kb.upload_document.await_count == 2
    assert all(
        call.args[0].endswith(("-player-help.md", "-player-services.md"))
        for call in kb.upload_document.await_args_list
    )
    documents.extend(
        [
            SimpleNamespace(doc_name="player-game.md", doc_id="old-manual"),
            SimpleNamespace(doc_name="superbot-old-player-help.md", doc_id="old-help"),
            SimpleNamespace(doc_name="custom-operator-notes.md", doc_id="custom"),
        ]
    )
    original_upload = kb.upload_document.side_effect
    documents[:] = [
        doc for doc in documents if not doc.doc_name.endswith("-player-services.md")
    ]
    kb.upload_document.side_effect = RuntimeError("Embedding unavailable")
    calls = kb.upload_document.await_count
    await prepare(context, store, "dedicated", "chat", "embed", sync_knowledge=False)
    assert kb.upload_document.await_count == calls
    kb.delete_document.assert_not_awaited()
    with pytest.raises(RuntimeError, match="Embedding unavailable"):
        await prepare(context, store, "dedicated", "chat", "embed")
    kb.delete_document.assert_not_awaited()
    kb.upload_document.side_effect = original_upload
    await prepare(context, store, "dedicated", "chat", "embed")
    assert {c.args[0] for c in kb.delete_document.await_args_list} == {
        "old-manual",
        "old-help",
    }
    assert manager.confs["new"]["admins_id"] == []
    assert manager.confs["new"]["kb_names"] == [
        "大海传媒超级机器人-"
        + __import__("hashlib").sha256(b"dedicated").hexdigest()[:12]
        + "-使用知识"
    ]
    assert routing == {"dedicated:*:*": "new"}
    context.get_provider_by_id = lambda key: None
    assert await prepare(context, store, "dedicated", "chat", "embed") == "new"
    assert kb.upload_document.await_count == 4
    manager.confs["new"]["admins_id"] = ["unauthorized"]
    with pytest.raises(Rejected):
        await prepare(context, store, "dedicated", "chat", "embed")
    manager.confs["new"]["admins_id"] = []
    routing["dedicated:*:*"] = "other"
    with pytest.raises(Rejected):
        await prepare(context, store, "dedicated", "chat", "embed")
