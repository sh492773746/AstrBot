"""Offline regressions for reviewed identity, cleanup and revocation races."""

import asyncio
import copy
import json
import sqlite3
import threading
import time
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.commands import Commands
from astrbot.builtin_stars.wangshangliao_moderation.group_management import (
    GroupManagement,
)
from astrbot.builtin_stars.wangshangliao_moderation.lifecycle import clear_working_data
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
from astrbot.core.platform.sources.wangshangliao.event import WangshangliaoEvent
from astrbot.core.platform.sources.wangshangliao.policy import authorize_action
from astrbot.core.platform.sources.wangshangliao.storage import Ledger
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from tests.test_wangshangliao_command_menu import step
from tests.test_wangshangliao_command_parity import setup as setup
from tests.test_wangshangliao_integrated_service import service
from tests.test_wangshangliao_plain_commands import native_event

pytestmark = pytest.mark.usefixtures("wsl_active_policy", "wsl_command_classifier")


@pytest.mark.asyncio
@pytest.mark.parametrize("peer", ["1001", ""])
async def test_real_process_group_preserves_peer_or_safely_denies(tmp_path, peer):
    value = service(tmp_path)
    value.manager_command = AsyncMock(return_value="rules")
    ledger = Ledger(tmp_path / "messages.sqlite3")
    await ledger.open()
    worker = event = None
    try:
        await ledger.ingest(
            "99",
            "5",
            "offline-message",
            {
                "sender": "1",
                "name": "offline member",
                "text": "规则",
                "created_at": time.time() * 1000,
                "mentions": [],
                "recall_route": {"peer": peer, "time": str(time.time())},
            },
        )
        queue = asyncio.Queue()
        adapter = SimpleNamespace(
            account="99",
            nim_account="1099",
            config={
                "id": "offline",
                "_business_active": True,
                "_business_epoch": 1,
                "enabled_groups": ["5"],
            },
            enabled_groups={"5"},
            connection_state="online",
            stopping=asyncio.Event(),
            ledger=ledger,
            test_window=None,
            members={"5": {"1": "1001"}},
            events=set(),
            diagnostics=Mock(),
            record_error=Mock(),
            _event_queue=queue,
            meta=lambda: PlatformMetadata("wangshangliao", "offline", id="offline"),
        )
        worker = asyncio.create_task(
            WangshangliaoAdapter.process_group(adapter, "5", single=True)
        )
        event = await asyncio.wait_for(queue.get(), 5)
        event.send = AsyncMock()
        await value.on_event(event)
        assert event.actor_identity.peer == peer
        assert event.actor_identity.verified_uid == ("1" if peer else "")
        assert value.manager_command.await_count == bool(peer)
        if not peer:
            event.send.assert_not_awaited()
        assert event.is_stopped()
    finally:
        if event is not None and not event.processing_completion.done():
            event.processing_completion.set_result(None)
        if worker is not None:
            if event is None:
                worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        await ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("peer", [None, 1001, "", "x" * 1025, "\ud800"])
async def test_consumer_never_invents_group_identity(tmp_path, peer):
    template = native_event("菜单")
    template.message_obj.raw_message = {"_transport_peer": peer}
    event = WangshangliaoEvent(template.message_obj, template.platform, True)
    event.set_extra(
        "wangshangliao_payload", {"created_at": time.time() * 1000, "text": "菜单"}
    )
    event.send = AsyncMock()
    event.role = "admin"
    value = service(tmp_path)
    assert value.navigation.key(event) is None
    assert not event.is_admin()
    await value.on_event(event)
    event.send.assert_not_awaited()
    assert event.is_stopped()


def test_departure_does_not_delete_a_business_id_equal_to_transport_team(tmp_path):
    path = tmp_path / "messages.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "CREATE TABLE inbox(account TEXT,team TEXT,message TEXT,payload TEXT,"
            "sender TEXT,state TEXT,completed_at REAL,pruned_at REAL)"
        )
        db.execute("CREATE TABLE review_holds(account TEXT,team TEXT,message TEXT)")
        for account, team, message in [
            ("99", "5", "departed"),
            ("99", "unsupported/1/1005", "transport-reject"),
            ("99", "1005", "other-group"),
            ("199", "5", "other-account"),
        ]:
            db.execute(
                "INSERT INTO inbox VALUES(?,?,?,'keep','1','pending',NULL,NULL)",
                (account, team, message),
            )
            db.execute(
                "INSERT INTO review_holds VALUES(?,?,?)", (account, team, message)
            )
    clear_working_data(tmp_path, "99", "5", "1005", lambda: None)
    with closing(sqlite3.connect(path)) as db:
        records = dict(db.execute("SELECT message,state FROM inbox"))
        holds = {r[0] for r in db.execute("SELECT message FROM review_holds")}
    assert records == {
        "departed": "departed",
        "transport-reject": "departed",
        "other-group": "pending",
        "other-account": "pending",
    }
    assert holds == {"other-group", "other-account"}


@pytest.mark.asyncio
async def test_cancel_after_durable_revocation_cannot_leave_authorized_cache(tmp_path):
    event = native_event("公告 offline only")
    adapter = event.platform
    adapter.config["moderation"] = {
        "enabled": True,
        "permissions": {"5": ["announce"]},
    }
    adapter.get_moderation_members = AsyncMock(
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
    )

    async def offline_execute(operation, action, group, member, text, **kwargs):
        await kwargs["action_check"]()
        authorize_action(adapter.config, str(group), action)
        return {"status": "accepted"}

    adapter.execute_moderation.side_effect = offline_execute
    management = GroupManagement(
        SimpleNamespace(get_config=lambda *args: {"admins_id": []}), tmp_path
    )
    await management.save(adapter, "5", ["2"], 0, "offline", lambda: None)
    committed, release = threading.Event(), threading.Event()

    class CommitBarrier(sqlite3.Connection):
        def commit(self):
            super().commit()
            committed.set()
            if not release.wait(10):
                raise TimeoutError("offline_commit_barrier")

    management.database = lambda: sqlite3.connect(
        management.path, factory=CommitBarrier
    )
    revoke = asyncio.create_task(
        management.save(adapter, "5", [], 1, "offline", lambda: None)
    )
    try:
        assert await asyncio.to_thread(committed.wait, 10)
        assert not management.allowed(event, "5")
        revoke.cancel()
    finally:
        release.set()
        outcome = await asyncio.gather(revoke, return_exceptions=True)
    assert isinstance(outcome[0], asyncio.CancelledError)
    with closing(sqlite3.connect(management.path)) as db:
        assert db.execute("SELECT version,users FROM grants").fetchone() == (2, "[]")
    key = management.key(adapter, "5")
    assert management.assignments[key] == ()
    assert management.versions[key] == 2
    assert not management.allowed(event, "5")
    commands = Commands()
    commands.management = management
    await commands.run(event, "公告 offline only")
    adapter.execute_moderation.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["admission", "commit"])
async def test_role_loss_fences_private_confirmation_even_when_notice_waits_for_db(
    setup, monkeypatch, phase
):
    value, _, group_event = setup
    value.roster.return_value["3"]["groupRole"] = "GROUP_ROLE_ADMIN"
    event = native_event("确认授权", private=True)
    event.platform = group_event.platform
    event.platform.dispatch_wakeup = asyncio.Event()
    event.message_obj.message_id = "confirm-message"
    value.plugin.commands.selections[
        (id(event.platform), "1", "2", event.unified_msg_origin)
    ] = {"group": "5", "expires": time.monotonic() + 600}
    row = value.tenants[("1", "5")]
    untouched = copy.deepcopy(value.store.tenant("1", "6"))
    token = value.store.preview_manager(
        "1",
        "5",
        row["version"],
        "2",
        event.unified_msg_origin,
        "preview",
        "3",
        "add",
        "903",
    )
    waiting, release, receiving = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    original_confirm, original_log = value.store.confirm_manager, value.store.log
    original_notice = value.store.membership_notice

    def block():
        waiting.set()
        if not release.wait(10):
            raise TimeoutError("offline_transaction_barrier")

    def delayed_confirm(*args, **kwargs):
        block()
        return original_confirm(*args, **kwargs)

    def delayed_log(*args, **kwargs):
        result = original_log(*args, **kwargs)
        if args[4] == "manager_add":
            block()
        return result

    def incoming_notice(*args, **kwargs):
        receiving.set()
        return original_notice(*args, **kwargs)

    if phase == "admission":
        monkeypatch.setattr(value.store, "confirm_manager", delayed_confirm)
    else:
        monkeypatch.setattr(value.store, "log", delayed_log)
    monkeypatch.setattr(value.store, "membership_notice", incoming_notice)
    command = asyncio.create_task(value.manager_command(event, "确认授权 " + token))
    notice = None
    try:
        assert await asyncio.to_thread(waiting.wait, 10)
        notice = asyncio.create_task(
            value.receive_membership(
                event.platform,
                {
                    0: "1",
                    1: "905",
                    2: "902",
                    7: str(time.time()),
                    8: "5",
                    10: json.dumps({"id": 8, "data": {"ids": ["901"]}}),
                    12: "777",
                },
            )
        )
        assert await asyncio.to_thread(receiving.wait, 10)
        assert not value.management.allowed(event, "5")
        assert value.management.allowed(event, "6")
    finally:
        release.set()
        outcome = await asyncio.gather(command, return_exceptions=True)
        if notice is not None:
            await notice
    assert isinstance(outcome[0], (OpenError, ProtocolError))
    assert value.store.tenant("1", "5")["managers"] == ["2"]
    assert value.store.tenant("1", "6") == untouched
    value.store.membership_finish("1", "905", "777", "role_verified")
    value.suspended_groups.discard(("1", "5"))
    await value.refresh()
    with pytest.raises(OpenError):
        value.store.confirm_manager(
            token,
            "2",
            event.unified_msg_origin,
            "retry",
            "1",
            "5",
            {"2": "902", "3": "903"},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["paused", "left", "role", "version", "actor", "target", "transport", "scope"],
)
async def test_exact_target_guard_rechecks_before_commit(setup, monkeypatch, change):
    value, _, event = setup
    value.plugin.commands.selections[
        (id(event.platform), "1", "2", event.unified_msg_origin)
    ] = {
        "group": "5",
        "expires": time.monotonic() + 600,
        "members": [{"userId": "3", "nimId": "903"}],
    }
    before = value.store.tenant("1", "5")
    token = value.store.preview_manager(
        "1",
        "5",
        before["version"],
        "2",
        event.unified_msg_origin,
        "preview",
        "3",
        "add",
        "903",
    )
    original = value.store.log

    def revoke(*args, **kwargs):
        original(*args, **kwargs)
        if args[4] != "manager_add":
            return
        tenant = value.tenants[("1", "5")]
        if change == "paused":
            tenant["paused"] = True
        elif change == "left":
            tenant["present"] = False
        elif change == "role":
            tenant["native_role"] = "GROUP_ROLE_MEMBER"
        elif change == "version":
            tenant["version"] += 1
        elif change == "actor":
            value.member_evidence[("1", "5")].pop("2")
        elif change == "target":
            value.member_evidence[("1", "5")]["3"]["nimId"] = "changed"
        elif change == "transport":
            event.platform.members["5"].pop("2")
        else:
            event.platform.config["enabled_groups"] = ["6"]

    monkeypatch.setattr(value.store, "log", revoke)
    with pytest.raises((OpenError, ProtocolError)):
        await value.manager_command(event, "确认授权 " + token)
    assert value.store.tenant("1", "5") == before
    assert value.management.allowed(event, "6")


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["native_role", "tenant", "identity"])
async def test_early_denials_and_navigation_share_a_silent_cooldown(
    setup, monkeypatch, denial
):
    from astrbot.builtin_stars.wangshangliao_moderation import open_service

    value, _, event = setup
    clock = [100.0]
    monkeypatch.setattr(
        open_service,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0], time=time.time),
    )
    if denial == "native_role":
        event.platform.config["_open_native_roles"]["5"] = "GROUP_ROLE_MEMBER"
    elif denial == "tenant":
        value.tenants[("1", "5")]["managers"] = ["3"]
    else:
        private = native_event("规则", private=True)
        private.platform = event.platform
        event = private
        value.verify_actor = AsyncMock(
            side_effect=ProtocolError("private_identity_unverified")
        )
    assert await step(value, event, "规则", native=True, pace=False)
    clock[0] = 100.5
    assert await step(value, event, "菜单", native=True, pace=False) == ""
    assert await step(value, event, "规则", native=True, pace=False) == ""
    clock[0] = 103.0
    assert await step(value, event, "菜单", native=True, pace=False)
    clock[0] = 103.5
    assert await step(value, event, "0", native=True, pace=False) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["取消", "/exit"])
@pytest.mark.parametrize("paused", [False, True])
async def test_exit_clears_state_before_cooldown_or_service_pause(
    setup, monkeypatch, tmp_path, control, paused
):
    from astrbot.builtin_stars.wangshangliao_moderation import (
        open_service,
        private_interaction,
    )

    value, _, group_event = setup
    monkeypatch.setattr(
        private_interaction, "instance_dir", lambda _: tmp_path / "chat"
    )
    interaction = private_interaction.PrivateInteraction(SimpleNamespace())
    interaction.active = 2
    value.plugin.private_interaction = interaction
    with closing(interaction.database("fixture")) as db, db:
        db.execute(
            "INSERT INTO chat_sessions VALUES('1','nim/902',?,0)",
            (time.time() + 600,),
        )
    event = native_event("菜单", private=True)
    event.platform = group_event.platform
    clock = [100.0]
    monkeypatch.setattr(
        open_service,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0], time=time.time),
    )
    await step(value, event, "菜单", native=True, pace=False)
    state = value.navigation.sessions[value.navigation.key(event)]
    state.confirm = "关闭防刷屏"
    state.preview_message = "preview"
    state.busy = True
    value.settings["paused"] = paused
    clock[0] = 100.5
    assert await step(value, event, control, native=True, pace=False) == ""
    assert state.closed
    assert not state.confirm and state.pending is None and not state.preview_message
    assert value.chat_generations[("1", "nim/902")] >= 1
    with closing(interaction.database("fixture")) as db:
        assert db.execute("SELECT expires FROM chat_sessions").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_native_role_denial_survives_an_obsolete_confirmation_revision(setup):
    value, _, event = setup
    event.message_str = "确认授权 old-confirmation"
    event.get_extra("wangshangliao_payload")["_open_revision"] = "obsolete"
    event.platform.config["_open_native_roles"]["5"] = "GROUP_ROLE_MEMBER"
    event.send = AsyncMock()
    await value.on_event(event)
    assert "群管理员" in event.send.call_args.args[0].chain[0].text
    value.roster.assert_not_awaited()


@pytest.mark.asyncio
async def test_control_manager_guard_preserves_pause_control_but_checks_members(setup):
    value, _, event = setup
    key = ("1", "5")
    tenant = value.tenants[key]
    members = value.roster.return_value
    value.member_evidence[key] = members
    value.settings["paused"] = tenant["paused"] = True
    guard = value.write_guard(
        event.platform, group="5", tenant=tenant, members=members, control=True
    )
    guard()
    members["3"]["nimId"] = "changed"
    with pytest.raises(OpenError, match="membership_changed"):
        guard()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "ingress_busy",
        "personal_quota",
        "global_quota",
        "chat_cooldown",
        "protocol",
        "value",
        "missing_model",
        "missing_budget",
        "provider",
        "chat_busy",
    ],
)
async def test_ordinary_ai_rejections_share_command_cooldown(
    setup, monkeypatch, failure
):
    from astrbot.builtin_stars.wangshangliao_moderation import open_service

    value, _, group_event = setup
    event = native_event("hello", private=True)
    event.platform = group_event.platform
    value.plugin.private_interaction.handle = AsyncMock(return_value=False)
    clock = [100.0]
    monkeypatch.setattr(
        open_service,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0], time=time.time),
    )
    if failure == "ingress_busy":
        event.get_extra("wangshangliao_payload")["_ai_busy"] = True
        value.chat = AsyncMock()
    elif failure in {"personal_quota", "global_quota", "chat_cooldown"}:
        value.chat = AsyncMock(side_effect=OpenError(failure))
    elif failure == "protocol":
        value.chat = AsyncMock(side_effect=ProtocolError("provider_unavailable"))
    elif failure == "value":
        value.chat = AsyncMock(side_effect=ValueError("offline"))
    elif failure != "missing_model":
        value.model = {"provider_id": "missing"}
        if failure != "missing_budget":
            value.store.confirm_budget(
                1, True, "missing", "offline hard cap", "dashboard/root"
            )
            value.budget = value.store.budget()
        if failure == "chat_busy":
            value.chat_users.add("nim/902")
    assert await step(value, event, "hello", native=True, pace=False)
    clock[0] = 100.1
    assert await step(value, event, "hello again", native=True, pace=False) == ""
    clock[0] = 100.2
    assert await step(value, event, "菜单", native=True, pace=False) == ""
    clock[0] = 103.0
    assert await step(value, event, "菜单", native=True, pace=False)
    clock[0] = 103.1
    assert await step(value, event, "another question", native=True, pace=False) == ""
    assert value.store.usage()["used"] == 0


@pytest.mark.asyncio
async def test_successful_ai_reply_does_not_use_the_command_rejection_cooldown(
    setup, monkeypatch
):
    from astrbot.builtin_stars.wangshangliao_moderation import open_service

    value, _, group_event = setup
    event = native_event("hello", private=True)
    event.platform = group_event.platform
    value.plugin.private_interaction.handle = AsyncMock(return_value=False)
    monkeypatch.setattr(
        open_service,
        "time",
        SimpleNamespace(monotonic=lambda: 100.5, time=time.time),
    )
    value.command_at[("1", "nim/902")] = 100.0

    async def answer(current):
        await current.send(current.plain_result("offline AI answer"))

    value.chat = AsyncMock(side_effect=answer)
    assert (
        await step(value, event, "hello", native=True, pace=False)
        == "offline AI answer"
    )
    assert value.command_at[("1", "nim/902")] == 100.0
