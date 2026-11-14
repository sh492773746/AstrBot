"""Explicit unmute ends the captured blacklist cycle, never a concurrent re-add."""

import sqlite3
from contextlib import closing
from unittest.mock import AsyncMock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from astrbot.core.platform.sources.wangshangliao.blacklist import BlacklistStore
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from tests import test_wangshangliao_chat_unmute as chat_unmute
from tests.test_wangshangliao_command_menu import step
from tests.test_wangshangliao_member_mutes import invoke, preview
from tests.test_wangshangliao_member_mutes import scope as scope
from tests.test_wangshangliao_open_rpc_guards import rpc_scope as rpc_scope

pytestmark = pytest.mark.usefixtures("wsl_active_policy", "wsl_command_classifier")
base_setup = chat_unmute.base_setup
chat_scope = chat_unmute.setup


@pytest.fixture(autouse=True)
def isolated_blacklist(monkeypatch, tmp_path):
    """Keep every blacklist read and mutation in isolated test storage.

    Args:
        monkeypatch: Test-scoped attribute replacements.
        tmp_path: Private fixture database directory.
    """
    from astrbot.core.platform.sources.wangshangliao import blacklist

    monkeypatch.setattr(blacklist, "instance_dir", lambda _: tmp_path)


def enroll(adapter, *, group="5", member="2", peer="1002", enabled=True):
    """Create or end one enrollment through the real transactional store.

    Args:
        adapter: Offline receiving adapter.
        group: Business group.
        member: Business target.
        peer: Native target.
        enabled: Desired enrollment state.

    Returns:
        Isolated blacklist store.
    """
    store = BlacklistStore(adapter.config["id"])
    store.set_member(
        str(adapter.account),
        group,
        member,
        peer,
        "Target",
        enabled=enabled,
        actor="1",
        guard=lambda: None,
    )
    return store


@pytest.mark.asyncio
async def test_dashboard_unmute_removes_only_this_group_and_preserves_audit(scope):
    _, adapter, _, _ = scope
    store = enroll(adapter)
    enroll(adapter, group="6")
    before = store.read("99", "6")
    current = await invoke(scope)
    target = {"version": current["version"], "member": "2"}
    draft = await invoke(scope, "preview", **target)
    assert draft["removes_blacklist"]
    assert store.read("99", "5")[1]

    result = await invoke(scope, "commit", **target, confirmation=draft["confirmation"])

    assert result["verified"] and result["blacklist_status"] == "removed"
    assert store.read("99", "5")[1] == []
    assert store.read("99", "6") == before
    with closing(sqlite3.connect(store.path)) as db:
        assert db.execute(
            "SELECT status FROM blacklist_entries WHERE account='99' AND group_id='5'"
        ).fetchone() == ("removed",)
        assert db.execute(
            "SELECT kind FROM blacklist_audit WHERE account='99' AND group_id='5' "
            "ORDER BY id"
        ).fetchall() == [("add",), ("remove",)]


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt", ["unknown", "rejected", "accepted_unverified"])
async def test_unverified_native_unmute_preserves_blacklist(scope, receipt):
    _, adapter, _, _ = scope
    store = enroll(adapter)
    before = store.read("99", "5")
    payload = await preview(scope)
    adapter.execute_action.side_effect = None
    adapter.execute_action.return_value = {
        "status": "accepted" if receipt == "accepted_unverified" else receipt
    }

    result = await invoke(scope, "commit", **payload)

    assert not result["verified"] and result["blacklist_status"] == "retained"
    assert store.read("99", "5") == before
    with pytest.raises(OpenError, match="confirmation_invalid"):
        await invoke(scope, "commit", **payload)
    adapter.execute_action.assert_awaited_once()


@pytest.mark.asyncio
async def test_reblacklist_after_preview_invalidates_confirmation(scope):
    _, adapter, _, _ = scope
    store = enroll(adapter)
    payload = await preview(scope)
    enroll(adapter, enabled=False)
    enroll(adapter)
    before = store.read("99", "5")

    with pytest.raises(OpenError, match="confirmation_invalid"):
        await invoke(scope, "commit", **payload)

    assert store.read("99", "5") == before
    adapter.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_blacklist_inside_submission_guard_blocks_native_unmute(scope):
    _, adapter, _, _ = scope
    store = enroll(adapter)
    payload = await preview(scope)

    async def changed(request, *, authorize):
        enroll(adapter, enabled=False)
        enroll(adapter)
        await authorize()
        pytest.fail("A changed blacklist must not reach native submission")

    adapter.execute_action.side_effect = changed
    with pytest.raises(ProtocolError, match="blacklist_version_changed"):
        await invoke(scope, "commit", **payload)
    assert store.read("99", "5")[1][0]["cycle"] == 2


@pytest.mark.asyncio
async def test_blacklist_peer_mismatch_prevents_unmute_and_removal(scope):
    _, adapter, _, _ = scope
    store = enroll(adapter, peer="9999")
    before = store.read("99", "5")
    payload = await preview(scope)

    with pytest.raises(ProtocolError, match="blacklist_version_changed"):
        await invoke(scope, "commit", **payload)

    assert store.read("99", "5") == before
    adapter.execute_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_reblacklist_after_submission_is_not_removed_by_old_unmute(scope):
    _, adapter, _, muted = scope
    store = enroll(adapter)
    payload = await preview(scope)

    async def concurrent_add(request, *, authorize):
        await authorize()
        enroll(adapter, enabled=False)
        enroll(adapter)
        muted.clear()
        return {"status": "accepted"}

    adapter.execute_action.side_effect = concurrent_add
    result = await invoke(scope, "commit", **payload)

    assert result["verified"] and result["blacklist_status"] == "changed"
    assert store.read("99", "5")[1][0]["cycle"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["role", "permission", "query"])
async def test_post_submission_verification_failure_retains_entry(scope, change):
    value, adapter, rows, muted = scope
    store = enroll(adapter)
    before = store.read("99", "5")
    payload = await preview(scope)

    async def unmute(request, *, authorize):
        await authorize()
        muted.clear()
        if change == "role":
            rows[2]["groupRole"] = "GROUP_ROLE_ADMIN"
        elif change == "permission":
            value.suspended_groups.add(("99", "5"))
        else:
            adapter.nim.get_team_muted_members.side_effect = ProtocolError("offline")
        return {"status": "accepted"}

    adapter.execute_action.side_effect = unmute
    result = await invoke(scope, "commit", **payload)

    assert not result["verified"] and result["blacklist_status"] == "retained"
    assert store.read("99", "5") == before


@pytest.mark.asyncio
async def test_blacklist_write_failure_is_separate_from_verified_unmute(
    scope, monkeypatch
):
    _, adapter, _, _ = scope
    store = enroll(adapter)
    before = store.read("99", "5")
    payload = await preview(scope)

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("offline")

    monkeypatch.setattr(BlacklistStore, "set_member", fail)
    result = await invoke(scope, "commit", **payload)

    assert result["verified"] and result["blacklist_status"] == "failed"
    assert store.read("99", "5") == before


@pytest.mark.asyncio
async def test_reading_absent_platform_mute_does_not_unblacklist(scope):
    _, adapter, _, muted = scope
    store = enroll(adapter)
    before = store.read("99", "5")
    muted.clear()

    assert (await invoke(scope))["members"] == []
    assert store.read("99", "5") == before
    adapter.execute_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("already_unmuted", [False, True])
async def test_real_group_unmute_also_ends_blacklist(chat_scope, already_unmuted):
    value, _, event = chat_scope
    adapter = event.platform
    store = enroll(adapter, member="3", peer="903")
    muted = set() if already_unmuted else {"903"}
    adapter.nim.get_team_muted_members = AsyncMock(side_effect=lambda _: set(muted))
    payload = event.get_extra("wangshangliao_payload")
    payload.update(
        mentions=["903"],
        mention_spans=[{"uid": "903", "nick": "Target", "start": 3, "end": 10}],
    )

    async def execute(request, *, authorize):
        await authorize()
        assert request.expected_nim == "903"
        muted.clear()
        return {"status": "accepted"}

    adapter.execute_action.side_effect = execute
    reply = await step(value, event, "解禁 @Target", native=True)

    assert "已同时移出本群黑名单" in reply
    assert store.read("1", "5")[1] == []
    assert adapter.execute_action.await_count == (0 if already_unmuted else 1)


@pytest.mark.asyncio
async def test_unknown_group_unmute_keeps_blacklist_and_explains_partial_state(
    chat_scope,
):
    value, _, event = chat_scope
    store = enroll(event.platform, member="3", peer="903")
    before = store.read("1", "5")
    payload = event.get_extra("wangshangliao_payload")
    payload.update(
        mentions=["903"],
        mention_spans=[{"uid": "903", "nick": "Target", "start": 3, "end": 10}],
    )
    event.platform.execute_action.return_value = {"status": "unknown"}

    reply = await step(value, event, "解禁 @Target", native=True)

    assert "未知，不自动重试" in reply and "黑名单未移除" in reply
    assert store.read("1", "5") == before
