import copy
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telethon import errors, types

from data.plugins.astrbot_plugin_telethon_ai.group_names import GroupNames


def setup(connected=True, entity=None):
    client = SimpleNamespace(
        is_connected=Mock(return_value=connected),
        get_entity=AsyncMock(
            return_value=entity
            or types.Chat(
                id=123,
                title="Test group",
                photo=types.ChatPhotoEmpty(),
                participants_count=2,
                date=None,
                version=1,
            )
        ),
    )
    context = SimpleNamespace(
        get_platform_inst=lambda _: SimpleNamespace(client=client)
    )
    directory = GroupNames(context)
    status = {
        "accounts": [
            {
                "account": "one",
                "platform_id": "TelethonAI_one",
                "allowed_chats": ["-123"],
                "tenant": "A",
            },
        ],
        "tenants": [
            {"id": "A", "groups": ["-123"]},
            {"id": "B", "groups": ["-123"]},
        ],
    }
    return directory, client, status


@pytest.mark.asyncio
async def test_title_and_id_are_read_only_cached_and_tenant_scoped():
    directory, client, status = setup()
    before = copy.deepcopy(status)
    await directory.enrich(status)
    group = status["accounts"][0]["groups"][0]
    assert group["id"] == "-123" and group["name"] == "Test group"
    assert (
        status["accounts"][0]["allowed_chats"] == before["accounts"][0]["allowed_chats"]
    )
    assert status["tenants"][0]["group_details"][0]["name"] == "Test group"
    assert status["tenants"][1]["group_details"][0]["name"] is None
    await directory.enrich(status)
    client.get_entity.assert_awaited_once_with(-123)


@pytest.mark.asyncio
async def test_disconnected_account_never_queries_or_connects():
    directory, client, status = setup(connected=False)
    await directory.enrich(status)
    group = status["accounts"][0]["groups"][0]
    assert group["name"] is None and group["name_state"] == "disconnected"
    client.get_entity.assert_not_awaited()


@pytest.mark.asyncio
async def test_known_titles_survive_disconnect_and_revoked_groups_are_removed():
    directory, client, status = setup()
    await directory.enrich(status)
    client.is_connected.return_value = False
    await directory.enrich(status)
    assert status["accounts"][0]["groups"][0]["name_state"] == "cached"
    status["accounts"][0]["allowed_chats"] = []
    await directory.enrich(status)
    assert not directory.cache
    assert status["tenants"][0]["group_details"][0]["name"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entity",
    [
        types.User(id=123, first_name="Not a group"),
        types.Chat(
            id=999,
            title="Wrong group",
            photo=types.ChatPhotoEmpty(),
            participants_count=2,
            date=None,
            version=1,
        ),
    ],
)
async def test_wrong_identity_or_entity_does_not_supply_a_title(entity):
    directory, client, status = setup(entity=entity)
    await directory.enrich(status)
    assert status["accounts"][0]["groups"][0]["name"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ValueError("private error"),
        errors.FloodWaitError(None, capture=600),
    ],
)
async def test_lookup_failure_keeps_id_and_throttles_retry(error):
    directory, client, status = setup()
    client.get_entity.side_effect = error
    await directory.enrich(status)
    await directory.enrich(status)
    group = status["accounts"][0]["groups"][0]
    assert group["id"] == "-123" and group["name"] is None
    client.get_entity.assert_awaited_once()
    assert directory.cache[("one", "-123")]["retry_at"] > time.monotonic()


@pytest.mark.asyncio
async def test_invalid_id_does_not_lookup_arbitrary_entities():
    directory, client, status = setup()
    status["accounts"][0]["allowed_chats"] = ["@not_authorized", "123"]
    await directory.enrich(status)
    client.get_entity.assert_not_awaited()
