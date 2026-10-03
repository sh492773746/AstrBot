"""Isolated stranger-private threat probes without live writes or paid calls."""

# ruff: noqa: F811

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.ext import ApplicationHandlerStop
from test_superbot import env  # noqa: F401
from test_superbot_avatar import avatar, confirmation, update  # noqa: F401
from test_superbot_avatar_ai import ai  # noqa: F401

from data.plugins.astrbot_plugin_superbot.ads import Ads
from data.plugins.astrbot_plugin_superbot.main import Main
from data.plugins.astrbot_plugin_superbot.points import Points
from data.plugins.astrbot_plugin_superbot.store import Rejected
from data.plugins.astrbot_plugin_superbot.ui import UI


@pytest.fixture
def private_bot(env):
    store, game, _ = env
    plugin = Main(MagicMock(), {"platform_id": "fixture"})
    plugin.store, plugin.game = store, game
    plugin.points, plugin.ads = Points(store), Ads(store)
    plugin.application = SimpleNamespace(
        bot=SimpleNamespace(send_message=AsyncMock(), username="fixture_bot")
    )
    plugin.ui = UI(plugin)
    return plugin


def incoming(text="", uid=777, callback=None):
    """Build an ordinary unprivileged private Telegram update.

    Args:
        text: Literal message content.
        uid: Telegram sender identity.
        callback: Optional server-bound button string.

    Returns:
        Mock update suitable for the native plugin entry point.
    """
    return SimpleNamespace(
        effective_user=SimpleNamespace(
            id=uid, is_bot=False, username="stranger", full_name="Stranger"
        ),
        effective_chat=SimpleNamespace(id=uid, type="private"),
        callback_query=SimpleNamespace(
            data=callback, answer=AsyncMock(), edit_message_text=AsyncMock()
        )
        if callback
        else None,
        message=None
        if callback
        else SimpleNamespace(text=text, caption=None, photo=[]),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"action": "admin"},
        {"action": "modules"},
        {"action": "admin_points"},
        {"action": "admin_ads"},
        {"action": "admin_game"},
        {"action": "mod_home"},
        {"action": "module_save", "key": "game", "enabled": False},
    ],
)
async def test_stranger_admin_callbacks_rejected(private_bot, payload):
    bot = private_bot
    before = bot.store.get("modules")
    callback = bot.store.callback("777", "777", payload)
    with pytest.raises(ApplicationHandlerStop):
        await bot.receive(incoming(callback=callback), None)
    assert bot.bot.send_message.await_args.kwargs["text"] == "无权限"
    assert bot.store.get("modules") == before
    assert not bot.store.allowed("777")


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_uid,owner_chat", [("1", "1"), ("777", "-1001")])
async def test_stranger_cannot_replay_another_identity(
    private_bot, owner_uid, owner_chat
):
    callback = private_bot.store.callback(owner_uid, owner_chat, {"action": "account"})
    with pytest.raises(ApplicationHandlerStop):
        await private_bot.receive(incoming(callback=callback), None)
    assert "不属于当前会话" in private_bot.bot.send_message.await_args.kwargs["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["bet_submit", "chase_submit", "bet_pick", "room"])
async def test_stranger_private_legacy_bets_never_submit(private_bot, action):
    callback = private_bot.store.callback("777", "777", {"action": action})
    with pytest.raises(ApplicationHandlerStop):
        await private_bot.receive(incoming(callback=callback), None)
    assert "私聊仅查询" in private_bot.bot.send_message.await_args.kwargs["text"]
    assert not private_bot.store.db.execute(
        "SELECT 1 FROM bets WHERE uid='777'"
    ).fetchone()
    assert private_bot.store.balance("777") == 0


@pytest.mark.asyncio
async def test_unknown_expired_and_double_use_callbacks(private_bot, monkeypatch):
    bot = private_bot
    ticks = [1000.0]
    monkeypatch.setattr(
        "data.plugins.astrbot_plugin_superbot.main.time.monotonic", lambda: ticks[0]
    )
    for callback in (
        "sb:missing",
        bot.store.callback("777", "777", {"action": "home"}),
    ):
        bot.store.db.execute("UPDATE callbacks SET expires=0")
        ticks[0] += 1
        with pytest.raises(ApplicationHandlerStop):
            await bot.receive(incoming(callback=callback), None)
        assert "按钮已过期" in bot.bot.send_message.await_args.kwargs["text"]
    token = bot.store.callback("777", "777", {"action": "home"})[3:]
    bot.store.consume(token)
    with pytest.raises(Rejected):
        bot.store.consume(token)


@pytest.mark.asyncio
async def test_stranger_cannot_accept_another_users_grant(private_bot):
    bot = private_bot
    grant = bot.store.grant("1", "2", ["manager"], 30)
    callback = bot.store.callback(
        "777", "777", {"action": "grant_accept", "token": grant}
    )
    with pytest.raises(ApplicationHandlerStop):
        await bot.receive(incoming(callback=callback), None)
    assert not bot.store.allowed("777")
    assert not bot.store.allowed("2")


@pytest.mark.asyncio
async def test_private_callbacks_are_throttled_before_rendering(
    private_bot, monkeypatch
):
    monkeypatch.setattr(
        "data.plugins.astrbot_plugin_superbot.main.time.monotonic", lambda: 1000.0
    )
    bot = private_bot
    bot.keyboard_versions["777"] = str(bot.ui.keyboard("777").to_dict())
    callback = bot.store.callback("777", "777", {"action": "account"})
    before = bot.store.db.execute("SELECT count(*) FROM callbacks").fetchone()[0]
    edits, notices = 0, 0
    for _ in range(20):
        event = incoming(callback=callback)
        with pytest.raises(ApplicationHandlerStop):
            await bot.receive(event, None)
        edits += event.callback_query.edit_message_text.await_count
        notices += event.callback_query.answer.await_count
    after = bot.store.db.execute("SELECT count(*) FROM callbacks").fetchone()[0]
    assert edits == 1 and notices == 2
    assert after - before == 7
    assert not bot.active_updates


@pytest.mark.asyncio
async def test_unregistered_stranger_cannot_reserve_paid_avatar(ai):
    event = update(uid=777, chat=777)
    payload, token = confirmation(ai, event)
    with pytest.raises(Rejected, match="当前成员"):
        await ai.action(event, payload, token)
    assert not ai.store.db.execute("SELECT 1 FROM roles WHERE uid='777'").fetchone()
    assert not ai.store.db.execute(
        "SELECT 1 FROM mod_groups WHERE chat='777'"
    ).fetchone()
    assert not ai.store.db.execute("SELECT 1 FROM avatar_ai").fetchone()
    assert not ai.store.db.execute("SELECT 1 FROM avatar_paid_budget").fetchone()
    assert ai.remaining(777) == 2
    ai.runtime.bot.get_chat_member.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_text_and_callbacks_share_window(private_bot, monkeypatch):
    ticks = [1000.0]
    monkeypatch.setattr(
        "data.plugins.astrbot_plugin_superbot.main.time.monotonic", lambda: ticks[0]
    )
    bot = private_bot
    callback = bot.store.callback("777", "777", {"action": "account"})
    for n in range(30):
        event = incoming("我的") if n % 2 else incoming(callback=callback)
        with pytest.raises(ApplicationHandlerStop):
            await bot.receive(event, None)
        ticks[0] += 1
    before = bot.store.db.execute("SELECT count(*) FROM callbacks").fetchone()[0]
    event = incoming(callback=callback)
    with pytest.raises(ApplicationHandlerStop):
        await bot.receive(event, None)
    event.callback_query.edit_message_text.assert_not_awaited()
    assert (
        bot.store.db.execute("SELECT count(*) FROM callbacks").fetchone()[0] == before
    )
    ticks[0] = 1060.0
    event = incoming(callback=callback)
    with pytest.raises(ApplicationHandlerStop):
        await bot.receive(event, None)
    event.callback_query.edit_message_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_private_global_window_is_bounded(private_bot, monkeypatch):
    ticks = [1000.0]
    monkeypatch.setattr(
        "data.plugins.astrbot_plugin_superbot.main.time.monotonic", lambda: ticks[0]
    )
    bot = private_bot
    bot.ui.action = AsyncMock()
    for uid in range(100, 220):
        with pytest.raises(ApplicationHandlerStop):
            await bot.receive(incoming("我的", uid=uid), None)
    assert bot.ui.action.await_count == 120
    for uid in range(220, 240):
        with pytest.raises(ApplicationHandlerStop):
            await bot.receive(incoming("我的", uid=uid), None)
    assert bot.ui.action.await_count == 120
    assert len(bot.private_requests) == 120
    ticks[0] += 60
    with pytest.raises(ApplicationHandlerStop):
        await bot.receive(incoming("我的", uid=241), None)
    assert bot.ui.action.await_count == 121
