"""Offline challenge transitions, isolation and point-free settlement."""

# ruff: noqa: F811

from types import SimpleNamespace

import pytest
from test_superbot_community import community  # noqa: F401
from test_superbot_moderation import setup  # noqa: F401
from test_superbot_text_game import text_service, update  # noqa: F401

from data.plugins.astrbot_plugin_superbot.duel import outcome, select_play
from data.plugins.astrbot_plugin_superbot.rules import room_defaults
from data.plugins.astrbot_plugin_superbot.store import Rejected


async def send(s, text, source, uid=2, chat=-1001, target=None):
    event = update(s, text, source=source, uid=uid, chat=chat)
    if target:
        event.message.reply_to_message = SimpleNamespace(
            from_user=SimpleNamespace(
                id=target, username=f"player{target}", is_bot=False
            )
        )
    return await s.text.message(event, text)


async def ready(s):
    await send(s, "dd", 1, target=3)
    await send(s, "对赌 同意", 2, uid=3)
    await send(s, "对赌 彩头 唱歌", 3)
    await send(s, "对赌 彩头 跳舞", 4, uid=3)
    await send(s, "对赌 确认", 5)
    await send(s, "对赌 确认", 6, uid=3)


@pytest.mark.asyncio
async def test_lifecycle_snapshot_no_wallet_and_once(text_service):
    s = text_service
    before = s.store.db.execute("SELECT count(*) FROM ledger").fetchone()[0]
    await ready(s)
    row = s.store.db.execute("SELECT * FROM duels").fetchone()
    assert row["status"] == "choosing" and row["issue"] == 101
    await send(s, "对赌 选择 大", 7)
    await send(s, "对赌 选择 双", 8, uid=3)
    assert s.store.db.execute("SELECT status FROM duels").fetchone()[0] == "pending"
    await send(s, "对赌 选择 小", 9)
    await send(s, "对赌 取消", 10)
    assert s.store.db.execute("SELECT play1 FROM duels").fetchone()[0] == "big"
    s.store.db.execute(
        "INSERT INTO draws(issue,at,raw,balls,evidence,received) VALUES(101,?,'[]','[6,6,6]','test',?)",
        (s.clock[0], s.clock[0]),
    )
    s.text.duel.tick()
    s.text.duel.tick()
    row = s.store.db.execute("SELECT * FROM duels").fetchone()
    assert row["status"] == "settled"
    assert '"winner":0' in row["result"]
    assert (
        s.store.db.execute(
            "SELECT count(*) FROM gt_requests WHERE result='duel_result'"
        ).fetchone()[0]
        == 1
    )
    assert s.store.db.execute("SELECT count(*) FROM ledger").fetchone()[0] == before
    assert s.store.db.execute("SELECT count(*) FROM bets").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_consent_isolation_duplicates_and_self(text_service):
    s = text_service
    await send(s, "dd", 1, target=2)
    assert not s.store.db.execute("SELECT 1 FROM duels").fetchone()
    await send(s, "dd", 2, target=3)
    await send(s, "dd", 2, target=3)
    await send(s, "对赌 同意", 3)
    await send(s, "对赌 同意", 4, uid=4)
    assert s.store.db.execute("SELECT status FROM duels").fetchone()[0] == "invited"
    await send(s, "对赌 同意", 5, uid=3)
    await send(s, "对赌 彩头 100元", 6)
    assert s.store.db.execute("SELECT terms1 FROM duels").fetchone()[0] == ""
    await send(s, "对赌 确认", 7)
    assert s.store.db.execute("SELECT yes1 FROM duels").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_expiry_restart_and_reply(text_service):
    from data.plugins.astrbot_plugin_superbot.duel import Duel

    s = text_service
    await send(s, "dd", 1, target=3)
    await s.text.deliver("-1001")
    assert s.bot.send_message.await_args.kwargs["reply_parameters"].message_id == 1
    assert (
        s.store.db.execute("SELECT due FROM gt_delete").fetchone()[0]
        == s.clock[0] + 120
    )
    s.clock[0] += 121
    Duel(s.text).tick()
    assert s.store.db.execute("SELECT status FROM duels").fetchone()[0] == "expired"
    Duel(s.text).tick()
    assert (
        s.store.db.execute(
            "SELECT count(*) FROM gt_requests WHERE result='duel_result'"
        ).fetchone()[0]
        == 1
    )


@pytest.mark.asyncio
async def test_terms_changes_clear_confirmation_and_combination_rejected(text_service):
    s = text_service
    await ready(s)
    await send(s, "对赌 选择 ds100 bz1", 7)
    await send(s, "对赌 选择 大 小", 8)
    assert s.store.db.execute("SELECT play1 FROM duels").fetchone()[0] == ""
    await send(s, "对赌 选择 18", 9)
    assert s.store.db.execute("SELECT play1 FROM duels").fetchone()[0] == "number_18"


@pytest.mark.parametrize("text", ["ds100 bz1", "大 小", "28", ""])
def test_single_noncombo_only(text):
    with pytest.raises(Rejected):
        select_play(text, room_defaults()["room28"])


@pytest.mark.parametrize(
    "text,play",
    [
        ("压大", "big"),
        ("押大", "big"),
        ("大100", "big"),
        ("ds", "big_even"),
        ("ds100", "big_even"),
        ("dd", "big_odd"),
        ("大单", "big_odd"),
        ("bz1", "triple"),
    ],
)
def test_flexible_single_selection(text, play):
    assert select_play(text, room_defaults()["room28"]) == play


@pytest.mark.asyncio
async def test_both_miss_void_mentions_and_no_replay(text_service):
    import json

    s = text_service
    await ready(s)
    await send(s, "ds", 7)
    await send(s, "s", 8, uid=3)
    s.store.db.execute(
        "INSERT INTO draws(issue,at,raw,balls,evidence,received) VALUES(101,?,'[]','[1,1,1]','test',?)",
        (s.clock[0], s.clock[0]),
    )
    s.text.duel.tick()
    s.text.duel.tick()
    row = s.store.db.execute(
        "SELECT * FROM gt_requests WHERE result='duel_result'"
    ).fetchone()
    assert "双方未中，本局作废" in row["text"]
    assert "重新发起" in row["text"]
    assert len(json.loads(row["items"])["mentions"]) == 2
    assert (
        s.store.db.execute(
            "SELECT count(*) FROM duels WHERE status='pending'"
        ).fetchone()[0]
        == 0
    )
    room = room_defaults()["room28"]
    assert outcome(room, "triple", "odd", [1, 1, 1])[0] == 1


@pytest.mark.asyncio
async def test_plain_terms_confirmation_aliases_and_no_points(text_service):
    s = text_service
    await send(s, "dd", 1, target=3)
    for source, uid, text in [
        (2, 3, "同意"),
        (3, 2, "唱歌"),
        (4, 3, "跳舞"),
        (5, 2, "确定"),
        (6, 3, "QD"),
        (7, 2, "ds100 bz1"),
    ]:
        await send(s, text, source, uid=uid)
    assert s.store.db.execute("SELECT play1 FROM duels").fetchone()[0] == ""
    await send(s, "大100", 8)
    await send(s, "dd", 9, uid=3)
    row = s.store.db.execute("SELECT * FROM duels").fetchone()
    assert (row["terms1"], row["terms2"]) == ("唱歌", "跳舞")
    assert (row["play1"], row["play2"]) == ("big", "big_odd")
    assert row["status"] == "pending"
    assert s.store.db.execute("SELECT count(*) FROM bets").fetchone()[0] == 0
    assert s.store.balance("2") == 1000


def test_winners_use_admin_odds_and_hit_not_payout():
    room = room_defaults()["room28"]
    room["odds"]["big"] = 1234
    room["odds"]["even"] = 999
    assert outcome(room, "big", "even", [6, 6, 6])[0] == 1
    assert outcome(room, "big", "small", [1, 2, 3])[0] == 2
    assert outcome(room, "big", "odd", [1, 2, 3])[0] == 0


@pytest.mark.asyncio
async def test_dd_amount_not_challenge(text_service):
    s = text_service
    await send(s, "dd100", 1)
    assert not s.store.db.execute("SELECT 1 FROM duels").fetchone()


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["对赌", "dd", "DD"])
async def test_no_space_complete_flow(text_service, prefix):
    s = text_service
    await send(s, prefix, 1, target=3)
    await send(s, prefix + "同意", 2, uid=3)
    await send(s, prefix + "彩头唱 一首歌", 3)
    await send(s, prefix + "彩头跳舞", 4, uid=3)
    await send(s, prefix + "确认", 5)
    await send(s, prefix + "确认", 6, uid=3)
    await send(s, prefix + "选择大", 7)
    await send(s, prefix + "选择18", 8, uid=3)
    row = s.store.db.execute("SELECT * FROM duels").fetchone()
    assert row["status"] == "pending"
    assert row["terms1"] == "唱 一首歌"
    assert row["play1"] == "big" and row["play2"] == "number_18"


@pytest.mark.asyncio
async def test_unprefixed_flow_and_result_replies_to_last_selection(text_service):
    s = text_service
    await send(s, "dd", 1, target=3)
    await send(s, "同意", 2, uid=3)
    await send(s, "筹码 唱歌", 3)
    await send(s, "彩头 跳舞", 4, uid=3)
    await send(s, "确认", 5)
    await send(s, "确认", 6, uid=3)
    await send(s, "押注 大", 7)
    await send(s, "双", 8, uid=3)
    row = s.store.db.execute("SELECT * FROM duels").fetchone()
    assert (row["status"], row["play1"], row["play2"], row["last_source"]) == (
        "pending",
        "big",
        "even",
        8,
    )
    s.store.db.execute(
        "INSERT INTO draws(issue,at,raw,balls,evidence,received) VALUES(101,?,'[]','[6,6,6]','test',?)",
        (s.clock[0], s.clock[0]),
    )
    s.text.duel.tick()
    result = s.store.db.execute(
        "SELECT items,text FROM gt_requests WHERE result='duel_result'"
    ).fetchone()
    assert '"reply_to":8' in result["items"]
    assert "🏆 结果：" in result["text"]
    s.store.db.execute(
        "UPDATE gt_requests SET status='sent' WHERE result='duel' AND chat='-1001'"
    )
    await s.text.deliver("-1001")
    assert s.bot.send_message.await_args.kwargs["reply_parameters"].message_id == 8


@pytest.mark.parametrize("word", ["dd100", "DD50", "dd100小20", "ddabc"])
def test_no_space_does_not_capture_bets_or_unknown_text(word):
    from data.plugins.astrbot_plugin_superbot.duel import COMMAND

    assert COMMAND.fullmatch(word) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["拒绝", "取消"])
async def test_no_space_cancel(text_service, action):
    s = text_service
    await send(s, "dd", 1, target=3)
    await send(s, "对赌" + action, 2, uid=3)
    assert s.store.db.execute("SELECT status FROM duels").fetchone()[0] == "cancelled"


@pytest.mark.asyncio
async def test_no_space_username_invitation(text_service, monkeypatch):
    from unittest.mock import AsyncMock

    from data.plugins.astrbot_plugin_superbot import duel

    resolver = AsyncMock(return_value={"target": "3", "target_username": "player3"})
    monkeypatch.setattr(duel, "resolve", resolver)
    await send(text_service, "dd@player3", 1)
    resolver.assert_awaited_once_with(text_service.runtime, "@player3")
    assert (
        text_service.store.db.execute("SELECT second FROM duels").fetchone()[0] == "3"
    )


@pytest.mark.asyncio
async def test_config_change_between_confirmation_rejected(text_service):
    s = text_service
    await send(s, "dd", 1, target=3)
    await send(s, "对赌 同意", 2, uid=3)
    await send(s, "对赌 彩头 唱歌", 3)
    await send(s, "对赌 彩头 跳舞", 4, uid=3)
    await send(s, "对赌 确认", 5)
    with s.store.tx() as db:
        rooms = s.runtime.game.rooms(db)
        rooms["room28"]["odds"]["big"] += 1
        s.store.put(db, "rooms", rooms)
    await send(s, "对赌 确认", 6, uid=3)
    assert s.store.db.execute("SELECT status FROM duels").fetchone()[0] == "terms"
    await send(s, "对赌 彩头 唱两首歌", 7)
    row = s.store.db.execute("SELECT * FROM duels").fetchone()
    assert row["yes1"] == row["yes2"] == 0
    await send(s, "对赌 确认", 8)
    await send(s, "对赌 确认", 9, uid=3)
    assert s.store.db.execute("SELECT status FROM duels").fetchone()[0] == "choosing"


@pytest.mark.asyncio
async def test_cross_round_and_conflicting_draw(text_service):
    s = text_service
    await ready(s)
    await send(s, "对赌 选择 大", 7)
    s.store.db.execute(
        "INSERT INTO draws(issue,at,raw,balls,evidence,received) VALUES(101,?,'[]','[6,6,6]','test',?)",
        (s.clock[0], s.clock[0]),
    )
    await send(s, "对赌 选择 双", 8, uid=3)
    assert s.store.db.execute("SELECT play2 FROM duels").fetchone()[0] == ""
    s.store.db.execute("DELETE FROM draws WHERE issue=101")
    await send(s, "对赌 选择 双", 9, uid=3)
    s.store.db.execute(
        "INSERT INTO draws(issue,at,raw,balls,evidence,received,conflict) "
        "VALUES(101,?,'[]','[6,6,6]','test',?,1)",
        (s.clock[0], s.clock[0]),
    )
    s.text.duel.tick()
    assert s.store.db.execute("SELECT status FROM duels").fetchone()[0] == "pending"
    s.store.db.execute("UPDATE draws SET conflict=0 WHERE issue=101")
    s.text.duel.tick()
    assert s.store.db.execute("SELECT status FROM duels").fetchone()[0] == "settled"


@pytest.mark.asyncio
async def test_other_group_disabled_and_departure(text_service):
    from unittest.mock import AsyncMock

    s = text_service
    await ready(s)
    await send(s, "对赌 选择 大", 7, chat=-1002)
    assert s.store.db.execute("SELECT play1 FROM duels").fetchone()[0] == ""
    original = s.runtime.community.member
    s.runtime.community.member = AsyncMock(side_effect=[None, Rejected("成员已离群")])
    await send(s, "对赌 选择 大", 8)
    s.runtime.community.member = original
    assert s.store.db.execute("SELECT play1 FROM duels").fetchone()[0] == ""
    s.store.db.execute("UPDATE mod_groups SET enabled=0 WHERE chat='-1001'")
    await send(s, "对赌 选择 大", 9)
    assert s.store.db.execute("SELECT play1 FROM duels").fetchone()[0] == ""


@pytest.mark.asyncio
async def test_prefix_free_flow_final_reply_and_native_entities(text_service):
    import json

    from data.plugins.astrbot_plugin_superbot.duel import Duel

    s = text_service
    await send(s, "dd", 1, target=3)
    for source, uid, text in [
        (2, 3, "同意"),
        (3, 2, "筹码 唱歌🎵"),
        (4, 3, "彩头 跳舞"),
        (5, 2, "确认"),
        (6, 3, "确认"),
        (7, 2, "大"),
        (8, 3, "押注 双"),
    ]:
        assert await send(s, text, source, uid=uid)
    row = s.store.db.execute("SELECT * FROM duels").fetchone()
    assert row["status"] == "pending" and row["last_source"] == 8
    assert (
        "您的筹码：“跳舞”"
        in s.store.db.execute("SELECT text FROM gt_requests WHERE source=8").fetchone()[
            0
        ]
    )
    s.store.db.execute("UPDATE gt_requests SET status='sent'")
    s.store.db.execute(
        "INSERT INTO draws(issue,at,raw,balls,evidence,received) VALUES(101,?,'[]','[6,6,6]','test',?)",
        (s.clock[0], s.clock[0]),
    )
    Duel(s.text).tick()
    result = s.store.db.execute(
        "SELECT * FROM gt_requests WHERE result='duel_result'"
    ).fetchone()
    assert json.loads(result["items"])["reply_to"] == 8
    await s.text.deliver("-1001")
    args = s.bot.send_message.await_args.kwargs
    assert args["reply_parameters"].message_id == 8
    assert not args["reply_parameters"].allow_sending_without_reply
    assert "🏆 结果：平局" in args["text"]
    assert any(e.type == "expandable_blockquote" for e in args["entities"])
    assert any(e.type == "bold" for e in args["entities"])
    assert (
        s.store.db.execute("SELECT due FROM gt_delete").fetchone()[0] == s.clock[0] + 30
    )


@pytest.mark.asyncio
async def test_bare_commands_scoped_to_participants_and_phase(text_service):
    s = text_service
    assert not await send(s, "同意", 1)
    await send(s, "dd", 2, target=3)
    assert not await send(s, "确认", 3, uid=4)
    assert not await send(s, "同意", 4, uid=3, chat=-1002)
    assert not await send(s, "大", 5)
    assert not await send(s, "今天聊天", 6)
    await send(s, "同意", 7, uid=3)
    assert s.store.db.execute("SELECT status FROM duels").fetchone()[0] == "terms"
