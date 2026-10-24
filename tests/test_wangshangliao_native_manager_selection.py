"""Native manager selection and observable AI admission without external calls."""

import copy
import re
from datetime import datetime
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.group_management import (
    native_manager_ids,
)
from astrbot.builtin_stars.wangshangliao_moderation.open_rpc import OpenCall, OpenRPC
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from tests.test_wangshangliao_command_parity import setup as setup
from tests.test_wangshangliao_group_ai import model_scope
from tests.test_wangshangliao_open_rpc_guards import (
    rpc_scope as rpc_scope,
)
from tests.test_wangshangliao_open_rpc_guards import (
    tenant_payload,
)

pytestmark = pytest.mark.usefixtures("wsl_active_policy", "wsl_command_classifier")


def test_native_candidates_exclude_members_unknown_roles_and_bot():
    rows = [
        {"userId": "1", "nimId": "101", "groupRole": "GROUP_ROLE_OWNER"},
        {"userId": "2", "nimId": "102", "groupRole": "GROUP_ROLE_ADMIN"},
        {"userId": "3", "nimId": "103", "groupRole": "GROUP_ROLE_MEMBER"},
        {"userId": "4", "nimId": "104"},
        {"userId": "5", "groupRole": "GROUP_ROLE_ADMIN"},
        {"userId": "99", "nimId": "199", "groupRole": "GROUP_ROLE_ADMIN"},
    ]
    assert native_manager_ids(rows, "99") == {"1", "2"}


@pytest.mark.asyncio
async def test_member_api_preserves_directory_but_exposes_native_roles(rpc_scope):
    value, _, _ = rpc_scope
    result = await OpenRPC(value).dispatch(
        OpenCall(
            action="tenant_members",
            actor="dashboard/test",
            payload={"account": "99", "group_id": "5"},
        )
    )
    assert {r["id"]: r["role"] for r in result["members"]} == {
        "1": "GROUP_ROLE_OWNER",
        "2": "GROUP_ROLE_MEMBER",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["tenant_preview", "tenant"])
async def test_crafted_dashboard_member_grant_is_rejected(rpc_scope, action):
    value, _, _ = rpc_scope
    before = value.store.tenant("99", "5")
    payload = tenant_payload(value)
    if action == "tenant":
        payload["confirmation"] = value.store.preview_control(
            "99",
            "5",
            before["version"],
            "dashboard/test",
            before["rules"],
            payload["managers"],
            False,
        )
    with pytest.raises(OpenError, match="manager_native_role_required"):
        await OpenRPC(value).dispatch(
            OpenCall(action=action, actor="dashboard/test", payload=payload)
        )
    assert value.store.tenant("99", "5") == before
    with value.store.connect() as db:
        assert not db.execute("SELECT 1 FROM capabilities WHERE used=1").fetchone()


@pytest.mark.asyncio
async def test_native_demotion_during_commit_rolls_back_grant(rpc_scope, monkeypatch):
    value, _, rows = rpc_scope
    rows[2]["groupRole"] = "GROUP_ROLE_ADMIN"
    rpc = OpenRPC(value)
    payload = tenant_payload(value)
    preview = await rpc.dispatch(
        OpenCall(action="tenant_preview", actor="dashboard/test", payload=payload)
    )
    payload["confirmation"] = preview["confirmation"]
    before = value.store.tenant("99", "5")
    original = value.store.log

    def demote(*args, **kwargs):
        original(*args, **kwargs)
        if args[4] == "tenant_changed":
            value.member_evidence[("99", "5")]["2"]["groupRole"] = "GROUP_ROLE_MEMBER"

    monkeypatch.setattr(value.store, "log", demote)
    with pytest.raises(OpenError, match="manager_native_role_required"):
        await rpc.dispatch(
            OpenCall(action="tenant", actor="dashboard/test", payload=payload)
        )
    assert value.store.tenant("99", "5") == before
    with value.store.connect() as db:
        assert db.execute("SELECT used FROM capabilities").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_native_admin_can_be_granted_after_explicit_preview(rpc_scope):
    value, _, rows = rpc_scope
    rows[2]["groupRole"] = "GROUP_ROLE_ADMIN"
    rpc = OpenRPC(value)
    payload = tenant_payload(value)
    preview = await rpc.dispatch(
        OpenCall(action="tenant_preview", actor="dashboard/test", payload=payload)
    )
    await rpc.dispatch(
        OpenCall(
            action="tenant",
            actor="dashboard/test",
            payload={**payload, "confirmation": preview["confirmation"]},
        )
    )
    assert value.store.tenant("99", "5")["managers"] == ["1", "2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("demote_after_preview", [False, True])
async def test_chat_cannot_add_native_member_or_use_a_demoted_preview(
    setup, demote_after_preview
):
    value, commands, event = setup
    key = (id(event.platform), "1", "2", event.unified_msg_origin)
    commands.selections[key] = {
        "group": "5",
        "expires": float("inf"),
        "members": [{"userId": "3", "nimId": "903"}],
    }
    roster = value.roster.return_value
    before = value.store.tenant("1", "5")
    if demote_after_preview:
        roster["3"]["groupRole"] = "GROUP_ROLE_ADMIN"
        preview = await value.manager_command(event, "添加管理员 1")
        token = re.search(r"确认授权 ([A-Za-z0-9_-]+)", preview)[1]
        event.message_obj.message_id = "confirm"
        roster["3"]["groupRole"] = "GROUP_ROLE_MEMBER"
        command = "确认授权 " + token
    else:
        command = "添加管理员 1"
    with pytest.raises(OpenError, match="manager_native_role_required"):
        await value.manager_command(event, command)
    assert value.store.tenant("1", "5") == before


@pytest.mark.asyncio
async def test_ai_status_explains_all_blockers_without_calling_or_charging(rpc_scope):
    value, adapter, _ = rpc_scope
    row = copy.deepcopy(await model_scope(value, adapter))
    row["payer"]["confirmed"] = False
    row["config"].update(start_hour=8, end_hour=23)
    value.budget["confirmed"] = False
    with value.store.connect(write=True) as db:
        db.execute("DELETE FROM ai_prices")
    before = value.plans.status()
    now = datetime(2026, 10, 20, 1, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    result = await value.group_ai.readiness(adapter, row, now=now)
    assert not result["ready"]
    for text in ("本人", "发言时段", "硬预算", "计费依据"):
        assert any(text in reason for reason in result["reasons"])
    assert value.plans.status() == before
    adapter.send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_ai_status_ready_is_not_a_forced_reply(rpc_scope):
    value, adapter, _ = rpc_scope
    row = await model_scope(value, adapter)
    now = datetime(2026, 10, 19, 12, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    result = await value.group_ai.readiness(adapter, row, now=now)
    assert result == {"ready": True, "reasons": []}
    assert not value.group_ai.tasks
    adapter.send_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["mentions", "autonomous", "idle"])
async def test_every_group_mode_checks_only_the_exact_group_profile(
    rpc_scope, monkeypatch, mode
):
    value, adapter, _ = rpc_scope
    row = await model_scope(value, adapter)
    row["config"].update(
        {name: name == mode for name in ("mentions", "autonomous", "idle")}
    )
    resolve = Mock(wraps=value.profiles.resolve)
    monkeypatch.setattr(value.profiles, "resolve", resolve)
    now = datetime(2026, 10, 19, 12, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    assert await value.group_ai.readiness(adapter, row, now=now) == {
        "ready": True,
        "reasons": [],
    }
    resolve.assert_called_once_with("group", "99", "5")
    adapter.send_text.assert_not_awaited()
