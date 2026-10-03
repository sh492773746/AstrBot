"""Offline grant alias resolution, identity binding and owner-only acceptance."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import TimedOut
from test_superbot import env  # noqa: F401

from data.plugins.astrbot_plugin_superbot.grant_target import resolve
from data.plugins.astrbot_plugin_superbot.store import Rejected
from data.plugins.astrbot_plugin_superbot.ui import UI
from data.plugins.astrbot_plugin_superbot.wizard import run


@pytest.fixture
def runtime(env):  # noqa: F811
    store, _, _ = env
    store.db.execute("INSERT INTO user_labels VALUES('3','Example')")
    return SimpleNamespace(
        store=store,
        bot=SimpleNamespace(
            username="testbot",
            get_chat=AsyncMock(
                return_value=SimpleNamespace(id=3, type="private", username="Example")
            ),
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    [
        "@example",
        "@EXAMPLE",
        "t.me/example",
        "https://t.me/Example",
        "http://t.me/example/",
    ],
)
async def test_verified_alias_binds_uid(runtime, target):
    assert await resolve(runtime, target) == {
        "target": "3",
        "target_username": "example",
    }
    assert runtime.bot.get_chat.await_args.args == (3,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    [
        "t.me/+invite",
        "t.me/c/123/4",
        "t.me/example/1",
        "https://t.me.evil/example",
        "t.me/example?start=x",
        "@example/other",
        "https://user@t.me/example",
        "-100123",
        "１２３",
        "example",
    ],
)
async def test_invalid_target_never_queries_telegram(runtime, target):
    with pytest.raises(Rejected):
        await resolve(runtime, target)
    runtime.bot.get_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_numeric_uid_unchanged_and_unknown_ambiguous_alias_rejected(runtime):
    assert await resolve(runtime, "345") == {"target": "345"}
    with pytest.raises(Rejected, match="start"):
        await resolve(runtime, "@unknown")
    runtime.store.db.execute("INSERT INTO user_labels VALUES('4','example')")
    with pytest.raises(Rejected, match="唯一"):
        await resolve(runtime, "@example")
    runtime.bot.get_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_alias_and_network_failure_fail_closed(runtime):
    runtime.bot.get_chat.return_value.username = "changed"
    with pytest.raises(Rejected, match="变更"):
        await resolve(runtime, "@example")
    runtime.bot.get_chat.side_effect = TimedOut()
    with pytest.raises(Rejected, match="核对"):
        await resolve(runtime, "@example")
    assert runtime.store.db.execute("SELECT count(*) FROM grants").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_wizard_confirmation_rechecks_identity_and_uid_bound_redemption(runtime):
    ui = UI(runtime)
    ui.render = AsyncMock()
    event = SimpleNamespace(effective_user=SimpleNamespace(id=1))
    await run(ui, event, "grant", ["t.me/example", "全功能", "30"])
    assert "最终授权 UID：3" in ui.render.await_args.args[1]
    payload = ui.render.await_args.args[2][0][1]
    assert runtime.store.db.execute("SELECT count(*) FROM grants").fetchone()[0] == 0
    runtime.bot.get_chat.return_value.username = "renamed"
    with pytest.raises(Rejected):
        await ui.admin(event, payload, "")
    runtime.bot.get_chat.return_value.username = "Example"
    await ui.admin(event, payload, "")
    link = ui.render.await_args.args[1]
    token = link.split("?start=sbg_", 1)[1]
    with pytest.raises(Rejected):
        runtime.store.accept_grant("4", token)
    runtime.store.accept_grant("3", token)
    assert runtime.store.allowed("3", "ads")
    assert runtime.store.allowed("3", "points")
    assert runtime.store.allowed("3", "game")
    assert runtime.store.allowed("3", "manager")
    with pytest.raises(Rejected):
        runtime.store.accept_grant("3", token)


@pytest.mark.asyncio
async def test_reassigned_cached_username_cannot_change_confirmed_uid(runtime):
    with pytest.raises(Rejected, match="变化"):
        await resolve(runtime, "@example", expected="4")
    runtime.bot.get_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_form_and_non_owner(runtime):
    ui = UI(runtime)
    ui.render = AsyncMock()
    event = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        message=SimpleNamespace(text="@example 游戏 7", caption=None),
    )
    await ui.input(event, {"form": "grant"})
    data = ui.render.await_args.args[2][0][1]["data"]
    assert data["target"] == "3" and data["target_username"] == "example"
    event.effective_user.id = 2
    with pytest.raises(Rejected):
        await ui.input(event, {"form": "grant"})
    with pytest.raises(Rejected):
        await run(ui, event, "grant", ["@example", "广告", "30"])
    with pytest.raises(Rejected):
        await ui.admin(
            event, {"action": "save_form", "form": "grant", "data": data}, ""
        )
    assert runtime.store.db.execute("SELECT count(*) FROM grants").fetchone()[0] == 0
