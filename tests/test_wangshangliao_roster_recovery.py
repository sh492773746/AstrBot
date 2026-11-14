"""Bound transient membership reads without replaying moderation or trusting caches."""

import asyncio
import copy
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from astrbot.builtin_stars.wangshangliao_moderation import dashboard, open_service
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from astrbot.core.platform.sources.wangshangliao.business import BusinessError
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from astrbot.dashboard.responses import ApiError
from tests.test_wangshangliao_command_menu import step
from tests.test_wangshangliao_command_parity import setup as setup
from tests.test_wangshangliao_member_blacklist import (
    direct,
    mention,
)
from tests.test_wangshangliao_member_blacklist import scope as scope

pytestmark = pytest.mark.usefixtures("wsl_active_policy", "wsl_command_classifier")


@pytest_asyncio.fixture
async def roster_scope(scope, monkeypatch):
    """Exercise the real reader against isolated native membership and storage.

    Args:
        scope: Authorized group, member identities and temporary ledgers.
        monkeypatch: Replace only provider reads and the retry delay.

    Returns:
        Service, event, rows, store, provider reader and retry delay.
    """
    value, event, rows, store = scope
    event.platform.business = SimpleNamespace()
    event.platform.diagnostics = SimpleNamespace(emit=Mock())
    value.roster = open_service.OpenService.roster.__get__(value)
    fetch = AsyncMock(
        return_value={"complete": True, "groupMemberInfo": list(rows.values())}
    )
    delay = AsyncMock()
    monkeypatch.setattr(open_service, "member_directory", fetch)
    monkeypatch.setattr(open_service.asyncio, "sleep", delay)
    value.store.remember_roster = Mock(wraps=value.store.remember_roster)
    return value, event, rows, store, fetch, delay


@pytest.mark.asyncio
async def test_successful_read_does_not_retry(roster_scope):
    value, event, rows, _, fetch, delay = roster_scope
    assert await value.roster(event.platform, "5") == rows
    fetch.assert_awaited_once_with(event.platform.business, "5")
    delay.assert_not_awaited()
    assert value.capabilities[("1", "5")]["state"] == "available"


@pytest.mark.asyncio
async def test_final_http_authorization_never_waits_for_shared_refresh_lock(
    roster_scope,
):
    value, event, rows, _, fetch, _ = roster_scope
    adapter = event.platform
    # Another reader may own the refresh lock while waiting on the HTTP lock.
    adapter.refresh_member_mapping = AsyncMock(
        side_effect=AssertionError("HTTP owner cannot acquire the refresh lock")
    )

    async def authorize():
        adapter.business._authorization_task = asyncio.current_task()
        return await value.roster(adapter, "5")

    assert await asyncio.wait_for(authorize(), 1) == rows
    adapter.refresh_member_mapping.assert_not_awaited()
    fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_normal_roster_uses_shared_complete_reader_without_duplicate_io(
    roster_scope,
):
    value, event, rows, _, fetch, _ = roster_scope
    adapter = event.platform
    adapter.refresh_member_mapping = AsyncMock(return_value=fetch.return_value)
    assert await value.roster(adapter, "5") == rows
    adapter.refresh_member_mapping.assert_awaited_once_with("5")
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_transient_rejection_retries_only_the_current_read(roster_scope):
    value, event, rows, _, fetch, delay = roster_scope
    fetch.side_effect = [
        BusinessError("business_rejected", -1),
        fetch.return_value,
    ]
    assert await value.roster(event.platform, "5") == rows
    assert fetch.await_count == 2
    delay.assert_awaited_once_with(1)
    value.store.remember_roster.assert_called_once()
    event.platform.execute_action.assert_not_awaited()
    diagnostic = event.platform.diagnostics.emit.call_args.kwargs
    assert diagnostic["group"] == "5" and diagnostic["provider_code"] == -1


@pytest.mark.asyncio
async def test_persistent_rejection_never_publishes_cached_evidence(roster_scope):
    value, event, _, store, fetch, delay = roster_scope
    old = copy.deepcopy(value.member_evidence)
    fetch.side_effect = BusinessError("business_rejected", -1)
    with pytest.raises(OpenError, match="^member_directory_rejected$"):
        await value.roster(event.platform, "5")
    assert fetch.await_count == 2
    delay.assert_awaited_once_with(1)
    value.store.remember_roster.assert_not_called()
    assert value.member_evidence == old and store.read("1", "5") == (0, [])
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        BusinessError("rate_limited", 429),
        BusinessError("reauth_required", 401),
        BusinessError("secondary_security_code_required", 1400),
        BusinessError("business_rejected", 403),
        BusinessError("transport"),
        ProtocolError("member_identity"),
        ProtocolError("members_shape"),
    ],
)
async def test_security_rate_limit_transport_and_shape_errors_do_not_retry(
    roster_scope, error
):
    value, event, _, _, fetch, delay = roster_scope
    fetch.side_effect = error
    with pytest.raises(
        OpenError if str(error) == "business_rejected" else ProtocolError
    ):
        await value.roster(event.platform, "5")
    fetch.assert_awaited_once()
    delay.assert_not_awaited()
    value.store.remember_roster.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["epoch", "business", "offline", "closed"])
async def test_retry_rechecks_original_account_and_lifecycle(roster_scope, change):
    value, event, _, _, fetch, delay = roster_scope
    fetch.side_effect = [BusinessError("business_rejected", -1), fetch.return_value]

    async def revoke(_):
        if change == "epoch":
            event.platform.config["_business_epoch"] = "revoked"
        elif change == "business":
            event.platform.business = SimpleNamespace()
        elif change == "offline":
            event.platform.connection_state = "stopped"
        else:
            value.active = False

    delay.side_effect = revoke
    with pytest.raises((OpenError, ProtocolError)):
        await value.roster(event.platform, "5")
    fetch.assert_awaited_once()
    value.store.remember_roster.assert_not_called()
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelling_retry_does_not_read_or_submit_again(roster_scope):
    value, event, _, _, fetch, delay = roster_scope
    fetch.side_effect = BusinessError("business_rejected", -1)
    delay.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await value.roster(event.platform, "5")
    fetch.assert_awaited_once()
    value.store.remember_roster.assert_not_called()
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_incomplete_retry_does_not_publish_membership(roster_scope):
    value, event, rows, _, fetch, _ = roster_scope
    fetch.side_effect = [
        BusinessError("business_rejected", -1),
        {"complete": False, "groupMemberInfo": list(rows.values())},
    ]
    with pytest.raises(OpenError, match="membership_unproven"):
        await value.roster(event.platform, "5")
    value.store.remember_roster.assert_not_called()


@pytest.mark.asyncio
async def test_recovered_blacklist_command_submits_only_once(roster_scope):
    value, event, _, store, fetch, _ = roster_scope
    responses = iter([BusinessError("business_rejected", -1), fetch.return_value])

    async def read(*_):
        result = next(responses, fetch.return_value)
        if isinstance(result, Exception):
            raise result
        return result

    fetch.side_effect = read
    mention(event)

    async def execute(request, *, authorize):
        await authorize()
        return {
            "status": "accepted",
            "submitted": True,
            "operation": request.operation,
            "action": request.action,
            "group": request.group,
            "member": request.member,
            "minutes": request.minutes,
            "expected_nim": request.expected_nim,
        }

    event.platform.execute_action.side_effect = execute
    result = await direct(value, event)
    assert result["enforcement"]["status"] == "accepted"
    assert len(store.read("1", "5")[1]) == 1
    event.platform.execute_action.assert_awaited_once()
    assert not store.read("1", "6")[1]


@pytest.mark.asyncio
async def test_recovered_read_still_protects_promoted_target(roster_scope):
    value, event, rows, store, fetch, _ = roster_scope
    promoted = copy.deepcopy(rows)
    promoted["3"]["groupRole"] = "GROUP_ROLE_ADMIN"
    fetch.side_effect = [
        BusinessError("business_rejected", -1),
        {"complete": True, "groupMemberInfo": list(promoted.values())},
    ]
    mention(event)
    with pytest.raises(OpenError, match="blacklist_protected"):
        await direct(value, event)
    assert store.read("1", "5") == (0, [])
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_failure_reply_does_not_blame_command_or_change_list(roster_scope):
    value, event, _, store, fetch, _ = roster_scope
    fetch.side_effect = BusinessError("business_rejected", -1)
    mention(event)
    event.get_extra("wangshangliao_payload").update(
        sender="2", created_at=time.time() * 1000
    )
    reply = await step(value, event, "黑名单 @Target", native=True)
    assert "不是命令格式错误" in reply and "无法核实管理员和目标身份" in reply
    assert "请检查命令后重新发送" not in reply
    assert store.read("1", "5") == (0, [])
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["demoted", "absent", "inactive"])
async def test_revoked_rejoin_notice_stops_before_provider_reads(roster_scope, change):
    value, event, _, store, fetch, _ = roster_scope
    mention(event)
    await direct(value, event)
    tenant = value.tenants[("1", "5")]
    store.observe(
        "1",
        "5",
        "revoked-notice",
        ["903"],
        time.time(),
        guard=lambda: None,
        scope=value.member_blacklist.notice_scope(event.platform, tenant),
    )
    if change == "demoted":
        tenant["native_role"] = "GROUP_ROLE_MEMBER"
    elif change == "absent":
        tenant["present"] = False
    else:
        tenant["active"] = False
    fetch.reset_mock()
    event.platform.execute_action.reset_mock()
    await value.member_blacklist.process(event.platform)
    assert store.pending("1") == []
    fetch.assert_not_awaited()
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_dashboard_explains_directory_rejection(monkeypatch):
    monkeypatch.setattr(
        dashboard,
        "request",
        SimpleNamespace(_request=SimpleNamespace(method="GET"), username="test"),
    )
    page = dashboard.DashboardPage(SimpleNamespace())
    page.authorize = AsyncMock()
    page.plugin = SimpleNamespace(
        open_service=SimpleNamespace(
            active=True,
            integrated=True,
            rpc=SimpleNamespace(
                lock=asyncio.Lock(),
                dispatch=AsyncMock(side_effect=OpenError("member_directory_rejected")),
            ),
        )
    )
    with pytest.raises(ApiError, match="未使用旧名单继续操作"):
        await page.open_service()
