"""Delegated superadmins share authority without upgrading business managers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from data.plugins.astrbot_plugin_superbot.store import Rejected, Store
from data.plugins.astrbot_plugin_superbot.ui import UI, parse_form
from data.plugins.astrbot_plugin_superbot.wizard import STEPS


@pytest.fixture
def authority(tmp_path):
    clock = [100000.0]
    store = Store(tmp_path / "superadmin.db", "1", lambda: clock[0])
    yield store, clock
    store.close()


def test_superadmin_delegation_revocation_and_expiry(authority):
    store, clock = authority
    with store.tx() as db:
        db.execute(
            "INSERT INTO roles(uid,scopes,expires) VALUES(?,?,?)",
            ("2", '["superadmin"]', clock[0] + 200),
        )
        db.execute(
            "INSERT INTO roles(uid,scopes,expires) VALUES(?,?,?)",
            ("3", '["manager"]', clock[0] + 200),
        )
    assert store.is_superadmin("1")
    assert store.is_superadmin("2")
    assert store.allowed("2", "ads")
    assert not store.is_superadmin("3")
    with pytest.raises(Rejected):
        store.grant("3", "4", ["superadmin"], 1)
    token = store.grant("2", "4", ["superadmin"], 1)
    store.accept_grant("4", token)
    assert store.is_superadmin("4")
    token = store.grant("4", "5", ["superadmin"], 1)
    store.revoke("2", "4")
    assert not store.is_superadmin("4")
    with pytest.raises(Rejected):
        store.accept_grant("5", token)
    with pytest.raises(Rejected):
        store.revoke("2", "1")
    clock[0] += 201
    assert not store.is_superadmin("2")


def test_revoked_issuer_cannot_leave_unclaimed_superadmin_link(authority):
    store, _ = authority
    initial_link = store.grant("1", "2", ["superadmin"], 30)
    store.accept_grant("2", initial_link)
    assert store.is_superadmin("2")
    token = store.grant("2", "4", ["superadmin"], 1)
    store.revoke("1", "2")
    with pytest.raises(Rejected):
        store.accept_grant("4", token)


@pytest.mark.asyncio
async def test_delegated_superadmin_sees_global_access_and_semantic(authority):
    store, clock = authority
    with store.tx() as db:
        db.execute(
            "INSERT INTO roles(uid,scopes,expires) VALUES(?,?,?)",
            ("2", '["superadmin"]', clock[0] + 200),
        )
        db.execute(
            "INSERT INTO roles(uid,scopes,expires) VALUES(?,?,?)",
            ("3", '["manager"]', clock[0] + 200),
        )
    ui = UI(SimpleNamespace(store=store))
    ui.render = AsyncMock()
    event = SimpleNamespace(effective_user=SimpleNamespace(id=2))
    await ui.action(event, {"action": "bot_control"})
    buttons = [label for label, _ in ui.render.await_args.args[2]]
    assert "🔐 授权管理" in buttons
    assert "语义复核" in buttons
    event.effective_user.id = 3
    await ui.action(event, {"action": "bot_control"})
    buttons = [label for label, _ in ui.render.await_args.args[2]]
    assert "🔐 授权管理" not in buttons
    assert "语义复核" not in buttons


def test_grant_wizard_exposes_distinct_superadmin_role():
    assert STEPS["grant"][1][1] == ["超管", "全功能"]
    assert parse_form("grant", "@target 超管 365", {})["scopes"] == ["superadmin"]


def test_bottom_keyboard_follows_current_role_and_scoped_manager(authority):
    store, clock = authority
    ui = UI(SimpleNamespace(store=store))

    def labels(uid):
        return [key.text for row in ui.keyboard(uid).keyboard for key in row]

    assert labels("4") == ["我的群", "客服", "帮助"]
    with store.tx() as db:
        db.execute(
            "INSERT INTO roles(uid,scopes,expires) VALUES(?,?,?)",
            ("2", '["superadmin"]', clock[0] + 100),
        )
        db.execute(
            "INSERT INTO roles(uid,scopes,expires) VALUES(?,?,?)",
            ("3", '["game"]', clock[0] + 100),
        )
    assert "本BOT总控" in labels("1")
    assert "本BOT总控" in labels("2")
    assert "本BOT总控" in labels("3")
    clock[0] += 101
    assert labels("2") == labels("4")
    assert labels("3") == labels("4")
