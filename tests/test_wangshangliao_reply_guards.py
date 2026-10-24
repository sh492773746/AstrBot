"""Offline delivery guards for private management and sender-only queries."""

import asyncio
import socket
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
from astrbot.core.platform.sources.wangshangliao.group_scope import group_revision
from astrbot.core.platform.sources.wangshangliao.runtime_policy import runtime
from astrbot.core.platform.sources.wangshangliao.storage import Ledger
from tests.test_wangshangliao_command_parity import setup as setup
from tests.test_wangshangliao_plain_commands import native_event
from tests.test_wangshangliao_transport_hardening import BlockingLock

pytestmark = pytest.mark.usefixtures("wsl_active_policy", "wsl_command_classifier")


@pytest_asyncio.fixture(autouse=True)
async def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("real_network_forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


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
    await adapter.ledger.ingest("1", "private/2/OTAy", "inbound", {"sender": "2"})
    try:
        yield service, engine, event
    finally:
        await adapter.ledger.close()


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
async def test_member_directory_rechecks_exact_scope_after_send_lock(delivery, change):
    service, engine, event = delivery
    adapter = event.platform
    response = await engine.run(event, "成员列表")
    assert "Target" in response
    await adapter.send_lock.lock.acquire()
    task = asyncio.create_task(event.send(event.plain_result(response)))
    try:
        await asyncio.wait_for(adapter.send_lock.waiting.wait(), 3)
        if change == "manager":
            await revoke(service, adapter)
            assert not service.management.allowed(event, "5")
            assert service.management.allowed(event, "6")
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
        adapter.send_lock.lock.release()
        with pytest.raises(wire.ProtocolError, match="management_reply_scope_changed"):
            await asyncio.wait_for(task, 3)
        adapter.nim.request.assert_not_awaited()
        assert await adapter.ledger.receipt("1/private/2/OTAy/message/0") is None
    finally:
        if adapter.send_lock.lock.locked():
            adapter.send_lock.lock.release()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("test_scope", [False, True])
async def test_member_directory_revoked_between_segments_keeps_first_receipt(
    delivery, test_scope
):
    service, engine, event = delivery
    adapter = event.platform
    if test_scope:
        event.set_extra("wsl_command_result", True)
    response = await engine.run(event, "成员列表")

    async def send(*args):
        await revoke(service, adapter)
        return 200, wire.properties([(7, b"1000000"), (12, b"123")])

    adapter.nim.request.side_effect = send
    with pytest.raises(wire.ProtocolError, match="management_reply_scope_changed"):
        await event.send(event.plain_result(response + "\n" + "x" * 9000))
    assert adapter.nim.request.await_count == 1
    key = "1/private/2/OTAy/message/0"
    assert (await adapter.ledger.receipt(key + "/part/0"))["status"] == "accepted"
    assert (await adapter.ledger.receipt(key))["status"] == "unknown"


@pytest.mark.asyncio
async def test_unrelated_group_revocation_does_not_block_selected_group_reply(delivery):
    service, engine, event = delivery
    adapter = event.platform
    response = await engine.run(event, "成员列表")
    await adapter.send_lock.lock.acquire()
    task = asyncio.create_task(event.send(event.plain_result(response)))
    try:
        await asyncio.wait_for(adapter.send_lock.waiting.wait(), 3)
        await revoke(service, adapter, "6")
        adapter.send_lock.lock.release()
        await asyncio.wait_for(task, 3)
        assert adapter.nim.request.await_count == 1
        assert (await adapter.ledger.receipt("1/private/2/OTAy/message/0"))[
            "status"
        ] == "accepted"
    finally:
        if adapter.send_lock.lock.locked():
            adapter.send_lock.lock.release()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["3", "Target"])
async def test_menu_parameter_reply_retains_management_delivery_guard(delivery, text):
    service, engine, event = delivery
    adapter = event.platform
    response = await engine.run(event, "成员列表")
    event.message_str = text
    await adapter.send_lock.lock.acquire()
    task = asyncio.create_task(event.send(event.plain_result(response)))
    try:
        await asyncio.wait_for(adapter.send_lock.waiting.wait(), 3)
        await revoke(service, adapter)
        adapter.send_lock.lock.release()
        with pytest.raises(wire.ProtocolError, match="management_reply_scope_changed"):
            await asyncio.wait_for(task, 3)
        adapter.nim.request.assert_not_awaited()
    finally:
        if adapter.send_lock.lock.locked():
            adapter.send_lock.lock.release()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_selected_group_revoked_before_send_cannot_fall_back_to_other_grant(
    delivery,
):
    service, engine, event = delivery
    response = await engine.run(event, "成员列表")
    await revoke(service, event.platform)
    assert service.management.allowed(event, "6")
    await event.send(event.plain_result(response))
    event.platform.nim.request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("selection_change", ["clear", "switch", "expire"])
@pytest.mark.parametrize("remaining_grant", [False, True])
async def test_generated_member_reply_never_rebinds_scope_before_send(
    delivery, selection_change, remaining_grant
):
    service, engine, event = delivery
    response = await engine.run(event, "成员列表")
    assert "Target" in response
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
    await event.send(event.plain_result(response))
    event.platform.nim.request.assert_not_awaited()
    assert await event.platform.ledger.receipt("1/private/2/OTAy/message/0") is None


@pytest.mark.asyncio
async def test_original_group_guard_survives_selecting_a_revoked_other_group(delivery):
    service, engine, event = delivery
    response = await engine.run(event, "成员列表")
    key = (id(event.platform), "1", "2", event.unified_msg_origin)
    engine.selections[key] = {"group": "6", "expires": time.monotonic() + 600}
    await revoke(service, event.platform, "6")
    await event.send(event.plain_result(response))
    assert event.platform.nim.request.await_count == 1


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
async def test_private_management_results_freeze_scope_at_real_dispatch(
    delivery, command, expected
):
    service, engine, event = delivery
    adapter = event.platform
    key = (id(adapter), "1", "2", event.unified_msg_origin)
    if command == "选择群 1":
        await engine.run(event, "群列表")
    elif command == "下一页":
        engine.selections[key].update(
            all_members=[{"userId": "3", "display_name": "Target"}], offset=0
        )
    response = await service.manager_command(event, command)
    assert expected in response
    engine.selections.clear()
    await revoke(service, adapter)
    await revoke(service, adapter, "6")
    await event.send(event.plain_result(response))
    adapter.nim.request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["成员列表", "选择群 1", "群列表"])
async def test_non_sensitive_denial_and_missing_selection_remain_deliverable(
    delivery, command
):
    service, engine, event = delivery
    adapter = event.platform
    engine.selections.clear()
    if command == "群列表":
        await revoke(service, adapter)
        await revoke(service, adapter, "6")
    response = await service.manager_command(event, command)
    assert response and "Target" not in response and "Group 5" not in response
    assert event.get_extra("_wsl_management_delivery_guard") is None
    await event.send(event.plain_result(response))
    assert adapter.nim.request.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target", ["groups", "members", "next", "back", "activity", "schedule"]
)
async def test_native_menu_chain_keeps_generated_result_scope(delivery, target):
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
    texts = []
    for number, text in enumerate(paths[target]):
        event = native_event(text, private=True)
        event.platform = adapter
        event.session_id = "1/private/2/OTAy"
        event.message_obj.session_id = event.session_id
        event.message_obj.message_id = f"menu-{target}-{number}"
        payload = event.get_extra("wangshangliao_payload")
        payload["_open_revision"] = group_revision(adapter.config, "")
        service.command_at = {k: v - 4 for k, v in service.command_at.items()}
        real_send = event.send
        final = number == len(paths[target]) - 1

        async def deliver(message, *, final=final, real_send=real_send, event=event):
            texts.append(message.chain[0].text)
            if final:
                engine.selections.clear()
                await revoke(service, adapter)
                await revoke(service, adapter, "6")
            await real_send(message)

        event.send = deliver
        adapter.nim.request.reset_mock()
        await service.on_event(event)
    if target == "back":
        assert "Target" not in texts[-1]
        assert adapter.nim.request.await_count == 1
    else:
        assert any(
            label in texts[-1] for label in ("Group 5", "Target", "奖品", "定时规则")
        )
        adapter.nim.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_list_does_not_require_native_admin_for_listing_only(delivery):
    service, engine, event = delivery
    adapter = event.platform
    tenant = service.store.tenant("1", "6")
    service.store.native_role("1", "6", tenant["epoch"], "GROUP_ROLE_MEMBER")
    await service.refresh()
    adapter.config.update(service.policy_values("1", adapter.config))
    event.message_str = "群列表"
    response = await engine.run(event, "群列表")
    assert "Group 5" in response and "Group 6" in response
    await event.send(event.plain_result(response))
    assert adapter.nim.request.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("boundary", ["lock", "segment"])
async def test_invitation_guard_rechecks_delivery_boundaries(
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
    task = asyncio.create_task(event.send(event.plain_result("x" * 9000)))
    try:
        if boundary == "lock":
            await asyncio.wait_for(adapter.send_lock.waiting.wait(), 3)
            state["allowed"] = False
            adapter.send_lock.lock.release()
        with pytest.raises(wire.ProtocolError, match="private_query_scope_changed"):
            await asyncio.wait_for(task, 3)
        assert adapter.nim.request.await_count == (boundary == "segment")
        if boundary == "segment":
            key = "1/private/2/OTAy/message/0"
            assert (await adapter.ledger.receipt(key + "/part/0"))[
                "status"
            ] == "accepted"
            assert (await adapter.ledger.receipt(key))["status"] == "unknown"
        assert state["checks"] > 1
    finally:
        if adapter.send_lock.lock.locked():
            adapter.send_lock.lock.release()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["你好", "菜单", "帮助", "我的邀请"])
async def test_ordinary_private_reply_does_not_require_manager_authority(
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
    assert adapter.nim.request.await_count == 1
    assert (await adapter.ledger.receipt("1/private/2/OTAy/message/0"))[
        "status"
    ] == "accepted"
