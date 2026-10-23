"""List removal stays bound to stored identities, not current nicknames."""

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import member_exemptions
from tests.test_wangshangliao_command_parity import setup as setup
from tests.test_wangshangliao_member_blacklist import (
    direct,
    mention,
)
from tests.test_wangshangliao_member_blacklist import (
    scope as scope,
)
from tests.test_wangshangliao_member_whitelist import whitelist as whitelist

pytestmark = pytest.mark.usefixtures("wsl_active_policy", "wsl_command_classifier")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["left", "renamed", "peer_changed", "legacy"])
async def test_whitelist_stored_binding_can_be_removed(whitelist, change):
    value, event, rows = whitelist
    workflow, adapter = value.member_whitelist, event.platform
    member_exemptions.set_exemption(
        adapter.config["id"],
        "1",
        "5",
        "3",
        "903",
        name="" if change == "legacy" else "Original nickname",
        enabled=True,
        actor="server/test",
        reason="Removal fixture",
        guard=lambda: None,
    )
    if change in {"left", "legacy"}:
        rows.pop("3")
    elif change == "renamed":
        rows["3"]["userNick"] = "New nickname"
    else:
        rows["3"]["nimId"] = "999"
    options = {
        "actor": "dashboard/offline",
        "session": "dashboard/offline",
        "tenant_version": value.tenants[("1", "5")]["version"],
    }
    visible = await workflow.run(adapter, "5", "read", **options)
    assert [row["id"] for row in visible["members"]] == ["3"]
    search = await workflow.run(
        adapter, "5", "search", mode="remove", query="3", **options
    )
    assert [row["id"] for row in search["members"]] == ["3"]
    preview = await workflow.run(
        adapter, "5", "preview", selection=search["selection"], index=1, **options
    )
    saved = await workflow.run(
        adapter, "5", "commit", token=preview["token"], **options
    )
    assert saved["saved"] and saved["mode"] == "remove"
    assert member_exemptions.read_exemptions(adapter.config["id"], "1", "5") == (2, {})
    adapter.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_whitelist_remove_preview_survives_target_rename_and_departure(whitelist):
    value, event, rows = whitelist
    workflow, adapter = value.member_whitelist, event.platform
    member_exemptions.set_exemption(
        adapter.config["id"],
        "1",
        "5",
        "3",
        "903",
        name="Target",
        enabled=True,
        actor="server/test",
        reason="Removal fixture",
        guard=lambda: None,
    )
    options = {
        "actor": "dashboard/offline",
        "session": "dashboard/offline",
        "tenant_version": value.tenants[("1", "5")]["version"],
    }
    search = await workflow.run(
        adapter, "5", "search", mode="remove", query="Target", **options
    )
    rows["3"]["userNick"] = "Changed while previewing"
    preview = await workflow.run(
        adapter, "5", "preview", selection=search["selection"], index=1, **options
    )
    rows.pop("3")
    assert (
        await workflow.run(adapter, "5", "commit", token=preview["token"], **options)
    )["saved"]


@pytest.mark.asyncio
async def test_whitelist_exact_stored_uid_ignores_many_numeric_name_matches(whitelist):
    value, event, _ = whitelist
    instance = event.platform.config["id"]
    for uid in ["3", *(str(i) for i in range(10, 32))]:
        member_exemptions.set_exemption(
            instance,
            "1",
            "5",
            uid,
            str(900 + int(uid)),
            name="Member 3",
            enabled=True,
            actor="server/test",
            reason="Search fixture",
            guard=lambda: None,
        )
    result = await value.member_whitelist.run(
        event.platform,
        "5",
        "search",
        mode="remove",
        query="3",
        actor="dashboard/offline",
        session="dashboard/offline",
        tenant_version=value.tenants[("1", "5")]["version"],
    )
    assert [row["id"] for row in result["members"]] == ["3"]


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["Target", "Current nickname", "3"])
async def test_blacklist_removal_accepts_saved_current_name_or_exact_uid(scope, query):
    value, event, rows, store = scope
    mention(event)
    await direct(value, event)
    rows["3"]["userNick"] = "Current nickname"
    workflow, adapter = value.member_blacklist, event.platform
    options = {
        "actor": "dashboard/offline",
        "session": "dashboard/offline",
        "tenant_version": value.tenants[("1", "5")]["version"],
    }
    search = await workflow.run(
        adapter, "5", "search", mode="remove", query=query, **options
    )
    assert search["members"] == [{"id": "3", "name": "Current nickname"}]
    rows.pop("3")
    preview = await workflow.run(
        adapter, "5", "preview", selection=search["selection"], index=1, **options
    )
    assert (
        await workflow.run(adapter, "5", "commit", token=preview["token"], **options)
    )["saved"]
    assert store.read("1", "5") == (2, [])
    adapter.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_blacklist_exact_stored_uid_ignores_many_numeric_name_matches(scope):
    value, event, _, store = scope
    mention(event)
    await direct(value, event)
    for uid in range(10, 32):
        store.set_member(
            "1",
            "5",
            str(uid),
            str(uid + 900),
            "Member 3",
            enabled=True,
            actor="dashboard/offline",
            guard=lambda: None,
        )
    result = await value.member_blacklist.run(
        event.platform,
        "5",
        "search",
        mode="remove",
        query="3",
        actor="dashboard/offline",
        session="dashboard/offline",
        tenant_version=value.tenants[("1", "5")]["version"],
    )
    assert [row["id"] for row in result["members"]] == ["3"]


@pytest.mark.asyncio
async def test_historical_overlap_is_flagged_in_both_lists_without_deleting(
    whitelist, tmp_path, monkeypatch
):
    import sqlite3
    from contextlib import closing

    from astrbot.core.platform.sources.wangshangliao import blacklist

    value, event, _ = whitelist
    monkeypatch.setattr(blacklist, "instance_dir", lambda _: tmp_path)
    store = blacklist.BlacklistStore(event.platform.config["id"])
    store.set_member(
        "1",
        "5",
        "3",
        "903",
        "Target",
        enabled=True,
        actor="dashboard/offline",
        guard=lambda: None,
    )
    member_exemptions.set_exemption(
        event.platform.config["id"],
        "1",
        "5",
        "3",
        "903",
        name="Target",
        enabled=False,
        actor="server/test",
        reason="Legacy fixture",
        guard=lambda: None,
    )
    path = tmp_path / "member_exemptions.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.execute("UPDATE speech_exemptions SET enabled=1")
        db.commit()
    before = path.read_bytes()
    options = {
        "actor": "dashboard/offline",
        "session": "dashboard/offline",
        "tenant_version": value.tenants[("1", "5")]["version"],
    }
    white = await value.member_whitelist.run(event.platform, "5", "read", **options)
    black = await value.member_blacklist.run(event.platform, "5", "read", **options)
    assert white["members"][0]["conflict"] is True
    assert black["members"][0]["conflict"] is True
    assert path.read_bytes() == before
    assert member_exemptions.is_exempt(
        event.platform.config["id"], "1", "5", "3", "903"
    )
    assert len(store.read("1", "5")[1]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_name", ["policy", "content_rules"])
async def test_automatic_rule_entry_reads_whitelist_off_event_loop(
    tmp_path, monkeypatch, handler_name
):
    import asyncio
    import importlib
    import threading

    from tests.test_wangshangliao_content_rules import fixture

    handler = importlib.import_module(
        "astrbot.builtin_stars.wangshangliao_moderation." + handler_name
    )
    adapter, _ = fixture(tmp_path, monkeypatch)
    loop_thread = threading.get_ident()
    workers = []

    def read(*args):
        workers.append(threading.get_ident())
        return True

    monkeypatch.setattr(member_exemptions, "is_exempt", read)
    call = handler.handle if handler_name == "policy" else handler.enforce
    assert not await asyncio.wait_for(
        call(adapter, "5", "123", {"sender": "2", "recall_route": {"peer": "22"}}),
        timeout=2,
    )
    assert workers and all(thread != loop_thread for thread in workers)
    adapter.execute_moderation.assert_not_awaited()
