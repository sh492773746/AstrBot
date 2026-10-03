"""Offline group keyboard publication, local identity and navigation tests."""

# ruff: noqa: F811
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import Forbidden, RetryAfter, TimedOut
from test_superbot_community import community  # noqa: F401
from test_superbot_moderation import setup  # noqa: F401
from test_superbot_text_game import text_service, update  # noqa: F401

from data.plugins.astrbot_plugin_superbot.group_keyboard import QUERIES, GroupKeyboard
from data.plugins.astrbot_plugin_superbot.store import Rejected


@pytest.fixture
def keyboard(text_service):
    s = text_service
    s.bot.get_chat.return_value = SimpleNamespace(type="supergroup")
    s.runtime.avatar = SimpleNamespace(enabled=lambda: True, message=AsyncMock())
    s.runtime.group_keyboard = GroupKeyboard(s.runtime)
    return s


def test_keyboard_is_group_scoped_and_contains_no_admin_or_bet_buttons(keyboard):
    s = keyboard
    markup, labels, text = s.runtime.group_keyboard.render("-1001")
    assert not markup.is_persistent and markup.resize_keyboard and not markup.selective
    assert not markup.one_time_keyboard
    assert markup.input_field_placeholder is None
    assert "input_field_placeholder" not in markup.to_dict()
    assert {"🎮 玩法大全", *QUERIES, "客服", "帮助"} <= set(labels)
    assert "📖 玩法规则" not in labels
    assert all(
        b["style"] == "primary" for row in markup.to_dict()["keyboard"] for b in row
    )
    assert "⚙️ 管理" not in labels and "广告投递" not in labels
    assert "🎡 积分转盘" not in labels
    assert s.store.balance("2") == 1000
    with s.store.tx() as db:
        s.store.put(db, "modules", {"points": True})
    _, labels, _ = s.runtime.group_keyboard.render("-1001")
    assert "🎮 玩法大全" not in labels and "📜 历史" not in labels
    assert "💰 积分" in labels
    remove, labels, _ = s.runtime.group_keyboard.render("-999")
    assert remove.remove_keyboard and labels == []
    assert "1000" not in text


@pytest.mark.asyncio
async def test_send_once_across_restart_and_two_groups(keyboard):
    s = keyboard
    s.store.db.execute(
        "INSERT INTO mod_groups(chat,title,enabled) VALUES('-1002','Second',1)"
    )
    s.store.db.execute(
        "INSERT INTO mod_groups(chat,title,enabled) VALUES('-1003','Off',0)"
    )
    await s.runtime.group_keyboard.sync()
    assert s.bot.send_message.await_count == 2
    assert {c.kwargs["chat_id"] for c in s.bot.send_message.await_args_list} == {
        "-1001",
        "-1002",
    }
    for call in s.bot.send_message.await_args_list:
        assert not call.kwargs["reply_markup"].is_persistent
        assert not call.kwargs["reply_markup"].one_time_keyboard
        assert call.kwargs["disable_notification"]
        assert "reply_to_message_id" not in call.kwargs
    s.clock[0] += 31
    s.runtime.group_keyboard = GroupKeyboard(s.runtime)
    await s.runtime.group_keyboard.sync()
    assert s.bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_module_change_and_disabled_group_removal(keyboard):
    s = keyboard
    await s.runtime.group_keyboard.sync()
    s.clock[0] += 31
    with s.store.tx() as db:
        s.store.put(db, "modules", {"points": True})
    await s.runtime.group_keyboard.sync()
    assert s.bot.send_message.await_count == 2
    assert "玩法大全" not in str(s.bot.send_message.await_args.kwargs["reply_markup"])
    s.store.db.execute("UPDATE mod_groups SET enabled=0 WHERE chat='-1001'")
    s.clock[0] += 31
    await s.runtime.group_keyboard.sync()
    assert s.bot.send_message.await_args.kwargs["reply_markup"].remove_keyboard
    s.clock[0] += 31
    await s.runtime.group_keyboard.sync()
    assert s.bot.send_message.await_count == 3


@pytest.mark.asyncio
async def test_unknown_send_not_replayed_and_menu_commands_removed(keyboard):
    s = keyboard
    s.bot.send_message.side_effect = TimedOut()
    await s.runtime.group_keyboard.sync()
    assert (
        s.store.db.execute("SELECT status FROM group_keyboards").fetchone()[0]
        == "review"
    )
    s.runtime.group_keyboard = GroupKeyboard(s.runtime)
    await s.runtime.group_keyboard.sync()
    assert s.bot.send_message.await_count == 1
    s.bot.send_message.side_effect = None
    assert not await s.runtime.group_keyboard.message(update(s, "菜单", source=123))
    assert not await s.runtime.group_keyboard.message(update(s, "显示键盘", source=124))
    assert s.bot.send_message.await_count == 1
    s.clock[0] += 31
    await s.runtime.group_keyboard.message(update(s, "菜单", source=123))
    assert s.bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_retry_after_and_permission_failure(keyboard):
    s = keyboard
    s.bot.send_message.side_effect = RetryAfter(12)
    await s.runtime.group_keyboard.sync()
    s.bot.send_message.side_effect = None
    s.clock[0] += 11
    await s.runtime.group_keyboard.sync()
    assert s.bot.send_message.await_count == 1
    s.clock[0] += 2
    await s.runtime.group_keyboard.sync()
    assert s.bot.send_message.await_count == 2
    s.clock[0] += 31
    s.bot.send_message.side_effect = Forbidden("test")
    s.store.db.execute("UPDATE mod_groups SET title='Updated group' WHERE chat='-1001'")
    await s.runtime.group_keyboard.sync("-1001")
    assert (
        s.store.db.execute("SELECT status FROM group_keyboards").fetchone()[0]
        == "blocked"
    )
    await s.runtime.group_keyboard.sync()
    assert s.bot.send_message.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("label,action", list(QUERIES.items()))
async def test_queries_reuse_real_sender_and_source(keyboard, label, action):
    s = keyboard
    query = s.text.query_points = AsyncMock()
    event = update(s, label, uid=3, source=177)
    assert await s.runtime.group_keyboard.message(event)
    query.assert_awaited_once_with(event, action)
    assert event.effective_user.id == 3
    assert event.message.message_id == 177
    assert event.effective_chat.id == -1001


@pytest.mark.asyncio
async def test_points_query_keeps_original_receipt_id_and_dedup(keyboard):
    s = keyboard
    event = update(s, "💰 积分", source=222)
    await s.runtime.group_keyboard.message(event)
    await s.runtime.group_keyboard.message(event)
    rows = s.store.db.execute("SELECT chat,source,uid FROM gt_requests").fetchall()
    assert [tuple(row) for row in rows] == [("-1001", 222, "2")]


@pytest.mark.asyncio
async def test_keyboard_labels_ignored_when_forwarded_or_anonymous(keyboard):
    s = keyboard
    event = update(s, "💰 积分")
    event.message.forward_origin = object()
    assert not await s.runtime.group_keyboard.message(event)
    event.message.forward_origin = None
    event.message.sender_chat = object()
    assert not await s.runtime.group_keyboard.message(event)
    event.message.sender_chat = None
    event.message.date = update(s, "💰 积分", age=70).message.date
    assert await s.runtime.group_keyboard.message(event)
    assert s.store.db.execute("SELECT count(*) FROM gt_requests").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_stale_keyboard_cannot_bypass_disabled_module(keyboard):
    s = keyboard
    with s.store.tx() as db:
        s.store.put(db, "modules", {"game": True})
    with pytest.raises(Rejected, match="停用"):
        await s.runtime.group_keyboard.message(update(s, "💰 积分"))
    s.store.db.execute("UPDATE mod_groups SET enabled=0")
    with pytest.raises(Rejected):
        await s.runtime.group_keyboard.message(update(s, "📜 历史"))


@pytest.mark.asyncio
async def test_simultaneous_sync_does_not_duplicate_send(keyboard):
    s = keyboard
    await asyncio.gather(
        s.runtime.group_keyboard.sync(), s.runtime.group_keyboard.sync()
    )
    assert s.bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_game_catalog_alias_has_only_games(keyboard):
    s = keyboard
    assert await s.text.message(update(s, "🎮 玩法大全"), "🎮 玩法大全")
    await s.text.center.tick()
    args = s.bot.send_message.await_args.kwargs
    actions = {
        b.callback_data.split(":")[-1]
        for row in args["reply_markup"].inline_keyboard
        for b in row
    }
    assert actions == {"activate", "duel"}
    assert "玩法大全" in args["text"]


def test_wheel_removed_from_command_menu_not_legacy_text_handler():
    from data.plugins.astrbot_plugin_superbot.main import GROUP_SHORTCUTS

    assert "wheel" not in GROUP_SHORTCUTS
