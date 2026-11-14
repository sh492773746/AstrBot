"""Public service boundary regressions using isolated stores and native events."""

import asyncio
import json
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot.api.message_components import Plain
from astrbot.builtin_stars.wangshangliao_moderation.group_ai import DEFAULT
from astrbot.builtin_stars.wangshangliao_moderation.open_service import (
    ActivationContext,
    OpenService,
)
from astrbot.builtin_stars.wangshangliao_moderation.open_store import (
    OpenError,
    OpenStore,
)
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.event import WangshangliaoEvent
from astrbot.core.platform.sources.wangshangliao.identity import ActorIdentity
from astrbot.core.platform.sources.wangshangliao.storage import Ledger
from tests.fixtures.wsl_native_conversations import conversation_manager
from tests.test_wangshangliao_group_ai import fake_http, model_scope
from tests.test_wangshangliao_open_rpc_guards import rpc_scope as rpc_scope


@pytest.fixture
def public(tmp_path, monkeypatch):
    context = SimpleNamespace(
        conversation_manager=conversation_manager(),
        platform_manager=SimpleNamespace(platform_insts=[]),
        get_config=lambda *args: {"admins_id": ["1"]},
    )
    plugin = SimpleNamespace(context=context, commands=SimpleNamespace(selections={}))
    service = OpenService(plugin, tmp_path)
    service.store.discover(
        "99", {"5": {"name": "test", "team": "1005"}}, allow_auto=True
    )
    monkeypatch.setattr(
        "astrbot.builtin_stars.wangshangliao_moderation.open_service.account_guard",
        lambda *args: lambda: None,
    )
    delivery = AsyncMock(return_value="accepted")
    monkeypatch.setattr(
        "astrbot.builtin_stars.wangshangliao_moderation.open_service.send_onboarding_text",
        delivery,
    )
    adapter = SimpleNamespace(
        account="99",
        nim_account="1099",
        config={"id": "fixture", "_business_epoch": 1},
        connection_state="online",
        groups={},
        group_names={},
        group_catalog={"5": {"team": "1005"}},
        members={},
        business=SimpleNamespace(request=AsyncMock()),
        send_reply_text=AsyncMock(),
        onboarding_reply=delivery,
        meta=lambda: PlatformMetadata("wangshangliao", "fixture", id="fixture"),
    )
    rows = [
        {"userId": "99", "nimId": "1099", "groupRole": "GROUP_ROLE_OWNER"},
        {
            "userId": "1",
            "nimId": "1001",
            "userNick": "manager",
            "groupRole": "GROUP_ROLE_ADMIN",
        },
    ]
    adapter.business.request.return_value = {"groupMemberInfo": rows}
    return service, adapter, rows


def chat_event(adapter, message="offline"):
    """Build an offline private transport envelope for retirement checks.

    Args:
        adapter: Authenticated synthetic account.
        message: Unique native message identifier.

    Returns:
        Private event with a transport-bound sender and persistent extra fields.
    """
    extras = {"_wsl_native_reply_result": "accepted"}
    return SimpleNamespace(
        platform=adapter,
        actor_identity=ActorIdentity("99", "1", "1001"),
        get_sender_id=lambda: "1",
        get_group_id=lambda: "",
        is_private_chat=lambda: True,
        unified_msg_origin=f"{adapter.config['id']}:FriendMessage:99/private/1001",
        message_obj=SimpleNamespace(message_id=message),
        message_str="hello",
        get_extra=extras.get,
        set_extra=lambda key, val: extras.update({key: val}),
        send=AsyncMock(),
        plain_result=lambda text: text,
        stop_event=Mock(),
    )


def invitation(*, identity="123", stamp=None, kind="INVITATION"):
    return {
        "groupId": "5",
        "applyId": identity,
        "groupApplyType": kind,
        "state": "MEMBER_STATE_GOOD",
        "applyTime": datetime.fromtimestamp(
            stamp or time.time() - 1, timezone.utc
        ).isoformat(),
        "applicant": {"userId": "99", "nimId": "1099"},
        "inviter": {"userId": "1"},
    }


@pytest.mark.asyncio
async def test_unmatched_private_uid_never_gets_manager_authority(public, monkeypatch):
    service, adapter, _ = public
    await service.refresh()
    event = chat_event(adapter)
    event.actor_identity = ActorIdentity("99", "1", "1999")
    assert not hasattr(service, "verify_actor")
    blocked = Mock(side_effect=AssertionError("Retired private event attempted IO"))
    with monkeypatch.context() as guard:
        guard.setattr(service.store, "connect", blocked)
        guard.setattr(service, "roster", blocked)
        await service.on_event(event)
    blocked.assert_not_called()
    assert event.actor_identity.verified_uid == ""
    assert not service.management.groups(event)
    event.stop_event.assert_called_once()
    event.send.assert_not_awaited()
    adapter.business.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_retired_private_identity_preserves_sender_without_verification(public):
    service, adapter, _ = public
    await service.refresh()
    event = chat_event(adapter)
    await service.on_event(event)
    with pytest.raises(wire.ProtocolError, match="private_identity_unverified"):
        event.actor_identity.require_verified()
    assert event.actor_identity.principal == "nim/1001"
    event.send.assert_not_awaited()
    event.stop_event.assert_called_once()
    adapter.business.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_candidate_path_activates_without_framework_methods(public):
    service, adapter, rows = public
    await service.refresh()

    async def request(route, _, **kwargs):
        return (
            {"list": [invitation()]}
            if route.endswith("get-apply-logs")
            else {"groupMemberInfo": rows}
        )

    adapter.business.request.side_effect = request
    inner = wire.ApplicationMessage(
        sender={"id": 1},
        target={"id": 5},
        session=2,
        format=0,
        content={"data": "开通"},
    )
    await service.candidate(
        adapter, "5", {1: "1005", 2: "1001", 7: str(time.time()), 12: "open"}, inner
    )
    assert service.store.tenant("99", "5")["managers"] == ["1"]
    adapter.onboarding_reply.assert_awaited_once()
    call = adapter.onboarding_reply.call_args
    assert call.args[:3] == (adapter, "5", "1005")
    assert not call.kwargs
    assert "本群已开通" in call.args[4]
    call.args[5]()
    adapter.send_reply_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("queued", [False, True])
@pytest.mark.parametrize("known_group", [False, True])
async def test_own_account_activation_never_replies_or_checks_membership(
    public, queued, known_group
):
    service, adapter, _ = public
    await service.refresh()
    before = service.store.tenant("99", "5")
    if not known_group:
        service.tenants.clear()
    service.discover = AsyncMock()
    service.roster = AsyncMock()
    service.activate = AsyncMock()
    inner = wire.ApplicationMessage(
        sender={"id": 99},
        target={"id": 5},
        session=2,
        format=0,
        content={"data": "开通"},
    )
    outer = {1: "1005", 2: "1099", 7: str(time.time()), 12: "own-message"}
    if queued:
        service.admit_candidate(adapter, "5", outer, inner)
        await asyncio.sleep(0)
        assert not service.candidate_tasks
    else:
        await service.candidate(adapter, "5", outer, inner)
    service.discover.assert_not_awaited()
    service.roster.assert_not_awaited()
    service.activate.assert_not_awaited()
    adapter.onboarding_reply.assert_not_awaited()
    adapter.business.request.assert_not_awaited()
    assert service.store.tenant("99", "5") == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("inviter_unproven", "还没确认是谁拉我进群"),
        ("permission_denied", "群主或群管理员"),
        ("original_manager_required", "原来的机器人管理员"),
        ("activation_changed", "这次没有开通"),
    ],
)
async def test_candidate_failure_replies_in_group_without_private_history(
    public, reason, expected
):
    service, adapter, _ = public
    await service.refresh()
    service.activate = AsyncMock(side_effect=OpenError(reason))
    inner = wire.ApplicationMessage(
        sender={"id": 1},
        target={"id": 5},
        session=2,
        format=0,
        content={"data": "开通"},
    )
    await service.candidate(
        adapter, "5", {1: "1005", 2: "1001", 7: str(time.time()), 12: "open"}, inner
    )
    call = adapter.onboarding_reply.call_args
    assert call.args[1:3] == ("5", "1005")
    assert expected in call.args[4]
    assert not any(word in call.args[4] for word in ("原生", "总控", "回执", "UID"))
    call.args[5]()
    adapter.send_reply_text.assert_not_awaited()
    assert not service.store.tenant("99", "5")["active"]


@pytest.mark.asyncio
async def test_candidate_never_replies_on_forged_transport(public):
    service, adapter, _ = public
    await service.refresh()
    inner = wire.ApplicationMessage(
        sender={"id": 1},
        target={"id": 5},
        session=2,
        format=0,
        content={"data": "开通"},
    )
    with pytest.raises(OpenError, match="membership_unproven"):
        await service.candidate(
            adapter, "5", {1: "1005", 2: "1002", 7: str(time.time()), 12: "open"}, inner
        )
    adapter.onboarding_reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_candidate_reply_cancelled_after_group_pause(public):
    service, adapter, _ = public
    await service.refresh()
    service.activate = AsyncMock(side_effect=OpenError("inviter_unproven"))
    inner = wire.ApplicationMessage(
        sender={"id": 1},
        target={"id": 5},
        session=2,
        format=0,
        content={"data": "开通"},
    )
    await service.candidate(
        adapter, "5", {1: "1005", 2: "1001", 7: str(time.time()), 12: "open"}, inner
    )
    guard = adapter.onboarding_reply.call_args.args[5]
    guard()
    service.tenants[("99", "5")]["paused"] = True
    with pytest.raises(OpenError, match="operation_revoked"):
        guard()


@pytest.mark.asyncio
async def test_activation_reply_survives_its_own_policy_publication(
    public, monkeypatch
):
    from astrbot.core.platform.sources.wangshangliao.database import account_guard

    service, adapter, rows = public
    monkeypatch.setattr(
        "astrbot.builtin_stars.wangshangliao_moderation.open_service.account_guard",
        account_guard,
    )
    await service.refresh()
    service.roster = AsyncMock(return_value={r["userId"]: r for r in rows})

    async def activate(_, group):
        service.store.prove_inviter("99", group, "1", "123", 1, time.time())
        service.store.activate("99", group, "1", epoch=1, version=1)
        await service.refresh()
        adapter.config["_business_epoch"] = 2
        return "本群已开通"

    service.activate = AsyncMock(side_effect=activate)
    inner = wire.ApplicationMessage(
        sender={"id": 1},
        target={"id": 5},
        session=2,
        format=0,
        content={"data": "开通"},
    )
    await service.candidate(
        adapter, "5", {1: "1005", 2: "1001", 7: str(time.time()), 12: "open"}, inner
    )
    assert adapter.onboarding_reply.call_args.args[4] == "本群已开通"
    adapter.onboarding_reply.call_args.args[5]()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["APPLY", "QUIT", "KICK"])
async def test_newer_membership_event_blocks_retained_inviter(public, kind):
    service, adapter, _ = public
    await service.refresh()
    adapter.business.request.return_value = {
        "list": [
            invitation(stamp=time.time() - 2),
            invitation(identity="124", stamp=time.time() - 1, kind=kind),
        ]
    }
    with pytest.raises(OpenError, match="inviter_unproven"):
        await service.prove(adapter, "5")
    assert service.store.tenant("99", "5")["managers"] == []


@pytest.mark.asyncio
async def test_activation_rechecks_members_after_invitation_io(public):
    service, adapter, rows = public
    service.store.prove_inviter("99", "5", "1", "123", 1, time.time())
    await service.refresh()
    service.roster = AsyncMock(
        side_effect=[
            {m["userId"]: m for m in rows},
            {m["userId"]: m for m in rows},
            {"99": rows[0]},
        ]
    )
    service.prove = AsyncMock()
    with pytest.raises(OpenError, match="permission_denied"):
        await service.activate(ActivationContext(adapter, "1", 1), "5")
    assert service.store.tenant("99", "5")["managers"] == []


def test_restart_preserves_pending_trusted_claim_but_not_unknown_history(tmp_path):
    store = OpenStore(tmp_path)
    catalog = {"5": {"name": "test", "team": "1005"}}
    store.discover("99", catalog, allow_auto=True)
    reopened = OpenStore(tmp_path)
    reopened.discover("99", catalog, allow_auto=False)
    assert reopened.tenant("99", "5")["auto_claim"] == 1
    reopened.discover("100", catalog, allow_auto=False)
    assert reopened.tenant("100", "5")["auto_claim"] == 0


@pytest.mark.asyncio
async def test_budget_default_denies_http_and_does_not_reserve(rpc_scope, monkeypatch):
    service, adapter, _ = rpc_scope
    tenant = service.store.tenant("99", "5")
    service.group_ai.configure(
        "99",
        "5",
        0,
        tenant["version"],
        {**DEFAULT, "enabled": True},
        "dashboard/test",
        guard=lambda: None,
        control=True,
    )
    await service.group_ai.refresh()
    service.resolve_model = Mock(
        side_effect=AssertionError("Closed budget resolved model")
    )
    requests = fake_http(monkeypatch)
    await service.group_ai.run(
        adapter,
        service.group_ai.configs[("99", "5")],
        "mention/no-budget/2/1002",
        "hello",
    )
    assert not requests
    assert service.store.usage()["used"] == 0
    service.resolve_model.assert_not_called()
    assert not service.context.conversation_manager.db.rows
    with service.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM ai_charges").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_invalid_model_does_not_consume_quota(rpc_scope, monkeypatch):
    service, adapter, _ = rpc_scope
    row = await model_scope(service, adapter)
    service.context.get_provider_by_id = lambda _: None
    requests = fake_http(monkeypatch)
    await service.group_ai.run(adapter, row, "mention/missing-model/2/1002", "hello")
    assert not requests
    adapter.send_text.assert_not_awaited()
    assert service.store.usage()["used"] == 0
    with service.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_model_credential_change_requires_new_hard_budget(rpc_scope, monkeypatch):
    service, adapter, _ = rpc_scope
    row = await model_scope(service, adapter)
    old = service.resolve_model()
    service.resolve_model = lambda selection=None: {**old, "api_key": "new"}
    requests = fake_http(monkeypatch)
    await service.group_ai.run(adapter, row, "mention/changed-key/2/1002", "hello")
    assert not requests
    adapter.send_text.assert_not_awaited()
    assert service.store.usage()["used"] == 0
    with service.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_group_close_cancels_inflight_and_revokes_late_delivery(
    rpc_scope, monkeypatch
):
    service, adapter, _ = rpc_scope
    row = await model_scope(service, adapter)
    waiting, release = asyncio.Event(), asyncio.Event()
    requests = fake_http(monkeypatch, barrier=(waiting, release))
    native = service.context.conversation_manager
    origin = (
        "rpc-offline:GroupMessage:99/5/wsl-ai/99/group/"
        + service.profiles.resolve("group", "99", "5")["fingerprint"]
        + "/mention/2/1002"
    )
    cid = await native.new_conversation(
        origin, content=[{"role": "user", "content": "existing group context"}]
    )
    service.group_ai.launch(adapter, row, "mention/cancelled-model/2/1002", "hello")
    tasks = list(service.group_ai.tasks.values())
    try:
        await asyncio.wait_for(waiting.wait(), 3)
        await service.group_ai.close()
    finally:
        release.set()
        await service.group_ai.close()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert len(requests) == 1 and len(tasks) == 1
    assert tasks[0].cancelled()
    assert not service.group_ai.tasks and service.chat_count == 0
    adapter.send_text.assert_not_awaited()
    assert json.loads((await native.get_conversation(origin, cid)).history) == [
        {"role": "user", "content": "existing group context"}
    ]
    dialogue = await service.conversations.load(origin, "test", "", fresh=True)
    assert dialogue.history == [] and dialogue.cid is None
    with service.store.connect() as db:
        assert db.execute("SELECT state FROM calls").fetchone()[0] == "unknown"


@pytest.mark.asyncio
async def test_cancelled_durable_reservation_releases_unsent_quota(
    rpc_scope, monkeypatch
):
    service, adapter, _ = rpc_scope
    row = await model_scope(service, adapter)
    requests = fake_http(monkeypatch)
    entered, release = asyncio.Event(), threading.Event()
    loop, reserve = asyncio.get_running_loop(), service.group_ai.reserve

    def delayed(*args, **kwargs):
        result = reserve(*args, **kwargs)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        return result

    monkeypatch.setattr(service.group_ai, "reserve", delayed)
    service.group_ai.launch(
        adapter, row, "mention/cancelled-reservation/2/1002", "hello"
    )
    task = next(iter(service.group_ai.tasks.values()))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()
        assert service.store.usage()["used"] == 0
        assert service.chat_count == 0 and not service.group_ai.tasks
        assert not requests and not service.context.conversation_manager.db.rows
        adapter.send_text.assert_not_awaited()
        with service.store.connect() as db:
            assert db.execute("SELECT state FROM calls").fetchone()[0] == "not_sent"
            assert (
                db.execute("SELECT state FROM ai_charges").fetchone()[0] == "not_sent"
            )
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_group_overflow_holds_automatic_review_durably(tmp_path):
    ledger = Ledger(tmp_path / "bounded.sqlite3")
    await ledger.open()
    try:
        await ledger.db.executemany(
            "INSERT INTO inbox(account,team,message,payload,state,sender,received_at) "
            "VALUES('99','5',?,'{}','pending','1',?)",
            [(str(i), time.time()) for i in range(4096)],
        )
        await ledger.db.commit()
        await ledger.ingest("99", "5", "overflow", {"sender": "1", "text": "evidence"})
        assert ledger.review_groups == {"5"}
        async with ledger.db.execute(
            "SELECT state,payload FROM inbox WHERE message='overflow'"
        ) as cursor:
            state, payload = await cursor.fetchone()
        assert state == "queue_full" and "evidence" not in payload
        await ledger.mark("99", "5", "0", "processed")
        await ledger.ingest("99", "5", "new", {"sender": "1", "text": "new"})
        async with ledger.db.execute(
            "SELECT payload FROM inbox WHERE message='new'"
        ) as cursor:
            assert json.loads((await cursor.fetchone())[0])["_audit_incomplete"]
        await ledger.close()
        await ledger.open()
        assert ledger.review_groups == {"5"}
        with pytest.raises(wire.ProtocolError, match="review_unresolved"):
            await ledger.resume_review("99", "5", guard=lambda: None)
        await ledger.db.execute(
            "UPDATE inbox SET state='processed' WHERE state='pending'"
        )
        await ledger.db.execute(
            "UPDATE review_holds SET created=?", (time.time() - 301,)
        )
        await ledger.db.commit()
        await ledger.resume_review("99", "5", guard=lambda: None)
        assert ledger.review_groups == set()
    finally:
        await ledger.close()


def test_transport_alias_does_not_reset_daily_quota(tmp_path, monkeypatch):
    anchor = datetime(2026, 10, 17, 4, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(time, "time", lambda: anchor)
    store = OpenStore(tmp_path)
    now = time.time()
    for index in range(20):
        store.reserve("1", str(index), now=now - 200 + 6 * index)
    store.link_principal("1", "nim/1001")
    with pytest.raises(OpenError, match="personal_quota"):
        store.reserve("nim/1001", "extra", now=now)
    with pytest.raises(OpenError, match="private_identity_unverified"):
        store.link_principal("2", "nim/1001")


def test_retention_preserves_uncertain_calls_and_onboarding_independent(tmp_path):
    store = OpenStore(tmp_path)
    old = time.time() - 91 * 86400
    store.reserve("1", "uncertain", now=old)
    store.finish("uncertain", "unknown")
    store.reserve("2", "finished", now=old)
    store.finish("finished", "completed")
    store.prune()
    with store.connect() as db:
        assert [r[0] for r in db.execute("SELECT request FROM calls")] == ["uncertain"]


@pytest.mark.asyncio
async def test_private_backlog_is_dropped_despite_claimed_uid_rotation(tmp_path):
    ledger = Ledger(tmp_path / "inbox.sqlite3")
    await ledger.open()
    try:
        for index in range(20):
            assert not await ledger.ingest(
                "99",
                f"private/{index + 1}/MTAwMQ==",
                str(index),
                {
                    "text": "管理 群列表",
                    "sender": str(index + 1),
                    "transport_peer": "1001",
                },
            )
        async with ledger.db.execute(
            "SELECT state,COUNT(*) FROM inbox GROUP BY state"
        ) as cursor:
            assert await cursor.fetchall() == []
        assert not ledger.review_groups
    finally:
        await ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("length", [5, 16384, 16385])
async def test_private_text_is_never_stored_without_widening_decoder(tmp_path, length):
    ledger = Ledger(tmp_path / "inbox.sqlite3")
    await ledger.open()
    try:
        observer = Mock()
        assert not await ledger.ingest(
            "99",
            "private/1/MTAwMQ==",
            "huge",
            {"text": "x" * length, "sender": "1", "transport_peer": "1001"},
            observe=observer,
        )
        async with ledger.db.execute("SELECT state,payload FROM inbox") as cursor:
            assert await cursor.fetchone() is None
        observer.assert_not_called()
        assert not ledger.review_groups
        assert wire.LIMIT == 1024 * 1024
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_event_without_verified_uid_cannot_authorize_financial_query():
    message = AstrBotMessage()
    message.type = MessageType.FRIEND_MESSAGE
    message.self_id = "99"
    message.session_id = "99/private/1/MTAwMQ=="
    message.sender = MessageMember("1", "manager")
    message.message_str = "我的邀请"
    message.message = [Plain("我的邀请")]
    message.raw_message = {"_transport_peer": "1001"}
    event = WangshangliaoEvent(
        message,
        SimpleNamespace(
            account="99",
            config={},
            meta=lambda: PlatformMetadata("wangshangliao", "test", id="fixture"),
        ),
        True,
    )
    with pytest.raises(wire.ProtocolError):
        event.actor_identity.require_verified()
    from astrbot.core.star.filter.permission import PermissionType, PermissionTypeFilter

    event.role = "admin"
    permission = PermissionTypeFilter(PermissionType.ADMIN)
    assert not event.is_admin()
    assert not permission.filter(event, {})
    event.actor_identity = ActorIdentity("99", "1", "1001", "1")
    assert event.is_admin() and permission.filter(event, {})
    event.actor_identity = ActorIdentity("98", "1", "1001", "1")
    assert not event.is_admin()


@pytest.mark.asyncio
async def test_public_event_never_enters_framework_command_filters(monkeypatch):
    from astrbot.core.platform.sources.wangshangliao.runtime_policy import runtime

    message = AstrBotMessage()
    message.type = MessageType.FRIEND_MESSAGE
    message.self_id = "99"
    message.session_id = "99/private/1/MTAwMQ=="
    message.sender = MessageMember("1", "manager")
    message.message_str = "/plugin off"
    message.message = [Plain(message.message_str)]
    message.raw_message = {"_transport_peer": "1001"}
    event = WangshangliaoEvent(
        message,
        SimpleNamespace(
            account="99",
            config={},
            meta=lambda: PlatformMetadata("wangshangliao", "test", id="fixture"),
        ),
        True,
    )
    event.actor_identity = ActorIdentity("99", "1", "1001", "1")
    monkeypatch.setattr(runtime, "open_service", object())
    registry = Mock()
    assert not await event.prepare_waking_check({"admins_id": ["1"]}, registry)
    assert not registry.mock_calls
    assert event.is_stopped()
    assert event.role == "member" and not event.is_admin()
    assert event.get_extra("_context_only") is True
