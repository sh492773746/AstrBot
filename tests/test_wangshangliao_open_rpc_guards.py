"""Offline RPC barriers for exact tenant authority at admission and commit."""

import asyncio
import copy
import json
import sqlite3
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from astrbot.builtin_stars.wangshangliao_moderation import open_rpc
from astrbot.builtin_stars.wangshangliao_moderation.open_rpc import OpenCall, OpenRPC
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.platform.sources.wangshangliao.storage import Ledger
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from tests.test_wangshangliao_integrated_service import service


@pytest_asyncio.fixture
async def rpc_scope(tmp_path):
    """Provide real tenant and ledger transactions with a synthetic roster API.

    Args:
        tmp_path: Isolated pytest data directory.

    Yields:
        Service, offline adapter and mutable upstream member records.
    """
    value = service(tmp_path)
    catalog = {
        group: {"name": "Offline " + group, "team": "100" + group}
        for group in ("5", "6")
    }
    for account in ("99", "199"):
        value.store.discover(account, catalog)
        for group in catalog:
            value.store.import_group(
                account,
                group,
                {"advertising": True, "flood": True, "keywords": []},
                ["1"],
                "system/offline",
            )
            tenant = value.store.tenant(account, group)
            value.store.native_role(account, group, tenant["epoch"], "GROUP_ROLE_ADMIN")
    await value.refresh()
    rows = [
        {"userId": "99", "nimId": "1099", "groupRole": "GROUP_ROLE_ADMIN"},
        {"userId": "1", "nimId": "1001", "groupRole": "GROUP_ROLE_OWNER"},
        {"userId": "2", "nimId": "1002", "groupRole": "GROUP_ROLE_MEMBER"},
    ]

    async def request(path, payload):
        assert path == "/v1/group/get-group-members"
        assert payload["groupId"] in {5, 6}
        return {"groupMemberInfo": copy.deepcopy(rows)}

    ledger = Ledger(tmp_path / "rpc-messages.sqlite3")
    await ledger.open()

    class OfflineAdapter:
        account = "99"
        nim_account = "1099"
        connection_state = "online"
        dispatch_wakeup = asyncio.Event()
        business = SimpleNamespace(request=AsyncMock(side_effect=request))
        members = {
            group: {row["userId"]: row["nimId"] for row in rows} for group in catalog
        }

        @property
        def config(self):
            return value.policy_values(
                self.account,
                {
                    "id": "rpc-offline",
                    "account_id": self.account,
                    "enabled_groups": ["5", "6"],
                    "_business_active": True,
                    "_business_epoch": 1,
                    "_business_revision": "offline-base",
                },
            )

        def meta(self):
            return PlatformMetadata("wangshangliao", "offline", id="rpc-offline")

    adapter = OfflineAdapter()
    adapter.ledger = ledger
    value.context.platform_manager.platform_insts = [adapter]
    await value.refresh()
    await ledger.db.executemany(
        "INSERT INTO review_holds VALUES('99',?,?, 'offline')",
        [(group, time.time() - 600) for group in catalog],
    )
    await ledger.db.commit()
    ledger.review_groups.update(catalog)
    try:
        yield value, adapter, rows
    finally:
        await ledger.close()


def tenant_payload(value, *, paused=False):
    """Build a total-control change for one exact tenant generation.

    Args:
        value: Isolated integrated service.
        paused: Requested tenant pause value.

    Returns:
        Server-bound control payload with no authority inferred from its actor.
    """
    tenant = value.store.tenant("99", "5")
    return {
        "account": "99",
        "group_id": "5",
        "version": tenant["version"],
        "rules": tenant["rules"],
        "managers": ["1", "2"],
        "paused": paused,
    }


@pytest.mark.asyncio
async def test_tenant_members_can_read_only_current_native_mutes(rpc_scope):
    value, adapter, _ = rpc_scope
    adapter.groups = {"5": "1005", "6": "1006"}
    adapter.nim = SimpleNamespace(
        get_team_muted_members=AsyncMock(return_value={"1002"})
    )
    reply = await OpenRPC(value).dispatch(
        OpenCall(
            action="tenant_members",
            actor="dashboard/offline",
            payload={"account": "99", "group_id": "5", "muted_only": True},
        )
    )
    assert [row["id"] for row in reply["members"]] == ["2"]
    adapter.nim.get_team_muted_members.assert_awaited_once_with("1005")


@pytest.mark.asyncio
async def test_failed_native_mute_query_preserves_the_protocol_error(rpc_scope):
    value, adapter, _ = rpc_scope
    adapter.groups = {"5": "1005"}
    adapter.nim = SimpleNamespace(
        get_team_muted_members=AsyncMock(
            side_effect=ProtocolError("mute_list_rejected")
        )
    )
    with pytest.raises(ProtocolError, match="mute_list_rejected"):
        await OpenRPC(value).dispatch(
            OpenCall(
                action="tenant_members",
                actor="dashboard/offline",
                payload={"account": "99", "group_id": "5", "muted_only": True},
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [1, "true", None, [], {}])
async def test_tenant_mute_filter_requires_a_real_boolean(rpc_scope, flag):
    value, _, _ = rpc_scope
    with pytest.raises(OpenError, match="tenant_arguments"):
        await OpenRPC(value).dispatch(
            OpenCall(
                action="tenant_members",
                actor="dashboard/offline",
                payload={"account": "99", "group_id": "5", "muted_only": flag},
            )
        )


@pytest.mark.asyncio
async def test_billing_rpc_derives_identity_and_requires_self_consent(rpc_scope):
    value, _, _ = rpc_scope
    rpc = OpenRPC(value)
    value.plans.configure(1, "客服", "测试客服", "1000", "offline")
    state = value.plans.snapshot("nim/1001", "1")
    tenant = value.store.tenant("99", "5")
    payload = {
        "account": "99",
        "group_id": "5",
        "tenant_version": tenant["version"],
        "uid": "1",
        "version": state["version"],
        "tier": "plus",
        "received_cny": "134",
    }
    preview = await rpc.dispatch(
        OpenCall(action="plan_preview", actor="dashboard/offline", payload=payload)
    )
    assert preview["principal"] == "nim/1001"
    assert not value.plans.status()["subscriptions"]
    await rpc.dispatch(
        OpenCall(
            action="plan_grant",
            actor="dashboard/offline",
            payload={**payload, "confirmation": preview["confirmation"]},
        )
    )
    assert value.plans.snapshot("nim/1001", "1")["daily"] == 2000
    await rpc.dispatch(
        OpenCall(
            action="payer_nominate",
            actor="dashboard/offline",
            payload={
                "account": "99",
                "group_id": "5",
                "tenant_version": tenant["version"],
                "uid": "1",
                "version": 0,
                "pool_version": value.plans.snapshot("nim/1001", "1")["version"],
            },
        )
    )
    assert not value.plans.payer("99", "5")["confirmed"]
    with pytest.raises(OpenError, match="billing_payer"):
        value.plans.group_pool("99", "5")
    with pytest.raises(OpenError, match="billing_grant_invalid"):
        await rpc.dispatch(
            OpenCall(
                action="plan_preview",
                actor="dashboard/offline",
                payload={**payload, "principal": "nim/forged"},
            )
        )


@pytest.mark.asyncio
async def test_billing_rpc_rejects_nonmanager_and_stale_tenant(rpc_scope):
    value, _, _ = rpc_scope
    value.plans.configure(1, "客服", "测试客服", "1000", "offline")
    payload = {
        "account": "99",
        "group_id": "5",
        "tenant_version": value.store.tenant("99", "5")["version"],
        "uid": "2",
        "version": 0,
        "tier": "pro",
        "received_cny": "335",
    }
    rpc = OpenRPC(value)
    with pytest.raises(OpenError, match="manager_required"):
        await rpc.dispatch(
            OpenCall(action="plan_preview", actor="dashboard/offline", payload=payload)
        )
    with pytest.raises(OpenError, match="tenant_changed"):
        await rpc.dispatch(
            OpenCall(
                action="plan_preview",
                actor="dashboard/offline",
                payload={**payload, "uid": "1", "tenant_version": -1},
            )
        )
    assert not value.plans.status()["subscriptions"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "phase", "revocation"),
    [
        (action, phase, revocation)
        for action in ("tenant", "tenant_preview", "review_resume")
        for phase in ("admission", "commit")
        for revocation in (
            "pending",
            "native",
            "member",
            "version",
            "left",
            "durable_native",
            "durable_version",
        )
        if not (
            phase == "commit"
            and action != "review_resume"
            and revocation.startswith("durable_")
        )
    ],
)
async def test_rpc_writes_recheck_exact_scope_at_both_barriers(
    rpc_scope, monkeypatch, action, phase, revocation
):
    value, adapter, rows = rpc_scope
    payload = tenant_payload(value)
    rows[2]["groupRole"] = "GROUP_ROLE_ADMIN"
    if action == "tenant":
        payload["confirmation"] = value.store.preview_control(
            "99",
            "5",
            payload["version"],
            "dashboard/root",
            payload["rules"],
            payload["managers"],
            payload["paused"],
        )
    elif action == "review_resume":
        payload = {k: payload[k] for k in ("account", "group_id", "version")}
    before = value.store.tenant("99", "5")
    untouched = [
        copy.deepcopy(value.store.tenant(account, group))
        for account, group in (("99", "6"), ("199", "5"))
    ]
    waiting, release, receiving = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )

    def block():
        waiting.set()
        if not release.wait(10):
            raise TimeoutError("offline_rpc_barrier")

    if action == "review_resume":

        class ReviewBarrier(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if phase == "admission" and sql == "BEGIN IMMEDIATE":
                    block()
                result = super().execute(sql, parameters)
                if phase == "commit" and sql.startswith("DELETE FROM review_holds"):
                    block()
                return result

        monkeypatch.setattr(
            open_rpc,
            "sqlite3",
            SimpleNamespace(
                connect=lambda *args, **kwargs: sqlite3.connect(
                    *args, **kwargs, factory=ReviewBarrier
                )
            ),
        )
    elif phase == "admission":
        method = "change" if action == "tenant" else "preview_control"
        original = getattr(value.store, method)

        def delayed(*args, **kwargs):
            block()
            return original(*args, **kwargs)

        monkeypatch.setattr(value.store, method, delayed)
    else:
        original_log = value.store.log

        def delayed_log(*args, **kwargs):
            original_log(*args, **kwargs)
            if args[4] == (
                "tenant_changed" if action == "tenant" else "manager_preview"
            ):
                block()

        monkeypatch.setattr(value.store, "log", delayed_log)
    original_notice = value.store.membership_notice

    def incoming_notice(*args, **kwargs):
        receiving.set()
        return original_notice(*args, **kwargs)

    monkeypatch.setattr(value.store, "membership_notice", incoming_notice)
    task = asyncio.create_task(
        OpenRPC(value).dispatch(
            OpenCall(action=action, actor="dashboard/root", payload=payload)
        )
    )
    notice = None
    try:
        assert await asyncio.to_thread(waiting.wait, 10)
        if revocation == "pending":
            notice = asyncio.create_task(
                value.receive_membership(
                    adapter,
                    {
                        0: "1",
                        1: "1005",
                        2: "1001",
                        7: str(time.time()),
                        8: "5",
                        10: json.dumps({"id": 8, "data": {"ids": ["1099"]}}),
                        12: "777",
                    },
                )
            )
            assert await asyncio.to_thread(receiving.wait, 10)
        elif revocation == "native":
            value.tenants[("99", "5")]["native_role"] = "GROUP_ROLE_MEMBER"
        elif revocation == "member":
            value.member_evidence[("99", "5")].pop("2")
        elif revocation == "version":
            value.tenants[("99", "5")]["version"] += 1
        elif revocation.startswith("durable_"):
            with value.store.connect(write=True) as db:
                if revocation == "durable_native":
                    db.execute(
                        "UPDATE tenants SET native_role='GROUP_ROLE_MEMBER' "
                        "WHERE account='99' AND group_id='5'"
                    )
                else:
                    db.execute(
                        "UPDATE tenants SET version=version+1 "
                        "WHERE account='99' AND group_id='5'"
                    )
            assert value.tenants[("99", "5")] == before
        else:
            value.tenants[("99", "5")]["present"] = False
    finally:
        release.set()
        outcome = await asyncio.gather(task, return_exceptions=True)
        if notice is not None:
            await notice
    assert isinstance(outcome[0], (OpenError, ProtocolError)), outcome[0]
    after = value.store.tenant("99", "5")
    assert after["managers"] == before["managers"]
    assert after["rules"] == before["rules"] and after["paused"] == before["paused"]
    assert [
        value.store.tenant(account, group)
        for account, group in (("99", "6"), ("199", "5"))
    ] == untouched
    with value.store.connect() as db:
        assert not db.execute(
            "SELECT 1 FROM audit WHERE action IN ('tenant_changed','review_resumed')"
        ).fetchone()
        if action == "tenant_preview":
            assert not db.execute("SELECT 1 FROM capabilities").fetchone()
        elif action == "tenant" and revocation != "pending":
            assert db.execute("SELECT used FROM capabilities").fetchone()[0] == 0
    async with adapter.ledger.db.execute(
        "SELECT team FROM review_holds WHERE account='99' ORDER BY team"
    ) as cursor:
        assert await cursor.fetchall() == [("5",), ("6",)]
    assert adapter.ledger.review_groups == {"5", "6"}


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["tenant", "tenant_preview", "review_resume"])
async def test_total_control_can_recover_while_tenant_and_service_are_paused(
    rpc_scope, action
):
    value, adapter, rows = rpc_scope
    rows[2]["groupRole"] = "GROUP_ROLE_ADMIN"
    current = value.store.tenant("99", "5")
    value.store.change(
        "99", "5", current["version"], "dashboard/root", paused=True, control=True
    )
    value.store.configure(value.settings["version"], True, 20, 100000, "dashboard/root")
    await value.refresh()
    payload = tenant_payload(value)
    if action == "tenant":
        payload["confirmation"] = value.store.preview_control(
            "99",
            "5",
            payload["version"],
            "dashboard/root",
            payload["rules"],
            payload["managers"],
            False,
        )
    elif action == "review_resume":
        payload = {k: payload[k] for k in ("account", "group_id", "version")}
    result = await OpenRPC(value).dispatch(
        OpenCall(action=action, actor="dashboard/root", payload=payload)
    )
    if action == "tenant":
        assert result == {"saved": True}
        assert value.store.tenant("99", "5")["managers"] == ["1", "2"]
        assert not value.store.tenant("99", "5")["paused"]
    elif action == "tenant_preview":
        assert result["confirmation"] and result["expires_in"] == 600
        assert value.store.tenant("99", "5")["paused"]
    else:
        assert result == {"resumed": True}
        assert adapter.ledger.review_groups == {"6"}
        assert value.store.tenant("99", "5")["paused"]
    assert value.settings["paused"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unresolved", ["recent", "pending", "processing", "needs_review"]
)
async def test_rpc_review_keeps_existing_evidence_hold_conditions(
    rpc_scope, unresolved
):
    value, adapter, _ = rpc_scope
    if unresolved == "recent":
        await adapter.ledger.db.execute(
            "UPDATE review_holds SET created=? WHERE team='5'", (time.time(),)
        )
    else:
        await adapter.ledger.db.execute(
            "INSERT INTO inbox(account,team,message,payload,state) VALUES('99','5','m','{}',?)",
            (unresolved,),
        )
    await adapter.ledger.db.commit()
    with pytest.raises(OpenError, match="review_unresolved"):
        await OpenRPC(value).dispatch(
            OpenCall(
                action="review_resume",
                actor="dashboard/root",
                payload={
                    "account": "99",
                    "group_id": "5",
                    "version": value.store.tenant("99", "5")["version"],
                },
            )
        )
    assert "5" in adapter.ledger.review_groups


@pytest.mark.asyncio
async def test_readonly_members_and_activation_preserve_their_original_gates(rpc_scope):
    value, _, rows = rpc_scope
    tenant = value.store.tenant("99", "5")
    value.store.native_role("99", "5", tenant["epoch"], "GROUP_ROLE_MEMBER")
    rows[0]["groupRole"] = "GROUP_ROLE_MEMBER"
    await value.refresh()
    rpc = OpenRPC(value)
    result = await rpc.dispatch(
        OpenCall(
            action="tenant_members",
            actor="dashboard/root",
            payload={"account": "99", "group_id": "5"},
        )
    )
    assert result["native_capability"]["state"] == "missing"
    assert {row["id"] for row in result["members"]} == {"1", "2"}
    with pytest.raises(OpenError, match="inviter_unproven"):
        await rpc.dispatch(
            OpenCall(
                action="activation_code",
                actor="dashboard/root",
                payload={"account": "99", "group_id": "5"},
            )
        )
    with value.store.connect(write=True) as db:
        db.execute(
            "UPDATE tenants SET active=0,admission='verified' WHERE account='99' AND group_id='5'"
        )
    result = await rpc.dispatch(
        OpenCall(
            action="activation_code",
            actor="dashboard/root",
            payload={"account": "99", "group_id": "5"},
        )
    )
    assert result["code"] and result["expires_in"] == 600


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["tenant", "tenant_preview", "review_resume"])
async def test_rpc_freezes_account_before_the_first_tenant_read(
    rpc_scope, monkeypatch, action
):
    value, adapter, rows = rpc_scope
    payload = tenant_payload(value)
    if action == "tenant":
        payload["confirmation"] = value.store.preview_control(
            "99",
            "5",
            payload["version"],
            "dashboard/root",
            payload["rules"],
            payload["managers"],
            payload["paused"],
        )
    elif action == "review_resume":
        payload = {k: payload[k] for k in ("account", "group_id", "version")}
    original = value.store.tenant
    waiting, release = threading.Event(), threading.Event()

    def delayed(*args, **kwargs):
        waiting.set()
        if not release.wait(10):
            raise TimeoutError("offline_account_read_barrier")
        return original(*args, **kwargs)

    monkeypatch.setattr(value.store, "tenant", delayed)
    task = asyncio.create_task(
        OpenRPC(value).dispatch(
            OpenCall(action=action, actor="dashboard/root", payload=payload)
        )
    )
    try:
        assert await asyncio.to_thread(waiting.wait, 10)
        adapter.account, adapter.nim_account = "199", "1199"
        rows[0].update(userId="199", nimId="1199")
    finally:
        release.set()
        outcome = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(outcome[0], ProtocolError)
    assert str(outcome[0]) == "database_authority_changed"
    adapter.business.request.assert_not_awaited()
    assert original("99", "5")["managers"] == ["1"]
    assert adapter.ledger.review_groups == {"5", "6"}
