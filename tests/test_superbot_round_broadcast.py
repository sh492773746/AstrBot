"""Offline continuous-round lifecycle, isolation and formatting acceptance."""

from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest
from test_superbot_community import community, private  # noqa: F401
from test_superbot_game_hardening import flow, preview  # noqa: F401
from test_superbot_moderation import Member, setup  # noqa: F401

from data.plugins.astrbot_plugin_superbot.group_game import GroupGame
from data.plugins.astrbot_plugin_superbot.main import GROUP_SHORTCUTS
from data.plugins.astrbot_plugin_superbot.rich_text import cards, send_html
from data.plugins.astrbot_plugin_superbot.store import Rejected


@pytest.fixture
def rounds(flow):  # noqa: F811
    s = flow
    s.end = s.clock[0] + 60
    s.runtime.game.current = lambda db=None: (101, s.end)
    s.runtime.group_game = s.flow
    s.service = s.flow.broadcast
    s.store.db.execute("INSERT INTO gb_policy(chat,enabled) VALUES('-1001',1)")
    return s


@pytest.mark.asyncio
async def test_retired_cycles_never_send_even_with_enabled_policy(rounds):
    s = rounds
    for _ in range(3):
        await s.service.tick()
        await s.flow.tick()
        await s.flow.countdown_tick()
        s.clock[0] += 210
    s.bot.send_message.assert_not_awaited()
    s.bot.edit_message_text.assert_not_awaited()
    assert not s.store.db.execute("SELECT 1 FROM gg_dispatch").fetchone()
    assert not s.store.db.execute("SELECT 1 FROM gb_rounds").fetchone()


@pytest.mark.asyncio
async def test_old_broadcast_settings_cannot_reenable(rounds):
    s = rounds
    for op in ("mod_gb_home", "mod_gb_preview", "mod_gb_save"):
        with pytest.raises(Rejected, match="停用"):
            await s.service.configure(s.runtime.ui, private(), {"action": op}, "")
    assert "bet" not in GROUP_SHORTCUTS and "game" not in GROUP_SHORTCUTS
    assert {"games", "points", "checkin"} <= GROUP_SHORTCUTS.keys()
    assert not {"play", "bets"} & GROUP_SHORTCUTS.keys()


@pytest.mark.asyncio
async def test_upgrade_suppresses_pending_preserves_unknown_and_sent(rounds):
    s = rounds
    for status in ("pending", "sent", "review", "sending"):
        s.store.db.execute(
            "INSERT INTO gg_dispatch(id,chat,op,kind,text,status,message) "
            "VALUES(?,'-1001','old','round_open','original text',?,?)",
            (status, status, 7 if status == "sent" else None),
        )
    rebuilt = GroupGame(s.runtime)
    rows = {r["id"]: r for r in s.store.db.execute("SELECT * FROM gg_dispatch")}
    assert rows["pending"]["status"] == "superseded"
    assert rows["sent"]["status"] == "sent" and rows["sent"]["message"] == 7
    assert rows["review"]["status"] == rows["sending"]["status"] == "review"
    assert all(r["text"] == "original text" for r in rows.values())
    for key in rows:
        await rebuilt.deliver(key)
    s.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_safe_format_chunks_and_only_explicit_rejection_fallback():
    raw = "<用户>&😀\n" * 1000
    pages = cards(raw)
    assert "".join(p[0] for p in pages) == raw
    assert all(len(p[1]) < 4000 and "<用户>" not in p[1] for p in pages)
    emoji = "😀" * 9000
    pages = cards(emoji)
    assert "".join(p[0] for p in pages) == emoji
    assert all(len(p[1].encode("utf-16-le")) // 2 < 4000 for p in pages)
    call = AsyncMock(side_effect=[BadRequest("Can't parse entities"), "ok"])
    assert await send_html(call, "<b>&lt;用户&gt;</b>") == "ok"
    assert call.await_args.kwargs["text"] == "<用户>"
    assert call.await_args.kwargs["parse_mode"] is None
    call = AsyncMock(side_effect=TimeoutError)
    with pytest.raises(TimeoutError):
        await send_html(call, "<b>Hello</b>")
    assert call.await_count == 1
    call = AsyncMock(side_effect=BadRequest("message to edit not found"))
    with pytest.raises(BadRequest):
        await send_html(call, "<b>Hello</b>")
    assert call.await_count == 1
