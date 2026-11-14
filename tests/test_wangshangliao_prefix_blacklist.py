"""Prefix enrollment is scoped, protected, durable and independent of OCR."""

import asyncio
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from astrbot.builtin_stars.wangshangliao_moderation import (
    member_exemptions,
    prefix_blacklist,
)
from astrbot.core.platform.sources.wangshangliao import blacklist
from astrbot.core.platform.sources.wangshangliao.diagnostics import Diagnostics
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from tests.test_wangshangliao_blacklist_core import action_request, authorize, proof
from tests.test_wangshangliao_blacklist_core import native as native
from tests.test_wangshangliao_blacklist_core import store as store
from tests.test_wangshangliao_command_parity import setup as setup
from tests.test_wangshangliao_member_blacklist import scope as scope

pytestmark = pytest.mark.usefixtures("wsl_active_policy", "wsl_command_classifier")


@pytest_asyncio.fixture
async def prefix_scope(scope, monkeypatch):
    """Install an explicit test-only policy and isolated platform receipts.

    Args:
        scope: Existing offline two-group blacklist service.
        monkeypatch: Isolated private paths and native status.

    Returns:
        Service, event, roster, store, policy path and mutable configuration.
    """
    service, event, rows, store = scope
    monkeypatch.setattr(prefix_blacklist, "instance_dir", lambda _: store.path.parent)
    monkeypatch.setattr(member_exemptions, "instance_dir", lambda _: store.path.parent)
    store.configure("1", "5", 10080, actor="dashboard/operator", guard=lambda: None)
    policy = {
        "account": "1",
        "groups": ["5"],
        "prefixes": ["免费上分", "免费P图上分"],
        "minutes": 10080,
        "actor": "dashboard/operator",
    }
    path = store.path.with_name("blacklist_prefixes.json")
    path.write_text(json.dumps(policy), encoding="utf-8")
    rows["3"]["userNick"] = "免费p图上分-123456"
    adapter = event.platform
    adapter.diagnostics = Diagnostics("prefix-test")
    adapter.business = SimpleNamespace(request=AsyncMock(return_value={"list": []}))
    monkeypatch.setattr(
        prefix_blacklist, "muted_member_ids", AsyncMock(return_value=set())
    )

    async def execute(request, *, authorize):
        await authorize()
        request.blacklist_guard()
        intent = request.blacklist_intent
        await asyncio.to_thread(
            blacklist.authorize_intent,
            intent.instance,
            intent.account,
            intent.operation,
            intent.action,
            intent.group,
            intent.member,
            intent.minutes,
            intent,
        )
        return proof(intent)

    adapter.execute_action.side_effect = execute
    return service, event, rows, store, path, policy


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,expected",
    [
        ("免费上分-123", True),
        ("免费P图上分123", True),
        ("免费p图上分123", True),
        ("免费上分", True),
        ("本人提到免费上分", False),
        ("普通成员", False),
        (" 免费上分", False),
        ("免费图上分", False),
    ],
)
async def test_only_real_display_prefix_matches(prefix_scope, name, expected):
    service, event, rows, store, _, _ = prefix_scope
    rows["3"]["userNick"] = name
    await service.member_blacklist.process(event.platform, group="5")
    entries = store.read("1", "5")[1]
    assert [row["member"] for row in entries] == (["3"] if expected else [])
    if expected:
        request = event.platform.execute_action.await_args.args[0]
        assert request.minutes == 10080 and request.action == "mute"
        assert request.blacklist_intent.trigger == "prefix"
        assert request.blacklist_intent.join_id == "prefix/" + entries[0]["id"]
    else:
        event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "wrong_group",
        "wrong_account",
        "missing_file",
        "invalid_file",
        "paused",
        "inactive",
        "admin",
        "owner",
        "managed_bot",
    ],
)
async def test_protected_or_unconfigured_scope_never_enrolls(
    prefix_scope, monkeypatch, change
):
    service, event, rows, store, path, policy = prefix_scope
    group = "5"
    if change == "wrong_group":
        group = "6"
    elif change == "wrong_account":
        policy["account"] = "9"
        path.write_text(json.dumps(policy))
    elif change == "missing_file":
        path.unlink()
    elif change == "invalid_file":
        path.write_text("{")
    elif change in {"paused", "inactive"}:
        tenant = service.tenants[("1", "5")]
        tenant["paused" if change == "paused" else "active"] = change == "paused"
    elif change in {"admin", "owner"}:
        rows["3"]["groupRole"] = "GROUP_ROLE_" + change.upper()
    else:
        monkeypatch.setattr(blacklist, "protected_target", lambda *a, **kw: True)
        monkeypatch.setattr(prefix_blacklist, "protected_target", lambda *a, **kw: True)
    await service.member_blacklist.process(event.platform, group=group)
    assert store.read("1", group)[1] == []
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_scan_is_paced_but_join_wakeup_invalidates_pacing(prefix_scope):
    service, event, rows, store, _, _ = prefix_scope
    worker, adapter = service.member_blacklist, event.platform
    await worker.process(adapter, group="5")
    reads = service.roster.await_count
    await worker.process(adapter, group="5")
    assert service.roster.await_count == reads
    adapter.execute_action.assert_awaited_once()
    worker.prefix_scans.clear()
    await worker.process(adapter, group="5")
    assert service.roster.await_count > reads
    adapter.execute_action.assert_awaited_once()
    assert store.read("1", "5")[1][0]["cycle"] == 1
    service.adapters = lambda: [adapter]
    await worker.receive(
        adapter,
        "5",
        "fresh-join",
        {"event_at": int(time.time() * 1000), "member_peers": ["903"]},
    )
    assert ("1", "5") not in worker.prefix_scans
    await asyncio.gather(*worker.tasks.values())
    adapter.execute_action.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["unknown", "rejected"])
async def test_failure_or_restart_never_retries_same_enrollment(prefix_scope, status):
    service, event, _, store, _, _ = prefix_scope
    adapter = event.platform
    adapter.execute_action.side_effect = None
    adapter.execute_action.return_value = {
        "status": status,
        "submitted": status != "rejected",
    }
    await service.member_blacklist.process(adapter, group="5")
    service.member_blacklist.prefix_scans.clear()
    await service.member_blacklist.process(adapter, group="5")
    adapter.execute_action.assert_awaited_once()
    assert store.read("1", "5")[1][0]["enforcement_status"] == status


@pytest.mark.asyncio
async def test_existing_native_mute_is_not_shortened_or_renewed(
    prefix_scope, monkeypatch
):
    service, event, _, store, _, _ = prefix_scope
    monkeypatch.setattr(
        prefix_blacklist, "muted_member_ids", AsyncMock(return_value={"3"})
    )
    await service.member_blacklist.process(event.platform, group="5")
    assert store.read("1", "5")[1][0]["member"] == "3"
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_whitelist_is_not_silently_removed(prefix_scope):
    service, event, _, store, _, _ = prefix_scope
    member_exemptions.set_exemption(
        event.platform.config["id"],
        "1",
        "5",
        "3",
        "903",
        enabled=True,
        actor="server/operator",
        reason="test",
        guard=lambda: None,
    )
    await service.member_blacklist.process(event.platform, group="5")
    assert store.read("1", "5")[1] == []
    assert member_exemptions.read_exemptions(event.platform.config["id"], "1", "5")[
        1
    ] == {"3": "903"}
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["rename", "promotion", "rule_removed", "entry_removed", "duration"]
)
async def test_changes_before_http_block_submission(prefix_scope, change):
    service, event, rows, store, path, _ = prefix_scope
    submissions = []

    async def execute(request, *, authorize):
        if change == "rename":
            rows["3"]["userNick"] = "Normal"
        elif change == "promotion":
            rows["3"]["groupRole"] = "GROUP_ROLE_ADMIN"
        elif change == "rule_removed":
            path.unlink()
        elif change == "entry_removed":
            await asyncio.to_thread(
                store.set_member,
                "1",
                "5",
                "3",
                "903",
                "Target",
                False,
                "dashboard/operator",
                lambda: None,
            )
        else:
            await asyncio.to_thread(
                store.configure,
                "1",
                "5",
                60,
                actor="dashboard/operator",
                guard=lambda: None,
            )
        await authorize()
        request.blacklist_guard()
        intent = request.blacklist_intent
        await asyncio.to_thread(
            blacklist.authorize_intent,
            intent.instance,
            intent.account,
            intent.operation,
            intent.action,
            intent.group,
            intent.member,
            intent.minutes,
            intent,
        )
        submissions.append(request)
        return proof(intent)

    event.platform.execute_action.side_effect = execute
    await service.member_blacklist.process(event.platform, group="5")
    assert submissions == []


@pytest.mark.asyncio
async def test_wrong_duration_pauses_without_changing_existing_policy(prefix_scope):
    service, event, _, store, _, _ = prefix_scope
    store.configure("1", "5", 41760, actor="dashboard/operator", guard=lambda: None)
    await service.member_blacklist.process(event.platform, group="5")
    assert store.read("1", "5")[1] == []
    assert store.settings("1", "5")["mute_minutes"] == 41760
    event.platform.execute_action.assert_not_awaited()


def test_prefix_intent_is_separate_durable_and_seven_days_only(store):
    entry = store.read("1", "5")[1][0]
    args = ("1", "5", "2", "22", entry["id"], lambda: None)
    assert store.reserve_prefix(*args) is None
    store.configure("1", "5", 10080, actor="dashboard/operator", guard=lambda: None)
    intent = store.reserve_prefix(*args)
    assert intent.trigger == "prefix" and intent.minutes == 10080
    authorize(intent)
    for changed in (
        replace(intent, trigger="join"),
        replace(intent, trigger="command"),
        replace(intent, minutes=41760),
        replace(intent, join_id="prefix/" + "f" * 32),
    ):
        with pytest.raises(ProtocolError):
            authorize(changed)
    store.done(intent, proof(intent))
    assert store.reserve_prefix(*args) is None
    joined = store.reserve("1", "5", "2", "22", "101", time.time(), lambda: None)
    assert joined.trigger == "join" and joined.minutes == 10080
    authorize(joined)


@pytest.mark.asyncio
async def test_prefix_intent_reaches_native_mute_with_exact_seven_days(store, native):
    from astrbot.core.platform.sources.wangshangliao.actions import dispatch

    adapter, state = native
    store.configure("1", "5", 10080, actor="dashboard/operator", guard=lambda: None)
    entry = store.read("1", "5")[1][0]
    intent = store.reserve_prefix("1", "5", "2", "22", entry["id"], lambda: None)
    result = await dispatch(adapter, action_request(adapter, intent))
    assert result["status"] == "accepted" and result["minutes"] == 10080
    assert state.mutations == [
        ("/v1/group/set-member-mute", {"groupId": 5, "userId": 2, "min": 10080})
    ]
    store.done(intent, result)
    with pytest.raises(ProtocolError):
        await dispatch(adapter, action_request(adapter, intent))
    assert len(state.mutations) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("minutes", True),
        ("minutes", 41760),
        ("groups", ["5", "5"]),
        ("groups", ["05"]),
        ("prefixes", []),
        ("prefixes", ["p", "P"]),
        ("prefixes", [" 免费上分"]),
        ("actor", "2"),
        ("extra", True),
    ],
)
def test_malformed_policy_is_never_authority(tmp_path, monkeypatch, field, value):
    monkeypatch.setattr(prefix_blacklist, "instance_dir", lambda _: tmp_path)
    rule = {
        "account": "1",
        "groups": ["5"],
        "prefixes": ["免费上分"],
        "minutes": 10080,
        "actor": "dashboard/operator",
    }
    rule[field] = value
    (tmp_path / "blacklist_prefixes.json").write_text(json.dumps(rule))
    assert prefix_blacklist.read_prefix_rule("instance", "1", "5") is None
