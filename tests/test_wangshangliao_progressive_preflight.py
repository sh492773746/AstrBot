"""Ordinary progressive mutes share one fresh native roster, not cached authority."""

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.core.platform.sources.wangshangliao import moderation
from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError


@pytest.fixture
def native_case(tmp_path, monkeypatch):
    """Bind real adapter execution to a test ledger and serialized native stub.

    Args:
        tmp_path: Isolated operation ledger directory.
        monkeypatch: Test-scoped storage replacement.

    Returns:
        Adapter, mutable native evidence, recorded mutations and submission event.
    """
    monkeypatch.setattr(moderation, "instance_dir", lambda _: tmp_path)
    evidence = {
        "groups": {
            "owner": [{"groupId": 5, "me": {"role": "GROUP_ROLE_ADMIN"}}],
        },
        "members": {
            "groupMemberInfo": [
                {"userId": 2, "nimId": "22", "groupRole": "GROUP_ROLE_MEMBER"},
            ],
        },
    }
    mutations = []
    submitting = asyncio.Event()
    client = SimpleNamespace(fields=[0, 0, 0, "1"], lock=asyncio.Lock())

    async def request(route, params, *, authorize=None):
        if route == "/v1/group/get-group-list":
            return evidence["groups"]
        if route == "/v1/group/get-group-members":
            return evidence["members"]
        assert route in {
            "/v1/group/set-member-mute",
            "/v1/group/member-mute-cancel",
        }
        submitting.set()
        async with client.lock:
            try:
                if authorize is not None:
                    checked = authorize()
                    if inspect.isawaitable(checked):
                        await checked
            except BaseException as exc:
                exc.submitted = False
                raise
            mutations.append((route, dict(params)))
            return {}

    client.request = AsyncMock(side_effect=request)
    adapter = SimpleNamespace(
        account="1",
        config={
            "id": "progressive-preflight",
            "enable": True,
            "enabled_groups": ["5"],
            "_business_active": True,
            "_business_epoch": "epoch",
            "_business_revision": "revision",
            "moderation": {
                "enabled": True,
                "automation_enabled": True,
                "progressive_mute": True,
                "recall_enabled": True,
                "permissions": {"5": ["mute", "unmute", "recall"]},
            },
        },
        connection_state="online",
        members={"5": {"2": "22"}},
        business=client,
        stopping=asyncio.Event(),
    )

    async def preflight(group):
        page = await moderation.member_directory(client, group)
        adapter.members[group] = {
            row["userId"]: row["nimId"] for row in page["groupMemberInfo"]
        }
        return page

    adapter.get_moderation_members = AsyncMock(side_effect=preflight)
    for name in ("execute_moderation", "_execute_moderation"):
        setattr(adapter, name, getattr(WangshangliaoAdapter, name).__get__(adapter))
    return adapter, evidence, mutations, submitting


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["progressive/", "flood/"])
async def test_progressive_mute_uses_only_native_preflight_and_keeps_dedup(
    native_case, prefix
):
    adapter, _, mutations, _ = native_case
    check = AsyncMock()
    result = await adapter.execute_moderation(
        prefix + "once", "mute", 5, 2, minutes=3, action_check=check
    )
    assert result["status"] == "accepted"
    assert mutations == [
        ("/v1/group/set-member-mute", {"groupId": 5, "userId": 2, "min": 3}),
    ]
    adapter.get_moderation_members.assert_not_awaited()
    assert [call.args[0] for call in adapter.business.request.await_args_list] == [
        "/v1/group/get-group-list",
        "/v1/group/get-group-members",
        "/v1/group/set-member-mute",
    ]
    assert check.await_count == 3
    assert (
        await adapter.execute_moderation(
            prefix + "once", "mute", 5, 2, minutes=3, action_check=check
        )
        == result
    )
    assert adapter.business.request.await_count == 3
    assert len(mutations) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["progressive/", "flood/"])
@pytest.mark.parametrize("expected_nim", ["stale", None, ""])
async def test_native_progressive_requires_the_frozen_target(
    native_case, prefix, expected_nim
):
    adapter, _, mutations, _ = native_case
    with pytest.raises(ProtocolError, match="moderation_target"):
        await moderation.execute(
            adapter.business,
            adapter.config["id"],
            adapter.account,
            prefix + "identity",
            "mute",
            5,
            2,
            expected_nim=expected_nim,
        )
    assert mutations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["progressive/", "flood/"])
async def test_progressive_rejects_incomplete_native_directory(
    native_case, monkeypatch, prefix
):
    adapter, evidence, mutations, _ = native_case
    directory = AsyncMock(return_value={**evidence["members"], "complete": False})
    monkeypatch.setattr(moderation, "member_directory", directory)
    with pytest.raises(ProtocolError, match="moderation_target"):
        await adapter.execute_moderation(prefix + "partial", "mute", 5, 2)
    assert mutations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["progressive/", "flood/"])
@pytest.mark.parametrize("change", ["bot_role", "target_role", "peer", "departed"])
async def test_native_permission_and_target_checks_remain_required(
    native_case, prefix, change
):
    adapter, evidence, mutations, _ = native_case
    error = "moderation_target"
    if change == "bot_role":
        evidence["groups"]["owner"][0]["me"]["role"] = "GROUP_ROLE_MEMBER"
        error = "moderation_permission"
    elif change == "target_role":
        evidence["members"]["groupMemberInfo"][0]["groupRole"] = "GROUP_ROLE_ADMIN"
    elif change == "peer":
        evidence["members"]["groupMemberInfo"][0]["nimId"] = "23"
    else:
        evidence["members"]["groupMemberInfo"] = []
        error = "moderation_member_absent"
    with pytest.raises(ProtocolError, match=error):
        await adapter.execute_moderation(prefix + "changed", "mute", 5, 2)
    assert mutations == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,action",
    [
        ("manual", "mute"),
        ("manual", "unmute"),
        ("progressive/unmute", "unmute"),
        ("flood/unmute", "unmute"),
        ("progressive-looking", "mute"),
    ],
)
async def test_non_progressive_paths_keep_adapter_preflight(
    native_case, operation, action
):
    adapter, _, mutations, _ = native_case
    result = await adapter.execute_moderation(operation, action, 5, 2)
    assert result["status"] == "accepted"
    adapter.get_moderation_members.assert_awaited_once_with("5")
    assert (
        sum(
            call.args[0] == "/v1/group/get-group-members"
            for call in adapter.business.request.await_args_list
        )
        == 2
    )
    assert len(mutations) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["progressive/", "flood/"])
async def test_offline_progressive_mute_stops_before_native_io(native_case, prefix):
    adapter, _, mutations, _ = native_case
    adapter.connection_state = "reconnecting"
    with pytest.raises(ProtocolError, match="moderation_identity"):
        await adapter.execute_moderation(prefix + "offline", "mute", 5, 2)
    adapter.business.request.assert_not_awaited()
    assert mutations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["progressive/", "flood/"])
@pytest.mark.parametrize(
    "change", ["permission", "peer", "revision", "stopping", "offline"]
)
async def test_progressive_rechecks_authority_after_http_lock_admission(
    native_case, prefix, change
):
    adapter, _, mutations, submitting = native_case
    lock = adapter.business.lock
    await lock.acquire()
    work = asyncio.create_task(
        adapter.execute_moderation(prefix + "queued", "mute", 5, 2)
    )
    try:
        await asyncio.wait_for(submitting.wait(), 3)
        assert not work.done()
        if change == "permission":
            adapter.config["moderation"]["permissions"]["5"] = []
        elif change == "peer":
            adapter.members["5"]["2"] = "23"
        elif change == "revision":
            adapter.config["_business_revision"] = "revoked"
        elif change == "stopping":
            adapter.stopping.set()
        else:
            adapter.connection_state = "reconnecting"
        lock.release()
        with pytest.raises(ProtocolError) as caught:
            await asyncio.wait_for(work, 3)
        assert caught.value.submitted is False
        assert mutations == []
        adapter.get_moderation_members.assert_not_awaited()
    finally:
        if lock.locked():
            lock.release()
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)
