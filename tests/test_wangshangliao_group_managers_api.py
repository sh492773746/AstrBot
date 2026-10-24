"""Dashboard grant authorization and independent optimistic concurrency."""

import copy
from unittest.mock import AsyncMock

import httpx
import pytest

from astrbot.builtin_stars.wangshangliao_moderation.group_management import (
    GroupManagement,
)
from tests.test_wangshangliao_dashboard import headers as headers
from tests.test_wangshangliao_dashboard import page_app as page_app

PREFIX = "/api/v1/plugins/extensions/wangshangliao_moderation"


@pytest.mark.asyncio
async def test_grants_are_dashboard_only_versioned_and_preserve_business(
    page_app, headers, tmp_path
):
    app, page, _, configs, _ = page_app
    adapter = page.context.platform_manager.platform_insts[0]
    adapter.connection_state = "online"
    adapter.get_moderation_members = AsyncMock(
        return_value={
            "complete": True,
            "groupMemberInfo": [
                {
                    "userId": "22",
                    "userNick": "Manager",
                    "nimId": "922",
                    "groupRole": "GROUP_ROLE_ADMIN",
                },
                {
                    "userId": "23",
                    "userNick": "Member",
                    "nimId": "923",
                    "groupRole": "GROUP_ROLE_MEMBER",
                },
                {"userId": "1", "userNick": "Bot"},
            ],
        }
    )
    page.management = GroupManagement(page.context, tmp_path)
    await page.management.load()
    original = copy.deepcopy(configs)
    scope = {"bot_id": "fixture", "group_id": "5"}
    endpoint = PREFIX + "/group-managers"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        assert (await client.get(endpoint, params=scope)).status_code in {401, 403}
        response = await client.get(endpoint, params=scope, headers=headers)
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["users"] == [] and data["version"] == 0
        assert [m["id"] for m in data["members"]] == ["22"]
        body = {**scope, "users": ["22"], "version": 0, "revision": data["revision"]}
        rejected = await client.post(
            endpoint, json={**body, "users": ["23"]}, headers=headers
        )
        assert rejected.status_code == 409
        response = await client.post(endpoint, json=body, headers=headers)
        assert response.status_code == 200, response.text
        assert (
            await client.post(endpoint, json=body, headers=headers)
        ).status_code == 409
        read = (await client.get(endpoint, params=scope, headers=headers)).json()[
            "data"
        ]
        assert read["users"] == ["22"] and read["version"] == 1
        other = (
            await client.get(
                endpoint, params={**scope, "group_id": "6"}, headers=headers
            )
        ).json()["data"]
        assert other["users"] == []
        assert (
            await client.post(endpoint, json={**body, "group_id": "7"}, headers=headers)
        ).status_code == 409
    assert configs == original
