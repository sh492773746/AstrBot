"""Delegated business administrators cannot delegate or revoke access."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_superbot_avatar import avatar, update  # noqa: F401
from test_superbot_moderation import setup  # noqa: F401

from data.plugins.astrbot_plugin_superbot.store import Rejected
from data.plugins.astrbot_plugin_superbot.wizard import run


def test_manager_scope_expiry_revocation_and_no_redelegation(setup):  # noqa: F811
    s = setup
    token = s.store.grant("1", "2", ["manager"], 1)
    s.store.accept_grant("2", token)
    for scope in ("ads", "points", "game", "moderation", "manager"):
        assert s.store.allowed("2", scope)
    assert not s.store.allowed("2", "owner")
    for scopes in (["manager"], ["ads"], ["moderation"]):
        with pytest.raises(Rejected):
            s.store.grant("2", "3", scopes, 30)
    with pytest.raises(Rejected):
        s.store.revoke("2", "1")
    with pytest.raises(Rejected):
        s.store.revoke("2", "3")
    s.clock[0] += 86401
    assert not s.store.allowed("2", "manager")
    s.store.accept_grant("2", s.store.grant("1", "2", ["manager"], 1))
    s.store.revoke("1", "2")
    assert not s.store.allowed("2", "ads")
    assert s.store.allowed("1", "manager")


@pytest.mark.asyncio
async def test_manager_menu_and_forged_access_callbacks(setup):  # noqa: F811
    s = setup
    s.store.accept_grant("2", s.store.grant("1", "2", ["manager"], 30))
    s.runtime.avatar = SimpleNamespace(enabled=lambda: False)
    ui = s.runtime.ui
    ui.render = AsyncMock()
    event = SimpleNamespace(
        effective_user=SimpleNamespace(id=2),
        effective_chat=SimpleNamespace(id=2, type="private"),
    )
    await ui.admin(event, {"action": "admin"}, "")
    choices = ui.render.await_args.args[2]
    assert {p["action"] for _, p in choices} >= {
        "modules",
        "avatar_admin",
        "admin_ads",
        "admin_points",
        "admin_game",
        "mod_home",
    }
    assert not any(p.get("form") in {"grant", "revoke"} for _, p in choices)
    await ui.admin(
        event, {"action": "module_preview", "key": "game", "enabled": True}, ""
    )
    confirmation = ui.render.await_args.args[2][0][1]
    await ui.admin(event, confirmation, "")
    assert s.store.get("modules")["game"]
    for form in ("grant", "revoke"):
        with pytest.raises(Rejected):
            await ui.admin(event, {"action": "form", "form": form}, "")
        with pytest.raises(Rejected):
            await ui.admin(
                event,
                {
                    "action": "save_form",
                    "form": form,
                    "data": {"target": "3", "scopes": ["manager"], "days": 30},
                },
                "",
            )
    with pytest.raises(Rejected):
        await run(ui, event, "grant", [])
    for payload in (
        {"action": "mod_form", "kind": "grant"},
        {
            "action": "mod_confirm",
            "kind": "grant",
            "expires": s.clock[0] + 600,
            "data": {},
        },
    ):
        with pytest.raises(Rejected):
            await ui.moderation_ui.action(event, payload)


@pytest.mark.asyncio
async def test_manager_all_groups_still_requires_telegram_admin(setup):  # noqa: F811
    s = setup
    s.store.accept_grant("2", s.store.grant("1", "2", ["manager"], 30))
    assert not s.store.db.execute("SELECT 1 FROM mod_acl WHERE uid='2'").fetchone()
    with pytest.raises(Rejected, match="Telegram"):
        await s.mod.check("2", "-1001")
    s.members[2].status = "administrator"
    await s.mod.check("2", "-1001")
    s.store.db.execute(
        "INSERT INTO mod_groups(chat,title,enabled) VALUES('-1002','Second',1)"
    )
    await s.mod.check("2", "-1002")
    s.store.revoke("1", "2")
    with pytest.raises(Rejected):
        await s.mod.check("2", "-1002")


@pytest.mark.asyncio
async def test_manager_avatar_controls_keep_quota(avatar):  # noqa: F811
    avatar.store.accept_grant("2", avatar.store.grant("1", "2", ["manager"], 30))
    await avatar.action(update(), {"action": "avatar_toggle_preview"})
    assert avatar.remaining(2) == 2
    avatar.store.revoke("1", "2")
    with pytest.raises(Rejected):
        await avatar.action(update(), {"action": "avatar_toggle_preview"})
