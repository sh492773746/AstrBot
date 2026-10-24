"""Offline-only coverage of the dedicated, exact-target blacklist capability."""

import asyncio
import copy
import socket
import sqlite3
from contextlib import closing
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from astrbot.builtin_stars.wangshangliao_moderation import test_blacklist as binding
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from astrbot.core.platform.sources.wangshangliao import blacklist
from astrbot.core.platform.sources.wangshangliao import test_blacklist as core
from astrbot.core.platform.sources.wangshangliao.group_scope import GroupAdapter
from astrbot.core.platform.sources.wangshangliao.test_window import TestWindow as Window
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from scripts import wsl_test_blacklist as helper
from scripts import wsl_test_membership as old_helper

GROUP, OWNER, RECEIVER, TARGET = core.GROUP, core.OWNER, core.RECEIVER, core.TARGET
OPERATION = "WSL_TEST_BLACKLIST_" + "a" * 32
ERRORS = (OpenError, ProtocolError)


@pytest_asyncio.fixture
async def lab(tmp_path, monkeypatch):
    """Build original authenticated adapter doubles with all IO mocked.

    Args:
        tmp_path: Pytest-only private storage root.
        monkeypatch: Scoped dependency replacement.

    Returns:
        Three original adapters, fixed roster, service and isolated IO recorder.
    """

    def no_network(*args, **kwargs):
        raise AssertionError("Network access is forbidden in blacklist fixture tests")

    # Start the loop before blocking sockets: Windows uses TCP for its wakeup pair.
    asyncio.get_running_loop()
    monkeypatch.setattr(socket.socket, "connect", no_network)
    clock = [1000.0]
    fake_time = SimpleNamespace(monotonic=lambda: clock[0], time=lambda: clock[0])
    from astrbot.core.platform.sources.wangshangliao import test_window

    for module in (core, binding, helper, test_window):
        monkeypatch.setattr(module, "time", fake_time)
    for module in (core, binding, blacklist):
        monkeypatch.setattr(module, "instance_dir", lambda key: tmp_path / key)
    bots = {}
    for index, uid in enumerate((OWNER, RECEIVER, TARGET), start=1):
        bots[uid] = SimpleNamespace(
            account=uid,
            nim_account=str(900000 + index),
            config={
                "id": f"offline-{uid}",
                "enable": True,
                "_business_active": True,
                "_business_epoch": 7,
                "_business_revision": "offline-policy-revision",
            },
            business=SimpleNamespace(jwt="offline-only", fields=[0, 0, 0, int(uid)]),
            nim=object(),
            ledger=object(),
            connection_state="online",
            stopping=asyncio.Event(),
            groups={GROUP: "team-1459282"},
            group_catalog={GROUP: {"team": "team-1459282"}},
            enabled_groups=[GROUP],
        )
    receiver, target = bots[RECEIVER], bots[TARGET]
    roster = {
        uid: {
            "userId": uid,
            "nimId": bot.nim_account,
            "groupRole": {
                OWNER: "GROUP_ROLE_OWNER",
                RECEIVER: "GROUP_ROLE_ADMIN",
                TARGET: "GROUP_ROLE_MEMBER",
            }[uid],
            "accountState": "ACCOUNT_STATE_GOOD",
        }
        for uid, bot in bots.items()
    }
    target_row = copy.deepcopy(roster[TARGET])

    async def directory(group):
        assert group == GROUP
        return {
            "complete": True,
            "groupMemberInfo": copy.deepcopy(list(roster.values())),
        }

    receiver.get_moderation_members = AsyncMock(side_effect=directory)
    tenant = {
        "active": 1,
        "present": 1,
        "paused": 0,
        "managers": [OWNER],
        "version": 2,
        "epoch": 3,
        "rules": {"advertising": False, "flood": False, "keywords": []},
    }
    service = SimpleNamespace(
        active=True,
        integrated=True,
        settings={"paused": 0, "version": 1},
        adapters=lambda: list(bots.values()),
        tenants={(RECEIVER, GROUP): tenant},
        store=SimpleNamespace(tenant=lambda *_: copy.deepcopy(tenant)),
        suspended_groups=set(),
        membership_receiving={},
    )

    def renew(scope="group_rules"):
        receiver.test_window = Window(
            receiver, target, [GROUP], [scope], [OPERATION], 300, 10
        )
        return receiver.test_window

    renew()
    writes = []

    async def sleep(seconds):
        clock[0] += max(0, seconds)

    monkeypatch.setattr(
        helper, "asyncio", SimpleNamespace(Lock=asyncio.Lock, sleep=sleep)
    )

    async def exchange(client, route, params, *, authorize):
        await authorize()
        assert client is bots[OWNER].business
        assert params == {"groupId": int(GROUP), "groupMemberIds": [int(TARGET)]}
        writes.append((route, clock[0]))
        if route == helper.REMOVE:
            roster.pop(TARGET)
        elif route == helper.INVITE:
            roster[TARGET] = copy.deepcopy(target_row)
        else:
            raise AssertionError("Unexpected raw membership route")
        return {}

    upstream = AsyncMock(side_effect=exchange)
    monkeypatch.setattr(helper, "request_business", upstream)
    monkeypatch.setattr(helper, "_exchange_business", upstream)
    monkeypatch.setattr(binding, "muted_member_ids", AsyncMock(return_value=set()))
    return SimpleNamespace(
        bots=bots,
        receiver=receiver,
        target=target,
        roster=roster,
        target_row=target_row,
        tenant=tenant,
        service=service,
        clock=clock,
        renew=renew,
        writes=writes,
        upstream=upstream,
        root=tmp_path,
        begin=lambda **kw: binding.begin_blacklist(
            service,
            kw.get("actor", "dashboard/offline"),
            kw.get("payload", {"operation_id": OPERATION}),
        ),
    )


@pytest.mark.asyncio
async def test_offline_guard_rejects_even_loopback_after_loop_start(lab):
    assert asyncio.get_running_loop().is_running()
    with socket.socket() as connection:
        with pytest.raises(AssertionError, match="Network access is forbidden"):
            connection.connect(("127.0.0.1", 9))


def native_intent(
    lab, *, number=1, action="mute", minutes=None, state="unknown", finished=0
):
    """Write test-only native evidence using the actual native schema.

    Args:
        lab: Offline-only lab.
        number: Fixed evidence index.
        action: Native action kind.
        minutes: Optional configured mute duration.
        state: Durable receipt status.
        finished: Native completion flag.

    Returns:
        Matching durable native capability, not an arbitrary exemption.
    """
    store = blacklist.BlacklistStore(lab.receiver.config["id"])
    if action == "mute" and minutes is not None:
        store.configure(RECEIVER, GROUP, minutes, "dashboard/offline", lambda: None)
    policy = store.settings(RECEIVER, GROUP)
    intent = blacklist.BlacklistIntent(
        id=f"{number:032x}",
        instance=lab.receiver.config["id"],
        account=RECEIVER,
        group=GROUP,
        member=TARGET,
        peer=lab.target.nim_account,
        entry="test-entry",
        cycle=1,
        version=1,
        join_id=str(number),
        action=action,
        minutes=(
            minutes
            if minutes is not None
            else 1
            if action == "kick"
            else policy["mute_minutes"]
        ),
        policy_version=policy["policy_version"],
    )
    with store._transaction() as db:
        db.execute(
            "INSERT OR IGNORE INTO blacklist_entries"
            "(id,account,group_id,member,peer,name,cycle,version,kicks,created,status,"
            "actor,actor_peer) VALUES(?,?,?,?,?,'Target',1,1,0,1,'active',?,'')",
            (intent.entry, RECEIVER, GROUP, TARGET, intent.peer, "dashboard/offline"),
        )
        fields = {
            ("group_id" if key == "group" else key): value
            for key, value in asdict(intent).items()
        }
        fields.update(event_at=1, status=state, finished=finished, result="{}")
        db.execute(
            f"INSERT INTO blacklist_intents({','.join(fields)}) "
            f"VALUES({','.join('?' for _ in fields)})",
            list(fields.values()),
        )
    return intent


@pytest.mark.asyncio
async def test_accepted_unverified_kick_freezes_fixture_and_renewal(lab):
    await lab.begin()
    native_intent(lab, action="kick", state="accepted", finished=1)
    with pytest.raises(ProtocolError, match="unknown"):
        lab.service._blacklist_fixture.check()
    with pytest.raises(ProtocolError, match="unknown"):
        await lab.begin()
    assert lab.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"operation": "arbitrary", "operation_id": OPERATION},
        {"operation": "begin", "operation_id": OPERATION, "member": TARGET},
        {"operation": "membership", "operation_id": OPERATION},
        {"operation": "unmute", "operation_id": OPERATION, "minutes": 1},
    ],
)
async def test_control_rejects_extra_or_missing_fields_without_io(lab, payload):
    with pytest.raises(OpenError, match="arguments"):
        await binding.control_blacklist(lab.service, "dashboard/offline", payload)
    assert lab.writes == []


@pytest.mark.asyncio
async def test_control_begin_and_membership_use_original_capability(lab):
    result = await binding.control_blacklist(
        lab.service,
        "dashboard/offline",
        {"operation": "begin", "operation_id": OPERATION},
    )
    assert result["test_only"] and result["operation_id"] == OPERATION
    result = await binding.control_blacklist(
        lab.service,
        "dashboard/offline",
        {"operation": "membership", "operation_id": OPERATION, "step": "leave"},
    )
    assert result["state"] == "verified"
    assert len(lab.writes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("revision", [11, True, "", "x" * 129])
async def test_fixture_requires_the_real_bounded_string_policy_revision(lab, revision):
    lab.receiver.config["_business_revision"] = revision
    with pytest.raises(OpenError, match="identity"):
        await lab.begin()
    assert lab.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ambiguous", [False, True])
async def test_pending_invitation_accepts_only_unique_fresh_native_evidence(
    lab, ambiguous
):
    from datetime import datetime, timezone

    await lab.begin()
    fixture = lab.service._blacklist_fixture
    worker = helper.BlacklistMembershipTest(fixture)
    await worker.change("leave")
    original = lab.upstream.side_effect
    invite = {
        "applyId": "101",
        "groupId": GROUP,
        "groupApplyType": "INVITATION",
        "state": "MEMBER_STATE_INVITED",
        "applicant": {"userId": TARGET, "nimId": lab.target.nim_account},
        "inviter": {"userId": OWNER},
    }

    async def exchange(client, route, params, *, authorize):
        if route == helper.INVITE:
            await authorize()
            invite["applyTime"] = datetime.fromtimestamp(
                lab.clock[0], timezone.utc
            ).isoformat()
            return {}
        if route == helper.ACCEPT:
            await authorize()
            assert client is lab.target.business
            assert params == {"applyId": "101", "agree": True}
            lab.roster[TARGET] = copy.deepcopy(lab.target_row)
            invite["state"] = "MEMBER_STATE_GOOD"
            return {}
        return await original(client, route, params, authorize=authorize)

    lab.target.business.request = AsyncMock(
        side_effect=lambda *args: {"list": [invite] * (2 if ambiguous else 1)}
    )
    lab.upstream.side_effect = exchange
    if ambiguous:
        with pytest.raises(ProtocolError, match="invitation"):
            await worker.change("invite/1")
        assert TARGET not in lab.roster
        assert all(
            call.args[1] != helper.ACCEPT for call in lab.upstream.await_args_list
        )
    else:
        assert (await worker.change("invite/1"))["state"] == "verified"
        assert TARGET in lab.roster
        assert (
            sum(call.args[1] == helper.ACCEPT for call in lab.upstream.await_args_list)
            == 1
        )
        with closing(sqlite3.connect(fixture.path)) as db:
            assert db.execute("SELECT state FROM acceptances").fetchall() == [
                ("verified",)
            ]


@pytest.mark.asyncio
async def test_begin_binds_only_original_window_without_business_or_window_edits(lab):
    before = (
        copy.deepcopy(lab.receiver.config),
        copy.deepcopy(lab.tenant),
        dict(lab.receiver.test_window.__dict__),
    )
    result = await lab.begin()
    fixture = lab.service._blacklist_fixture
    assert result == {
        "test_only": True,
        "operation_id": OPERATION,
        "replayed": False,
        "rebound": False,
    }
    assert fixture.window is lab.receiver.test_window
    assert fixture.window._blacklist_fixture is fixture
    assert before[:2] == (lab.receiver.config, lab.tenant)
    assert all(
        getattr(fixture.window, key) == value for key, value in before[2].items()
    )
    assert not (fixture.path.parents[2] / "member_blacklist.sqlite3").exists()
    assert fixture.path.stat().st_mode & 0o777 == 0o600
    assert (await lab.begin())["replayed"]
    lab.upstream.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["settings_paused", "active", "present", "paused"])
@pytest.mark.parametrize("value", ["0", "1", None, 0.0, 1.0, 2, -1])
async def test_begin_rejects_noncanonical_persisted_flags(lab, field, value):
    if field == "settings_paused":
        lab.service.settings["paused"] = value
    else:
        lab.tenant[field] = value
    with pytest.raises(OpenError, match="revoked"):
        await lab.begin()
    lab.upstream.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"actor": "nim/900001"},
        {"actor": "dashboard/"},
        {"actor": True},
        {"payload": {}},
        {"payload": {"operation_id": OPERATION, "target": TARGET}},
        {"payload": {"operation_id": OPERATION, "path": "/tmp/anything"}},
        {"payload": {"operation_id": "WSL_TEST_BLACKLIST_" + "A" * 32}},
        {"payload": {"operation_id": "WSL_TEST_AD_" + "a" * 32}},
        {"payload": {"operation_id": "WSL_TEST_BLACKLIST_../else"}},
    ],
)
async def test_exact_dashboard_arguments_only(lab, kwargs):
    with pytest.raises(OpenError, match="arguments"):
        await lab.begin(**kwargs)
    lab.upstream.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "extra_adapter",
        "missing_adapter",
        "duplicate_id",
        "duplicate_nim",
        "owner_epoch",
        "target_revision",
        "target_auth",
        "receiver_auth",
        "disabled",
        "inactive",
        "stopping",
        "offline",
        "manager",
        "paused",
        "store_diverged",
        "extra_group",
        "private_scope",
        "both_scopes",
        "marker",
        "expired",
        "extended",
        "infinite",
        "wrong_sender",
    ],
)
async def test_begin_rejects_broadened_or_unauthenticated_bindings(lab, fault):
    receiver, owner, target = lab.receiver, lab.bots[OWNER], lab.target
    window = receiver.test_window
    if fault == "extra_adapter":
        lab.service.adapters = lambda: list(lab.bots.values()) + [target]
    elif fault == "missing_adapter":
        lab.service.adapters = lambda: [receiver, target]
    elif fault == "duplicate_id":
        owner.config["id"] = receiver.config["id"]
    elif fault == "duplicate_nim":
        owner.nim_account = receiver.nim_account
    elif fault == "owner_epoch":
        owner.config.pop("_business_epoch")
    elif fault == "target_revision":
        target.config["_business_revision"] = None
    elif fault.endswith("_auth"):
        lab.bots[TARGET if fault == "target_auth" else RECEIVER].business.jwt = ""
    elif fault == "disabled":
        owner.config["enable"] = False
    elif fault == "inactive":
        target.config["_business_active"] = False
    elif fault == "stopping":
        owner.stopping.set()
    elif fault == "offline":
        target.connection_state = "stopped"
    elif fault == "manager":
        lab.tenant["managers"].append(TARGET)
    elif fault == "paused":
        lab.service.settings["paused"] = True
    elif fault == "store_diverged":
        lab.service.store.tenant = lambda *_: {}
    elif fault == "extra_group":
        window.groups.append("9999")
    elif fault == "private_scope":
        window.scopes = ["private_commands"]
    elif fault == "both_scopes":
        window.scopes = ["group_commands", "group_rules"]
    elif fault == "marker":
        window.keywords = [OPERATION, "WSL_TEST_OTHER"]
    elif fault == "expired":
        window.expires = 0
    elif fault == "extended":
        window.expires = lab.clock[0] + 301
    elif fault == "infinite":
        window.expires = float("inf")
    elif fault == "wrong_sender":
        window.sender = owner
    with pytest.raises(ERRORS):
        await lab.begin()
    assert not list(lab.root.rglob("fixture.sqlite3"))
    lab.upstream.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["owner", "admin", "target", "peer", "ban", "extra", "partial"]
)
async def test_exact_native_roster_roles_and_identity(lab, fault):
    if fault in {"owner", "admin", "target"}:
        uid = {"owner": OWNER, "admin": RECEIVER, "target": TARGET}[fault]
        lab.roster[uid]["groupRole"] = "GROUP_ROLE_OTHER"
    elif fault == "peer":
        lab.roster[TARGET]["nimId"] = "999"
    elif fault == "ban":
        lab.roster[OWNER]["accountState"] = "ACCOUNT_STATE_BAN"
    elif fault == "extra":
        lab.roster["99"] = {**lab.target_row, "userId": "99"}
    else:
        lab.receiver.get_moderation_members.side_effect = None
        lab.receiver.get_moderation_members.return_value = {
            "complete": False,
            "groupMemberInfo": list(lab.roster.values()),
        }
    with pytest.raises(ERRORS):
        await lab.begin()
    assert not list(lab.root.rglob("fixture.sqlite3"))


@pytest.mark.asyncio
async def test_capability_is_exact_not_bool_string_or_copied_object(lab):
    await lab.begin()
    fixture = lab.service._blacklist_fixture
    peer = lab.target.nim_account
    assert core.protected_target_allowed(
        lab.receiver, GROUP, TARGET, peer, fixture=fixture
    )
    assert core.protected_target_allowed(
        GroupAdapter(lab.receiver, GROUP), GROUP, TARGET, peer, fixture=fixture
    )
    for fake in (None, True, OPERATION, object()):
        assert not core.protected_target_allowed(
            lab.receiver, GROUP, TARGET, peer, fixture=fake
        )
    for adapter, group, member, nim in (
        (lab.target, GROUP, TARGET, peer),
        (lab.receiver, "1516387", TARGET, peer),
        (lab.receiver, GROUP, RECEIVER, peer),
        (lab.receiver, GROUP, OWNER, peer),
        (lab.receiver, GROUP, TARGET, "999"),
        (GroupAdapter(lab.receiver, "999"), GROUP, TARGET, peer),
    ):
        assert not core.protected_target_allowed(
            adapter, group, member, nim, fixture=fixture
        )
    with pytest.raises(ProtocolError):
        core.protected_target_allowed(
            lab.receiver, GROUP, TARGET, peer, fixture=copy.copy(fixture)
        )
    with pytest.raises(TypeError):
        fixture.peers[TARGET] = "999"


@pytest.mark.asyncio
@pytest.mark.parametrize("uid", [OWNER, RECEIVER, TARGET])
@pytest.mark.parametrize(
    "fault", ["epoch", "revision", "id", "nim", "business", "ledger", "replacement"]
)
async def test_all_original_account_snapshots_revoke_and_cannot_rebase(lab, uid, fault):
    await lab.begin()
    fixture = lab.service._blacklist_fixture
    bot = lab.bots[uid]
    if fault == "epoch":
        bot.config["_business_epoch"] += 1
    elif fault == "revision":
        bot.config["_business_revision"] += "-changed"
    elif fault == "id":
        bot.config["id"] += "-changed"
    elif fault == "nim":
        bot.nim_account = "999999"
    elif fault == "business":
        bot.business = copy.copy(bot.business)
    elif fault == "ledger":
        bot.ledger = object()
    else:
        lab.bots[uid] = copy.copy(bot)
    with pytest.raises(ERRORS):
        fixture.check()
    lab.renew()
    with pytest.raises(ERRORS):
        await lab.begin()
    lab.upstream.assert_not_awaited()


@pytest.mark.asyncio
async def test_segment_renewal_revokes_old_capability_without_extending_windows(lab):
    await lab.begin()
    old = lab.service._blacklist_fixture
    old_deadline = old.deadline
    lab.clock[0] += 301
    with pytest.raises(ProtocolError):
        old.check()
    new_window = lab.renew("group_commands")
    new_deadline = new_window.expires
    assert (await lab.begin())["rebound"]
    new = lab.service._blacklist_fixture
    assert new is not old and new.path == old.path
    assert new.window is new_window and new.deadline == new_deadline
    assert old.window.expires == old_deadline
    with pytest.raises(ProtocolError):
        old.check()
    new.check()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["role", "nim", "manager", "epoch", "window", "group_view"]
)
async def test_final_submission_rechecks_role_identity_and_revocation(lab, fault):
    await lab.begin()
    fixture = lab.service._blacklist_fixture
    adapter = GroupAdapter(lab.receiver, GROUP)
    original = lab.receiver.get_moderation_members.side_effect

    async def changed(group):
        if fault == "role":
            lab.roster[TARGET]["groupRole"] = "GROUP_ROLE_ADMIN"
        elif fault == "nim":
            lab.roster[TARGET]["nimId"] = "999"
        elif fault == "manager":
            lab.tenant["managers"] = []
        elif fault == "epoch":
            lab.bots[OWNER].config["_business_epoch"] += 1
        elif fault == "window":
            lab.receiver.test_window = None
        else:
            object.__setattr__(adapter, "group", "999")
        return await original(group)

    lab.receiver.get_moderation_members.side_effect = changed
    with pytest.raises(ERRORS):
        await fixture.authorize_submission(
            adapter, GROUP, TARGET, lab.target.nim_account
        )
    lab.upstream.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_native_sanction_requires_matching_unfinished_durable_intent(lab):
    await lab.begin()
    fixture = lab.service._blacklist_fixture
    intent = native_intent(lab)
    with pytest.raises(ProtocolError, match="unknown"):
        fixture.check()
    for fake in (True, OPERATION, asdict(intent)):
        with pytest.raises(ProtocolError):
            fixture.check(blacklist_intent=fake)
    await fixture.authorize_submission(
        lab.receiver, GROUP, TARGET, lab.target.nim_account, blacklist_intent=intent
    )
    native_intent(lab, number=2)
    with pytest.raises(ProtocolError, match="unknown"):
        fixture.check(blacklist_intent=intent)
    lab.renew()
    with pytest.raises(ERRORS):
        await lab.begin()


@pytest.mark.asyncio
async def test_unknown_native_before_begin_never_writes_test_storage(lab):
    native_intent(lab)
    with pytest.raises(ProtocolError, match="unknown"):
        await lab.begin()
    assert not list(lab.root.rglob("fixture.sqlite3"))


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", [1, 1441, 41760])
async def test_single_rejoin_direct_mute_and_explicit_restoration(lab, minutes):
    await lab.begin()
    fixture = lab.service._blacklist_fixture
    worker = helper.BlacklistMembershipTest(fixture)
    await worker.change("leave")
    assert TARGET not in lab.roster
    await worker.change("invite/1")
    assert TARGET in lab.roster
    intent = native_intent(lab, minutes=minutes)
    await fixture.authorize_submission(
        lab.receiver, GROUP, TARGET, lab.target.nim_account, blacklist_intent=intent
    )
    native = fixture.path.parents[2] / "member_blacklist.sqlite3"
    with closing(sqlite3.connect(native)) as db, db:
        db.execute("UPDATE blacklist_intents SET status='accepted',finished=1")
    assert core.STEPS == ("leave", "invite/1")
    assert [route for route, _ in lab.writes] == [helper.REMOVE, helper.INVITE]
    assert lab.writes[1][1] - lab.writes[0][1] >= 30
    count = len(lab.writes)
    for step in ("leave", "invite/1", "invite/2", "invite/3", "invite/4"):
        with pytest.raises(ProtocolError):
            await worker.change(step)
    assert len(lab.writes) == count
    with pytest.raises(ProtocolError, match="restoration"):
        await fixture.restore()
    with closing(sqlite3.connect(native)) as db, db:
        db.execute("UPDATE blacklist_entries SET status='removed'")
    binding.muted_member_ids.return_value = {TARGET}
    with pytest.raises(ProtocolError, match="restoration"):
        await fixture.restore()
    binding.muted_member_ids.return_value = set()
    await fixture.restore()
    with pytest.raises(ProtocolError, match="restored"):
        fixture.check()
    assert len(lab.writes) == count


@pytest.mark.asyncio
async def test_matching_durable_kick_never_gets_fixture_authority(lab):
    await lab.begin()
    fixture = lab.service._blacklist_fixture
    intent = native_intent(lab, action="kick")
    with pytest.raises(ProtocolError, match="unknown"):
        await fixture.authorize_submission(
            lab.receiver, GROUP, TARGET, lab.target.nim_account, blacklist_intent=intent
        )
    lab.upstream.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", ["missing_column", "default_minus_one"])
async def test_legacy_native_mute_cannot_gain_fixture_authority(lab, legacy):
    await lab.begin()
    fixture = lab.service._blacklist_fixture
    intent = native_intent(lab)
    store = blacklist.BlacklistStore(lab.receiver.config["id"])
    with closing(sqlite3.connect(store.path)) as db, db:
        if legacy == "missing_column":
            db.execute("ALTER TABLE blacklist_intents DROP COLUMN policy_version")
        else:
            db.execute("UPDATE blacklist_intents SET policy_version=-1")
    before = store.path.read_bytes()
    with pytest.raises(ProtocolError, match="unknown"):
        await fixture.authorize_submission(
            lab.receiver, GROUP, TARGET, lab.target.nim_account, blacklist_intent=intent
        )
    with pytest.raises(ProtocolError, match="unknown"):
        await lab.begin()
    assert store.path.read_bytes() == before
    lab.upstream.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", [True, 0, -1, 41761, "1"])
async def test_noncanonical_mute_duration_never_gets_fixture_authority(lab, minutes):
    await lab.begin()
    fixture = lab.service._blacklist_fixture
    intent = replace(native_intent(lab), minutes=minutes)
    store = blacklist.BlacklistStore(lab.receiver.config["id"])
    with closing(sqlite3.connect(store.path)) as db, db:
        db.execute("UPDATE blacklist_intents SET minutes=?", (minutes,))
    with pytest.raises(ProtocolError, match="unknown"):
        fixture.check(blacklist_intent=intent)
    lab.upstream.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("policy_version", [True, -1, "0"])
async def test_noncanonical_policy_version_never_gets_fixture_authority(
    lab, policy_version
):
    await lab.begin()
    fixture = lab.service._blacklist_fixture
    intent = replace(native_intent(lab), policy_version=policy_version)
    store = blacklist.BlacklistStore(lab.receiver.config["id"])
    with closing(sqlite3.connect(store.path)) as db, db:
        db.execute("UPDATE blacklist_intents SET policy_version=?", (policy_version,))
    with pytest.raises(ProtocolError, match="unknown"):
        fixture.check(blacklist_intent=intent)
    lab.upstream.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("during_read", [False, True])
async def test_policy_change_revokes_inflight_fixture_mute_even_if_duration_returns(
    lab, during_read
):
    await lab.begin()
    fixture = lab.service._blacklist_fixture
    intent = native_intent(lab)
    store = blacklist.BlacklistStore(lab.receiver.config["id"])
    original = lab.receiver.get_moderation_members.side_effect

    def change_policy():
        store.configure(RECEIVER, GROUP, 1, "dashboard/offline", lambda: None)
        store.configure(RECEIVER, GROUP, 41760, "dashboard/offline", lambda: None)

    async def changed(group):
        change_policy()
        return await original(group)

    if during_read:
        lab.receiver.get_moderation_members.side_effect = changed
    else:
        change_policy()
    with pytest.raises(ProtocolError, match="intent"):
        await fixture.authorize_submission(
            lab.receiver, GROUP, TARGET, lab.target.nim_account, blacklist_intent=intent
        )
    assert store.settings(RECEIVER, GROUP)["policy_version"] == 2
    with pytest.raises(ProtocolError, match="unknown"):
        fixture.check()
    lab.upstream.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "kick",
        "rejected",
        "verified",
        "unknown",
        "unfinished",
        "legacy_policy",
        "extra_mute",
        "peer",
        "instance",
        "cycle",
        "minutes",
        "no_mute",
    ],
)
async def test_restore_requires_one_completed_current_policy_mute(lab, fault):
    await lab.begin()
    fixture = lab.service._blacklist_fixture
    worker = helper.BlacklistMembershipTest(fixture)
    await worker.change("leave")
    await worker.change("invite/1")
    native_intent(lab, state="accepted", finished=1)
    if fault == "extra_mute":
        native_intent(lab, number=2, state="accepted", finished=1)
    store = blacklist.BlacklistStore(lab.receiver.config["id"])
    with closing(sqlite3.connect(store.path)) as db, db:
        db.execute("UPDATE blacklist_entries SET status='removed'")
        if fault == "kick":
            db.execute("UPDATE blacklist_intents SET action='kick',minutes=1")
        elif fault in {"rejected", "verified", "unknown"}:
            db.execute("UPDATE blacklist_intents SET status=?", (fault,))
        elif fault == "unfinished":
            db.execute("UPDATE blacklist_intents SET finished=0")
        elif fault == "legacy_policy":
            db.execute("UPDATE blacklist_intents SET policy_version=-1")
        elif fault == "peer":
            db.execute("UPDATE blacklist_intents SET peer='999'")
        elif fault == "instance":
            db.execute("UPDATE blacklist_intents SET instance='other-instance'")
        elif fault == "cycle":
            db.execute("UPDATE blacklist_intents SET cycle=2")
        elif fault == "minutes":
            db.execute("UPDATE blacklist_intents SET minutes=41761")
        elif fault == "no_mute":
            db.execute("DELETE FROM blacklist_intents")
    before = store.path.read_bytes()
    with pytest.raises(ProtocolError, match="restoration|unknown"):
        await fixture.restore()
    assert store.path.read_bytes() == before
    with closing(sqlite3.connect(fixture.path)) as db:
        assert db.execute("SELECT restored FROM fixture").fetchone() == (0,)
    assert [route for route, _ in lab.writes] == [helper.REMOVE, helper.INVITE]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["timeout", "cancel", "pending", "revoked_at_wire"])
@pytest.mark.parametrize("step", ["leave", "invite/1"])
async def test_unknown_helper_never_retries_rebinds_or_restores(lab, fault, step):
    await lab.begin()
    fixture = lab.service._blacklist_fixture
    worker = helper.BlacklistMembershipTest(fixture)
    if step == "invite/1":
        await worker.change("leave")
        lab.target.business.request = AsyncMock(return_value={"list": []})
    original = lab.upstream.side_effect

    async def failed(client, route, params, *, authorize):
        if fault == "revoked_at_wire":
            lab.roster[OWNER]["groupRole"] = "GROUP_ROLE_MEMBER"
            await authorize()
        elif fault == "pending":
            await authorize()
            return {}
        elif fault == "cancel":
            raise asyncio.CancelledError()
        else:
            raise TimeoutError()
        return await original(client, route, params, authorize=authorize)

    lab.upstream.side_effect = failed
    with pytest.raises((ProtocolError, TimeoutError, asyncio.CancelledError)):
        await worker.change(step)
    with closing(sqlite3.connect(fixture.path)) as db:
        assert db.execute(
            "SELECT step,state FROM attempts ORDER BY rowid"
        ).fetchall() == (
            [("leave", "verified"), ("invite/1", "unknown")]
            if step == "invite/1"
            else [("leave", "unknown")]
        )
    calls = lab.upstream.await_count
    with pytest.raises(ProtocolError, match="unknown"):
        await worker.change(step)
    with pytest.raises(ProtocolError, match="unknown"):
        helper.BlacklistMembershipTest(fixture)
    with pytest.raises(ProtocolError, match="unknown"):
        await fixture.restore()
    lab.renew()
    with pytest.raises(ERRORS):
        await lab.begin()
    del lab.service._blacklist_fixture
    with pytest.raises(ERRORS):
        await lab.begin()
    assert lab.upstream.await_count == calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "step,group,member",
    [
        ("invite/0", GROUP, TARGET),
        ("invite/2", GROUP, TARGET),
        ("invite/3", GROUP, TARGET),
        ("invite/4", GROUP, TARGET),
        ("invite/5", GROUP, TARGET),
        ("kick", GROUP, TARGET),
        ("leave", "1516387", TARGET),
        ("leave", GROUP, RECEIVER),
        ("leave", GROUP, OWNER),
        ("leave", GROUP, [TARGET]),
    ],
)
async def test_helper_scope_does_not_generalize_old_membership_peer(
    lab, step, group, member
):
    await lab.begin()
    worker = helper.BlacklistMembershipTest(lab.service._blacklist_fixture)
    with pytest.raises(ProtocolError):
        await worker.change(step, group=group, member=member)
    lab.upstream.assert_not_awaited()
    assert old_helper.PEER == RECEIVER
    assert old_helper.ORDINARY == TARGET


@pytest.mark.asyncio
async def test_raw_routes_unreserved_and_incomplete_restore_are_denied(lab):
    await lab.begin()
    fixture = lab.service._blacklist_fixture
    worker = helper.BlacklistMembershipTest(fixture)
    for route in (
        helper.INVITE,
        helper.REMOVE,
        "/v1/group/leave-group",
        "/v1/friend/delete-friend",
        "/v1/group/group-member-invite",
    ):
        with pytest.raises(ProtocolError, match="unreserved"):
            await worker.request(
                route, {"groupId": int(GROUP), "groupMemberIds": [int(TARGET)]}
            )
    with pytest.raises(ProtocolError, match="incomplete"):
        await fixture.restore()
    lab.upstream.assert_not_awaited()


@pytest.mark.asyncio
async def test_second_invitation_is_rejected_even_after_target_leaves_again(lab):
    await lab.begin()
    fixture = lab.service._blacklist_fixture
    worker = helper.BlacklistMembershipTest(fixture)
    await worker.change("leave")
    await worker.change("invite/1")
    lab.roster.pop(TARGET)
    calls = lab.upstream.await_count
    for step in ("leave", "invite/1", "invite/2", "invite/3", "invite/4"):
        with pytest.raises(ProtocolError):
            await worker.change(step)
        with pytest.raises(ProtocolError, match="no_replay"):
            fixture.reserve(step)
    assert lab.upstream.await_count == calls
    with closing(sqlite3.connect(fixture.path)) as db:
        assert db.execute(
            "SELECT step,state FROM attempts ORDER BY rowid"
        ).fetchall() == [
            ("leave", "verified"),
            ("invite/1", "verified"),
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [True, False])
async def test_new_operation_after_service_restart_cannot_hide_unrestored_work(
    lab, unknown
):
    await lab.begin()
    fixture = lab.service._blacklist_fixture
    if unknown:
        fixture.reserve("leave")
    del lab.service._blacklist_fixture
    operation = "WSL_TEST_BLACKLIST_" + "b" * 32
    lab.renew().keywords = [operation]
    with pytest.raises(OpenError, match="unresolved"):
        await lab.begin(payload={"operation_id": operation})
    assert len(list(lab.root.rglob("fixture.sqlite3"))) == 1
    lab.upstream.assert_not_awaited()


@pytest.mark.asyncio
async def test_rebind_while_target_absent_keeps_steps_and_original_scope_frozen(lab):
    await lab.begin()
    old = lab.service._blacklist_fixture
    await helper.BlacklistMembershipTest(old).change("leave")
    lab.clock[0] += 301
    lab.renew("group_commands")
    await lab.begin()
    fixture = lab.service._blacklist_fixture
    await helper.BlacklistMembershipTest(fixture).change("invite/1")
    with pytest.raises(ProtocolError, match="no_replay"):
        fixture.reserve("leave")
    fixture.window.scopes = ["group_rules"]
    with pytest.raises(ProtocolError, match="revoked"):
        fixture.check()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route,params",
    [
        (
            "/v1/friend/delete-friend",
            {"groupId": int(GROUP), "groupMemberIds": [int(TARGET)]},
        ),
        (helper.INVITE, {"groupId": int(GROUP), "groupMemberIds": [int(TARGET)]}),
        (helper.REMOVE, {"groupId": "1459282", "groupMemberIds": [int(TARGET)]}),
        (helper.REMOVE, {"groupId": int(GROUP), "groupMemberIds": [int(RECEIVER)]}),
        (
            helper.REMOVE,
            {"groupId": int(GROUP), "groupMemberIds": [int(TARGET), int(OWNER)]},
        ),
        (
            helper.REMOVE,
            {"groupId": int(GROUP), "groupMemberIds": [int(TARGET)], "extra": 1},
        ),
    ],
)
async def test_even_reserved_raw_route_cannot_broaden_target_or_action(
    lab, route, params
):
    await lab.begin()
    fixture = lab.service._blacklist_fixture
    worker = helper.BlacklistMembershipTest(fixture)
    worker._sending = fixture.reserve("leave")
    with pytest.raises(ProtocolError, match="unreserved"):
        await worker.request(route, params)
    lab.upstream.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["deadline", "window_sender", "service_fixture"])
async def test_original_window_capability_cannot_be_extended_or_reinstalled(lab, fault):
    await lab.begin()
    fixture = lab.service._blacklist_fixture
    if fault == "deadline":
        fixture.window.expires += 1
    elif fault == "window_sender":
        fixture.window.sender = copy.copy(lab.target)
    else:
        del lab.service._blacklist_fixture
    with pytest.raises(ProtocolError, match="revoked"):
        fixture.check()


@pytest.mark.asyncio
async def test_binding_during_await_cannot_replace_another_window(lab):
    original = lab.receiver.get_moderation_members.side_effect

    async def replaced(group):
        lab.renew()
        return await original(group)

    lab.receiver.get_moderation_members.side_effect = replaced
    with pytest.raises(OpenError, match="revoked"):
        await lab.begin()
    assert not list(lab.root.rglob("fixture.sqlite3"))


@pytest.mark.asyncio
async def test_existing_native_history_is_read_only_and_does_not_count_as_test_round(
    lab,
):
    intent = native_intent(lab, state="verified", finished=1)
    native = lab.root / lab.receiver.config["id"] / "member_blacklist.sqlite3"
    with closing(sqlite3.connect(native)) as db, db:
        db.execute("UPDATE blacklist_entries SET status='removed'")
    before = native.read_bytes()
    await lab.begin()
    fixture = lab.service._blacklist_fixture
    fixture.check()
    await fixture.authorize_submission(
        lab.receiver, GROUP, TARGET, lab.target.nim_account
    )
    with closing(sqlite3.connect(fixture.path)) as db:
        assert db.execute("SELECT native_start FROM fixture").fetchone() == (1,)
    assert native.read_bytes() == before
    with pytest.raises(ProtocolError, match="unknown"):
        fixture.check(blacklist_intent=intent)
    assert native.read_bytes() == before


@pytest.mark.asyncio
async def test_durable_unknown_with_finished_flag_cannot_be_resubmitted(lab):
    await lab.begin()
    fixture = lab.service._blacklist_fixture
    intent = native_intent(lab, finished=1)
    with pytest.raises(ProtocolError, match="unknown"):
        fixture.check(blacklist_intent=intent)
    with pytest.raises(ProtocolError, match="unknown"):
        await fixture.authorize_submission(
            lab.receiver, GROUP, TARGET, lab.target.nim_account, blacklist_intent=intent
        )
    lab.upstream.assert_not_awaited()


@pytest.fixture
def abort_lab(lab):
    """Provide durable empty message ledgers for read-only abort verification."""
    for adapter in lab.bots.values():
        path = lab.root / adapter.config["id"] / "messages.sqlite3"
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE inbox(account TEXT,team TEXT,state TEXT)")
            db.execute("CREATE TABLE outbox(key TEXT,state TEXT)")
    return lab


@pytest.mark.asyncio
@pytest.mark.parametrize("reloaded", [False, True])
async def test_unsent_abort_closes_without_claiming_acceptance(abort_lab, reloaded):
    lab = abort_lab
    await lab.begin()
    fixture = lab.service._blacklist_fixture
    worker = helper.BlacklistMembershipTest(fixture)
    await worker.change("leave")
    await worker.change("invite/1")
    lab.clock[0] += 301
    if reloaded:
        del lab.service._blacklist_fixture
    result = await binding.control_blacklist(
        lab.service,
        "dashboard/offline",
        {"operation": "abort", "operation_id": OPERATION},
    )
    assert result == {
        "test_only": True,
        "restored": True,
        "outcome": "aborted",
        "passed": False,
    }
    assert getattr(lab.service, "_blacklist_fixture", None) is None
    with closing(sqlite3.connect(fixture.path)) as db:
        assert db.execute("SELECT restored FROM fixture").fetchone() == (1,)
        assert db.execute("SELECT outcome,actor FROM closure").fetchall() == [
            ("aborted", "dashboard/offline")
        ]
    assert len(lab.writes) == 2
    assert not list(lab.root.rglob("member_blacklist.sqlite3"))
    lab.renew()
    lab.receiver.test_window.keywords = ["WSL_TEST_BLACKLIST_" + "b" * 32]
    await lab.begin(payload={"operation_id": "WSL_TEST_BLACKLIST_" + "b" * 32})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "open_window",
        "unknown_membership",
        "partial_membership",
        "native_unknown",
        "native_rejected",
        "native_accepted",
        "muted",
        "target_promoted",
        "missing_member",
        "incomplete",
        "wrong_peer",
        "pending",
        "processing",
        "outbox_unknown",
        "outbox_sending",
        "missing_ledger",
        "command_unknown",
        "command_accepted",
        "paused",
        "disabled",
    ],
)
async def test_abort_never_hides_unknown_work_or_changed_state(abort_lab, fault):
    lab = abort_lab
    await lab.begin()
    fixture = lab.service._blacklist_fixture
    if fault == "partial_membership":
        fixture.reserve("leave")
        with closing(sqlite3.connect(fixture.path)) as db, db:
            db.execute("UPDATE attempts SET state='verified'")
    elif fault == "unknown_membership":
        fixture.reserve("leave")
    elif fault.startswith("native_"):
        native_intent(lab, state=fault.removeprefix("native_"), finished=1)
    elif fault == "muted":
        binding.muted_member_ids.return_value = {TARGET}
    elif fault == "target_promoted":
        lab.roster[TARGET]["groupRole"] = "GROUP_ROLE_ADMIN"
    elif fault == "missing_member":
        lab.roster.pop(TARGET)
    elif fault == "incomplete":
        lab.receiver.get_moderation_members.side_effect = None
        lab.receiver.get_moderation_members.return_value = {"complete": False}
    elif fault == "wrong_peer":
        lab.roster[TARGET]["nimId"] = "999"
    elif fault in {"pending", "processing", "outbox_unknown", "outbox_sending"}:
        path = lab.root / lab.receiver.config["id"] / "messages.sqlite3"
        with closing(sqlite3.connect(path)) as db, db:
            if fault.startswith("outbox_"):
                db.execute(
                    "INSERT INTO outbox VALUES(?,?)",
                    ("proactive/" + OPERATION + "/command", fault[7:]),
                )
            else:
                db.execute("INSERT INTO inbox VALUES(?,?,?)", (RECEIVER, GROUP, fault))
    elif fault == "missing_ledger":
        (lab.root / lab.target.config["id"] / "messages.sqlite3").unlink()
    elif fault.startswith("command_"):
        with closing(sqlite3.connect(fixture.path)) as db, db:
            db.execute("CREATE TABLE commands(operation TEXT,state TEXT)")
            db.execute("INSERT INTO commands VALUES('test',?)", (fault[8:],))
    elif fault == "paused":
        lab.service.settings["paused"] = 1
    elif fault == "disabled":
        lab.target.config["enable"] = False
    if fault != "open_window":
        lab.clock[0] += 301
    with pytest.raises(ERRORS):
        await binding.control_blacklist(
            lab.service,
            "dashboard/offline",
            {"operation": "abort", "operation_id": OPERATION},
        )
    with closing(sqlite3.connect(fixture.path)) as db:
        assert db.execute("SELECT restored FROM fixture").fetchone() == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["accepted", "preflight", "unknown"])
async def test_fixed_native_send_refreshes_sender_and_never_retries(lab, outcome):
    from astrbot.api.message_components import At, Plain

    owner = lab.bots[OWNER]
    owner.get_moderation_members = AsyncMock(
        side_effect=lab.receiver.get_moderation_members.side_effect
    )
    owner.send_by_session = AsyncMock(
        return_value={"status": "accepted", "operation_id": OPERATION + "/command"}
    )
    owner.ledger = SimpleNamespace(receipt=AsyncMock(return_value=None))
    if outcome != "accepted":
        owner.send_by_session.side_effect = ProtocolError("mention_identity")
        if outcome == "unknown":
            owner.ledger.receipt.return_value = {"status": "unknown"}
    await lab.begin()
    body = {"operation": "send", "operation_id": OPERATION}
    owner.get_moderation_members.reset_mock()
    if outcome == "accepted":
        result = await binding.control_blacklist(lab.service, "dashboard/offline", body)
        assert result["status"] == "accepted"
    else:
        with pytest.raises(ProtocolError, match="mention_identity"):
            await binding.control_blacklist(lab.service, "dashboard/offline", body)
    owner.get_moderation_members.assert_awaited_once_with(GROUP)
    args = owner.send_by_session.call_args
    assert args.args[0].session_id == GROUP
    parts = args.args[1].chain
    assert isinstance(parts[0], At) and str(parts[0].qq) == TARGET
    assert isinstance(parts[1], Plain) and parts[1].text == "黑名单"
    with closing(sqlite3.connect(lab.service._blacklist_fixture.path)) as db:
        assert db.execute("SELECT operation,state FROM commands").fetchall() == [
            (
                OPERATION + "/command",
                "not_submitted" if outcome == "preflight" else outcome,
            )
        ]
    with pytest.raises(OpenError, match="unresolved"):
        await binding.control_blacklist(lab.service, "dashboard/offline", body)
    owner.send_by_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_abort_keeps_unrelated_historical_unknown_sends_unchanged(abort_lab):
    lab = abort_lab
    await lab.begin()
    path = lab.root / lab.bots[OWNER].config["id"] / "messages.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("INSERT INTO outbox VALUES('proactive/old-ai-test','unknown')")
    before = path.read_bytes()
    lab.clock[0] += 301
    result = await binding.control_blacklist(
        lab.service,
        "dashboard/offline",
        {"operation": "abort", "operation_id": OPERATION},
    )
    assert result["passed"] is False
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_failed_preflight_aborts_without_unmute_or_native_record(abort_lab):
    lab = abort_lab
    owner = lab.bots[OWNER]
    owner.get_moderation_members = AsyncMock(
        side_effect=lab.receiver.get_moderation_members.side_effect
    )
    owner.ledger = SimpleNamespace(receipt=AsyncMock(return_value=None))
    owner.send_by_session = AsyncMock(side_effect=ProtocolError("mention_identity"))
    await lab.begin()
    with pytest.raises(ProtocolError, match="mention_identity"):
        await binding.control_blacklist(
            lab.service,
            "dashboard/offline",
            {"operation": "send", "operation_id": OPERATION},
        )
    lab.clock[0] += 301
    result = await binding.control_blacklist(
        lab.service,
        "dashboard/offline",
        {"operation": "abort", "operation_id": OPERATION},
    )
    assert result["outcome"] == "aborted" and result["passed"] is False
    assert lab.writes == []
    assert not list(lab.root.rglob("member_blacklist.sqlite3"))
    owner.send_by_session.assert_awaited_once()
