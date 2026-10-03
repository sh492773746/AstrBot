"""Administrative navigation, authorization and per-group duel gates."""

# ruff: noqa: F811

from unittest.mock import AsyncMock

import pytest
from test_superbot_community import community, private  # noqa: F401
from test_superbot_moderation import setup  # noqa: F401
from test_superbot_play_center import click, invite
from test_superbot_text_game import text_service, update  # noqa: F401

from data.plugins.astrbot_plugin_superbot.store import Rejected


@pytest.mark.asyncio
async def test_global_switch_hub_has_no_group_or_business_navigation(text_service):
    s = text_service
    ui = s.runtime.ui
    ui.render = AsyncMock()
    await ui.admin(private(1), {"action": "modules"}, "")
    text = ui.render.await_args.args[1]
    choices = ui.render.await_args.args[2]
    assert "影响所有群" in text
    assert "🤝 双人对赌 · 🟢 已开启" in text
    assert not any(p["action"] in {"games_duel", "avatar_admin"} for _, p in choices)
    toggles = [(label, p) for label, p in choices if p["action"] == "module_preview"]
    assert all(label.startswith("设置 ") for label, _ in toggles)
    duel = next(p for _, p in toggles if p["key"] == "duel")
    await ui.admin(private(1), duel, "")
    save = ui.render.await_args.args[2][0][1]
    await ui.admin(private(1), save, "")
    assert "🔌 全部功能启停" in ui.render.await_args.args[1]
    assert "🤝 双人对赌 · ⚪ 已关闭" in ui.render.await_args.args[1]


@pytest.mark.asyncio
async def test_admin_navigation_and_actual_save_button(text_service):
    s = text_service
    ui = s.runtime.ui
    ui.render = AsyncMock()
    s.runtime.moderation.check = AsyncMock()
    await ui.admin(private(1), {"action": "admin"}, "")
    labels = [label for label, _ in ui.render.await_args.args[2]]
    assert "玩法管理" in labels and "全部功能启停" in labels
    await ui.admin(private(1), {"action": "admin_game"}, "")
    assert {p["action"] for _, p in ui.render.await_args.args[2]} >= {
        "games_duel",
        "games_canada",
    }
    await ui.admin(private(1), {"action": "games_duel"}, "")
    assert "Fixture" in ui.render.await_args.args[1]
    assert "采集状态" not in ui.render.await_args.args[1]
    await ui.admin(private(1), {"action": "games_canada"}, "")
    assert "采集状态" in ui.render.await_args.args[1]
    assert "对赌" not in ui.render.await_args.args[1]
    await ui.admin(
        private(1),
        {"action": "duel_group_preview", "chat": "-1001", "enabled": False},
        "",
    )
    save = ui.render.await_args.args[2][0][1]
    assert save["action"] == "duel_group_save"
    await ui.admin(private(1), save, "")
    assert (
        s.store.db.execute(
            "SELECT enabled FROM duel_groups WHERE chat='-1001'"
        ).fetchone()[0]
        == 0
    )
    with pytest.raises(Rejected):
        await ui.admin(private(1), save, "")
    s.runtime.moderation.check.assert_awaited()


@pytest.mark.asyncio
async def test_no_permission_cannot_change_duel_switch(text_service):
    s = text_service
    with pytest.raises(Rejected):
        await s.runtime.ui.admin(
            private(2),
            {"action": "duel_group_preview", "chat": "-1001", "enabled": False},
            "",
        )
    s.runtime.moderation.check = AsyncMock(side_effect=Rejected("没有此群授权"))
    with pytest.raises(Rejected):
        await s.runtime.ui.admin(
            private(1),
            {"action": "duel_group_preview", "chat": "-1001", "enabled": False},
            "",
        )


@pytest.mark.asyncio
async def test_disabled_group_blocks_old_callbacks_but_settles_locked(text_service):
    s = text_service
    panel = await invite(s)
    s.store.db.execute("UPDATE duel_groups SET enabled=0 WHERE chat='-1001'")
    with pytest.raises(Rejected, match="对赌已停用"):
        await s.text.center.action(click(s, panel, "accept", uid=3))
    s.store.db.execute("UPDATE duel_groups SET enabled=1 WHERE chat='-1001'")
    await s.text.center.action(click(s, panel, "accept", uid=3))
    await s.text.center.action(click(s, panel, "pick0"))
    await s.text.center.action(click(s, panel, "pick1", uid=3))
    s.store.db.execute("UPDATE duel_groups SET enabled=0 WHERE chat='-1001'")
    s.store.db.execute(
        "INSERT INTO draws(issue,at,raw,balls,evidence,received) VALUES(101,?,'[]','[1,1,1]','test',?)",
        (s.clock[0], s.clock[0]),
    )
    s.text.duel.tick()
    assert s.store.db.execute("SELECT status FROM duels").fetchone()[0] == "settled"
    await s.text.message(update(s, "jnd", source=55), "jnd")
    assert s.store.db.execute("SELECT 1 FROM gt_sessions WHERE uid='2'").fetchone()


@pytest.mark.asyncio
async def test_ad_management_entry_remains_reachable(text_service):
    s = text_service
    s.runtime.ui.render = AsyncMock()
    await s.runtime.ui.admin(private(1), {"action": "admin_ads"}, "")
    assert "广告管理" in s.runtime.ui.render.await_args.args[1]


@pytest.mark.asyncio
async def test_global_duel_switch_and_restart_preserve_group_setting(text_service):
    from data.plugins.astrbot_plugin_superbot.duel import Duel

    s = text_service
    ui = s.runtime.ui
    ui.render = AsyncMock()
    await ui.admin(
        private(1), {"action": "module_preview", "key": "duel", "enabled": False}, ""
    )
    save = ui.render.await_args.args[2][0][1]
    await ui.admin(private(1), save, "")
    assert s.store.get("modules")["duel"] is False
    await invite(s)
    assert not s.store.db.execute("SELECT 1 FROM game_panels").fetchone()
    s.store.db.execute("UPDATE duel_groups SET enabled=0 WHERE chat='-1001'")
    Duel(s.text)
    assert (
        s.store.db.execute(
            "SELECT enabled FROM duel_groups WHERE chat='-1001'"
        ).fetchone()[0]
        == 0
    )
