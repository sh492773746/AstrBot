"""Offline authorization and lifecycle audit regressions for 2026-10-19."""

import asyncio
import copy
import json
import re
import sqlite3
import threading
import time
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import astrbot.core as astrbot_core
from astrbot.api.message_components import At
from astrbot.builtin_stars.wangshangliao_moderation import group_management
from astrbot.builtin_stars.wangshangliao_moderation.ai_config import AdminConfigDrafts
from astrbot.builtin_stars.wangshangliao_moderation.cashback_finance import FinanceActor
from astrbot.builtin_stars.wangshangliao_moderation.cashback_store import CashbackError
from astrbot.builtin_stars.wangshangliao_moderation.conversation_profiles import (
    DEFAULT as PROFILE_DEFAULT,
)
from astrbot.builtin_stars.wangshangliao_moderation.group_management import (
    GroupManagement,
)
from astrbot.builtin_stars.wangshangliao_moderation.lifecycle import (
    clear_shared_group_data,
)
from astrbot.builtin_stars.wangshangliao_moderation.main import Main
from astrbot.builtin_stars.wangshangliao_moderation.migration import (
    backup,
    migrate_cashback_collector,
    recover_cashback_migration_entries,
)
from astrbot.builtin_stars.wangshangliao_moderation.open_rpc import (
    OpenCall,
    OpenRPC,
    call_open,
)
from astrbot.builtin_stars.wangshangliao_moderation.open_service import (
    ActivationContext,
)
from astrbot.builtin_stars.wangshangliao_moderation.open_store import (
    OpenError,
    OpenStore,
)
from astrbot.core.platform.sources.wangshangliao.event import WangshangliaoEvent
from astrbot.core.platform.sources.wangshangliao.identity import ActorIdentity
from tests.test_wangshangliao_ai_plans import setup_billing
from tests.test_wangshangliao_cashback import (
    CASHIER,
    OWNER,
    member,
    replacement_roster,
    select,
)
from tests.test_wangshangliao_cashback import (
    migration_recovery as migration_recovery,
)
from tests.test_wangshangliao_cashback import (
    store as store,
)
from tests.test_wangshangliao_group_ai import fake_http
from tests.test_wangshangliao_integrated_service import service
from tests.test_wangshangliao_lifecycle import lifecycle as lifecycle
from tests.test_wangshangliao_lifecycle import notice
from tests.test_wangshangliao_plain_commands import native_event


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    """Reject accidental network connections and disable metric persistence.

    Args:
        monkeypatch: Test-scoped environment and connection overrides.
    """
    monkeypatch.setenv("ASTRBOT_DISABLE_METRICS", "1")

    async def reject_connection(*args, **kwargs):
        raise AssertionError("audit_network_forbidden")

    monkeypatch.setattr(asyncio.BaseEventLoop, "create_connection", reject_connection)


def scoped_service(tmp_path):
    """Build two accounts and groups backed only by temporary tenant stores.

    Args:
        tmp_path: Isolated test directory.

    Returns:
        Integrated service, offline adapter and synthetic member records.
    """
    value = service(tmp_path)
    rows = {
        "99": {"userId": "99", "nimId": "1099", "groupRole": "GROUP_ROLE_ADMIN"},
        "1": {"userId": "1", "nimId": "1001", "groupRole": "GROUP_ROLE_OWNER"},
        "2": {"userId": "2", "nimId": "1002", "groupRole": "GROUP_ROLE_MEMBER"},
    }
    for account in ("99", "199"):
        value.store.discover(
            account,
            {g: {"name": "Offline " + g, "team": "100" + g} for g in ("5", "6")},
        )
        for group in ("5", "6"):
            value.store.import_group(
                account,
                group,
                {"advertising": True, "flood": True, "keywords": []},
                ["1", "2"],
                "system/offline",
            )
            value.store.native_role(account, group, 1, "GROUP_ROLE_ADMIN")
    adapter = SimpleNamespace(
        account="99",
        nim_account="1099",
        config={
            "id": "offline",
            "enabled_groups": ["5", "6"],
            "_business_active": True,
            "_business_epoch": 1,
            "_open_native_roles": dict.fromkeys(("5", "6"), "GROUP_ROLE_ADMIN"),
        },
        members={
            group: {uid: row["nimId"] for uid, row in rows.items()}
            for group in ("5", "6")
        },
        dispatch_wakeup=asyncio.Event(),
        meta=lambda: SimpleNamespace(name="wangshangliao", id="offline"),
    )

    async def roster(owner, group, **kwargs):
        assert owner is adapter and group in {"5", "6"}
        result = copy.deepcopy(rows)
        value.member_evidence[("99", group)] = result
        return result

    value.roster = roster
    return value, adapter, rows


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [1, 8])
async def test_activation_commit_rechecks_inflight_membership_revocation(
    lifecycle, monkeypatch, kind
):
    value, adapter, _ = lifecycle
    await value.refresh()
    await value.check_inviter(adapter, "5")
    waiting, release = threading.Event(), threading.Event()
    original_log = value.store.log

    def block_activation(*args, **kwargs):
        result = original_log(*args, **kwargs)
        if args[4] == "activated":
            waiting.set()
            if not release.wait(10):
                raise TimeoutError("offline_activation_barrier")
        return result

    monkeypatch.setattr(value.store, "log", block_activation)
    activation = asyncio.create_task(
        value.activate(ActivationContext(adapter, "1", 1), "5")
    )
    received = None
    try:
        assert await asyncio.to_thread(waiting.wait, 10)
        received = asyncio.create_task(
            value.receive_membership(adapter, notice(kind, message="9001"))
        )
        for _ in range(1000):
            if value.membership_receiving.get(("99", "5")):
                break
            await asyncio.to_thread(lambda: None)
        assert value.membership_receiving.get(("99", "5"))
        release.set()
        result = await asyncio.gather(activation, return_exceptions=True)
        await received
        assert isinstance(result[0], OpenError)
        tenant = value.store.tenant("99", "5")
        assert not tenant["active"] and tenant["managers"] == []
    finally:
        release.set()
        await asyncio.gather(
            activation, *([received] if received else []), return_exceptions=True
        )


@pytest.mark.asyncio
async def test_activation_store_rejects_pending_departure(lifecycle):
    value, adapter, _ = lifecycle
    await value.refresh()
    await value.check_inviter(adapter, "5")
    value.store.membership_notice("99", "1005", "9002", "kick", time.time())
    tenant = value.store.tenant("99", "5")
    with pytest.raises(OpenError):
        value.store.activate(
            "99", "5", "1", epoch=tenant["epoch"], version=tenant["version"]
        )
    assert not value.store.tenant("99", "5")["active"]


@pytest.mark.asyncio
async def test_activation_commit_rechecks_latest_actor_role(lifecycle, monkeypatch):
    value, adapter, _ = lifecycle
    await value.refresh()
    await value.check_inviter(adapter, "5")
    waiting, release = threading.Event(), threading.Event()
    original_log = value.store.log

    def block_activation(*args, **kwargs):
        result = original_log(*args, **kwargs)
        if args[4] == "activated":
            waiting.set()
            if not release.wait(10):
                raise TimeoutError("offline_actor_barrier")
        return result

    monkeypatch.setattr(value.store, "log", block_activation)
    activation = asyncio.create_task(
        value.activate(ActivationContext(adapter, "1", 1), "5")
    )
    try:
        assert await asyncio.to_thread(waiting.wait, 10)
        value.member_evidence[("99", "5")]["1"]["groupRole"] = "GROUP_ROLE_MEMBER"
        release.set()
        result = await asyncio.gather(activation, return_exceptions=True)
        assert isinstance(result[0], OpenError)
        assert not value.store.tenant("99", "5")["active"]
    finally:
        release.set()
        await asyncio.gather(activation, return_exceptions=True)


@pytest.mark.asyncio
async def test_rpc_postcommit_cancel_reconciles_revoked_manager(tmp_path, monkeypatch):
    value, adapter, _ = scoped_service(tmp_path)
    await value.refresh()
    value.adapters = lambda: [adapter]
    tenant = value.store.tenant("99", "5")
    untouched = [
        copy.deepcopy(t)
        for t in value.store.tenants()
        if (t["account"], t["group_id"]) != ("99", "5")
    ]
    payload = {
        "account": "99",
        "group_id": "5",
        "version": tenant["version"],
        "rules": tenant["rules"],
        "managers": ["1"],
        "paused": False,
    }
    payload["confirmation"] = value.store.preview_control(
        "99",
        "5",
        tenant["version"],
        "dashboard/offline",
        tenant["rules"],
        ["1"],
        False,
    )
    committed, release = threading.Event(), threading.Event()
    original_change = value.store.change

    def committed_change(*args, **kwargs):
        original_change(*args, **kwargs)
        committed.set()
        if not release.wait(10):
            raise TimeoutError("offline_postcommit_barrier")

    monkeypatch.setattr(value.store, "change", committed_change)
    value.refresh = AsyncMock(wraps=value.refresh)
    event = SimpleNamespace(
        platform=adapter,
        actor_identity=ActorIdentity("99", "2", "1002", "2"),
        is_private_chat=lambda: True,
        get_sender_id=lambda: "2",
        get_extra=lambda _: None,
    )
    assert value.management.allowed(event, "5")
    request = asyncio.create_task(
        OpenRPC(value).dispatch(
            OpenCall(action="tenant", actor="dashboard/offline", payload=payload)
        )
    )
    try:
        assert await asyncio.to_thread(committed.wait, 10)
        request.cancel()
        await asyncio.sleep(0)
        denied_while_draining = not value.management.allowed(event, "5")
        release.set()
        result = await asyncio.gather(request, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)
        assert value.store.tenant("99", "5")["managers"] == ["1"]
        assert not value.management.allowed(event, "5")
        assert denied_while_draining
        assert value.management.allowed(event, "6")
        assert [
            t
            for t in value.store.tenants()
            if (t["account"], t["group_id"]) != ("99", "5")
        ] == untouched
    finally:
        release.set()
        await asyncio.gather(request, return_exceptions=True)


@pytest.mark.asyncio
async def test_grant_reload_serializes_with_revocation(tmp_path, monkeypatch):
    value = GroupManagement(
        SimpleNamespace(get_config=lambda *args: {"admins_id": []}), tmp_path
    )
    adapter = SimpleNamespace(
        account="99",
        config={"id": "offline", "enabled_groups": ["5"]},
        get_moderation_members=AsyncMock(
            return_value={
                "complete": True,
                "groupMemberInfo": [
                    {
                        "userId": "2",
                        "nimId": "902",
                        "groupRole": "GROUP_ROLE_ADMIN",
                    }
                ],
            }
        ),
    )
    await value.save(adapter, "5", ["2"], 0, "offline", lambda: None)
    captured, release = asyncio.Event(), asyncio.Event()
    original_fetchall = group_management.AsyncDatabase.fetchall

    async def captured_read(db, sql, parameters=()):
        rows = await original_fetchall(db, sql, parameters)
        if sql.startswith("SELECT instance,account,group_id,version,users"):
            captured.set()
            await release.wait()
        return rows

    monkeypatch.setattr(group_management.AsyncDatabase, "fetchall", captured_read)
    loading = asyncio.create_task(value.load())
    await asyncio.wait_for(captured.wait(), 10)
    revoking = asyncio.create_task(
        value.save(adapter, "5", [], 1, "offline", lambda: None)
    )
    try:
        await asyncio.wait({revoking}, timeout=0.2)
    finally:
        release.set()
        await asyncio.gather(loading, revoking)
    key = value.key(adapter, "5")
    assert value.assignments[key] == ()
    assert value.versions[key] == 2


@pytest.mark.asyncio
async def test_role_transition_reconciliation_survives_own_event_cancel(tmp_path):
    value, adapter, _ = scoped_service(tmp_path)
    await value.refresh()
    adapter.dispatch_wakeup = asyncio.Event()
    adapter.meta = lambda: SimpleNamespace(name="wangshangliao", id="offline")
    adapter.events = []
    adapter.workers = {}
    value.adapters = lambda: [adapter]
    entered, finished = asyncio.Event(), asyncio.Event()
    completion = asyncio.get_running_loop().create_future()

    async def command():
        await entered.wait()
        await value.suspend_group(adapter, "5", retain=True)

    task = asyncio.create_task(command())
    event = SimpleNamespace(
        message_obj=SimpleNamespace(group_id="5"),
        processing_completion=completion,
        processing_task=task,
    )
    adapter.events.append(event)
    completion.add_done_callback(
        lambda future: task.cancel() if future.cancelled() else None
    )
    entered.set()
    try:
        result = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
        finished.set()
        assert isinstance(result[0], asyncio.CancelledError)
        assert ("99", "5") not in value.suspended_groups
        assert value.tenants[("99", "6")]["active"]
    finally:
        if not finished.is_set():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def test_backup_contains_integrated_tenant_authorization_store(tmp_path):
    data = tmp_path / "data"
    root = data / "plugin_data" / "wangshangliao_open"
    store = OpenStore(root)
    store.discover("99", {"5": {"name": "Offline", "team": "1005"}})
    path = store.path
    destination = tmp_path / "backup"
    manifest = backup(data, destination)
    relative = path.relative_to(data)
    assert str(relative) in {entry["path"] for entry in manifest["files"]}
    with closing(sqlite3.connect(destination / relative)) as db:
        assert db.execute(
            "SELECT account,group_id,managers FROM tenants"
        ).fetchall() == [("99", "5", "[]")]


@pytest.mark.asyncio
async def test_legacy_compatibility_entrypoints_are_inert():
    class Unreadable:
        def __getattribute__(self, name):
            raise AssertionError("retired_entrypoint_read_payload")

    drafts = AdminConfigDrafts(Unreadable(), Unreadable(), Unreadable())
    for method in (drafts.preview, drafts.confirm):
        assert json.loads(await method(Unreadable(), Unreadable())) == {
            "status": "rejected",
            "reason": "ai_management_retired",
        }
    with pytest.raises(OpenError, match="isolated_mode_retired"):
        await call_open(Unreadable(), Unreadable(), Unreadable())
    assert (await OpenRPC(Unreadable()).invoke(Unreadable())).status == 410


@pytest.mark.parametrize(
    "identity",
    [
        None,
        ActorIdentity("99", "2", "forged-peer"),
        ActorIdentity("199", "2", "1002", "2"),
    ],
)
def test_legacy_group_grants_reject_unverified_actor(tmp_path, identity):
    management = GroupManagement(
        SimpleNamespace(get_config=lambda *args: {"admins_id": []}), tmp_path
    )
    adapter = SimpleNamespace(
        account="99", config={"id": "offline", "enabled_groups": ["5"]}
    )
    management.assignments[management.key(adapter, "5")] = ("2",)
    event = SimpleNamespace(
        platform=adapter,
        actor_identity=identity,
        get_sender_id=lambda: "2",
        get_extra=lambda _: None,
        is_private_chat=lambda: True,
        is_admin=lambda: False,
    )
    assert not management.allowed(event, "5")


@pytest.fixture
def unrelated_finance_actor(store):
    """Grant a synthetic financial principal authority over a different group.

    Args:
        store: Temporary financial ledger with the target group already configured.

    Returns:
        Financial administrator restricted to account 199 and group 6.
    """
    store.configure(
        OWNER,
        {
            "id": "6",
            "name": "Offline",
            "bot_id": "other",
            "account": "199",
            "cashier": CASHIER,
            "enabled": True,
            "rate": 1800,
            "version": 0,
        },
        {"2": member("2")},
        set(),
    )
    with store.connect(True) as db:
        db.execute(
            "INSERT INTO chat_finance_grants VALUES"
            "('199','6','2','1002','Offline','finance_admin',1,1)"
        )
    return FinanceActor("199", "2", "1002", "6", 1)


def test_maintenance_collector_migration_rejects_other_group_actor(
    store, unrelated_finance_actor
):
    before = store.get_group("5")
    with pytest.raises(CashbackError, match="finance_forbidden"):
        migrate_cashback_collector(
            store, unrelated_finance_actor, "5", "bot-a", "99", 1, replacement_roster()
        )
    assert store.get_group("5") == before


def test_maintenance_recovery_rejects_other_group_actor(
    migration_recovery, unrelated_finance_actor
):
    store, roster, _ = migration_recovery
    item = select(store, "4")
    with pytest.raises(CashbackError, match="finance_forbidden"):
        recover_cashback_migration_entries(
            store, unrelated_finance_actor, "5", "bot-a", "99", 2, roster, [item]
        )
    assert select(store, "4") == item


@pytest.fixture
def private_chat_scope(tmp_path):
    """Provide a private consultation with only an in-memory HTTP test double.

    Args:
        tmp_path: Temporary service and billing storage.

    Returns:
        Service and synthetic private event, without native transport methods.
    """
    value, adapter, rows = scoped_service(tmp_path)
    value.model = {"provider_id": "existing/model"}
    value.profiles.save("consultation", "99", "", 0, PROFILE_DEFAULT, "offline/fixture")
    setup_billing(value)
    value.store.confirm_budget(
        value.budget["version"],
        True,
        "existing/model",
        "offline cap",
        "audit",
        binding=value.store.model_binding(value.resolve_model()),
    )
    value.budget = value.store.budget()
    value.store.remember_roster("99", rows)
    value.notify_quota = AsyncMock()
    extras = {"_wsl_native_reply_result": "accepted"}
    event = SimpleNamespace(
        platform=adapter,
        actor_identity=ActorIdentity("99", "1", "1001", "1"),
        get_sender_id=lambda: "1",
        is_private_chat=lambda: True,
        eligible=True,
        message_str="offline question",
        unified_msg_origin="offline:FriendMessage:99/private/1001",
        message_obj=SimpleNamespace(message_id="offline-chat"),
        get_extra=extras.get,
        set_extra=lambda key, value: extras.__setitem__(key, value),
        send=AsyncMock(),
        plain_result=lambda text: text,
    )
    return value, event


@pytest.mark.asyncio
@pytest.mark.parametrize("publish", [False, True])
async def test_budget_revocation_suppresses_late_private_reply_and_history(
    private_chat_scope, monkeypatch, publish
):
    value, event = private_chat_scope
    entered, release = asyncio.Event(), asyncio.Event()
    fake_http(monkeypatch, content="offline late answer", barrier=(entered, release))
    task = asyncio.create_task(value.chat(event))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        value.store.confirm_budget(
            value.budget["version"],
            False,
            "existing/model",
            "offline revoke",
            "audit",
            binding=value.budget["binding"],
        )
        if publish:
            value.budget = value.store.budget()
    finally:
        release.set()
        await asyncio.wait_for(task, 5)
    event.send.assert_not_awaited()
    assert all(
        not row.content for row in value.context.conversation_manager.db.rows.values()
    )
    assert value.store.usage()["used"] == 1
    with value.store.connect() as db:
        assert db.execute("SELECT state FROM ai_charges").fetchone()[0] == "settled"


@pytest.mark.asyncio
async def test_exit_fences_a_pending_native_history_append(
    private_chat_scope, monkeypatch
):
    value, event = private_chat_scope
    fake_http(monkeypatch, content="offline old answer")
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_save = value.conversations.save

    async def delayed_save(*args, **kwargs):
        entered.set()
        try:
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return await original_save(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(value.conversations, "save", delayed_save)
    task = asyncio.create_task(value.chat(event))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await value.end_chat("99", "nim/1001")
        assert all(
            not row.content
            for row in value.context.conversation_manager.db.rows.values()
        )
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert finished.is_set()
        assert all(
            not row.content
            for row in value.context.conversation_manager.db.rows.values()
        )
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_budget_revocation_at_private_delivery_boundary(
    private_chat_scope, monkeypatch
):
    value, event = private_chat_scope
    fake_http(monkeypatch, content="offline queued answer")
    entered, release = asyncio.Event(), asyncio.Event()
    delivered = []

    async def delayed_delivery(message):
        entered.set()
        await release.wait()
        guard = event.get_extra("_wsl_management_delivery_guard")
        if callable(guard):
            guard()
        delivered.append(message)

    event.send = delayed_delivery
    task = asyncio.create_task(value.chat(event))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        value.store.confirm_budget(
            value.budget["version"],
            False,
            "existing/model",
            "offline revoke",
            "audit",
            binding=value.budget["binding"],
        )
    finally:
        release.set()
        await asyncio.wait_for(task, 5)
    assert delivered == []
    assert all(
        not row.content for row in value.context.conversation_manager.db.rows.values()
    )


@pytest.mark.asyncio
async def test_native_event_delivers_with_latest_budget_authorization(
    private_chat_scope, monkeypatch
):
    value, template = private_chat_scope
    fake_http(monkeypatch, content="offline queued native answer")
    message = native_event(template.message_str, private=True).message_obj
    message.self_id = "99"
    message.session_id = "99/private/1/MTAwMQ=="
    message.sender.user_id = "1"
    message.raw_message = {"_transport_peer": "1001"}
    event = WangshangliaoEvent(message, template.platform, True)
    entered, release = asyncio.Event(), asyncio.Event()
    delivered, prior_checks = [], []

    def previous_delivery():
        prior_checks.append(True)

    event.set_extra("_wsl_management_delivery_guard", previous_delivery)

    async def offline_transport(target, key, text, *, authorize, **kwargs):
        entered.set()
        await release.wait()
        await authorize()
        delivered.append(text)
        return "accepted"

    event.platform.send_reply_text = offline_transport
    task = asyncio.create_task(value.chat(event))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        value.store.confirm_budget(
            value.budget["version"],
            False,
            "existing/model",
            "offline revoke",
            "audit",
            binding=value.budget["binding"],
        )
    finally:
        release.set()
        await asyncio.wait_for(task, 5)
    assert prior_checks == [True]
    assert delivered == []
    assert all(
        not row.content for row in value.context.conversation_manager.db.rows.values()
    )
    assert event.get_extra("_wsl_management_delivery_guard") is previous_delivery


@pytest.mark.asyncio
async def test_two_group_events_share_suspension_without_wait_cycle(tmp_path):
    value, adapter, _ = scoped_service(tmp_path)
    await value.refresh()
    adapter.events = []
    adapter.workers = {}
    ready = asyncio.Event()
    tasks = []

    async def command():
        await ready.wait()
        await value.suspend_group(adapter, "5", retain=True)

    for _ in range(2):
        task = asyncio.create_task(command())
        completion = asyncio.get_running_loop().create_future()
        completion.add_done_callback(
            lambda future, task=task: task.cancel() if future.cancelled() else None
        )
        adapter.events.append(
            SimpleNamespace(
                message_obj=SimpleNamespace(group_id="5"),
                processing_task=task,
                processing_completion=completion,
            )
        )
        tasks.append(task)
    ready.set()
    outcomes = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
    assert all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes)
    assert ("99", "5") not in value.suspended_groups
    assert not value.suspension_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("retain", [False, True])
async def test_suspension_during_final_refresh_has_its_own_reconciliation(
    tmp_path, monkeypatch, retain
):
    value, adapter, _ = scoped_service(tmp_path)
    original_refresh = value.refresh
    finishing, release, joined = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = 0

    async def final_refresh_barrier():
        nonlocal calls
        calls += 1
        if calls == 2:
            finishing.set()
            await release.wait()
        await original_refresh()

    monkeypatch.setattr(value, "refresh", final_refresh_barrier)
    first = asyncio.create_task(value.suspend_group(adapter, "5", retain=True))
    second = None

    async def later():
        joined.set()
        await value.suspend_group(adapter, "5", retain=retain)

    try:
        await asyncio.wait_for(finishing.wait(), 5)
        second = asyncio.create_task(later())
        await asyncio.wait_for(joined.wait(), 5)
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(first, *([second] if second else [])), 5)
    assert (("99", "5") in value.suspended_groups) is not retain
    assert not value.suspension_tasks


def test_departed_manager_can_be_revoked_without_current_membership(tmp_path):
    value, _, rows = scoped_service(tmp_path)
    value.store.remember_roster("99", rows)
    tenant = value.store.tenant("99", "5")
    token = value.store.preview_manager(
        "99", "5", tenant["version"], "1", "offline", "preview", "2", "remove", "1002"
    )
    value.store.confirm_manager(
        token, "1", "offline", "confirm", "99", "5", {"1": "1001", "99": "1099"}
    )
    assert value.store.tenant("99", "5")["managers"] == ["1"]


@pytest.mark.parametrize("instance", ["sales-bot", "wangshangliao_bot-1"])
def test_departure_cleans_shared_rows_for_valid_native_instance(
    tmp_path, monkeypatch, instance
):
    data = tmp_path / "data"
    data.mkdir()
    path = data / "data_v4.db"
    origin = f"{instance}:GroupMessage:99/5"
    routes = [
        origin,
        origin + "/wsl-ai",
        "telegram:GroupMessage:99/5",
        "wangshangliao_other_platform:GroupMessage:99/5",
        "wangshangliao_unconfigured:GroupMessage:99/5",
        f"{instance}:GroupMessage:199/5",
        f"{instance}:GroupMessage:99/51",
        f"{instance}:FriendMessage:99/5",
        "5",
    ]
    monkeypatch.setattr(
        astrbot_core,
        "astrbot_config",
        {
            "platform": [
                {"type": "wangshangliao", "id": instance, "enable": False},
                {"type": "telegram", "id": "wangshangliao_other_platform"},
            ]
        },
    )
    with closing(sqlite3.connect(path)) as db:
        db.executescript(
            "CREATE TABLE conversations(user_id TEXT);"
            "CREATE TABLE cron_jobs("
            "id INTEGER PRIMARY KEY,payload TEXT,enabled INTEGER,"
            "status TEXT,last_error TEXT);"
        )
        db.executemany("INSERT INTO conversations VALUES(?)", [(r,) for r in routes])
        db.executemany(
            "INSERT INTO cron_jobs VALUES(?,?,1,'active','')",
            [(i, json.dumps({"session_id": r})) for i, r in enumerate(routes)],
        )
        db.commit()
    clear_shared_group_data(data, "99", "5", lambda: None)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute(
            "SELECT user_id FROM conversations ORDER BY rowid"
        ).fetchall() == [(r,) for r in routes[2:]]
        assert db.execute("SELECT enabled FROM cron_jobs ORDER BY id").fetchall() == [
            (0,),
            (0,),
        ] + [(1,)] * (len(routes) - 2)


@pytest.mark.parametrize(
    "binding", ["missing", "conflicted", "changed", "other_account"]
)
def test_departed_manager_revocation_rejects_untrusted_bindings(tmp_path, binding):
    value, _, rows = scoped_service(tmp_path)
    value.store.remember_roster("199" if binding == "other_account" else "99", rows)
    tenant = value.store.tenant("99", "5")
    token = value.store.preview_manager(
        "99", "5", tenant["version"], "1", "offline", "preview", "2", "remove", "1002"
    )
    with value.store.connect(write=True) as db:
        if binding == "missing":
            db.execute("DELETE FROM actor_bindings WHERE account='99' AND uid='2'")
        elif binding == "conflicted":
            db.execute(
                "UPDATE actor_bindings SET conflicted=1 WHERE account='99' AND uid='2'"
            )
        elif binding == "changed":
            db.execute(
                "UPDATE actor_bindings SET peer='9002' WHERE account='99' AND uid='2'"
            )
    with pytest.raises(OpenError, match="permission_denied"):
        value.store.confirm_manager(
            token, "1", "offline", "confirm", "99", "5", {"1": "1001"}
        )
    assert value.store.tenant("99", "5")["managers"] == ["1", "2"]


def test_historical_binding_cannot_add_a_departed_member(tmp_path):
    value, _, rows = scoped_service(tmp_path)
    rows["3"] = {"nimId": "1003"}
    value.store.remember_roster("99", rows)
    tenant = value.store.tenant("99", "5")
    token = value.store.preview_manager(
        "99", "5", tenant["version"], "1", "offline", "preview", "3", "add", "1003"
    )
    with pytest.raises(OpenError, match="permission_denied"):
        value.store.confirm_manager(
            token, "1", "offline", "confirm", "99", "5", {"1": "1001"}
        )
    assert value.store.tenant("99", "5")["managers"] == ["1", "2"]


@pytest.mark.asyncio
async def test_chat_manager_list_can_revoke_departed_grant_but_cannot_add_it(tmp_path):
    value, adapter, rows = scoped_service(tmp_path)
    value.store.remember_roster("99", rows)
    rows.pop("2")
    await value.refresh()
    extras = {}
    event = SimpleNamespace(
        platform=adapter,
        actor_identity=ActorIdentity("99", "1", "1001", "1"),
        get_sender_id=lambda: "1",
        get_group_id=lambda: "",
        is_private_chat=lambda: True,
        get_extra=extras.get,
        get_messages=lambda: [],
        unified_msg_origin="offline:FriendMessage:99/private/1",
        message_obj=SimpleNamespace(message_id="list"),
    )
    key = (id(adapter), adapter.account, "1", event.unified_msg_origin)
    value.plugin.commands.selections[key] = {
        "expires": time.monotonic() + 600,
        "group": "5",
    }
    assert "UID 2" in await value.manager_command(event, "管理员列表")
    assert "不能用于添加" in await value.manager_command(event, "添加管理员 2")
    event.get_messages = lambda: [At(qq="2")]
    with pytest.raises(OpenError, match="member_unavailable"):
        await value.manager_command(event, "添加管理员")
    event.get_messages = lambda: []
    event.message_obj.message_id = "preview"
    preview = await value.manager_command(event, "移除管理员 2")
    token = re.search(r"确认授权 ([A-Za-z0-9_-]+)", preview)[1]
    event.message_obj.message_id = "confirm"
    assert "已保存" in await value.manager_command(event, "确认授权 " + token)
    assert value.store.tenant("99", "5")["managers"] == ["1"]
    assert value.store.tenant("99", "6")["managers"] == ["1", "2"]
    assert value.store.tenant("199", "5")["managers"] == ["1", "2"]
    rows["2"] = {"userId": "2", "nimId": "1002", "groupRole": "GROUP_ROLE_MEMBER"}
    await value.refresh()
    event.actor_identity = ActorIdentity("99", "2", "1002", "2")
    event.get_sender_id = lambda: "2"
    assert not value.management.allowed(event, "5")


def test_shared_cleanup_without_trusted_instances_preserves_native_looking_rows(
    tmp_path,
):
    with closing(sqlite3.connect(tmp_path / "data_v4.db")) as db:
        db.execute("CREATE TABLE conversations(user_id TEXT)")
        db.execute(
            "INSERT INTO conversations VALUES('wangshangliao_old:GroupMessage:99/5')"
        )
        db.commit()
    clear_shared_group_data(tmp_path, "99", "5", lambda: None, platforms=[])
    with closing(sqlite3.connect(tmp_path / "data_v4.db")) as db:
        assert db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("peer", ["902", "forged-peer"])
async def test_real_legacy_private_entry_denies_unverified_identity(tmp_path, peer):
    event = native_event("管理 群列表", private=True)
    event.actor_identity = ActorIdentity("1", "2", peer)
    event.send = AsyncMock()
    context = SimpleNamespace(get_config=lambda *args: {"admins_id": []})
    management = GroupManagement(context, tmp_path)
    management.assignments[management.key(event.platform, "5")] = ("2",)
    plugin = object.__new__(Main)
    plugin.context = context
    plugin.group_management = management
    plugin.commands = SimpleNamespace(run=AsyncMock())
    await Main.moderation_command(plugin, event, "群列表")
    plugin.commands.run.assert_not_awaited()
    event.send.assert_awaited_once()
