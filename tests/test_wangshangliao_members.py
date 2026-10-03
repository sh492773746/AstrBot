import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter


@pytest.mark.asyncio
async def test_member_cursor_and_identity_mapping():
    request = AsyncMock(
        side_effect=[
            {
                "owner": [],
                "member": [
                    {
                        "groupId": 7,
                        "groupCloudId": "77",
                        "groupName": "大海兼职群",
                    }
                ],
            },
            {
                "groupMemberInfo": [{"userId": 1, "nimId": "self"}],
                "nextCursor": "second",
            },
            {"groupMemberInfo": [{"userId": 2, "nimId": 123}]},
        ]
    )
    adapter = WangshangliaoAdapter(
        {"id": "members-fixture", "account_id": "1", "enabled_groups": ["7"]},
        {},
        asyncio.Queue(),
    )
    adapter.business = SimpleNamespace(request=request)
    adapter.nim_account = "self"
    await WangshangliaoAdapter.load_groups(adapter)
    assert request.call_args_list[1].args[1] == {"groupId": 7, "v": "0"}
    assert request.call_args_list[2].args[1]["cursor"] == "second"
    assert adapter.members == {"7": {"1": "self", "2": "123"}}
    assert adapter.group_names == {"7": "大海兼职群"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows",
    [
        [{"userId": 1, "nimId": "wrong-self"}],
        [{"userId": 2, "nimId": None}],
        [{"userId": 0, "nimId": "peer"}],
        [{"userId": 2, "nimId": "peer"}, {"userId": 3, "nimId": "peer"}],
    ],
)
async def test_member_identity_rejected(rows):
    request = AsyncMock(
        side_effect=[
            {"owner": [], "member": [{"groupId": 7, "groupCloudId": "77"}]},
            {"groupMemberInfo": rows},
        ]
    )
    adapter = WangshangliaoAdapter(
        {"id": "members-fixture", "account_id": "1", "enabled_groups": ["7"]},
        {},
        asyncio.Queue(),
    )
    adapter.business = SimpleNamespace(request=request)
    adapter.nim_account = "self"
    await adapter.load_groups()
    assert adapter.members == {}
    assert adapter.group_directory_state["7"]["complete"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("GROUP_ROLE_OWNER", "owner"),
        ("GROUP_ROLE_ADMIN", "admin"),
        ("GROUP_ROLE_MEMBER", "member"),
    ],
)
async def test_directory_uses_authenticated_self_role(raw, expected):
    from astrbot.core.platform.sources.wangshangliao.directory import group_directory

    client = SimpleNamespace(
        request=AsyncMock(
            return_value={"owner": [], "member": [{"groupId": 7, "me": {"role": raw}}]}
        )
    )
    result = await group_directory(client, "1")
    assert result["groups"][0]["role"] == expected
    assert result["groups"][0]["role_source"] == "group_list_me"
    client.request.assert_awaited_once()


@pytest.mark.asyncio
async def test_directory_conflicting_owner_evidence():
    from astrbot.core.platform.sources.wangshangliao.directory import group_directory

    client = SimpleNamespace(
        request=AsyncMock(
            return_value={
                "owner": [
                    {
                        "groupId": 7,
                        "ownerUserId": 1,
                        "me": {"role": "GROUP_ROLE_MEMBER"},
                    }
                ],
                "member": [],
            }
        )
    )
    result = await group_directory(client, "1")
    assert result["groups"][0]["role"] == "unknown"
    assert result["groups"][0]["role_reason"] == "role_conflict"
