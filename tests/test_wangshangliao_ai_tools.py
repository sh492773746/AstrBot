"""Legacy management calls must be inert even with authentic admin privileges."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.main import Main


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    [
        "groups",
        "select_group",
        "mute",
        "unmute",
        "kick",
        "announce",
        "mute_all",
        "unmute_all",
        "config_preview",
        "config_confirm",
        "schedule_confirm",
        "invitation_records",
        "invitation_stats",
        "delete_friend",
        "grant_admin",
    ],
)
@pytest.mark.parametrize("value", ["1", "{}", "确认设置 stolen", "x" * 4097])
async def test_cached_legacy_ai_tool_never_dispatches(action, value):
    plugin = object.__new__(Main)
    plugin.commands = SimpleNamespace(execute=AsyncMock())
    plugin.ai_config = SimpleNamespace(preview=AsyncMock(), confirm=AsyncMock())
    result = json.loads(await plugin.private_management(object(), action, value))
    assert result == {"status": "rejected", "reason": "ai_management_retired"}
    plugin.commands.execute.assert_not_awaited()
    plugin.ai_config.preview.assert_not_awaited()
    plugin.ai_config.confirm.assert_not_awaited()
