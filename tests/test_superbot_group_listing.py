"""Managed versus discovered groups and retired-chat handling."""

from types import SimpleNamespace

import pytest
from telegram.error import ChatMigrated
from test_superbot_community import community, private  # noqa: F401
from test_superbot_moderation import setup  # noqa: F401

from data.plugins.astrbot_plugin_superbot.tenants import migrate


@pytest.mark.asyncio
async def test_old_group_select_uses_my_groups_without_global_acl(community):  # noqa: F811
    s = community
    migrate(s.store)
    s.runtime.tenants = SimpleNamespace()
    s.runtime.merchant_ads = SimpleNamespace()
    s.store.db.execute(
        "INSERT INTO mod_groups(chat,title,enabled) VALUES('-1002','Discovered',0)"
    )
    await s.runtime.ui.action(private(), {"action": "mod_home"})
    args = s.bot.send_message.await_args.kwargs
    assert "Discovered" in str(args["reply_markup"])
    assert "Fixture" in str(args["reply_markup"])
    s.bot.get_chat_member.side_effect = ChatMigrated(-1003)
    await s.runtime.ui.action(private(), {"action": "mod_home", "pending": True})
    assert "我的群" in s.bot.send_message.await_args.kwargs["text"]
    keyboard = str(s.runtime.ui.keyboard("1"))
    assert "👥 群管理" not in keyboard and "我的群" in keyboard
