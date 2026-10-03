"""Offline public navigation, authorized challenge buttons and durable panels."""

# ruff: noqa: F811

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, RetryAfter, TimedOut
from test_superbot_community import community  # noqa: F401
from test_superbot_moderation import setup  # noqa: F401
from test_superbot_text_game import text_service, update  # noqa: F401

from data.plugins.astrbot_plugin_superbot.k3 import K3
from data.plugins.astrbot_plugin_superbot.play_center import PlayCenter
from data.plugins.astrbot_plugin_superbot.store import Rejected


async def invite(s, terms="输的人唱歌 <b>🎵</b>"):
    event = update(s, "dd " + terms, source=1)
    event.message.reply_to_message = SimpleNamespace(
        from_user=SimpleNamespace(id=3, is_bot=False, username="player3")
    )
    await s.text.message(event, event.message.text)
    s.bot.edit_message_text = AsyncMock()
    await s.text.center.tick()
    return s.store.db.execute("SELECT * FROM game_panels").fetchone()


def click(s, panel, action, uid=2, click_id=None, chat=-1001, message=None):
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat, type="supergroup"),
        effective_user=SimpleNamespace(id=uid, username=f"player{uid}", is_bot=False),
        callback_query=SimpleNamespace(
            data=f"pc:{panel['id']}:{panel['duel'] or 0}:{action}",
            id=click_id or f"{uid}-{action}",
            message=SimpleNamespace(message_id=message or panel["message"]),
        ),
    )


@pytest.mark.asyncio
async def test_hub_queries_are_clicker_scoped_and_never_edit_hub(text_service):
    s = text_service
    s.runtime.k3 = K3(s.runtime)
    s.text.k3 = s.runtime.k3
    await s.text.message(update(s, "玩法"), "玩法")
    await s.text.center.tick()
    panel = s.store.db.execute("SELECT * FROM game_panels").fetchone()
    kwargs = s.bot.send_message.await_args.kwargs
    assert len(kwargs["reply_markup"].inline_keyboard) == 1
    assert {
        button.text for row in kwargs["reply_markup"].inline_keyboard for button in row
    } == {
        "🎲 加拿大28",
        "🤝 双人对赌",
    }
    assert "reply_parameters" not in kwargs
    s.bot.edit_message_text = AsyncMock()
    await s.text.center.action(click(s, panel, "points"))
    await s.text.center.action(click(s, panel, "points", uid=3))
    rows = s.store.db.execute(
        "SELECT uid,chat FROM gt_requests ORDER BY rowid"
    ).fetchall()
    assert [(r["uid"], r["chat"]) for r in rows] == [("2", "-1001"), ("3", "-1001")]
    await s.text.center.action(click(s, panel, "points"))
    assert s.store.db.execute("SELECT count(*) FROM gt_requests").fetchone()[0] == 2
    await s.text.center.action(click(s, panel, "activate"))
    session = s.store.db.execute("SELECT * FROM gt_sessions").fetchone()
    assert session["uid"] == "2" and session["expires"] == s.clock[0] + 1800
    s.bot.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_k3_hub_button_activates_for_30_minutes_and_switches_game(text_service):
    s = text_service
    s.runtime.k3 = K3(s.runtime)
    s.text.k3 = s.runtime.k3
    with s.store.tx() as db:
        modules = s.store.get("modules", {}, db)
        modules.update({"game": True, "k3": True})
        s.store.put(db, "modules", modules)
        db.execute(
            "INSERT INTO k3_groups(chat,enabled,version) VALUES('-1001',1,1) "
            "ON CONFLICT(chat) DO UPDATE SET enabled=1"
        )
        db.execute(
            "INSERT INTO gt_sessions(chat,uid,expires,activated,last_message) "
            "VALUES('-1001','2',?,?,99)",
            (s.clock[0] + 100, s.clock[0]),
        )
    await s.text.message(update(s, "玩法"), "玩法")
    await s.text.center.tick()
    panel = s.store.db.execute("SELECT * FROM game_panels").fetchone()

    answer = await s.text.center.action(click(s, panel, "k3", click_id="k3-open"))
    assert "已激活 30 分钟" in answer
    session = s.store.db.execute(
        "SELECT * FROM k3_sessions WHERE chat='-1001' AND uid='2'"
    ).fetchone()
    assert session["expires"] == s.clock[0] + 1800
    assert (
        s.store.db.execute(
            "SELECT expires FROM gt_sessions WHERE chat='-1001' AND uid='2'"
        ).fetchone()[0]
        == 0
    )

    repeated = await s.text.center.action(click(s, panel, "k3", click_id="k3-open"))
    assert "已激活" in repeated
    assert (
        s.store.db.execute(
            "SELECT count(*) FROM game_panel_clicks WHERE id='k3-open'"
        ).fetchone()[0]
        == 1
    )

    canada = await s.text.center.action(
        click(s, panel, "activate", click_id="canada-open")
    )
    assert "加拿大28已激活" in canada
    assert (
        s.store.db.execute(
            "SELECT expires FROM k3_sessions WHERE chat='-1001' AND uid='2'"
        ).fetchone()[0]
        == 0
    )
    assert (
        s.store.db.execute(
            "SELECT expires FROM gt_sessions WHERE chat='-1001' AND uid='2'"
        ).fetchone()[0]
        == s.clock[0] + 1800
    )


@pytest.mark.asyncio
async def test_k3_hub_button_rejects_disabled_group(text_service):
    s = text_service
    s.runtime.k3 = K3(s.runtime)
    s.text.k3 = s.runtime.k3
    await s.text.message(update(s, "玩法"), "玩法")
    await s.text.center.tick()
    panel = s.store.db.execute("SELECT * FROM game_panels").fetchone()
    with pytest.raises(Rejected, match="本群此玩法当前不可用"):
        await s.text.center.action(click(s, panel, "k3"))
    with s.store.tx() as db:
        s.store.put(db, "modules", {**s.store.get("modules"), "k3": True})
    with pytest.raises(Rejected, match="本群此玩法当前不可用"):
        await s.text.center.action(click(s, panel, "k3"))


@pytest.mark.asyncio
async def test_single_panel_lifecycle_no_wallet_replay_and_stale_buttons(text_service):
    s = text_service
    panel = await invite(s)
    before = s.store.db.execute("SELECT count(*) FROM ledger").fetchone()[0]
    assert "&lt;b&gt;" in s.bot.send_message.await_args.kwargs["text"]
    await s.text.center.action(click(s, panel, "accept", uid=3))
    duel = s.store.db.execute("SELECT * FROM duels").fetchone()
    room = json.loads(duel["snapshot"])
    options = list(room["odds"])
    await s.text.center.action(click(s, panel, f"pick{options.index('triple')}"))
    await s.text.center.action(click(s, panel, f"pick{options.index('odd')}", uid=3))
    await s.text.center.tick()
    assert "等待开奖" in s.bot.edit_message_text.await_args.kwargs["text"]
    assert s.bot.send_message.await_count == 1
    s.store.db.execute(
        "INSERT INTO draws(issue,at,raw,balls,evidence,received) VALUES(101,?,'[]','[1,1,1]','test',?)",
        (s.clock[0], s.clock[0]),
    )
    s.text.duel.tick()
    s.clock[0] += 3
    await s.text.center.tick()
    args = s.bot.edit_message_text.await_args.kwargs
    assert args["message_id"] == panel["message"]
    assert "玩家2 胜" in args["text"]
    assert s.store.db.execute("SELECT count(*) FROM gt_requests").fetchone()[0] == 0
    assert (
        s.store.db.execute("SELECT due FROM gt_delete").fetchone()[0] == s.clock[0] + 30
    )
    assert s.store.db.execute("SELECT count(*) FROM ledger").fetchone()[0] == before
    assert s.store.db.execute("SELECT count(*) FROM bets").fetchone()[0] == 0
    await s.text.center.action(click(s, panel, "again", uid=3))
    with pytest.raises(Rejected, match="上一局"):
        await s.text.center.action(click(s, panel, "accept"))
    new = s.store.db.execute("SELECT * FROM game_panels").fetchone()
    assert new["duel"] != panel["duel"]
    assert (
        s.store.db.execute(
            "SELECT status FROM duels WHERE id=?", (new["duel"],)
        ).fetchone()[0]
        == "invited"
    )
    assert s.store.db.execute("SELECT count(*) FROM gt_delete").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_authorization_phase_and_text_betting_isolation(text_service):
    s = text_service
    panel = await invite(s)
    with pytest.raises(Rejected):
        await s.text.center.action(click(s, panel, "accept"))
    with pytest.raises(Rejected):
        await s.text.center.action(click(s, panel, "accept", uid=4))
    with pytest.raises(Rejected):
        await s.text.center.action(click(s, panel, "accept", uid=3, message=999))
    with pytest.raises(Rejected):
        await s.text.center.action(click(s, panel, "accept", uid=3, chat=-1002))
    await s.text.message(update(s, "大100", source=2), "大100")
    assert s.store.db.execute("SELECT count(*) FROM bets").fetchone()[0] == 0
    await s.text.center.action(click(s, panel, "accept", uid=3))
    await s.text.center.action(click(s, panel, "pick0"))
    with pytest.raises(Rejected, match="锁定"):
        await s.text.center.action(click(s, panel, "pick1"))
    s.clock[0] += 200
    with pytest.raises(Rejected, match="超时"):
        await s.text.center.action(click(s, panel, "pick0", uid=3))
    s.text.duel.tick()
    await s.text.center.tick()
    assert "超时" in s.bot.edit_message_text.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_unknown_initial_send_not_retried_restart_or_tick(text_service):
    s = text_service
    await s.text.message(update(s, "玩法"), "玩法")
    s.bot.send_message.side_effect = TimedOut()
    await s.text.center.tick()
    s.clock[0] += 30
    await PlayCenter(s.text).tick()
    assert s.bot.send_message.await_count == 1
    assert (
        s.store.db.execute("SELECT status FROM game_panels").fetchone()[0] == "review"
    )


@pytest.mark.asyncio
async def test_rate_limit_retry_and_missing_panel_no_replacement(text_service):
    s = text_service
    await s.text.message(update(s, "玩法"), "玩法")
    s.bot.send_message.side_effect = RetryAfter(7)
    await s.text.center.tick()
    assert (
        s.store.db.execute("SELECT next FROM game_panels").fetchone()[0]
        == s.clock[0] + 7
    )
    s.clock[0] += 8
    s.bot.send_message.side_effect = None
    await s.text.center.tick()
    assert s.store.db.execute("SELECT status FROM game_panels").fetchone()[0] == "done"


@pytest.mark.asyncio
async def test_edit_network_retry_is_idempotent_and_restart_preserves_id(text_service):
    s = text_service
    panel = await invite(s)
    await s.text.center.action(click(s, panel, "accept", uid=3))
    s.bot.edit_message_text.side_effect = TimedOut()
    await s.text.center.tick()
    s.clock[0] += 16
    s.bot.edit_message_text.side_effect = None
    await PlayCenter(s.text).tick()
    assert s.bot.send_message.await_count == 1
    assert s.bot.edit_message_text.await_args.kwargs["message_id"] == panel["message"]
    await s.text.center.action(click(s, panel, "cancel"))
    s.bot.edit_message_text.side_effect = BadRequest("Message to edit not found")
    await s.text.center.tick()
    assert (
        s.store.db.execute("SELECT status FROM game_panels").fetchone()[0] == "blocked"
    )
    assert s.bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_disabled_group_and_duplicate_invitation(text_service):
    s = text_service
    panel = await invite(s)
    await invite(s)
    assert s.store.db.execute("SELECT count(*) FROM duels").fetchone()[0] == 1
    s.store.db.execute("UPDATE mod_groups SET enabled=0 WHERE chat='-1001'")
    with pytest.raises(Rejected):
        await s.text.center.action(click(s, panel, "accept", uid=3))


@pytest.mark.asyncio
async def test_dd100_reply_remains_normal_bet_not_custom_terms(text_service):
    s = text_service
    event = update(s, "dd100")
    event.message.reply_to_message = SimpleNamespace(
        from_user=SimpleNamespace(id=3, is_bot=False, username="player3")
    )
    assert not await s.text.center.message(event)
    assert s.store.db.execute("SELECT count(*) FROM duels").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("word", ["/games", "/games@fixture"])
async def test_group_menu_command_opens_center(text_service, word):
    from data.plugins.astrbot_plugin_superbot.main import GROUP_SHORTCUTS

    s = text_service
    assert GROUP_SHORTCUTS["games"][0] == "🎮 玩法大全"
    assert await s.group.message(update(s, word), word.split("@")[0], word)
    assert s.store.db.execute("SELECT kind FROM game_panels").fetchone()[0] == "hub"


@pytest.mark.asyncio
async def test_center_history_checkin_flow_profit_and_rules(text_service):
    from data.plugins.astrbot_plugin_superbot.points import DEFAULT, Points

    s = text_service
    s.runtime.points = Points(s.store)
    s.runtime.points.configure("1", {**DEFAULT, "enabled": True, "checkin": 10})
    await s.text.message(update(s, "游戏中心"), "游戏中心")
    await s.text.center.tick()
    panel = s.store.db.execute("SELECT * FROM game_panels").fetchone()
    for action in ("history", "checkin", "flow", "profit"):
        await s.text.center.action(click(s, panel, action))
    rows = s.store.db.execute("SELECT uid,chat,result FROM gt_requests").fetchall()
    assert {r["result"] for r in rows} == {"history", "checkin", "flow", "profit"}
    assert all(r["uid"] == "2" and r["chat"] == "-1001" for r in rows)
    s.bot.send_message.reset_mock()
    await s.text.center.action(click(s, panel, "rules"))
    for call in s.bot.send_message.await_args_list:
        assert (
            call.kwargs["api_kwargs"]["ephemeral_message_parameters"][
                "receiver_user_id"
            ]
            == 2
        )
        assert "<blockquote expandable>" in call.kwargs["text"]


@pytest.mark.asyncio
async def test_conflicting_draw_does_not_finish_panel(text_service):
    s = text_service
    panel = await invite(s)
    await s.text.center.action(click(s, panel, "accept", uid=3))
    await s.text.center.action(click(s, panel, "pick0"))
    await s.text.center.action(click(s, panel, "pick1", uid=3))
    s.store.db.execute(
        "INSERT INTO draws(issue,at,raw,balls,evidence,received,conflict) VALUES(101,?,'[]','[1,1,1]','test',?,1)",
        (s.clock[0], s.clock[0]),
    )
    s.text.duel.tick()
    await s.text.center.tick()
    assert s.store.db.execute("SELECT status FROM duels").fetchone()[0] == "pending"
    assert not s.store.db.execute("SELECT 1 FROM gt_delete").fetchone()


@pytest.mark.asyncio
async def test_decline_and_cancel_require_participants(text_service):
    s = text_service
    panel = await invite(s)
    with pytest.raises(Rejected):
        await s.text.center.action(click(s, panel, "cancel", uid=4))
    await s.text.center.action(click(s, panel, "cancel", uid=3))
    await s.text.center.tick()
    assert s.store.db.execute("SELECT status FROM duels").fetchone()[0] == "cancelled"
    assert s.bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_invitation_username_resolution_and_shared_terms(
    text_service, monkeypatch
):
    from data.plugins.astrbot_plugin_superbot import play_center

    s = text_service
    resolver = AsyncMock(return_value={"target": "3", "target_username": "player3"})
    monkeypatch.setattr(play_center, "resolve", resolver)
    await s.text.message(
        update(s, "对赌 @player3 输的人唱歌"), "对赌 @player3 输的人唱歌"
    )
    resolver.assert_awaited_once_with(s.runtime, "@player3")
    row = s.store.db.execute("SELECT * FROM duels").fetchone()
    assert row["terms1"] == row["terms2"] == "输的人唱歌"


@pytest.mark.asyncio
async def test_concurrent_queries_allocate_after_network_without_duplicate_ids(
    text_service,
):
    import asyncio

    s = text_service
    await s.text.message(update(s, "玩法"), "玩法")
    await s.text.center.tick()
    panel = s.store.db.execute("SELECT * FROM game_panels").fetchone()
    original = s.runtime.community.member

    async def delayed(*args, **kwargs):
        await asyncio.sleep(0)
        return await original(*args, **kwargs)

    s.runtime.community.member = delayed
    await asyncio.gather(
        s.text.center.action(click(s, panel, "flow")),
        s.text.center.action(click(s, panel, "points", uid=3)),
        s.text.center.action(click(s, panel, "flow")),
    )
    rows = s.store.db.execute("SELECT source,uid FROM gt_requests").fetchall()
    assert len(rows) == 2
    assert len({r["source"] for r in rows}) == 2
    assert {r["uid"] for r in rows} == {"2", "3"}
