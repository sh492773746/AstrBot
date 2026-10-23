"""Advertising display and scoped single-field edits never modify orders."""

# ruff: noqa: F811

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_ad_maintenance import env  # noqa: F401

from data.plugins.astrbot_plugin_superbot.ad_settings_ui import action
from data.plugins.astrbot_plugin_superbot.store import Rejected
from data.plugins.astrbot_plugin_superbot.ui import UI


def context(env, uid=1, chat_type="private"):
    store, ads, _, bot = env
    store.db.execute(
        "CREATE TABLE IF NOT EXISTS ad_channels(chat TEXT PRIMARY KEY,title TEXT,enabled INTEGER)"
    )
    runtime = SimpleNamespace(store=store, ads=ads, bot=bot)
    ui = UI(runtime)
    event = SimpleNamespace(
        effective_user=SimpleNamespace(id=uid),
        effective_chat=SimpleNamespace(id=uid, type=chat_type),
        callback_query=None,
    )
    ui.render = AsyncMock()
    return ui, event


@pytest.mark.asyncio
async def test_detail_plain_data_and_field_buttons(env):
    ui, event = context(env)
    await action(ui, event, {"action": "ap_detail", "key": "p"})
    args = ui.render.await_args
    assert "10 USDT" in args.args[1]
    assert "收款说明" in args.args[1]
    assert '"enabled":' not in args.args[1]
    assert args.kwargs["fold_sections"] is True
    assert len([p for _, p in args.args[2] if p["action"] == "ap_edit"]) == 9


@pytest.mark.asyncio
async def test_single_field_preview_save_and_stale_conflict(env):
    ui, event = context(env)
    before = ui.store.get("packages")["p"]
    payload = {
        "action": "ap_preview",
        "key": "p",
        "field": "price",
        "value": "25",
        "expected": before,
    }
    await action(ui, event, payload)
    assert ui.store.get("packages")["p"] == before
    await action(ui, event, {**payload, "action": "ap_save"})
    assert ui.store.get("packages")["p"] == {**before, "price": "25"}
    with pytest.raises(Rejected, match="配置已变化"):
        await action(ui, event, {**payload, "action": "ap_save", "value": "30"})
    assert ui.store.db.execute("SELECT count(*) FROM ads").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_private_permission_and_invalid_input(env):
    ui, event = context(env, uid=2)
    with pytest.raises(Rejected):
        await action(ui, event, {"action": "ap_home"})
    ui, event = context(env, chat_type="supergroup")
    with pytest.raises(Rejected):
        await action(ui, event, {"action": "ap_home"})
    ui, event = context(env)
    before = ui.store.get("packages")["p"]
    with pytest.raises(Rejected):
        await action(
            ui,
            event,
            {
                "action": "ap_save",
                "key": "p",
                "field": "price",
                "value": "-5",
                "expected": before,
            },
        )
    assert ui.store.get("packages")["p"] == before


@pytest.mark.asyncio
async def test_edit_field_dialog_and_target_options(env):
    ui, event = context(env)
    before = ui.store.get("packages")["p"]
    payload = {"action": "ap_edit", "key": "p", "field": "name", "expected": before}
    await action(ui, event, payload)
    event.message = SimpleNamespace(text="新名称")
    await ui.input(event, {"form": "ap_field", "payload": payload})
    assert "新名称" in ui.render.await_args.args[1]
    assert ui.store.get("packages")["p"] == before
    await action(ui, event, {**payload, "field": "target"})
    assert any(p.get("value") == "-1001" for _, p in ui.render.await_args.args[2])
    with pytest.raises(Rejected):
        await action(
            ui,
            event,
            {**payload, "field": "target", "action": "ap_save", "value": "-999"},
        )


@pytest.mark.asyncio
async def test_list_and_home_use_short_selection_labels(env):
    ui, event = context(env)
    await action(ui, event, {"action": "ap_home"})
    assert "广告位 1 个" in ui.render.await_args.args[1]
    await action(ui, event, {"action": "ap_list"})
    assert "10 USDT" in ui.render.await_args.args[1]
    assert ui.render.await_args.args[2][0][0] == "选择 1"
