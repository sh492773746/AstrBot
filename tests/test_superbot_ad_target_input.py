"""Exercise the ad search form through the public UI input dispatcher."""

# ruff: noqa: F811

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_superbot_tenants import merchant  # noqa: F401

from data.plugins.astrbot_plugin_superbot.store import Rejected
from data.plugins.astrbot_plugin_superbot.ui import UI


@pytest.fixture
def search_ui(merchant):
    runtime, _, _ = merchant
    runtime.bot.get_chat.return_value = SimpleNamespace(
        id=-1001, type="supergroup", title="test"
    )
    ui = UI(runtime)
    ui.render = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=30),
        effective_chat=SimpleNamespace(id=30, type="private"),
        message=SimpleNamespace(text=""),
    )
    return ui, update


@pytest.mark.parametrize(
    "target", ["@wdhihji1", "https://t.me/wdhihji1", "-1001", "test"]
)
@pytest.mark.asyncio
async def test_search_button_then_target_input_lists_group(search_ui, target):
    ui, update = search_ui
    await ui.action(update, {"action": "tenant_ad_search"})
    dialog = ui.store.dialog("30")
    assert dialog["kind"] == "ad_target"
    update.message.text = target
    await ui.input(update, dialog)
    assert "请选择目标群" in ui.render.await_args.args[1]
    buttons = ui.render.await_args.args[2]
    chosen = next(p for _, p in buttons if p.get("chat") == "-1001")
    assert chosen["action"] == "tenant_ad_group"
    await ui.action(update, chosen)
    assert "确认投放群" in ui.render.await_args.args[1]
    assert not ui.store.dialog("30")
    assert ui.store.db.execute("SELECT COUNT(*) FROM ads").fetchone()[0] == 0
    assert (
        ui.store.db.execute("SELECT COUNT(*) FROM merchant_invoices").fetchone()[0] == 0
    )


@pytest.mark.parametrize("failure", ["lookup", "unregistered", "paused", "no_address"])
@pytest.mark.asyncio
async def test_search_failure_keeps_form_for_retry(search_ui, failure):
    ui, update = search_ui
    await ui.action(update, {"action": "tenant_ad_search"})
    if failure == "lookup":
        ui.runtime.bot.get_chat.side_effect = RuntimeError("lookup unavailable")
    elif failure == "unregistered":
        ui.runtime.bot.get_chat.return_value.id = -999
    elif failure == "paused":
        ui.store.db.execute("UPDATE mod_groups SET enabled=0 WHERE chat='-1001'")
    else:
        ui.runtime.merchant_ads.address = lambda chat: None
    update.message.text = "@wdhihji1"
    with pytest.raises(Rejected):
        await ui.input(update, ui.store.dialog("30"))
    assert ui.store.dialog("30")["kind"] == "ad_target"


@pytest.mark.asyncio
async def test_search_no_matches_returns_search_again_button(search_ui):
    ui, update = search_ui
    await ui.action(update, {"action": "tenant_ad_search"})
    update.message.text = "no such registered group"
    await ui.input(update, ui.store.dialog("30"))
    assert "未找到可投递" in ui.render.await_args.args[1]
    assert any(
        p["action"] == "tenant_ad_search" for _, p in ui.render.await_args.args[2]
    )


@pytest.mark.asyncio
async def test_no_address_target_requires_superadmin_and_live_test_switch(search_ui):
    ui, update = search_ui
    ui.runtime.merchant_ads.address = lambda chat: None
    ui.store.db.execute("DELETE FROM merchant_addresses WHERE chat='-1001'")
    ui.store.db.execute(
        "INSERT INTO roles(uid,scopes,expires) VALUES(?,?,?)",
        ("10", '["superadmin"]', ui.store.clock() + 7200),
    )
    with pytest.raises(Rejected):
        ui.runtime.merchant_ads.test_switch("1", "-1001", True)
    ui.runtime.merchant_ads.test_switch("10", "-1001", True)
    update.effective_user.id = 10
    await ui.action(update, {"action": "tenant_ad_search"})
    update.message.text = "@wdhihji1"
    await ui.input(update, ui.store.dialog("10"))
    group = next(
        payload for _, payload in ui.render.await_args.args[2]
        if payload.get("action") == "tenant_ad_group"
    )
    await ui.action(update, group)
    assert "免付款" in ui.render.await_args.args[1]
    await ui.action(update, {**group, "action": "tenant_ad_packages"})
    package = next(
        payload for _, payload in ui.render.await_args.args[2]
        if payload.get("action") == "tenant_buy"
    )
    await ui.action(update, package)
    assert "超管测试免付款" in ui.render.await_args.args[1]
    update.message.text = "test creative"
    await ui.input(update, ui.store.dialog("10"))
    update.message.text = "test contact"
    await ui.input(update, ui.store.dialog("10"))
    assert "不生成真实付款单" in ui.render.await_args.args[1]

    update.effective_user.id = 30
    with pytest.raises(Rejected, match="收款地址"):
        await ui.action(update, package)
    ui.runtime.merchant_ads.test_switch("10", "-1001", False)
    update.effective_user.id = 10
    with pytest.raises(Rejected, match="收款地址"):
        await ui.action(update, package)
