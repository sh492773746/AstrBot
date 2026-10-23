"""Verify narrow controls and default shared service admission without paid I/O."""

# ruff: noqa: F811

from types import SimpleNamespace

import pytest
from test_superbot_group_ux import form  # noqa: F401
from test_superbot_tenant_settings_matrix import configured  # noqa: F401
from test_superbot_tenants import merchant  # noqa: F401

from data.plugins.astrbot_plugin_superbot.availability import snapshot
from data.plugins.astrbot_plugin_superbot.game_registry import (
    ENTERTAINMENT,
    LOCAL_SWITCHES,
)
from data.plugins.astrbot_plugin_superbot.game_schedule import reason, save
from data.plugins.astrbot_plugin_superbot.onboarding import invitation
from data.plugins.astrbot_plugin_superbot.store import Rejected


@pytest.mark.parametrize("public", [True, False])
def test_onboarding_describes_default_shared_services(form, public):
    ui, *_ = form
    with ui.store.tx() as db:
        ui.store.put(db, "tenant_policy", {"public": public})
    text, _ = invitation(ui.runtime)
    assert "默认授权AI客服与制图" in text
    assert "总控统一内容" in text and "服务限额" in text
    assert "另行开通" not in text


@pytest.mark.parametrize("uid", [10, 40])
@pytest.mark.asyncio
async def test_switches_and_schedule_exact_allowlists_and_back(form, uid):
    ui, event, *_ = form
    event.effective_user.id = uid
    await ui.action(event, {"action": "tenant_switches", "chat": "-1001"})
    buttons = ui.render.await_args.args[2]
    assert {p["feature"] for _, p in buttons if "feature" in p} == set(LOCAL_SWITCHES)
    assert len(LOCAL_SWITCHES) == 8
    assert not any(p["action"] == "modules" for _, p in buttons)
    await ui.action(event, next(p for label, p in buttons if "每日娱乐时段" in label))
    buttons = ui.render.await_args.args[2]
    assert {p["game"] for _, p in buttons if "game" in p} == set(ENTERTAINMENT)
    await ui.action(event, next(p for label, p in buttons if label == "返回上一级"))
    assert "功能状态" in ui.render.await_args.args[1]


@pytest.mark.parametrize("key", ["group", "ads", "points"])
@pytest.mark.parametrize("action", ["tenant_switch_preview", "tenant_switch_save"])
@pytest.mark.asyncio
async def test_obsolete_switch_buttons_cannot_mutate(form, key, action):
    ui, event, *_ = form
    before = [
        tuple(row)
        for row in ui.store.db.execute(
            "SELECT * FROM tenant_settings ORDER BY chat,key"
        )
    ]
    with pytest.raises(Rejected, match="移出"):
        await ui.action(
            event,
            {
                "action": action,
                "chat": "-1001",
                "feature": key,
                "enabled": False,
                "version": 1,
            },
        )
    # Verification may update its timestamp but cannot change feature settings.
    assert (
        ui.store.db.execute(
            "SELECT enabled FROM mod_groups WHERE chat='-1001'"
        ).fetchone()[0]
        == 1
    )
    assert [
        tuple(row)
        for row in ui.store.db.execute(
            "SELECT * FROM tenant_settings ORDER BY chat,key"
        )
    ] == before


@pytest.mark.parametrize("key", ["group", "ads", "points", "chat", "avatar"])
@pytest.mark.asyncio
async def test_retired_schedules_rejected_and_stored_values_ignored(form, key):
    ui, event, *_ = form
    with ui.store.tx() as db:
        ui.store.put(db, "game_schedule:-1001", {key: [1, 2]})
    assert not reason(ui.store, "-1001", key)
    assert not reason(ui.store, "-1001", "canada")
    with pytest.raises(Rejected):
        save(ui.store, "10", "-1001", key, 0, 1440, 1)
    event.message.text = "00:00"
    with pytest.raises(Rejected, match="旧时段"):
        await ui.input(event, {"form": "game_schedule", "game": key, "chat": "-1001"})


@pytest.mark.parametrize("resource", ["chat", "avatar"])
def test_default_resource_admission_audits_once_and_preserves_explicit_denial(
    form, resource
):
    ui, *_ = form
    tenants = ui.runtime.tenants
    assert tenants.resource_allowed("30", "-1001", resource)
    with ui.store.tx() as db:
        tenants.consume_resource("30", "-1001", resource, "first", db)
    with pytest.raises(Rejected), ui.store.tx() as db:
        tenants.consume_resource("30", "-1001", resource, "first", db)
    assert (
        ui.store.db.execute("SELECT COUNT(*) FROM tenant_resource_uses").fetchone()[0]
        == 1
    )
    ui.store.db.execute(
        "INSERT INTO roles(uid,scopes,expires) VALUES(?,?,?)",
        ("10", '["superadmin"]', ui.store.clock() + 7200),
    )
    tenants.resource_grant("10", "-1001", resource, 0, 0)
    assert not tenants.resource_allowed("30", "-1001", resource)
    assert tenants.resource_allowed("30", "-1002", resource)
    assert not tenants.resource_allowed("999", None, resource)


@pytest.mark.asyncio
async def test_default_ai_selection_requires_live_membership(form):
    ui, _, _, member, _ = form
    await ui.runtime.tenants.select_resource("30", "-1001", "chat")
    assert ui.runtime.tenants.resource_context("30", "chat") == "-1001"
    member.status = "left"
    with pytest.raises(Rejected):
        await ui.runtime.tenants.select_resource("30", "-1001", "chat")
    ui.runtime.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_cannot_customize_shared_provider_template_or_limits(form):
    ui, event, *_ = form
    for field in ("provider", "prompt", "template", "model", "resource_budget"):
        with pytest.raises(Rejected):
            await ui.action(
                event, {"action": "tenant_edit", "chat": "-1001", "field": field}
            )
    with pytest.raises(Rejected):
        await ui.action(
            event,
            {
                "action": "tenant_resource_save",
                "chat": "-1001",
                "resource": "chat",
                "budget": 1000,
                "version": 0,
            },
        )


def test_avatar_navigation_matches_default_admission(form):
    ui, *_ = form
    ui.runtime.avatar = SimpleNamespace(enabled=lambda: True)
    ui.runtime.community = SimpleNamespace(
        policy=lambda chat: {"config": {"enabled": {}}}
    )
    ui.runtime.navigation_permissions = {
        "-1001": {"ready": True, "expires": ui.store.clock() + 30}
    }
    assert snapshot(ui.runtime, "-1001")["avatar"]
    ui.store.db.execute(
        "INSERT INTO roles(uid,scopes,expires) VALUES(?,?,?)",
        ("10", '["superadmin"]', ui.store.clock() + 7200),
    )
    ui.runtime.tenants.resource_grant("10", "-1001", "avatar", 0, 0)
    assert not snapshot(ui.runtime, "-1001")["avatar"]
