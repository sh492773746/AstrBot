"""Retired private replies stay silent; active group delivery guards stay enforced."""

import asyncio
import socket
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
from astrbot.core.platform.sources.wangshangliao.group_scope import group_revision
from astrbot.core.platform.sources.wangshangliao.runtime_policy import runtime
from astrbot.core.platform.sources.wangshangliao.storage import Ledger
from tests.test_wangshangliao_command_parity import setup as setup
from tests.test_wangshangliao_commands import (
    TestOfflineNetworkGuard as TestOfflineNetworkGuard,
)
from tests.test_wangshangliao_plain_commands import native_event
from tests.test_wangshangliao_transport_hardening import BlockingLock

pytestmark = pytest.mark.usefixtures("wsl_active_policy", "wsl_command_classifier")


@pytest_asyncio.fixture(autouse=True)
async def offline(monkeypatch):
    """Deny network I/O after the runner initializes its Windows self-pipe.

    Args:
        monkeypatch: Test-scoped patch factory.

    Yields:
        Nothing; denial remains active through dependent-fixture cleanup.
    """

    def forbidden(*args, **kwargs):
        raise AssertionError("real_network_forbidden")

    with monkeypatch.context() as guard:
        guard.setattr(socket.socket, "connect", forbidden)
        guard.setattr(socket.socket, "connect_ex", forbidden)
        guard.setattr(socket, "create_connection", forbidden)
        yield


@pytest_asyncio.fixture
async def delivery(setup, monkeypatch, tmp_path):
    service, engine, group_event = setup
    adapter = group_event.platform
    monkeypatch.setattr(runtime, "open_service", service)
    adapter.config.update(enable=True, _business_active=True)
    adapter.enabled_groups = {"5", "6"}
    adapter.members = {
        group: {"1": "901", "2": "902", "3": "903"} for group in ("5", "6")
    }
    service.store.remember_roster(
        "1",
        {
            row["userId"]: row
            for row in adapter.get_moderation_members.return_value["groupMemberInfo"]
        },
    )
    adapter.ledger = Ledger(tmp_path / "private-replies.sqlite3")
    await adapter.ledger.open()
    adapter.send_lock = BlockingLock()
    adapter.business = SimpleNamespace(
        deployment=SimpleNamespace(key=lambda _: bytes(32))
    )
    adapter.test_window = None
    adapter.diagnostics = SimpleNamespace(emit=Mock())
    adapter.nim = SimpleNamespace(
        request=AsyncMock(
            return_value=(200, wire.properties([(7, b"1000000"), (12, b"123")]))
        )
    )
    for name in ("send_text", "send_reply_text"):
        setattr(adapter, name, getattr(WangshangliaoAdapter, name).__get__(adapter))
    event = native_event("成员列表", private=True)
    event.platform = adapter
    event.session_id = "1/private/2/OTAy"
    event.message_obj.session_id = event.session_id
    event.set_extra("wsl_plain_command", True)
    key = (id(adapter), "1", "2", event.unified_msg_origin)
    engine.selections[key] = {"group": "5", "expires": time.monotonic() + 600}
    try:
        yield service, engine, event
    finally:
        await adapter.ledger.close()


async def assert_private_retired(event):
    """Check that a cached private reply stopped without delivery effects.

    Args:
        event: Retired native private event with an isolated delivery adapter.
    """
    adapter = event.platform
    assert not event.eligible
    assert event.is_stopped()
    assert event.reply_index == 0
    assert event.get_extra("_wsl_native_reply_result") == "not_sent"
    assert not adapter.send_lock.waiting.is_set()
    adapter.nim.request.assert_not_awaited()
    adapter.get_moderation_members.assert_not_awaited()
    adapter.execute_action.assert_not_awaited()
    async with adapter.ledger.db.execute(
        "SELECT (SELECT COUNT(*) FROM inbox),"
        "(SELECT COUNT(*) FROM outbox),(SELECT COUNT(*) FROM reply_recalls)"
    ) as cursor:
        assert await cursor.fetchone() == (0, 0, 0)


async def revoke(service, adapter, group="5"):
    """Commit and refresh an isolated dashboard manager revocation.

    Args:
        service: Temporary embedded tenant service.
        adapter: Synthetic adapter whose effective policy is refreshed.
        group: Exact tenant losing its original manager.
    """
    tenant = service.store.tenant("1", group)
    token = service.store.preview_control(
        "1", group, tenant["version"], "system/test", tenant["rules"], ["3"], None
    )
    service.store.change(
        "1",
        group,
        tenant["version"],
        "system/test",
        managers=["3"],
        control=True,
        confirmation=token,
    )
    await service.refresh()
    adapter.config.update(service.policy_values("1", adapter.config))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["manager", "pause", "leave", "role", "actor", "peer", "session", "version"],
)
async def test_private_member_directory_stops_before_send_lock(delivery, change):
    service, engine, event = delivery
    adapter = event.platform
    assert await engine.run(event, "成员列表") is None
    await adapter.send_lock.lock.acquire()
    try:
        if change == "manager":
            await revoke(service, adapter)
            assert not service.management.allowed(event, "5")
        elif change in {"pause", "leave", "role"}:
            tenant = service.store.tenant("1", "5")
            if change == "pause":
                service.store.change(
                    "1",
                    "5",
                    tenant["version"],
                    "system/test",
                    paused=True,
                    control=True,
                )
            elif change == "leave":
                service.store.discover("1", {"6": {"name": "Group 6", "team": "906"}})
            else:
                service.store.native_role(
                    "1", "5", tenant["epoch"], "GROUP_ROLE_MEMBER"
                )
            await service.refresh()
            adapter.config.update(service.policy_values("1", adapter.config))
        elif change == "actor":
            event.actor_identity = replace(event.actor_identity, peer="")
        elif change == "peer":
            adapter.members["5"]["2"] = "changed-peer"
        elif change == "session":
            event.session_id = "1/private/3/OTAz"
        else:
            service.management.versions[service.management.key(adapter, "5")] += 1
        await asyncio.wait_for(
            event.send(event.plain_result("cached member directory")), 1
        )
        await assert_private_retired(event)
    finally:
        if adapter.send_lock.lock.locked():
            adapter.send_lock.lock.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("test_scope", [False, True])
async def test_private_member_directory_never_reserves_first_segment(
    delivery, test_scope
):
    service, engine, event = delivery
    adapter = event.platform
    if test_scope:
        event.set_extra("wsl_command_result", True)
    assert await engine.run(event, "成员列表") is None

    async def send(*args):
        await revoke(service, adapter)
        return 200, wire.properties([(7, b"1000000"), (12, b"123")])

    adapter.nim.request.side_effect = send
    await event.send(event.plain_result("cached directory\n" + "x" * 9000))
    await assert_private_retired(event)
    key = "1/private/2/OTAy/message/0"
    assert await adapter.ledger.receipt(key + "/part/0") is None
    assert await adapter.ledger.receipt(key) is None


@pytest.mark.asyncio
async def test_unrelated_group_grant_cannot_revive_private_reply(delivery):
    service, engine, event = delivery
    adapter = event.platform
    assert await engine.run(event, "成员列表") is None
    await adapter.send_lock.lock.acquire()
    try:
        await revoke(service, adapter, "6")
        await asyncio.wait_for(
            event.send(event.plain_result("cached group 5 directory")), 1
        )
        await assert_private_retired(event)
    finally:
        if adapter.send_lock.lock.locked():
            adapter.send_lock.lock.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["3", "Target"])
async def test_private_menu_parameter_cannot_deliver_cached_result(delivery, text):
    service, engine, event = delivery
    adapter = event.platform
    assert await engine.run(event, "成员列表") is None
    event.message_str = text
    await adapter.send_lock.lock.acquire()
    try:
        await revoke(service, adapter)
        await asyncio.wait_for(
            event.send(event.plain_result("cached member selection")), 1
        )
        await assert_private_retired(event)
    finally:
        if adapter.send_lock.lock.locked():
            adapter.send_lock.lock.release()


@pytest.mark.asyncio
async def test_selected_group_revoked_before_send_cannot_fall_back_to_other_grant(
    delivery,
):
    service, engine, event = delivery
    assert await engine.run(event, "成员列表") is None
    await revoke(service, event.platform)
    assert not service.management.allowed(event, "6")
    await event.send(event.plain_result("cached member directory"))
    await assert_private_retired(event)


@pytest.mark.asyncio
@pytest.mark.parametrize("selection_change", ["clear", "switch", "expire"])
@pytest.mark.parametrize("remaining_grant", [False, True])
async def test_generated_member_reply_never_rebinds_scope_before_send(
    delivery, selection_change, remaining_grant
):
    service, engine, event = delivery
    assert await engine.run(event, "成员列表") is None
    await revoke(service, event.platform)
    if not remaining_grant:
        await revoke(service, event.platform, "6")
    key = (id(event.platform), "1", "2", event.unified_msg_origin)
    if selection_change == "clear":
        engine.selections.clear()
    elif selection_change == "switch":
        engine.selections[key] = {"group": "6", "expires": time.monotonic() + 600}
    else:
        engine.selections[key]["expires"] = 0
    await event.send(event.plain_result("cached member directory"))
    await assert_private_retired(event)


@pytest.mark.asyncio
async def test_switching_groups_cannot_revive_private_reply(delivery):
    service, engine, event = delivery
    assert await engine.run(event, "成员列表") is None
    key = (id(event.platform), "1", "2", event.unified_msg_origin)
    engine.selections[key] = {"group": "6", "expires": time.monotonic() + 600}
    await revoke(service, event.platform, "6")
    await event.send(event.plain_result("cached group 5 directory"))
    await assert_private_retired(event)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("群列表", "Group 5"),
        ("选择群 1", "Group 5"),
        ("成员列表", "Target"),
        ("下一页", "Target"),
        ("管理员列表", "Manager"),
        ("规则", "广告审核"),
        ("抽奖设置", "奖品"),
        ("定时状态", "本群尚未启用定时规则"),
    ],
)
async def test_private_management_rejects_before_result_generation(
    delivery, command, expected
):
    service, engine, event = delivery
    adapter = event.platform
    key = (id(adapter), "1", "2", event.unified_msg_origin)
    if command == "选择群 1":
        assert await engine.run(event, "群列表") is None
    elif command == "下一页":
        engine.selections[key].update(
            all_members=[{"userId": "3", "display_name": "Target"}], offset=0
        )
    with pytest.raises(OpenError, match="private_messages_retired"):
        await service.manager_command(event, command)
    engine.selections.clear()
    await revoke(service, adapter)
    await revoke(service, adapter, "6")
    await event.send(event.plain_result(expected))
    await assert_private_retired(event)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["成员列表", "选择群 1", "群列表"])
async def test_private_denial_and_missing_selection_are_not_delivered(
    delivery, command
):
    service, engine, event = delivery
    adapter = event.platform
    engine.selections.clear()
    if command == "群列表":
        await revoke(service, adapter)
        await revoke(service, adapter, "6")
    with pytest.raises(OpenError, match="private_messages_retired"):
        await service.manager_command(event, command)
    assert event.get_extra("_wsl_management_delivery_guard") is None
    await event.send(event.plain_result("cached permission denial"))
    await assert_private_retired(event)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target", ["groups", "members", "next", "back", "activity", "schedule"]
)
async def test_private_native_menu_chain_stops_before_dispatch(delivery, target):
    service, engine, initial = delivery
    adapter = initial.platform
    paths = {
        "groups": ("完整菜单", "2", "2"),
        "members": ("完整菜单", "2", "3"),
        "next": ("完整菜单", "2", "5"),
        "back": ("完整菜单", "2", "3", "0"),
        "activity": ("完整菜单", "6", "3"),
        "schedule": ("完整菜单", "5", "3"),
    }
    key = (id(adapter), "1", "2", initial.unified_msg_origin)
    if target == "next":
        engine.selections[key].update(
            all_members=[{"userId": "3", "display_name": "Target"}], offset=0
        )
    selections = {key: dict(value) for key, value in engine.selections.items()}
    command = AsyncMock(side_effect=AssertionError("private_menu_must_not_dispatch"))
    service.manager_command = command
    for number, text in enumerate(paths[target]):
        event = native_event(text, private=True)
        event.platform = adapter
        event.session_id = "1/private/2/OTAy"
        event.message_obj.session_id = event.session_id
        event.message_obj.message_id = f"menu-{target}-{number}"
        payload = event.get_extra("wangshangliao_payload")
        payload["_open_revision"] = group_revision(adapter.config, "")
        real_send = event.send
        event.send = AsyncMock(wraps=real_send)
        await service.on_event(event)
        assert event.is_stopped()
        assert event.get_extra("_component_claimed") is True
        event.send.assert_not_awaited()
        command.assert_not_awaited()
        assert engine.selections == selections
        await real_send(event.plain_result("cached menu response"))
        await assert_private_retired(event)


@pytest.mark.asyncio
async def test_private_group_list_stays_retired_with_member_native_role(delivery):
    service, engine, event = delivery
    adapter = event.platform
    tenant = service.store.tenant("1", "6")
    service.store.native_role("1", "6", tenant["epoch"], "GROUP_ROLE_MEMBER")
    await service.refresh()
    adapter.config.update(service.policy_values("1", adapter.config))
    event.message_str = "群列表"
    assert await engine.run(event, "群列表") is None
    await event.send(event.plain_result("cached Group 5 and Group 6"))
    await assert_private_retired(event)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("boundary", ["lock", "segment"])
async def test_private_invitation_reply_stops_before_delivery_guards(
    delivery, asynchronous, boundary
):
    _, _, event = delivery
    adapter = event.platform
    event.message_str = "我的邀请"
    state = {"allowed": True, "checks": 0}

    def guard():
        state["checks"] += 1
        if not state["allowed"]:
            raise wire.ProtocolError("private_query_scope_changed")

    async def async_guard():
        await asyncio.sleep(0)
        guard()

    event.set_extra(
        "_wsl_invitation_delivery_guard", async_guard if asynchronous else guard
    )
    if boundary == "segment":

        async def send(*args):
            state["allowed"] = False
            return 200, wire.properties([(7, b"1000000"), (12, b"123")])

        adapter.nim.request.side_effect = send
    else:
        await adapter.send_lock.lock.acquire()
    try:
        state["allowed"] = False
        await asyncio.wait_for(event.send(event.plain_result("x" * 9000)), 1)
        assert state["checks"] == 0
        await assert_private_retired(event)
    finally:
        if adapter.send_lock.lock.locked():
            adapter.send_lock.lock.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["你好", "菜单", "帮助", "我的邀请"])
async def test_ordinary_private_reply_stays_silent_without_manager_authority(
    delivery, text
):
    service, _, event = delivery
    adapter = event.platform
    event.message_str = text
    event.set_extra("wsl_plain_command", text != "你好")
    await revoke(service, adapter)
    await revoke(service, adapter, "6")
    assert not service.management.allowed(event)
    await event.send(event.plain_result("ordinary reply"))
    await assert_private_retired(event)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("boundary", ["lock", "segment"])
async def test_group_reply_guard_rechecks_delivery_boundaries(
    delivery, asynchronous, boundary
):
    _, _, private_event = delivery
    adapter = private_event.platform
    event = native_event("group query")
    event.platform = adapter
    event.eligible = True
    state = {"allowed": True, "checks": 0}

    def guard():
        state["checks"] += 1
        if not state["allowed"]:
            raise wire.ProtocolError("group_query_scope_changed")

    async def async_guard():
        await asyncio.sleep(0)
        guard()

    event.set_extra(
        "_wsl_invitation_delivery_guard", async_guard if asynchronous else guard
    )
    if boundary == "segment":

        async def send(*args):
            state["allowed"] = False
            return 200, wire.properties([(7, b"1000000"), (12, b"123")])

        adapter.nim.request.side_effect = send
    else:
        await adapter.send_lock.lock.acquire()
    task = asyncio.create_task(event.send(event.plain_result("x" * 9000)))
    try:
        if boundary == "lock":
            await asyncio.wait_for(adapter.send_lock.waiting.wait(), 3)
            state["allowed"] = False
            adapter.send_lock.lock.release()
        with pytest.raises(wire.ProtocolError, match="group_query_scope_changed"):
            await asyncio.wait_for(task, 3)
        assert adapter.nim.request.await_count == (boundary == "segment")
        key = "1/5/message/0"
        if boundary == "segment":
            assert (await adapter.ledger.receipt(key + "/part/0"))[
                "status"
            ] == "accepted"
            assert (await adapter.ledger.receipt(key))["status"] == "unknown"
        else:
            assert await adapter.ledger.receipt(key + "/part/0") is None
        assert state["checks"] > 1
    finally:
        if adapter.send_lock.lock.locked():
            adapter.send_lock.lock.release()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        event.processing_completion.cancel()
