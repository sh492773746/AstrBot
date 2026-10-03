"""Group-wide multiplier authority and old confirmation invalidation."""

import pytest
from test_superbot_community import community, private  # noqa: F401
from test_superbot_game_hardening import flow, preview  # noqa: F401
from test_superbot_group_flows import group_update
from test_superbot_moderation import setup  # noqa: F401

from data.plugins.astrbot_plugin_superbot.group_multiplier import action
from data.plugins.astrbot_plugin_superbot.store import Rejected


@pytest.mark.asyncio
async def test_admin_switch_invalidates_preview_not_accepted_bets(flow):  # noqa: F811
    s = flow
    s.runtime.group_game = s.flow
    with s.store.tx() as db:
        s.store.put(db, "modules", {"moderation": True, "game": True})
        rooms = s.runtime.game.rooms()
        rooms["room27"]["enabled"] = True
        s.store.put(db, "rooms", rooms)
    old, bet = await preview(s)
    payload = {
        "action": "mod_multiplier_save",
        "chat": "-1001",
        "room": "room27",
        "version": 1,
    }
    token = s.store.callback("1", "1", payload)[3:]
    with pytest.raises(Rejected):
        await action(s.runtime.ui, private(2), payload, token)
    await action(s.runtime.ui, private(), payload, token)
    assert s.flow.group_room("-1001") == "room27"
    with pytest.raises(Rejected):
        await s.flow.action(group_update(), bet, old)
    with pytest.raises(Rejected):
        await action(s.runtime.ui, private(), payload, token)
    assert s.store.balance("2") == 100
