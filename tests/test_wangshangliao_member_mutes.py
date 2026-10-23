"""Dashboard unmute uses real mute evidence and actor-bound, single-use previews."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from pydantic import ValidationError

from astrbot.builtin_stars.wangshangliao_moderation.open_rpc import OpenCall, OpenRPC
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from tests.test_wangshangliao_open_rpc_guards import rpc_scope as rpc_scope


@pytest_asyncio.fixture
async def scope(rpc_scope):
    value, adapter, rows = rpc_scope
    adapter.groups = {"5": "1005", "6": "1006"}
    muted = {"1002"}
    adapter.nim = SimpleNamespace(
        get_team_muted_members=AsyncMock(side_effect=lambda _: set(muted))
    )

    async def execute(request, *, authorize):
        await authorize()
        muted.discard("1002")
        return {"status": "accepted"}

    adapter.execute_action = AsyncMock(side_effect=execute)
    return value, adapter, rows, muted


async def invoke(scope, operation="read", *, actor="dashboard/offline", **extra):
    return await OpenRPC(scope[0]).dispatch(
        OpenCall(
            action="member_mutes",
            actor=actor,
            payload={"operation": operation, "account": "99", "group_id": "5", **extra},
        )
    )


async def preview(scope):
    current = await invoke(scope)
    assert [row["id"] for row in current["members"]] == ["2"]
    payload = {"version": current["version"], "member": "2"}
    token = await invoke(scope, "preview", **payload)
    return {**payload, "confirmation": token["confirmation"]}


@pytest.mark.asyncio
async def test_read_preview_unmute_verify_and_no_replay(scope):
    payload = await preview(scope)
    value, adapter, _, _ = scope
    before = value.store.tenants()
    adapter.execute_action.assert_not_awaited()
    result = await invoke(scope, "commit", **payload)
    assert result["verified"] and result["status"] == "accepted"
    request = adapter.execute_action.await_args.args[0]
    assert (
        request.account,
        request.group,
        request.member,
        request.action,
        request.expected_nim,
    ) == ("99", 5, 2, "unmute", "1002")
    assert value.store.tenants() == before
    assert (await invoke(scope))["members"] == []
    with pytest.raises(OpenError):
        await invoke(scope, "commit", **payload)
    adapter.execute_action.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "actor",
        "group",
        "version",
        "peer",
        "role",
        "bot_role",
        "not_muted",
        "expired",
        "suspended",
    ],
)
async def test_unmute_preview_is_revoked_when_authority_changes(scope, change):
    payload = await preview(scope)
    value, adapter, rows, muted = scope
    actor = "dashboard/other" if change == "actor" else "dashboard/offline"
    if change == "group":
        payload["group_id"] = "6"
    elif change == "version":
        payload["version"] += 1
    elif change == "peer":
        rows[2]["nimId"] = "2002"
        muted.clear()
        muted.add("2002")
    elif change == "role":
        rows[2]["groupRole"] = "GROUP_ROLE_ADMIN"
    elif change == "bot_role":
        rows[0]["groupRole"] = "GROUP_ROLE_MEMBER"
    elif change == "not_muted":
        muted.clear()
    elif change == "expired":
        with value.store.connect(write=True) as db:
            db.execute("UPDATE capabilities SET expires=0")
    elif change == "suspended":
        value.suspended_groups.add(("99", "5"))
    with pytest.raises((OpenError, ProtocolError)):
        await invoke(scope, "commit", actor=actor, **payload)
    adapter.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_unmute_is_not_retried_or_reported_as_verified(scope):
    payload = await preview(scope)
    scope[1].execute_action.side_effect = None
    scope[1].execute_action.return_value = {"status": "unknown"}
    result = await invoke(scope, "commit", **payload)
    assert result["status"] == "unknown" and not result["verified"]
    with pytest.raises(OpenError, match="confirmation_invalid"):
        await invoke(scope, "commit", **payload)
    scope[1].execute_action.assert_awaited_once()


@pytest.mark.asyncio
async def test_native_mute_query_failure_is_not_an_empty_list(scope):
    scope[1].nim.get_team_muted_members.side_effect = ProtocolError("offline")
    with pytest.raises(ProtocolError):
        await invoke(scope)
    scope[1].execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_unmute_rechecks_permission_inside_native_submission(scope):
    payload = await preview(scope)
    value, adapter, _, _ = scope

    async def revoked(request, *, authorize):
        value.suspended_groups.add(("99", "5"))
        await authorize()
        pytest.fail("Revoked operation must not reach transport")

    adapter.execute_action.side_effect = revoked
    with pytest.raises(OpenError):
        await invoke(scope, "commit", **payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [{"member": "2"}, {"operation": "kick"}, {"group_id": "0"}, {"account": True}],
)
async def test_mute_api_rejects_ambiguous_arguments(scope, extra):
    with pytest.raises(ValidationError):
        await invoke(scope, **extra)


@pytest.mark.asyncio
async def test_chat_actor_cannot_call_dashboard_release(scope):
    with pytest.raises(OpenError, match="server_required"):
        await invoke(scope, actor="nim/1001")
