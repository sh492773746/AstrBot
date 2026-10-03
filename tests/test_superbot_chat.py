"""Explicit opt-in and role-dependent chat isolation."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from data.plugins.astrbot_plugin_superbot.main import Main
from data.plugins.astrbot_plugin_superbot.store import Store


@pytest.mark.asyncio
async def test_chat_gate_role_change_and_fresh_conversation(tmp_path):
    store = Store(tmp_path / "chat.db", "1")
    manager = SimpleNamespace(
        new_conversation=AsyncMock(return_value="fresh"),
        get_conversation=AsyncMock(return_value="new-history"),
    )
    plugin = Main(
        SimpleNamespace(conversation_manager=manager), {"platform_id": "dedicated"}
    )
    plugin.store = store
    event = MagicMock()
    event.get_platform_id.return_value = "dedicated"
    event.get_sender_id.return_value = "2"
    event.get_group_id.return_value = ""
    event.is_private_chat.return_value = True
    event.unified_msg_origin = "dedicated:FriendMessage:2"
    request = SimpleNamespace(
        contexts=[{"role": "assistant", "content": "old-admin-context"}],
        system_prompt="",
        func_tool=object(),
    )
    await plugin.chat_guard(event, request)
    event.stop_event.assert_called_once()
    plugin.chat_sessions[("2", "2")] = (time.monotonic() + 100, (), True)
    await plugin.chat_guard(event, request)
    assert request.contexts == [] and request.conversation == "new-history"
    assert "普通用户口径" in request.system_prompt and request.func_tool is None
    assert "1—3个短句" in request.system_prompt
    assert "广告投递：未开启" in request.system_prompt
    with store.tx() as db:
        store.put(db, "modules", {"points": True})
        store.put(
            db,
            "points",
            {
                "enabled": True,
                "checkin": 25,
                "chat": 2,
                "interval": 15,
                "cap": 60,
                "groups": ["-secret"],
            },
        )
    await plugin.chat_guard(event, request)
    assert "签到25积分" in request.system_prompt
    assert "间隔15秒" in request.system_prompt
    assert "-secret" not in request.system_prompt
    assert "互斥的30分钟房间" in request.system_prompt
    assert "公开可见" in request.system_prompt
    assert "当前成员" in request.system_prompt
    assert "积分快三：未开启" in request.system_prompt
    with store.tx() as db:
        store.put(db, "modules", {"game": True, "k3": True, "slots": True})
    await plugin.chat_guard(event, request)
    assert "积分快三：总开关已开启" in request.system_prompt
    assert "老虎机PvP：总开关已开启" in request.system_prompt
    assert "双人挑战：总开关已开启" in request.system_prompt
    assert "扫雷接龙：未开启" in request.system_prompt
    assert "不代表任何群" in request.system_prompt
    with store.tx() as db:
        store.put(db, "modules", {"game": False, "k3": True, "slots": True})
    await plugin.chat_guard(event, request)
    assert "积分快三：未开启" in request.system_prompt
    assert "老虎机PvP：未开启" in request.system_prompt
    manager.new_conversation.assert_awaited_once()
    token = store.grant("1", "2", ["points"], 1)
    store.accept_grant("2", token)
    event.stop_event.reset_mock()
    await plugin.chat_guard(event, request)
    event.stop_event.assert_called_once()
    plugin.chat_sessions[("2", "2")] = (time.monotonic() + 100, ("points",), True)
    request.system_prompt = ""
    await plugin.chat_guard(event, request)
    assert "已验证业务管理员" in request.system_prompt
    event.get_group_id.return_value = "-1001"
    event.is_private_chat.return_value = False
    plugin.chat_sessions[("-1001", "2")] = (time.monotonic() + 100, ("points",), True)
    request.system_prompt = ""
    await plugin.chat_guard(event, request)
    assert "普通用户口径" in request.system_prompt
    store.close()
