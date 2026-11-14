"""Offline native welcomes: corroboration, batching, authority and durable recall."""

import asyncio
import copy
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from pydantic import ValidationError

from astrbot.builtin_stars.wangshangliao_moderation import welcome
from astrbot.builtin_stars.wangshangliao_moderation.open_rpc import OpenCall, OpenRPC
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from astrbot.builtin_stars.wangshangliao_moderation.welcome import WelcomeConfig
from astrbot.core.platform.sources.wangshangliao import reply_recall
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from tests.test_wangshangliao_open_rpc_guards import rpc_scope as rpc_scope
from tests.test_wangshangliao_reply_recall import setup as setup


@pytest_asyncio.fixture
async def greeting(rpc_scope, monkeypatch):
    service, adapter, rows = rpc_scope
    clock = [round(time.time() - 10, 3)]
    monkeypatch.setattr(welcome.time, "time", lambda: clock[0])
    current = service.welcome
    current.started = clock[0] - 1
    tenant = service.store.tenant("99", "5")
    common = {"account": "99", "group_id": "5", "tenant_version": tenant["version"]}
    preview = current.control(
        {
            **common,
            "operation": "preview",
            "version": 0,
            "config": {"enabled": True, "text": "欢迎，祝交流愉快"},
        },
        "test",
        "session",
        tenant,
        lambda: None,
    )
    current.control(
        {**common, "operation": "commit", "token": preview["token"]},
        "test",
        "session",
        tenant,
        lambda: None,
    )
    clock[0] += 1
    adapter.stopping = asyncio.Event()
    adapter.supports_welcome_recall = True
    adapter.enabled_groups = {"5", "6"}
    adapter.groups = {"5": "1005", "6": "1006"}
    adapter.nim = SimpleNamespace()
    adapter.nim_account = "1099"
    adapter.diagnostics = SimpleNamespace(emit=Mock())
    adapter.send_text = AsyncMock(return_value="accepted")
    service.roster = AsyncMock(return_value={str(r["userId"]): r for r in rows})
    events = []

    async def logs(path, data):
        assert path == "/v1/group/get-apply-logs"
        return {"list": copy.deepcopy(events)}

    adapter.business.request = AsyncMock(side_effect=logs)
    monkeypatch.setattr(
        welcome, "BlacklistStore", lambda _: SimpleNamespace(read=lambda *a: (1, []))
    )
    service.member_evidence[("99", "5")] = {str(r["userId"]): r for r in rows}
    service.capabilities[("99", "5")] = {"checked_at": clock[0]}

    def join(uid="2", join_id="123", **changes):
        member = next((r for r in rows if str(r["userId"]) == uid), None)
        if member is None:
            member = {
                "userId": uid,
                "nimId": str(1000 + int(uid)),
                "userNick": "成员" + uid,
                "groupRole": "GROUP_ROLE_MEMBER",
            }
            rows.append(member)
        adapter.members["5"][uid] = member["nimId"]
        service.roster.return_value = {str(r["userId"]): r for r in rows}
        service.member_evidence[("99", "5")] = service.roster.return_value
        event = {
            "groupId": "5",
            "applyId": join_id,
            "state": "MEMBER_STATE_GOOD",
            "groupApplyType": "INVITATION",
            "applicant": member.copy(),
            "applyTime": datetime.fromtimestamp(clock[0], timezone.utc).isoformat(),
            **changes,
        }
        events.append(event)
        return {"event_at": int(clock[0] * 1000), "member_peers": [member["nimId"]]}

    original_sleep = asyncio.sleep

    async def no_sleep(_):
        await original_sleep(0)

    monkeypatch.setattr(welcome.asyncio, "sleep", no_sleep)
    try:
        yield current, service, adapter, clock, join, events, common
    finally:
        await current.close()


def test_config_default_disabled_and_strict():
    assert WelcomeConfig().enabled is False
    for data in [
        {"enabled": "true"},
        {"enabled": True, "text": " "},
        {"text": "x" * 301},
        {"text": "\x00"},
        {"recall_seconds": 0},
    ]:
        with pytest.raises(ValidationError):
            WelcomeConfig.model_validate(data)


@pytest.mark.asyncio
async def test_control_preview_scope_cas_and_no_business_changes(rpc_scope):
    service, _, _ = rpc_scope
    tenant = service.store.tenant("99", "5")
    before = service.store.tenants()
    rpc = OpenRPC(service)
    common = {"account": "99", "group_id": "5", "tenant_version": tenant["version"]}
    preview = await rpc.dispatch(
        OpenCall(
            action="group_welcome",
            actor="test",
            payload={
                **common,
                "operation": "preview",
                "version": 0,
                "config": {"enabled": True, "text": "欢迎"},
            },
        )
    )
    assert not service.welcome.configs
    with pytest.raises(OpenError, match="confirmation"):
        await rpc.dispatch(
            OpenCall(
                action="group_welcome",
                actor="other",
                payload={
                    **common,
                    "operation": "commit",
                    "token": preview["token"],
                },
            )
        )
    result = await rpc.dispatch(
        OpenCall(
            action="group_welcome",
            actor="test",
            payload={
                **common,
                "operation": "commit",
                "token": preview["token"],
            },
        )
    )
    assert result == {"saved": True, "version": 1}
    assert service.store.tenants() == before
    assert service.welcome.configs[("99", "5")]["config"]["text"] == "欢迎"
    with pytest.raises(OpenError, match="changed"):
        await rpc.dispatch(
            OpenCall(
                action="group_welcome",
                actor="test",
                payload={
                    **common,
                    "operation": "preview",
                    "version": 0,
                    "config": {},
                },
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 2, 3, 10])
async def test_one_message_for_concurrent_joins_real_mentions(greeting, count):
    current, _, adapter, _, join, _, _ = greeting
    for index in range(count):
        notice = join(str(index + 2), str(index + 100))
        current.receive(adapter, "5", str(index), notice)
    await asyncio.gather(*current.tasks.values())
    assert adapter.send_text.await_count == 1, (
        adapter.diagnostics.emit.call_args_list,
        current.pending,
        adapter.business.request.await_args_list,
        current.enabled(adapter, "5"),
        current.configs,
    )
    args, kwargs = adapter.send_text.await_args
    assert args[0] == "5"
    assert args[1].startswith("welcome/99/5/")
    assert kwargs["welcome_recall"] is True
    assert len(kwargs["mentions"]) == count
    encoded = args[2].encode("utf-16-le")
    for mention in kwargs["mentions"]:
        assert (
            encoded[mention["start"] * 2 : mention["end"] * 2].decode("utf-16-le")
            == f"@{mention['nick']} "
        )
    kwargs["authorize"]()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["old", "application", "left", "ambiguous", "peer", "disabled", "group"]
)
async def test_untrusted_or_disabled_admissions_do_not_send(greeting, case):
    current, service, adapter, clock, join, events, _ = greeting
    notice = join()
    if case == "old":
        notice["event_at"] -= 60000
    elif case == "application":
        events[0]["state"] = "MEMBER_STATE_WAIT"
    elif case == "left":
        service.roster.return_value.pop("2")
    elif case == "ambiguous":
        events.append({**events[0], "applyId": "124"})
    elif case == "peer":
        events[0]["applicant"] = {**events[0]["applicant"], "nimId": "untrusted"}
    elif case == "disabled":
        current.configs[("99", "5")]["config"]["enabled"] = False
    current.receive(adapter, "6" if case == "group" else "5", "123", notice)
    await asyncio.gather(*current.tasks.values())
    adapter.send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_join_and_restart_do_not_resend(greeting):
    current, service, adapter, _, join, _, _ = greeting
    notice = join()
    current.pending[("99", "5")] = {"123": notice}
    await current.send(adapter, "5")
    for mid in ["123", "124"]:
        current.pending[("99", "5")] = {mid: notice}
        await current.send(adapter, "5")
    assert adapter.send_text.await_count == 1
    restored = welcome.WelcomeMessages(service)
    restored.started = current.started
    restored.pending[("99", "5")] = {"125": notice}
    await restored.send(adapter, "5")
    assert adapter.send_text.await_count == 1
    await restored.close()


@pytest.mark.asyncio
async def test_guard_revocation_after_batch_prevents_send(greeting):
    current, service, adapter, _, join, _, _ = greeting
    current.pending[("99", "5")] = {"123": join()}
    service.roster.side_effect = lambda *a: service.settings.update(paused=True) or {}
    await current.send(adapter, "5")
    adapter.send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_large_batch_and_long_unicode_names_stay_one_native_message(greeting):
    current, service, adapter, _, join, _, _ = greeting
    notices = {}
    current.configs[("99", "5")]["config"]["text"] = "\U0001f642" * 300
    for index in range(25):
        uid = str(index + 2)
        notices[str(index)] = join(uid, str(index + 100))
        service.roster.return_value[uid]["userNick"] = "\U0001f642" * 32
    current.pending[("99", "5")] = notices
    await current.send(adapter, "5")
    assert adapter.send_text.await_count == 1
    args, kwargs = adapter.send_text.await_args
    assert 1 <= len(kwargs["mentions"]) <= 20
    assert "\n共25位新成员" in args[2]
    with current.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM welcome_admissions").fetchone()[0] == 25


@pytest.mark.asyncio
async def test_unknown_send_is_not_retried(greeting):
    current, _, adapter, _, join, _, _ = greeting
    notice = join()
    adapter.send_text.side_effect = TimeoutError()
    for mid in ["1", "2"]:
        current.pending[("99", "5")] = {mid: notice}
        await current.send(adapter, "5")
    assert adapter.send_text.await_count == 1
    with current.store.connect() as db:
        assert (
            db.execute("SELECT state FROM welcome_admissions").fetchone()[0]
            == "unknown"
        )


@pytest.mark.asyncio
async def test_welcome_twenty_seconds_and_new_acceptance_supersedes_old(setup):
    adapter, clock = setup
    first, second = "welcome/1/5/first", "welcome/1/5/second"
    await adapter.send_text("5", first, "Welcome A", welcome_recall=True)
    clock[0] += 5
    await adapter.send_text("5", "ordinary", "Warning", auto_recall=True)
    await adapter.send_text("5", second, "Welcome B", welcome_recall=True)
    async with adapter.ledger.db.execute(
        "SELECT key,deadline FROM reply_recalls ORDER BY key"
    ) as cursor:
        deadlines = dict(await cursor.fetchall())
    assert deadlines == {first: 1005, second: 1025, "ordinary": 1017}
    await reply_recall.recall_due(adapter)
    assert (
        len([c for c in adapter.nim.request.await_args_list if c.args[:2] == (7, 13)])
        == 1
    )
    clock[0] = 1024
    await reply_recall.recall_due(adapter)
    clock[0] = 1025
    await reply_recall.recall_due(adapter)
    async with adapter.ledger.db.execute("SELECT state FROM reply_recalls") as cursor:
        assert all(r[0] == "accepted" for r in await cursor.fetchall())


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["rejected", "unknown"])
async def test_failed_new_welcome_never_replaces_previous(setup, status):
    adapter, clock = setup
    await adapter.send_text("5", "welcome/1/5/first", "A", welcome_recall=True)
    clock[0] += 2
    adapter.nim.request = (
        AsyncMock(return_value=(400, b""))
        if status == "rejected"
        else AsyncMock(side_effect=TimeoutError())
    )
    if status == "unknown":
        with pytest.raises(TimeoutError):
            await adapter.send_text("5", "welcome/1/5/second", "B", welcome_recall=True)
    else:
        assert (
            await adapter.send_text("5", "welcome/1/5/second", "B", welcome_recall=True)
            == status
        )
    async with adapter.ledger.db.execute(
        "SELECT deadline FROM reply_recalls"
    ) as cursor:
        assert await cursor.fetchall() == [(1020,)]


@pytest.mark.asyncio
async def test_native_welcome_route_is_scoped_and_idempotent(setup):
    adapter, clock = setup
    with pytest.raises(ProtocolError, match="welcome_recall_scope"):
        await adapter.send_text("5", "unrelated", "Welcome", welcome_recall=True)
    await adapter.send_text("5", "welcome/1/5/a", "A", welcome_recall=True)
    clock[0] += 10
    await adapter.send_text("5", "welcome/1/5/a", "A", welcome_recall=True)
    async with adapter.ledger.db.execute(
        "SELECT deadline FROM reply_recalls"
    ) as cursor:
        assert await cursor.fetchall() == [(1020,)]
    assert adapter.nim.request.await_count == 1


@pytest.mark.asyncio
async def test_native_welcome_recall_survives_restart(setup):
    adapter, clock = setup
    await adapter.send_text("5", "welcome/1/5/a", "Welcome", welcome_recall=True)
    await adapter.ledger.close()
    await adapter.ledger.open()
    clock[0] = 1019
    await reply_recall.recall_due(adapter)
    assert adapter.nim.request.await_count == 1
    clock[0] = 1020
    await reply_recall.recall_due(adapter)
    assert adapter.nim.request.await_count == 2
    async with adapter.ledger.db.execute("SELECT state FROM reply_recalls") as cursor:
        assert await cursor.fetchall() == [("accepted",)]
