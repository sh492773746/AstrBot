"""Managed versus discovered groups and retired-chat handling."""

import pytest
from telegram.error import ChatMigrated
from test_superbot_community import community, private  # noqa: F401
from test_superbot_moderation import setup  # noqa: F401


@pytest.mark.asyncio
async def test_pending_is_separate_and_migration_is_not_generic_failure(community):  # noqa: F811
    s = community
    s.store.db.execute(
        "INSERT INTO mod_groups(chat,title,enabled) VALUES('-1002','Discovered',0)"
    )
    await s.runtime.ui.action(private(), {"action": "mod_home"})
    args = s.bot.send_message.await_args.kwargs
    assert "Discovered" not in str(args["reply_markup"])
    assert "Fixture" in str(args["reply_markup"])
    s.bot.get_chat_member.side_effect = ChatMigrated(-1003)
    await s.runtime.ui.action(private(), {"action": "mod_home", "pending": True})
    assert "部分群查询失败" not in s.bot.send_message.await_args.kwargs["text"]
    keyboard = str(s.runtime.ui.keyboard("1"))
    assert "👥 群管理" not in keyboard and "⚙️ 管理" in keyboard
