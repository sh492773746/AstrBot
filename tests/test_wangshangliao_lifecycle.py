"""Native-admin admission and corroborated, exact-group lifecycle regressions."""

import asyncio
import copy
import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import open_service as module
from astrbot.builtin_stars.wangshangliao_moderation.lifecycle import (
    clear_shared_group_data,
    clear_working_data,
    self_membership_notice,
)
from astrbot.builtin_stars.wangshangliao_moderation.open_store import (
    DEFAULT_RULES,
    OpenError,
    OpenStore,
)
from astrbot.core.platform.sources.wangshangliao.group_scope import group_config


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    context = SimpleNamespace(
        platform_manager=SimpleNamespace(platform_insts=[]),
        get_config=lambda *args: {"admins_id": []},
    )
    plugin = SimpleNamespace(context=context, commands=SimpleNamespace(selections={}))
    service = module.OpenService(plugin, tmp_path / "open", integrated=True)
    service.store.discover(
        "99", {"5": {"name": "test", "team": "1005"}}, allow_auto=True
    )
    state = {
        "present": True,
        "own_role": "GROUP_ROLE_ADMIN",
        "inviter_role": "GROUP_ROLE_ADMIN",
        "kind": "INVITATION",
        "stamp": time.time() - 1,
        "read_error": False,
        "exit_error": False,
        "exit_committed": True,
        "hook": None,
    }
    adapter = SimpleNamespace(
        account="99",
        nim_account="1099",
        config={
            "id": "fixture",
            "account_id": "99",
            "_business_epoch": 1,
            "_business_active": True,
        },
        connection_state="online",
        enabled_groups=set(),
        groups={},
        group_names={},
        group_catalog={},
        members={},
        verified_nickname="bot",
        dispatch_wakeup=asyncio.Event(),
        refresh_member_mapping=AsyncMock(),
        send_reply_text=AsyncMock(),
        workers={},
        events=(),
        meta=lambda: SimpleNamespace(name="wangshangliao", id="fixture"),
    )

    async def request(route, params, *, authorize=None):
        if authorize:
            authorize()
        if state["read_error"] and route != "/v1/group/leave-group":
            raise TimeoutError("read_unavailable")
        if route == "/v1/group/get-group-list":
            return {
                "owner": [],
                "member": [
                    {
                        "groupId": 5,
                        "groupCloudId": "1005",
                        "groupName": "test",
                        "me": {"role": state["own_role"]},
                    }
                ]
                if state["present"]
                else [],
            }
        if route == "/v1/group/get-apply-logs":
            return {
                "list": [
                    {
                        "groupId": "5",
                        "applyId": "123",
                        "groupApplyType": state["kind"],
                        "state": "MEMBER_STATE_GOOD",
                        "applyTime": datetime.fromtimestamp(
                            state["stamp"], timezone.utc
                        ).isoformat(),
                        "applicant": {"userId": "99", "nimId": "1099"},
                        "inviter": {"userId": "1"},
                    }
                ]
            }
        if route == "/v1/group/get-group-members":
            if state["hook"]:
                state["hook"]()
            return {
                "groupMemberInfo": [
                    {"userId": "99", "nimId": "1099", "groupRole": state["own_role"]},
                    {
                        "userId": "1",
                        "nimId": "1001",
                        "groupRole": state["inviter_role"],
                    },
                ]
            }
        assert route == "/v1/group/leave-group" and params == {"groupId": 5}
        if state["exit_committed"]:
            state["present"] = False
        if state["exit_error"]:
            raise TimeoutError("unknown_exit")
        return {}

    adapter.business = SimpleNamespace(request=AsyncMock(side_effect=request))
    context.platform_manager.platform_insts = [adapter]
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(module, "instance_dir", lambda _: tmp_path / "native")
    monkeypatch.setattr(module, "get_astrbot_data_path", lambda: tmp_path / "data")
    return service, adapter, state


def notice(kind=1, *, stamp=None, members=None, sender="1001", message="777"):
    """Build provider metadata used by multiple isolated lifecycle cases."""
    return {
        0: "1",
        1: "1005",
        2: sender,
        7: str(stamp or time.time()),
        8: "5",
        10: json.dumps({"id": kind, "data": {"ids": members or ["1099"]}}),
        12: message,
    }


@pytest.mark.parametrize(
    "kind,expected",
    [
        (0, "join"),
        (1, "kick"),
        (2, "leave"),
        (7, "role_gained"),
        (8, "role_lost"),
        (402, "kick"),
        (403, "leave"),
        (408, "role_lost"),
    ],
)
def test_metadata_decodes_only_this_transport_robot(kind, expected):
    value = notice(kind, sender="1099" if expected == "leave" else "1001")
    assert self_membership_notice(value, "1099")["kind"] == expected
    assert self_membership_notice(value, "2099") is None


@pytest.mark.parametrize(
    "change",
    [
        "text",
        "unrelated",
        "bool",
        "future",
        "malformed",
        "huge",
        "deep",
        "surrogate",
        "wrong_team",
    ],
)
def test_user_text_or_malformed_metadata_cannot_trigger_cleanup(change):
    value = notice()
    if change == "text":
        value[8] = "100"
    elif change == "unrelated":
        value[10] = json.dumps({"id": 1, "data": {"ids": ["2099"]}})
    elif change == "bool":
        value[10] = json.dumps({"id": True, "data": {"ids": ["1099"]}})
    elif change == "future":
        value[7] = str(time.time() + 60)
    elif change == "malformed":
        value[10] = "{"
    elif change == "huge":
        value[10] = "x" * 16385
    elif change == "deep":
        value[10] = '{"id":1,"data":' + "[" * 1500 + "0" + "]" * 1500 + "}"
    elif change == "surrogate":
        value[10] = "\ud800"
    else:
        value[1] = "not/a/team"
    assert self_membership_notice(value, "1099") is None


@pytest.mark.asyncio
async def test_native_admin_inviter_admits_without_automatic_authority(lifecycle):
    service, adapter, _ = lifecycle
    await service.refresh()
    await service.check_inviter(adapter, "5")
    tenant = service.store.tenant("99", "5")
    assert tenant["admission"] == "verified" and not tenant["active"]
    assert tenant["managers"] == [] and tenant["inviter"] == "1"
    assert not any(
        c.args[0].endswith("leave-group")
        for c in adapter.business.request.await_args_list
    )
    result = await service.activate(module.ActivationContext(adapter, "1", 1), "5")
    assert "已开通" in result and service.store.tenant("99", "5")["managers"] == ["1"]


@pytest.mark.asyncio
async def test_delayed_join_uses_confirmed_departure_not_notification_latency(
    lifecycle,
):
    service, adapter, state = lifecycle
    now = time.time()
    service.store.membership_notice("99", "1005", "leave-1", "leave", now - 600)
    service.store.membership_finish("99", "1005", "leave-1", "departure_verified")
    state["stamp"] = now - 145
    await service.refresh()
    await service.check_inviter(adapter, "5")
    row = service.store.tenant("99", "5")
    assert row["inviter"] == "1" and row["admission"] == "verified"
    assert not row["active"] and not row["managers"]
    assert not any(
        c.args[0].endswith("leave-group")
        for c in adapter.business.request.await_args_list
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [-1, 0])
async def test_invitation_at_or_before_last_confirmed_departure_is_rejected(
    lifecycle, offset
):
    service, adapter, state = lifecycle
    stamp = int(time.time()) - 600
    service.store.membership_notice("99", "1005", "leave-1", "leave", stamp)
    service.store.membership_finish("99", "1005", "leave-1", "departure_verified")
    state["stamp"] = stamp + offset
    await service.refresh()
    with pytest.raises(OpenError, match="inviter_unproven"):
        await service.check_inviter(adapter, "5")
    assert not service.store.tenant("99", "5")["inviter"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["other_account", "other_team", "unconfirmed"])
async def test_unrelated_departures_cannot_relax_the_invitation_window(
    lifecycle, change
):
    service, adapter, state = lifecycle
    service.store.membership_notice(
        "199" if change == "other_account" else "99",
        "2005" if change == "other_team" else "1005",
        "leave-1",
        "leave",
        time.time() - 600,
    )
    if change != "unconfirmed":
        service.store.membership_finish(
            "199" if change == "other_account" else "99",
            "2005" if change == "other_team" else "1005",
            "leave-1",
            "departure_verified",
        )
    state["stamp"] = time.time() - 145
    await service.refresh()
    with pytest.raises(OpenError, match="inviter_unproven"):
        await service.check_inviter(adapter, "5")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role", ["GROUP_ROLE_OWNER", "GROUP_ROLE_ADMIN", "GROUP_ROLE_MEMBER"]
)
async def test_discovery_enforces_inviter_role_without_waiting_for_command(
    lifecycle, role
):
    service, adapter, state = lifecycle
    state["inviter_role"] = role
    await service.refresh()
    await service.discover(adapter)
    row = service.store.tenant("99", "5")
    assert not row["active"] and not row["managers"]
    assert row["admission"] == (
        "rejected" if role == "GROUP_ROLE_MEMBER" else "verified"
    )
    assert sum(
        c.args[0].endswith("leave-group")
        for c in adapter.business.request.await_args_list
    ) == (1 if role == "GROUP_ROLE_MEMBER" else 0)


@pytest.mark.asyncio
async def test_bot_without_native_admin_can_be_bound_but_never_punishes(lifecycle):
    service, adapter, state = lifecycle
    state["own_role"] = "GROUP_ROLE_MEMBER"
    await service.refresh()
    reply = await service.activate(module.ActivationContext(adapter, "1", 1), "5")
    assert "本群已开通" in reply and "本群功能暂不可用" in reply
    assert "设为群管理员" in reply and "广告拦截和防刷屏已开启" not in reply
    row = service.store.tenant("99", "5")
    assert row["active"] and row["managers"] == ["1"]
    policy = service.policy_values("99", {"enabled_groups": [], "moderation": {}})
    assert not group_config(policy, "5")["moderation"]["automation_enabled"]
    assert policy["moderation"]["permissions"]["5"] == []


@pytest.mark.asyncio
async def test_final_roster_cannot_activate_an_inviter_demoted_during_claim(lifecycle):
    service, adapter, state = lifecycle
    await service.refresh()
    rows = [
        {"userId": "99", "nimId": "1099", "groupRole": "GROUP_ROLE_ADMIN"},
        {"userId": "1", "nimId": "1001", "groupRole": "GROUP_ROLE_ADMIN"},
        {"userId": "2", "nimId": "1002", "groupRole": "GROUP_ROLE_ADMIN"},
    ]
    count = 0

    async def roster(*args, **kwargs):
        nonlocal count
        count += 1
        if count >= 3:
            rows[1]["groupRole"] = "GROUP_ROLE_MEMBER"
            state["inviter_role"] = "GROUP_ROLE_MEMBER"
        return {row["userId"]: dict(row) for row in rows}

    service.roster = roster
    code = service.store.mint("99", "5", "dashboard")
    with pytest.raises(OpenError, match="inviter_not_native_admin"):
        await service.activate(module.ActivationContext(adapter, "2", 1), "5", code)
    row = service.store.tenant("99", "5")
    assert not row["active"] and row["admission"] == "rejected"
    assert (
        sum(
            c.args[0].endswith("leave-group")
            for c in adapter.business.request.await_args_list
        )
        == 1
    )


@pytest.mark.asyncio
async def test_ordinary_inviter_exits_once_and_only_cleans_after_signed_absence(
    lifecycle,
):
    service, adapter, state = lifecycle
    state["inviter_role"] = "GROUP_ROLE_MEMBER"
    await service.refresh()
    with pytest.raises(OpenError, match="inviter_not_native_admin"):
        await service.check_inviter(adapter, "5")
    tenant = service.store.tenant("99", "5")
    assert tenant["admission"] == "rejected" and tenant["leave_state"] == "accepted"
    assert tenant["cleanup_epoch"] == 0
    await service.discover(adapter)
    tenant = service.store.tenant("99", "5")
    assert not tenant["present"] and not tenant["active"]
    assert tenant["cleanup_epoch"] == tenant["cleanup_done"] > 0
    await service.discover(adapter)
    assert (
        sum(
            c.args[0].endswith("leave-group")
            for c in adapter.business.request.await_args_list
        )
        == 1
    )


@pytest.mark.asyncio
async def test_unknown_exit_is_not_retried_or_treated_as_deletion_proof(lifecycle):
    service, adapter, state = lifecycle
    state.update(inviter_role="GROUP_ROLE_MEMBER", exit_error=True)
    await service.refresh()
    with pytest.raises(OpenError, match="inviter_not_native_admin"):
        await service.check_inviter(adapter, "5")
    await service.discover(adapter)
    tenant = service.store.tenant("99", "5")
    assert tenant["leave_state"] == "unknown" and tenant["cleanup_epoch"] == 0
    service.store = OpenStore(service.root)
    await service.refresh()
    with pytest.raises(OpenError, match="inviter_not_native_admin"):
        await service.check_inviter(adapter, "5")
    assert (
        sum(
            c.args[0].endswith("leave-group")
            for c in adapter.business.request.await_args_list
        )
        == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["APPLY", "KICK", "QUIT"])
async def test_unknown_or_non_invitation_source_never_uses_owner_as_inviter(
    lifecycle, kind
):
    service, adapter, state = lifecycle
    state["kind"] = kind
    await service.refresh()
    with pytest.raises(OpenError, match="inviter_unproven"):
        await service.check_inviter(adapter, "5")
    assert service.store.tenant("99", "5")["managers"] == []
    assert not any(
        c.args[0].endswith("leave-group")
        for c in adapter.business.request.await_args_list
    )


@pytest.mark.asyncio
async def test_old_invitation_cannot_admit_or_force_exit(lifecycle):
    service, adapter, state = lifecycle
    state.update(stamp=time.time() - 600, inviter_role="GROUP_ROLE_MEMBER")
    await service.refresh()
    with pytest.raises(OpenError, match="inviter_unproven"):
        await service.check_inviter(adapter, "5")
    assert not any(
        c.args[0].endswith("leave-group")
        for c in adapter.business.request.await_args_list
    )


@pytest.mark.asyncio
async def test_membership_change_during_final_roster_prevents_exit(lifecycle):
    service, adapter, state = lifecycle
    state["inviter_role"] = "GROUP_ROLE_MEMBER"

    def mutate():
        with service.store.connect(write=True) as db:
            db.execute(
                "UPDATE tenants SET version=version+1 WHERE account='99' AND group_id='5'"
            )

    state["hook"] = mutate
    await service.refresh()
    with pytest.raises(OpenError, match="membership_changed"):
        await service.check_inviter(adapter, "5")
    assert not any(
        c.args[0].endswith("leave-group")
        for c in adapter.business.request.await_args_list
    )


@pytest.mark.asyncio
async def test_open_code_never_bypasses_native_admin_requirement(lifecycle):
    service, adapter, state = lifecycle
    code = service.store.mint("99", "5", "dashboard")
    state["inviter_role"] = "GROUP_ROLE_MEMBER"
    await service.refresh()
    with pytest.raises(OpenError, match="permission_denied"):
        await service.activate(module.ActivationContext(adapter, "1", 1), "5", code)
    assert not service.store.tenant("99", "5")["active"]


@pytest.mark.asyncio
async def test_directory_absence_alone_retains_working_rules(lifecycle):
    service, adapter, state = lifecycle
    with service.store.connect(write=True) as db:
        db.execute(
            "UPDATE tenants SET rules=? WHERE account='99'",
            (json.dumps({**DEFAULT_RULES, "keywords": ["keep"]}),),
        )
    state["present"] = False
    await service.refresh()
    await service.discover(adapter)
    row = service.store.tenant("99", "5")
    assert not row["present"] and row["rules"]["keywords"] == ["keep"]
    assert row["cleanup_epoch"] == 0


@pytest.mark.asyncio
async def test_read_failure_does_not_erase_or_mark_departure(lifecycle):
    service, adapter, state = lifecycle
    await service.refresh()
    before = service.store.tenant("99", "5")
    state["read_error"] = True
    with pytest.raises(TimeoutError):
        await service.discover(adapter)
    assert service.store.tenant("99", "5") == before


@pytest.mark.asyncio
async def test_confirmed_kick_cleans_and_replay_does_not_reset_again(lifecycle):
    service, adapter, state = lifecycle
    await service.refresh()
    adapter.member_snapshots = {"5": "private roster", "6": "other roster"}
    adapter.member_refresh_locks = {"5": asyncio.Lock()}
    adapter.group_directory_state = {"5": {"complete": True}, "6": {"complete": True}}
    adapter.members = {"5": {"1": "1001"}, "6": {"2": "1002"}}
    selection_key = (id(adapter), "99", "1", "private/1/1001")
    other_key = (id(adapter), "199", "1", "private/1/1001")
    service.plugin.commands.selections[selection_key] = {"groups": ["5", "6"]}
    service.plugin.commands.selections[other_key] = {"groups": ["5"]}
    state["present"] = False
    incoming = notice()
    assert await service.receive_membership(adapter, incoming)
    await service.discover(adapter)
    row = service.store.tenant("99", "5")
    assert (
        row["admission"] == "departed"
        and row["cleanup_done"] == row["cleanup_epoch"] > 0
    )
    assert adapter.member_snapshots == {"6": "other roster"}
    assert adapter.members == {"6": {"2": "1002"}}
    assert adapter.group_directory_state == {"6": {"complete": True}}
    assert selection_key not in service.plugin.commands.selections
    assert other_key in service.plugin.commands.selections
    assert not adapter.member_refresh_locks["5"].locked()
    assert await service.receive_membership(adapter, incoming)
    await service.discover(adapter)
    assert service.store.tenant("99", "5")["epoch"] == row["epoch"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [1, 8])
async def test_restart_restores_pending_membership_fence_before_dispatch(
    lifecycle, kind
):
    service, adapter, _ = lifecycle
    await service.refresh()
    await service.check_inviter(adapter, "5")
    row = service.store.tenant("99", "5")
    service.store.activate("99", "5", "1", epoch=row["epoch"], version=row["version"])
    await service.receive_membership(adapter, notice(kind))
    restarted = module.OpenService(service.plugin, service.root, integrated=True)
    await restarted.refresh()
    assert ("99", "5") in restarted.suspended_groups
    assert not restarted.policy_values("99", {"enabled_groups": [], "moderation": {}})[
        "enabled_groups"
    ]
    assert restarted.management.assignments == {}


@pytest.mark.asyncio
async def test_role_notice_is_not_released_until_native_role_is_verified(lifecycle):
    service, adapter, state = lifecycle
    await service.refresh()
    await service.check_inviter(adapter, "5")
    row = service.store.tenant("99", "5")
    service.store.activate("99", "5", "1", epoch=row["epoch"], version=row["version"])
    await service.refresh()
    await service.receive_membership(adapter, notice(8))
    state["own_role"] = ""
    await service.discover(adapter)
    assert ("99", "5") in service.suspended_groups
    assert service.store.membership_pending("99")[0]["kind"] == "role_lost"
    assert not service.policy_values("99", {"enabled_groups": [], "moderation": {}})[
        "enabled_groups"
    ]
    state["own_role"] = "GROUP_ROLE_MEMBER"
    await service.discover(adapter)
    assert ("99", "5") not in service.suspended_groups
    assert not service.store.membership_pending("99")
    row = service.store.tenant("99", "5")
    assert row["native_role"] == "GROUP_ROLE_MEMBER" and row["active"]
    assert row["managers"] == ["1"] and row["cleanup_epoch"] == 0
    policy = service.policy_values("99", {"enabled_groups": [], "moderation": {}})
    assert not group_config(policy, "5")["moderation"]["automation_enabled"]


@pytest.mark.asyncio
async def test_trusted_join_notice_survives_restart_before_first_discovery(lifecycle):
    service, adapter, _ = lifecycle
    with service.store.connect(write=True) as db:
        db.execute("DELETE FROM tenants")
    await service.receive_membership(adapter, notice(0))
    restarted = module.OpenService(service.plugin, service.root, integrated=True)
    await restarted.refresh()
    await restarted.discover(adapter)
    row = restarted.store.tenant("99", "5")
    assert row["auto_claim"] and row["admission"] == "verified"
    assert not row["active"] and row["managers"] == []
    assert not restarted.store.membership_pending("99")


@pytest.mark.asyncio
async def test_cancelled_cleanup_resumes_without_reopening_group(
    lifecycle, monkeypatch
):
    service, adapter, state = lifecycle
    await service.refresh()
    await service.receive_membership(adapter, notice())
    state["present"] = False
    original = module.clear_working_data
    monkeypatch.setattr(
        module,
        "clear_working_data",
        lambda *args: (_ for _ in ()).throw(TimeoutError()),
    )
    with pytest.raises(TimeoutError):
        await service.discover(adapter)
    row = service.store.tenant("99", "5")
    assert not row["present"] and row["cleanup_epoch"] > row["cleanup_done"]
    restarted = module.OpenService(service.plugin, service.root, integrated=True)
    await restarted.refresh()
    monkeypatch.setattr(module, "clear_working_data", original)
    await restarted.discover(adapter)
    row = restarted.store.tenant("99", "5")
    assert not row["present"] and row["cleanup_epoch"] == row["cleanup_done"]


@pytest.mark.asyncio
async def test_delayed_kick_after_new_join_never_erases_current_group(lifecycle):
    service, adapter, state = lifecycle
    await service.refresh()
    old = service.store.tenant("99", "5")
    state["stamp"] = time.time() - 1
    assert await service.receive_membership(adapter, notice(stamp=time.time() - 2))
    await service.discover(adapter)
    row = service.store.tenant("99", "5")
    assert row["present"] and row["cleanup_epoch"] == 0
    assert row["epoch"] == old["epoch"]


def test_departure_preserves_ownership_other_accounts_quotas_and_rejects_old_code(
    tmp_path,
):
    store = OpenStore(tmp_path)
    catalog = {
        "5": {"name": "test", "team": "1005"},
        "6": {"name": "other", "team": "1006"},
    }
    store.discover("99", catalog, allow_auto=True)
    store.discover("199", catalog, allow_auto=True)
    initial = store.tenant("99", "5")
    code = store.mint("99", "5", "root")
    store.activate(
        "99", "5", "1", code=code, epoch=initial["epoch"], version=initial["version"]
    )
    store.native_role("99", "5", initial["epoch"], "GROUP_ROLE_ADMIN")
    old = store.tenant("99", "5")
    store.change(
        "99", "5", old["version"], "1", rules={**old["rules"], "keywords": ["old"]}
    )
    other = copy.deepcopy(
        [t for t in store.tenants() if (t["account"], t["group_id"]) != ("99", "5")]
    )
    store.reserve("peer/1", "quota-preserved")
    epoch = store.depart("99", "5", old["epoch"], "native/777")
    row = store.tenant("99", "5")
    assert (
        row["managers"] == ["1"] and row["rules"] == DEFAULT_RULES and not row["active"]
    )
    store.discover("99", catalog, allow_auto=True)
    assert not store.tenant("99", "5")["present"]
    store.cleanup_finished("99", "5", epoch)
    store.discover("99", catalog, allow_auto=True)
    row = store.tenant("99", "5")
    assert row["epoch"] > old["epoch"]
    store.prove_inviter("99", "5", "2", "new-proof", row["epoch"], time.time())
    with pytest.raises(OpenError, match="original_manager_required"):
        store.activate("99", "5", "2", epoch=row["epoch"], version=row["version"])
    with pytest.raises(OpenError, match="activation_code_invalid"):
        store.activate(
            "99", "5", "1", code=code, epoch=row["epoch"], version=row["version"]
        )
    assert [
        t for t in store.tenants() if (t["account"], t["group_id"]) != ("99", "5")
    ] == other
    assert store.usage()["used"] == 1


@pytest.mark.asyncio
async def test_native_role_loss_keeps_rules_and_restoration_invalidates_old_actions(
    lifecycle,
):
    service, adapter, state = lifecycle
    await service.refresh()
    await service.check_inviter(adapter, "5")
    row = service.store.tenant("99", "5")
    service.store.activate("99", "5", "1", epoch=row["epoch"], version=row["version"])
    before = service.store.tenant("99", "5")
    state["own_role"] = "GROUP_ROLE_MEMBER"
    await service.discover(adapter)
    lost = service.store.tenant("99", "5")
    assert lost["rules"] == before["rules"] and lost["managers"] == before["managers"]
    assert (
        lost["cleanup_epoch"] == 0
        and lost["active"]
        and lost["version"] > before["version"]
    )
    overlay = service.policy_values("99", {"enabled_groups": [], "moderation": {}})
    assert not group_config(overlay, "5")["moderation"]["automation_enabled"]
    state["own_role"] = "GROUP_ROLE_ADMIN"
    await service.discover(adapter)
    restored = service.store.tenant("99", "5")
    assert (
        restored["version"] > lost["version"] and restored["rules"] == before["rules"]
    )
    assert group_config(
        service.policy_values("99", {"enabled_groups": [], "moderation": {}}), "5"
    )["moderation"]["automation_enabled"]


def test_native_cleanup_scrubs_contents_but_keeps_financial_and_unknown_receipts(
    tmp_path,
):
    with closing(sqlite3.connect(tmp_path / "messages.sqlite3")) as db, db:
        db.executescript("""
            CREATE TABLE inbox(account TEXT,team TEXT,message TEXT,payload TEXT,state TEXT,
                sender TEXT,completed_at REAL,pruned_at REAL);
            CREATE TABLE outbox(key TEXT,state TEXT);
            CREATE TABLE sync(account TEXT,cursor TEXT);
        """)
        db.executemany(
            "INSERT INTO inbox VALUES(?,?,?,?,?,?,NULL,NULL)",
            [
                ("99", "5", "old", '{"text":"secret"}', "pending", "1"),
                ("199", "5", "keep", '{"text":"other account"}', "processed", "2"),
                ("99", "6", "keep2", '{"text":"other group"}', "processed", "3"),
            ],
        )
        db.execute("INSERT INTO outbox VALUES('99/5/unknown','unknown')")
        db.execute("INSERT INTO sync VALUES('99','cursor-kept')")
    with closing(sqlite3.connect(tmp_path / "activities.sqlite3")) as db, db:
        db.execute(
            "CREATE TABLE invite_credits(account TEXT,group_id TEXT,member TEXT,amount INTEGER)"
        )
        db.execute("INSERT INTO invite_credits VALUES('99','5','1',600)")
    counts = clear_working_data(tmp_path, "99", "5", "1005", lambda: None)
    assert counts["messages.sqlite3"] == 1
    with closing(sqlite3.connect(tmp_path / "messages.sqlite3")) as db:
        assert db.execute(
            "SELECT payload,state,sender FROM inbox WHERE message='old'"
        ).fetchone() == ("{}", "departed", "")
        assert (
            db.execute("SELECT payload FROM inbox WHERE message='keep'").fetchone()[0]
            == '{"text":"other account"}'
        )
        assert db.execute("SELECT state FROM outbox").fetchone()[0] == "unknown"
        assert db.execute("SELECT cursor FROM sync").fetchone()[0] == "cursor-kept"
    with closing(sqlite3.connect(tmp_path / "activities.sqlite3")) as db:
        assert db.execute("SELECT amount FROM invite_credits").fetchone()[0] == 600


def test_cleanup_guard_failure_rolls_back_message_contents(tmp_path):
    with closing(sqlite3.connect(tmp_path / "messages.sqlite3")) as db, db:
        db.execute(
            "CREATE TABLE inbox(account TEXT,team TEXT,message TEXT,payload TEXT,state TEXT,sender TEXT,completed_at REAL,pruned_at REAL)"
        )
        db.execute(
            "INSERT INTO inbox VALUES('99','5','old','{\"text\":\"keep\"}','pending','1',NULL,NULL)"
        )
    calls = 0

    def guard():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OpenError("membership_changed")

    with pytest.raises(OpenError):
        clear_working_data(tmp_path, "99", "5", "1005", guard)
    with closing(sqlite3.connect(tmp_path / "messages.sqlite3")) as db:
        assert db.execute("SELECT payload,state FROM inbox").fetchone() == (
            '{"text":"keep"}',
            "pending",
        )


def test_shared_cleanup_requires_exact_account_group_route_and_preserves_finance(
    tmp_path,
):
    root = tmp_path / "plugin_data/wangshangliao_cashback"
    root.mkdir(parents=True)
    with closing(sqlite3.connect(root / "cashback.sqlite3")) as db, db:
        db.execute("CREATE TABLE entries(amount INTEGER,state TEXT)")
        db.execute("INSERT INTO entries VALUES(600,'pending')")
    with closing(sqlite3.connect(tmp_path / "data_v4.db")) as db, db:
        db.execute("CREATE TABLE platform_message_history(user_id TEXT,content TEXT)")
        db.executemany(
            "INSERT INTO platform_message_history VALUES(?,?)",
            [
                ("wangshangliao_old:GroupMessage:99/5", "clear"),
                ("wangshangliao_new:GroupMessage:99/5/wsl-ai", "clear2"),
                ("wangshangliao_old:GroupMessage:199/5", "other account"),
                ("wangshangliao_old:GroupMessage:99/51", "other group"),
                ("telegram:GroupMessage:99/5", "other platform"),
                ("wangshangliao_old:FriendMessage:99/5", "private"),
            ],
        )
    result = clear_shared_group_data(
        tmp_path,
        "99",
        "5",
        lambda: None,
        platforms=[
            {"id": "wangshangliao_old", "type": "wangshangliao"},
            {"id": "wangshangliao_new", "type": "wangshangliao"},
            {"id": "telegram", "type": "telegram"},
        ],
    )
    assert result["data_v4.db"] == 2
    with closing(sqlite3.connect(tmp_path / "data_v4.db")) as db:
        assert (
            db.execute("SELECT count(*) FROM platform_message_history").fetchone()[0]
            == 4
        )
    with closing(sqlite3.connect(root / "cashback.sqlite3")) as db:
        assert db.execute("SELECT * FROM entries").fetchone() == (600, "pending")


@pytest.mark.asyncio
async def test_automatic_requests_have_cooldown_and_exit_requires_durable_reservation(
    lifecycle,
):
    service, adapter, _ = lifecycle
    await service.refresh()
    await service.membership_request(adapter, "/v1/group/get-group-list", {"v": "0"})
    await service.membership_request(adapter, "/v1/group/get-group-list", {"v": "0"})
    assert module.asyncio.sleep.await_args.args[0] >= 2.9
    with pytest.raises(OpenError, match="membership_exit_denied"):
        await service.membership_request(
            adapter, "/v1/group/leave-group", {"groupId": 5}
        )
    with pytest.raises(OpenError, match="membership_route_denied"):
        await service.membership_request(
            adapter, "/v1/group/remove-group-member", {"groupId": 5}
        )
