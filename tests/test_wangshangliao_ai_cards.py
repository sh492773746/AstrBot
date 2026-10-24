"""AI card operations are retired; native dashboard card coverage is separate."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.main import Main


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    [
        "card_preview",
        "card_member_preview",
        "card_execute",
        "card_status",
        "card_stop",
    ],
)
@pytest.mark.parametrize("entry", ["private_management", "card_tool"])
async def test_retired_card_entry_never_reads_or_mutates_jobs(action, entry):
    plugin = object.__new__(Main)
    plugin.cards = SimpleNamespace(
        preview=AsyncMock(), start=AsyncMock(), status=AsyncMock()
    )
    result = json.loads(
        await getattr(plugin, entry)(object(), action, '{"member_number":1}')
    )
    assert result == {"status": "rejected", "reason": "ai_management_retired"}
    plugin.cards.preview.assert_not_awaited()
    plugin.cards.start.assert_not_awaited()
    plugin.cards.status.assert_not_awaited()
