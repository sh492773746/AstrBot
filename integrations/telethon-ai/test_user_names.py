from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import RetryAfter

from data.plugins.astrbot_plugin_telethon_ai.user_names import UserNames


def snapshot():
    return {
        "accounts": [
            {"user_id": "123", "username": None},
            {"user_id": "456", "username": "ai_worker"},
        ],
        "tenants": [{"owner": "123", "bot": "789", "bot_username": "customer_bot"}],
    }


@pytest.mark.asyncio
async def test_usernames_are_id_bound_cached_and_display_only():
    directory = UserNames()
    bot = SimpleNamespace(
        get_chat=AsyncMock(
            return_value=SimpleNamespace(
                id=123,
                type="private",
                username="owner_name",
            )
        )
    )
    state = snapshot()
    await directory.enrich(state, bot)
    assert state["tenants"][0]["owner_username"] == "owner_name"
    assert state["tenants"][0]["bot_username"] == "customer_bot"
    assert state["tenants"][0]["owner"] == "123"
    assert state["accounts"][0]["username"] == "owner_name"
    assert state["accounts"][1]["username"] == "ai_worker"
    await directory.enrich(snapshot(), bot)
    bot.get_chat.assert_awaited_once_with(123)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chat",
    [
        SimpleNamespace(id=999, type="private", username="wrong"),
        SimpleNamespace(id=123, type="group", username="wrong"),
        SimpleNamespace(id=123, type="private", username=None),
        SimpleNamespace(id=123, type="private", username="<img src=x>"),
    ],
)
async def test_failed_or_missing_username_keeps_numeric_identity(chat):
    directory = UserNames()
    state = snapshot()
    await directory.enrich(
        state, SimpleNamespace(get_chat=AsyncMock(return_value=chat))
    )
    assert state["tenants"][0]["owner_username"] is None
    assert state["tenants"][0]["owner"] == "123"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ValueError("private detail"), RetryAfter(300)])
async def test_failures_are_throttled_and_do_not_break_status(error):
    directory = UserNames()
    bot = SimpleNamespace(get_chat=AsyncMock(side_effect=error))
    await directory.enrich(snapshot(), bot)
    await directory.enrich(snapshot(), bot)
    bot.get_chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_disconnected_control_keeps_ids_and_existing_account_username():
    state = snapshot()
    await UserNames().enrich(state, None)
    assert state["tenants"][0]["owner_username"] is None
    assert state["accounts"][1]["username"] == "ai_worker"
