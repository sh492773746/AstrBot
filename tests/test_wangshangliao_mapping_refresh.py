import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot.core.platform.sources.wangshangliao import adapter as module


def make_adapter():
    adapter = SimpleNamespace(
        account="1",
        nim_account="901",
        nim=object(),
        connection_state="online",
        stopping=asyncio.Event(),
        config={
            "enable": True,
            "enabled_groups": ["5", "6"],
            "_business_active": True,
            "_business_epoch": 1,
            "_business_revision": "initial",
        },
        members={},
        groups={},
        enabled_groups={"5", "6"},
        group_directory_state={},
        member_refresh_locks={},
        member_refresh_at={},
        member_snapshots={},
        diagnostics=SimpleNamespace(emit=Mock(), observe=Mock()),
        business=SimpleNamespace(),
    )
    adapter.refresh_member_mapping = (
        module.WangshangliaoAdapter.refresh_member_mapping.__get__(adapter)
    )
    adapter.get_moderation_members = (
        module.WangshangliaoAdapter.get_moderation_members.__get__(adapter)
    )
    adapter.get_cached_members = module.WangshangliaoAdapter.get_cached_members.__get__(
        adapter
    )
    return adapter


@pytest.mark.asyncio
async def test_mapping_refresh_coalesces_and_publishes(monkeypatch):
    adapter = make_adapter()
    roster = {"complete": True, "groupMemberInfo": [{"userId": "2", "nimId": "902"}]}
    fetch = AsyncMock(return_value=roster)
    monkeypatch.setattr(module, "member_directory", fetch)
    await asyncio.gather(
        *(adapter.refresh_member_mapping("5", bounded=True) for _ in range(5))
    )
    assert fetch.await_count == 1
    assert adapter.members["5"] == {"2": "902"}
    assert adapter.group_directory_state["5"]["complete"]


@pytest.mark.asyncio
async def test_failed_mapping_preserves_valid_snapshot_and_throttles(monkeypatch):
    adapter = make_adapter()
    adapter.members["5"] = {"2": "902"}
    fetch = AsyncMock(side_effect=module.wire.ProtocolError("member_identity"))
    monkeypatch.setattr(module, "member_directory", fetch)
    with pytest.raises(module.wire.ProtocolError):
        await adapter.refresh_member_mapping("5", bounded=True)
    assert await adapter.refresh_member_mapping("5", bounded=True) is None
    assert adapter.members["5"] == {"2": "902"}
    assert not adapter.group_directory_state["5"]["complete"]
    assert fetch.await_count == 1


@pytest.mark.asyncio
async def test_bad_group_does_not_block_other_group(monkeypatch):
    adapter = make_adapter()
    adapter.business.request = AsyncMock(
        return_value={
            "owner": [],
            "member": [
                {"groupId": "5", "groupCloudId": "905"},
                {"groupId": "6", "groupCloudId": "906"},
            ],
        }
    )
    fetch = AsyncMock(
        side_effect=[
            module.wire.ProtocolError("member_identity"),
            {
                "complete": True,
                "groupMemberInfo": [{"userId": "2", "nimId": "902"}],
            },
        ]
    )
    monkeypatch.setattr(module, "member_directory", fetch)
    await module.WangshangliaoAdapter.load_groups(adapter)
    assert adapter.groups == {"5": "905", "6": "906"}
    assert adapter.members == {"6": {"2": "902"}}
    assert not adapter.group_directory_state["5"]["complete"]


@pytest.mark.asyncio
async def test_operation_query_missing_table_and_group_isolation(tmp_path, monkeypatch):
    import sqlite3

    monkeypatch.setattr(module, "instance_dir", lambda _: tmp_path)
    adapter = SimpleNamespace(config={"id": "test"})
    query = module.WangshangliaoAdapter.get_moderation_result
    assert await query(adapter, "op", "5") == {}
    with sqlite3.connect(tmp_path / "moderation.sqlite3") as db:
        assert await query(adapter, "op", "5") == {}
        db.execute("CREATE TABLE operations(id TEXT, result TEXT)")
        db.execute(
            'INSERT INTO operations VALUES(\'op\', \'{"group":5,"status":"accepted"}\')'
        )
    assert await query(adapter, "op", "6") == {}
    assert (await query(adapter, "op", "5"))["status"] == "accepted"


@pytest.mark.asyncio
async def test_transient_rejection_is_retried_once_and_shared_by_waiters(monkeypatch):
    adapter = make_adapter()
    page = {"complete": True, "groupMemberInfo": [{"userId": "2", "nimId": "902"}]}
    fetch = AsyncMock(side_effect=[module.BusinessError("business_rejected", -1), page])
    monkeypatch.setattr(module, "member_directory", fetch)
    results = await asyncio.gather(
        *(adapter.refresh_member_mapping("5") for _ in range(5))
    )
    assert all(result == page for result in results)
    assert fetch.await_count == 2
    assert adapter.members["5"] == {"2": "902"}


@pytest.mark.asyncio
async def test_persistent_rejection_does_not_stampede_or_use_old_evidence(monkeypatch):
    adapter = make_adapter()
    adapter.members["5"] = {"2": "old"}
    fetch = AsyncMock(side_effect=module.BusinessError("business_rejected", -1))
    monkeypatch.setattr(module, "member_directory", fetch)
    results = await asyncio.gather(
        *(adapter.refresh_member_mapping("5") for _ in range(8)), return_exceptions=True
    )
    assert all(isinstance(result, module.BusinessError) for result in results)
    assert fetch.await_count == 2
    assert not adapter.group_directory_state["5"]["complete"]
    assert "5" not in adapter.member_snapshots
    assert adapter.members["5"] == {"2": "old"}
    # Another group remains independently readable.
    fetch.side_effect = None
    fetch.return_value = {"complete": True, "groupMemberInfo": []}
    assert await adapter.refresh_member_mapping("6") == fetch.return_value


@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["rate_limited", "reauth_required", "members_shape"])
async def test_non_transient_directory_errors_are_not_retried(monkeypatch, error):
    adapter = make_adapter()
    fetch = AsyncMock(side_effect=module.BusinessError(error, 429))
    monkeypatch.setattr(module, "member_directory", fetch)
    with pytest.raises(module.BusinessError, match=error):
        await adapter.refresh_member_mapping("5")
    fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_retry_rejects_replaced_business_identity(monkeypatch):
    adapter = make_adapter()

    async def fetch(*_):
        adapter.business = SimpleNamespace()
        raise module.BusinessError("business_rejected", -1)

    request = AsyncMock(side_effect=fetch)
    monkeypatch.setattr(module, "member_directory", request)
    with pytest.raises(module.wire.ProtocolError, match="member_identity"):
        await adapter.refresh_member_mapping("5")
    request.assert_awaited_once()
    assert "5" not in adapter.member_snapshots


@pytest.mark.asyncio
async def test_repeated_directory_rejections_back_off_and_recover(monkeypatch):
    adapter = make_adapter()
    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    fetch = AsyncMock(side_effect=module.BusinessError("business_rejected", -1))
    monkeypatch.setattr(module, "member_directory", fetch)
    for round_number, delay in enumerate((1, 2, 4, 8, 16, 30, 30), start=1):
        with pytest.raises(module.BusinessError):
            await adapter.refresh_member_mapping("5")
        assert fetch.await_count == round_number * 2
        clock[0] += delay - 0.1
        with pytest.raises(module.BusinessError) as caught:
            await adapter.refresh_member_mapping("5")
        assert caught.value.retry_after == pytest.approx(0.1)
        assert fetch.await_count == round_number * 2
        clock[0] += 0.11
    fetch.side_effect = None
    fetch.return_value = {
        "complete": True,
        "groupMemberInfo": [{"userId": "2", "nimId": "902"}],
    }
    assert await adapter.refresh_member_mapping("5") == fetch.return_value
    assert "5" not in adapter.member_refresh_failures
    assert adapter.members["5"] == {"2": "902"}
    fetch.side_effect = module.BusinessError("business_rejected", -1)
    clock[0] += 1
    with pytest.raises(module.BusinessError):
        await adapter.refresh_member_mapping("5")
    assert adapter.member_refresh_failures["5"][3] == 1


@pytest.mark.asyncio
async def test_directory_backoff_does_not_cross_accounts_or_return_stale_roster(
    monkeypatch,
):
    adapter = make_adapter()
    adapter.member_refresh_failures = {
        "5": ("old-account", adapter.business, module.time.monotonic(), 6)
    }
    page = {"complete": True, "groupMemberInfo": [{"userId": "2", "nimId": "new"}]}
    fetch = AsyncMock(return_value=page)
    monkeypatch.setattr(module, "member_directory", fetch)
    assert await adapter.refresh_member_mapping("5") == page
    assert adapter.members["5"] == {"2": "new"}
    fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_overlapping_rosters_share_detached_snapshot_even_on_same_clock_tick(
    monkeypatch,
):
    adapter = make_adapter()
    monkeypatch.setattr(
        module, "time", SimpleNamespace(monotonic=lambda: 100.0, time=module.time.time)
    )
    entered, release = asyncio.Event(), asyncio.Event()
    waiting = asyncio.Queue()
    page = {"complete": True, "groupMemberInfo": [{"userId": "2", "nimId": "902"}]}

    async def fetch(*_):
        entered.set()
        await release.wait()
        return copy.deepcopy(page)

    async def read():
        waiting.put_nowait(True)
        return await adapter.get_moderation_members("5")

    fetcher = AsyncMock(side_effect=fetch)
    monkeypatch.setattr(module, "member_directory", fetcher)
    first = asyncio.create_task(adapter.get_moderation_members("5"))
    await asyncio.wait_for(entered.wait(), 1)
    others = [asyncio.create_task(read()) for _ in range(8)]
    for _ in others:
        await asyncio.wait_for(waiting.get(), 1)
    release.set()
    results = await asyncio.gather(first, *others)
    assert fetcher.await_count == 1
    results[0]["groupMemberInfo"].clear()
    results[1]["groupMemberInfo"][0]["nimId"] = "untrusted"
    assert results[2] == page
    assert await adapter.get_cached_members("5") == page
    assert fetcher.await_count == 1
    # Sequential execution checks must fetch even with a frozen/coarse clock.
    assert await adapter.get_moderation_members("5") == page
    assert fetcher.await_count == 2


@pytest.mark.asyncio
async def test_read_cache_does_not_queue_behind_forced_refresh_and_failure_invalidates(
    monkeypatch,
):
    adapter = make_adapter()
    page = {"complete": True, "groupMemberInfo": [{"userId": "2", "nimId": "902"}]}
    fetch = AsyncMock(return_value=page)
    monkeypatch.setattr(module, "member_directory", fetch)
    await adapter.get_moderation_members("5")
    entered, release = asyncio.Event(), asyncio.Event()

    async def fail(*_):
        entered.set()
        await release.wait()
        raise module.wire.ProtocolError("member_identity")

    fetch.side_effect = fail
    forced = asyncio.create_task(adapter.get_moderation_members("5"))
    await asyncio.wait_for(entered.wait(), 1)
    try:
        assert await asyncio.wait_for(adapter.get_cached_members("5"), 1) == page
        assert not forced.done()
    finally:
        release.set()
        with pytest.raises(module.wire.ProtocolError, match="member_identity"):
            await forced
    assert not adapter.group_directory_state["5"]["complete"]
    assert "5" not in adapter.member_snapshots
    with pytest.raises(module.wire.ProtocolError):
        await adapter.get_cached_members("5")
    assert fetch.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "business",
        "account",
        "nim",
        "nim_account",
        "team",
        "scope",
        "epoch",
        "revision",
        "active",
        "tenant_revision",
        "native_role",
        "paused",
        "offline",
        "disabled",
        "stopping",
        "directory_removed",
        "directory_incomplete",
        "mapping",
        "snapshot_replaced",
        "auth_expired",
        "client_account",
    ],
)
async def test_cached_roster_is_bound_to_identity_scope_and_complete_evidence(
    monkeypatch, change
):
    adapter = make_adapter()
    adapter.business.jwt = "synthetic"
    adapter.business.fields = [0, 0, 0, 1]
    page = {"complete": True, "groupMemberInfo": [{"userId": "2", "nimId": "902"}]}
    fetch = AsyncMock(return_value=page)
    monkeypatch.setattr(module, "member_directory", fetch)
    await adapter.refresh_member_mapping("5")
    if change == "business":
        adapter.business = SimpleNamespace()
    elif change == "account":
        adapter.account = "9"
    elif change == "nim":
        adapter.nim = object()
    elif change == "nim_account":
        adapter.nim_account = "909"
    elif change == "team":
        adapter.groups["5"] = "another-team"
    elif change == "scope":
        adapter.enabled_groups.remove("5")
        adapter.config["enabled_groups"].remove("5")
    elif change in {"epoch", "revision", "active"}:
        key = "_business_" + change
        adapter.config[key] = False if change == "active" else "revoked"
    elif change == "tenant_revision":
        adapter.config["_open_revisions"] = {"5": "revoked"}
    elif change == "native_role":
        adapter.config["_open_native_roles"] = {"5": "GROUP_ROLE_MEMBER"}
    elif change == "paused":
        adapter.config["_open_paused"] = True
    elif change == "offline":
        adapter.connection_state = "stopped"
    elif change == "disabled":
        adapter.config["enable"] = False
    elif change == "stopping":
        adapter.stopping.set()
    elif change == "directory_removed":
        adapter.group_directory_state.clear()
    elif change == "directory_incomplete":
        adapter.group_directory_state["5"]["complete"] = False
    elif change == "mapping":
        adapter.members["5"]["2"] = "changed-peer"
    elif change == "auth_expired":
        adapter.business.jwt = ""
    elif change == "client_account":
        adapter.business.fields[3] = 9
    else:
        adapter.member_snapshots["5"] = copy.deepcopy(adapter.member_snapshots["5"])
    # Invalid evidence must not survive even when a new read fails.
    fetch.side_effect = module.wire.ProtocolError("member_identity")
    with pytest.raises(module.wire.ProtocolError, match="member_identity"):
        await adapter.refresh_member_mapping("5", max_age=15)
    assert fetch.await_count == 2
    assert "5" not in adapter.member_snapshots
    assert not adapter.group_directory_state["5"]["complete"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["business", "epoch", "scope", "offline", "nim"])
async def test_waiting_moderation_readers_reject_revocation(monkeypatch, change):
    adapter = make_adapter()
    entered, release, waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    page = {"complete": True, "groupMemberInfo": [{"userId": "2", "nimId": "902"}]}

    async def fetch(*_):
        entered.set()
        await release.wait()
        return page

    async def read():
        waiting.set()
        return await adapter.get_moderation_members("5")

    fetcher = AsyncMock(side_effect=fetch)
    monkeypatch.setattr(module, "member_directory", fetcher)
    first = asyncio.create_task(adapter.get_moderation_members("5"))
    await asyncio.wait_for(entered.wait(), 1)
    second = asyncio.create_task(read())
    await asyncio.wait_for(waiting.wait(), 1)
    if change == "business":
        adapter.business = SimpleNamespace()
    elif change == "epoch":
        adapter.config["_business_epoch"] = 2
    elif change == "scope":
        adapter.enabled_groups.remove("5")
        adapter.config["enabled_groups"].remove("5")
    elif change == "offline":
        adapter.connection_state = "stopped"
    else:
        adapter.nim = object()
    release.set()
    results = await asyncio.wait_for(
        asyncio.gather(first, second, return_exceptions=True), 1
    )
    assert all(isinstance(result, module.wire.ProtocolError) for result in results)
    assert fetcher.await_count == 1
    assert "5" not in adapter.member_snapshots
    assert not adapter.group_directory_state["5"]["complete"]


@pytest.mark.asyncio
async def test_invalidation_during_read_cannot_republish_complete_roster(monkeypatch):
    adapter = make_adapter()
    page = {"complete": True, "groupMemberInfo": [{"userId": "2", "nimId": "902"}]}
    fetch = AsyncMock(return_value=page)
    monkeypatch.setattr(module, "member_directory", fetch)
    await adapter.refresh_member_mapping("5")
    entered, release = asyncio.Event(), asyncio.Event()

    async def read(*_):
        entered.set()
        await release.wait()
        return page

    fetch.side_effect = read
    pending = asyncio.create_task(adapter.get_moderation_members("5"))
    await asyncio.wait_for(entered.wait(), 1)
    adapter.group_directory_state["5"]["complete"] = False
    release.set()
    with pytest.raises(module.wire.ProtocolError, match="member_identity"):
        await pending
    assert "5" not in adapter.member_snapshots
    assert not adapter.group_directory_state["5"]["complete"]


@pytest.mark.asyncio
async def test_cancelled_refresh_discards_previous_complete_evidence(monkeypatch):
    adapter = make_adapter()
    page = {"complete": True, "groupMemberInfo": [{"userId": "2", "nimId": "902"}]}
    fetch = AsyncMock(return_value=page)
    monkeypatch.setattr(module, "member_directory", fetch)
    await adapter.refresh_member_mapping("5")
    entered = asyncio.Event()

    async def read(*_):
        entered.set()
        await asyncio.Event().wait()

    fetch.side_effect = read
    pending = asyncio.create_task(adapter.get_moderation_members("5"))
    await asyncio.wait_for(entered.wait(), 1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert "5" not in adapter.member_snapshots
    assert not adapter.group_directory_state["5"]["complete"]
    fetch.side_effect = None
    assert await adapter.get_cached_members("5") == page
    assert fetch.await_count == 3


@pytest.mark.asyncio
async def test_failed_refresh_backoff_is_shared_without_reusing_complete_snapshot(
    monkeypatch,
):
    adapter = make_adapter()
    clock = [100.0]
    monkeypatch.setattr(
        module,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0], time=module.time.time),
    )
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    page = {"complete": True, "groupMemberInfo": [{"userId": "2", "nimId": "902"}]}
    fetch = AsyncMock(return_value=page)
    monkeypatch.setattr(module, "member_directory", fetch)
    await adapter.refresh_member_mapping("5")
    fetch.side_effect = module.BusinessError("business_rejected", -1)
    results = await asyncio.gather(
        *(adapter.get_moderation_members("5") for _ in range(8)),
        return_exceptions=True,
    )
    assert all(isinstance(result, module.BusinessError) for result in results)
    assert fetch.await_count == 3
    assert adapter.member_refresh_failures["5"][3] == 1
    assert "5" not in adapter.member_snapshots
    with pytest.raises(module.BusinessError):
        await adapter.get_cached_members("5")
    assert fetch.await_count == 3
    clock[0] += 1.01
    fetch.side_effect = None
    assert await adapter.get_cached_members("5") == page
    assert fetch.await_count == 4
    assert "5" not in adapter.member_refresh_failures


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["business", "nim", "epoch"])
async def test_failure_backoff_does_not_survive_identity_generation_change(
    monkeypatch, change
):
    adapter = make_adapter()
    monkeypatch.setattr(
        module, "time", SimpleNamespace(monotonic=lambda: 100.0, time=module.time.time)
    )
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    fetch = AsyncMock(side_effect=module.BusinessError("business_rejected", -1))
    monkeypatch.setattr(module, "member_directory", fetch)
    with pytest.raises(module.BusinessError):
        await adapter.refresh_member_mapping("5")
    if change == "business":
        # Equal attribute values must not make two clients the same identity.
        adapter.business = SimpleNamespace()
    elif change == "nim":
        adapter.nim = object()
    else:
        adapter.config["_business_epoch"] = 2
    page = {"complete": True, "groupMemberInfo": [{"userId": "2", "nimId": "new"}]}
    fetch.side_effect = None
    fetch.return_value = page
    assert await adapter.refresh_member_mapping("5", max_age=15) == page
    assert fetch.await_count == 3
    assert "5" not in adapter.member_refresh_failures


@pytest.mark.asyncio
async def test_rejection_invalidates_read_cache_before_retry_wait(monkeypatch):
    adapter = make_adapter()
    old = {"complete": True, "groupMemberInfo": [{"userId": "2", "nimId": "old"}]}
    new = {"complete": True, "groupMemberInfo": [{"userId": "2", "nimId": "new"}]}
    fetch = AsyncMock(return_value=old)
    monkeypatch.setattr(module, "member_directory", fetch)
    await adapter.refresh_member_mapping("5")
    retry_waiting, release, cache_waiting = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )

    async def delay(_):
        retry_waiting.set()
        await release.wait()

    async def cached_read():
        cache_waiting.set()
        return await adapter.get_cached_members("5")

    monkeypatch.setattr(module.asyncio, "sleep", delay)
    fetch.side_effect = [module.BusinessError("business_rejected", -1), new]
    forced = asyncio.create_task(adapter.get_moderation_members("5"))
    await asyncio.wait_for(retry_waiting.wait(), 1)
    assert "5" not in adapter.member_snapshots
    assert not adapter.group_directory_state["5"]["complete"]
    cached = asyncio.create_task(cached_read())
    await asyncio.wait_for(cache_waiting.wait(), 1)
    try:
        assert not cached.done()
    finally:
        release.set()
        results = await asyncio.wait_for(asyncio.gather(forced, cached), 1)
    assert results == [new, new]
    assert fetch.await_count == 3


@pytest.mark.asyncio
async def test_moderation_reader_rechecks_scope_after_delegated_refresh():
    adapter = make_adapter()
    page = {"complete": True, "groupMemberInfo": []}

    async def revoke(_):
        adapter.config["enabled_groups"].remove("5")
        return page

    adapter.refresh_member_mapping = AsyncMock(side_effect=revoke)
    with pytest.raises(module.wire.ProtocolError, match="moderation_identity"):
        await adapter.get_moderation_members("5")


@pytest.mark.asyncio
async def test_final_http_authorization_refresh_is_fresh_without_reversing_locks(
    monkeypatch,
):
    from tests.test_wangshangliao_transport_hardening import Response, http_client

    adapter = make_adapter()
    rows = [{"userId": "2", "nimId": "902", "groupRole": "GROUP_ROLE_MEMBER"}]
    routes = []

    class RosterResponse(Response):
        async def iter_chunked(self, size):
            yield json.dumps(
                {"code": 0, "data": {"groupMemberInfo": copy.deepcopy(rows)}}
            ).encode()

    def post(url, **_):
        routes.append(url.rsplit("/", 1)[-1])
        return RosterResponse()

    adapter.business = http_client(post)
    await adapter.refresh_member_mapping("5")
    rows[0]["groupRole"] = "GROUP_ROLE_ADMIN"
    authorization_entered, reader_waiting = asyncio.Event(), asyncio.Event()
    real_directory = module.member_directory

    async def directory(client, group):
        if getattr(client, "_authorization_task", None) is not asyncio.current_task():
            reader_waiting.set()
        return await real_directory(client, group)

    monkeypatch.setattr(module, "member_directory", directory)

    async def authorize():
        authorization_entered.set()
        await reader_waiting.wait()
        assert adapter.member_refresh_locks["5"].locked()
        assert adapter.business.lock.locked()
        roster = await adapter.get_moderation_members("5")
        assert roster["groupMemberInfo"][0]["groupRole"] == "GROUP_ROLE_ADMIN"

    outer = asyncio.create_task(
        adapter.business.request(
            "/v1/group/get-group-list", {"v": "0"}, authorize=authorize
        )
    )
    await asyncio.wait_for(authorization_entered.wait(), 1)
    reader = asyncio.create_task(adapter.refresh_member_mapping("5"))
    results = await asyncio.wait_for(
        asyncio.gather(outer, reader, return_exceptions=True), 1
    )
    assert isinstance(results[0], dict)
    # A concurrent publisher changed this reader's evidence; it fails closed.
    assert isinstance(results[1], module.wire.ProtocolError)
    assert routes == [
        "get-group-members",
        "get-group-members",
        "get-group-list",
        "get-group-members",
    ]
    assert adapter.business._authorization_task is None
    assert not adapter.business.lock.locked()
    assert not adapter.member_refresh_locks["5"].locked()
