"""Automatic mutes preserve evidence without allowing legacy automatic removal."""

import asyncio
import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot.core.platform.sources.wangshangliao import automatic, moderation
from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from tests.fixtures.wsl_actions import bind_action_port


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    monkeypatch.setattr(automatic, "instance_dir", lambda _: tmp_path)
    instance = SimpleNamespace(
        account="1",
        config={
            "id": "bot",
            "enabled_groups": ["5", "6"],
            "moderation": {
                "enabled": True,
                "automation_enabled": True,
                "auto_kick": {"5": True, "6": True},
                "permissions": {"5": ["mute", "kick"], "6": ["mute", "kick"]},
            },
        },
        members={"5": {"2": "22", "3": "33"}, "6": {"2": "22"}},
        diagnostics=SimpleNamespace(emit=Mock()),
    )

    async def execute(operation, action, group, member, text="", *, minutes=1, action_check=None):
        if action_check:
            await action_check()
        if action == "kick":
            automatic.authorize_kick(instance, operation, str(group), str(member))
        result = {
            "operation": operation,
            "action": action,
            "group": group,
            "member": member,
            "status": "accepted",
        }
        with closing(automatic.database(instance)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,digest TEXT,result TEXT)"
            )
            db.execute(
                "INSERT INTO operations VALUES(?, 'fixture',?)",
                (operation, json.dumps(result)),
            )
        return result

    instance.execute_moderation = AsyncMock(side_effect=execute)
    return bind_action_port(instance)


@pytest.mark.asyncio
async def test_fourth_violation_mutes_even_with_legacy_kick_opt_in(adapter):
    for number in range(1, 4):
        result = await automatic.mute_and_escalate(
            adapter, f"mute/{number}", "5", "2", str(number)
        )
        assert result["mute_count"] == number
        assert result["action"] == "mute"
    assert adapter.execute_moderation.await_count == 3
    # A replay of the third message, even with another operation ID, is not new evidence.
    await automatic.mute_and_escalate(adapter, "mute/3", "5", "2", "3")
    await automatic.mute_and_escalate(adapter, "replayed/3", "5", "2", "3")
    assert adapter.execute_moderation.await_count == 3
    result = await automatic.mute_and_escalate(adapter, "mute/4", "5", "2", "4")
    assert result["action"] == "mute"
    assert result["mute_count"] == 4
    assert result["reason"] == "automatic_kick_disabled"
    assert [call.args[1] for call in adapter.execute_moderation.await_args_list] == [
        "mute",
        "mute",
        "mute",
        "mute",
    ]
    await automatic.mute_and_escalate(adapter, "mute/4", "5", "2", "4")
    assert adapter.execute_moderation.await_count == 4


@pytest.mark.asyncio
async def test_duplicate_does_not_increment_and_restart_retains_count(adapter):
    await automatic.mute_and_escalate(adapter, "m1", "5", "2", "1")
    await automatic.mute_and_escalate(adapter, "m1", "5", "2", "1")
    assert adapter.execute_moderation.await_count == 1
    await automatic.mute_and_escalate(adapter, "m2", "5", "2", "2")
    restarted = SimpleNamespace(**vars(adapter))
    result = await automatic.mute_and_escalate(restarted, "m3", "5", "2", "3")
    assert result["action"] == "mute"
    assert result["mute_count"] == 3
    restarted = SimpleNamespace(**vars(adapter))
    result = await automatic.mute_and_escalate(restarted, "m4", "5", "2", "4")
    assert result["action"] == "mute"
    assert result["mute_count"] == 4


@pytest.mark.asyncio
async def test_busy_mute_ledger_rechecks_grant_before_any_action(adapter, tmp_path):
    with closing(automatic.database(adapter)):
        pass
    blocker = sqlite3.connect(tmp_path / "moderation.sqlite3")
    blocker.execute("BEGIN IMMEDIATE")
    work = asyncio.create_task(
        automatic.mute_and_escalate(adapter, "blocked", "5", "2", "1")
    )
    try:
        await asyncio.sleep(0.03)
        assert not work.done()
        adapter.config["moderation"]["permissions"] = {}
        blocker.rollback()
        with pytest.raises(ProtocolError):
            await asyncio.wait_for(work, 3)
        adapter.execute_moderation.assert_not_awaited()
        assert blocker.execute("SELECT COUNT(*) FROM automatic_mutes").fetchone() == (
            0,
        )
    finally:
        blocker.rollback()
        blocker.close()
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["rejected", "unknown"])
async def test_failed_or_unknown_mute_never_counts(adapter, status):
    adapter.execute_moderation.side_effect = None
    adapter.execute_moderation.return_value = {"status": status}
    result = await automatic.mute_and_escalate(adapter, "m1", "5", "2", "1")
    assert result["mute_count"] == 0
    await automatic.mute_and_escalate(adapter, "m2", "5", "2", "2")
    assert adapter.execute_moderation.await_count == (1 if status == "unknown" else 2)
    assert all(
        call.args[1] == "mute" for call in adapter.execute_moderation.await_args_list
    )


@pytest.mark.asyncio
async def test_group_member_and_account_counts_are_isolated(adapter):
    await automatic.mute_and_escalate(adapter, "m1", "5", "2", "1")
    await automatic.mute_and_escalate(adapter, "m2", "5", "2", "2")
    result = await automatic.mute_and_escalate(adapter, "other-group", "6", "2", "3")
    assert result["mute_count"] == 1
    result = await automatic.mute_and_escalate(adapter, "other-member", "5", "3", "4")
    assert result["mute_count"] == 1
    adapter.account = "another-account"
    result = await automatic.mute_and_escalate(adapter, "other-account", "5", "2", "5")
    assert result["mute_count"] == 1
    assert all(
        call.args[1] == "mute" for call in adapter.execute_moderation.await_args_list
    )


@pytest.mark.asyncio
async def test_legacy_unknown_kick_is_preserved_but_never_retried(adapter):
    with closing(automatic.database(adapter)) as db, db:
        db.execute(
            "INSERT INTO automatic_kicks VALUES("
            "'automatic-kick/old','1','5','2','22','old-mute','unknown',0)"
        )
    for number in range(1, 6):
        await automatic.mute_and_escalate(adapter, f"m{number}", "5", "2", str(number))
    assert adapter.execute_moderation.await_count == 5
    assert all(
        call.args[1] == "mute" for call in adapter.execute_moderation.await_args_list
    )
    with closing(automatic.database(adapter)) as db:
        assert db.execute(
            "SELECT status FROM automatic_kicks WHERE operation='automatic-kick/old'"
        ).fetchone() == ("unknown",)


@pytest.mark.asyncio
async def test_mute_does_not_require_kick_grant_even_with_legacy_opt_in(adapter):
    adapter.config["moderation"]["auto_kick"] = {}
    for number in range(1, 5):
        await automatic.mute_and_escalate(adapter, f"m{number}", "5", "2", str(number))
    assert adapter.execute_moderation.await_count == 4
    adapter.config["moderation"]["auto_kick"] = {"5": True}
    adapter.config["moderation"]["permissions"]["5"] = ["mute"]
    result = await automatic.mute_and_escalate(adapter, "m5", "5", "2", "5")
    assert result["action"] == "mute"
    assert result["mute_count"] == 5
    assert adapter.execute_moderation.await_count == 5


@pytest.mark.asyncio
async def test_forged_kick_operation_and_wrong_identity_are_rejected(adapter):
    with pytest.raises(ProtocolError):
        automatic.authorize_kick(adapter, "automatic-kick/forged", "5", "2")
    for number in range(1, 5):
        result = await automatic.mute_and_escalate(
            adapter, f"m{number}", "5", "2", str(number)
        )
    key = result["operation"]
    with pytest.raises(ProtocolError, match="automatic_kick_not_authorized"):
        automatic.authorize_kick(adapter, key, "5", "3")
    adapter.members["5"]["2"] = "changed-peer"
    with pytest.raises(ProtocolError, match="automatic_kick_not_authorized"):
        automatic.authorize_kick(adapter, key, "5", "2")


@pytest.mark.asyncio
async def test_kick_denied_with_or_without_transport_receipts(adapter):
    for number in range(1, 5):
        result = await automatic.mute_and_escalate(
            adapter, f"m{number}", "5", "2", str(number)
        )
    with pytest.raises(ProtocolError, match="automatic_kick_not_authorized"):
        automatic.authorize_kick(adapter, result["operation"], "5", "2")
    with closing(automatic.database(adapter)) as db, db:
        db.execute("DELETE FROM operations WHERE id='m1'")
    with pytest.raises(ProtocolError, match="automatic_kick_not_authorized"):
        automatic.authorize_kick(adapter, result["operation"], "5", "2")


@pytest.mark.asyncio
async def test_persisted_legacy_kick_intent_cannot_reauthorize_removal(adapter):
    for number in range(1, 5):
        result = await automatic.mute_and_escalate(
            adapter, f"m{number}", "5", "2", str(number)
        )
    with closing(automatic.database(adapter)) as db, db:
        db.execute(
            "INSERT INTO automatic_kicks VALUES("
            "'automatic-kick/old','1','5','2','22',?,'accepted',0)",
            (result["operation"],),
        )
        db.execute("INSERT INTO automatic_kick_events VALUES('automatic-kick/old','4')")
    with pytest.raises(ProtocolError, match="automatic_kick_not_authorized"):
        automatic.authorize_kick(adapter, "automatic-kick/old", "5", "2")
    with closing(automatic.database(adapter)) as db, db:
        db.execute("DELETE FROM automatic_kick_events")
    with pytest.raises(ProtocolError, match="automatic_kick_not_authorized"):
        automatic.authorize_kick(adapter, "automatic-kick/old", "5", "2")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["automatic-kick/old", "message/5/old", "ai/old"])
async def test_manual_only_blocks_automatic_and_legacy_transport_kicks(
    adapter, operation
):
    adapter.config["moderation"]["manual_kick_only"] = True
    adapter.get_moderation_members = AsyncMock()
    with pytest.raises(ProtocolError, match="private_confirmation_required"):
        await WangshangliaoAdapter._execute_moderation(adapter, operation, "kick", 5, 2)
    adapter.get_moderation_members.assert_not_awaited()


@pytest.mark.asyncio
async def test_adapter_boundary_rejects_forged_automatic_kick(adapter):
    adapter.stopping = SimpleNamespace(is_set=lambda: False)
    adapter.get_moderation_members = AsyncMock()
    with pytest.raises(ProtocolError):
        await WangshangliaoAdapter._execute_moderation(
            adapter, "automatic-kick/forged", "kick", 5, 2
        )
    adapter.get_moderation_members.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation", ["message/5/old", "content/5/old", "progressive/old"]
)
async def test_native_boundary_rejects_retired_auto_kick_namespaces(adapter, operation):
    adapter.stopping = SimpleNamespace(is_set=lambda: False)
    adapter.get_moderation_members = AsyncMock()
    with pytest.raises(ProtocolError, match="automatic_kick_not_authorized"):
        await WangshangliaoAdapter._execute_moderation(
            adapter, operation, "kick", 5, 2
        )
    adapter.get_moderation_members.assert_not_awaited()


@pytest.mark.asyncio
async def test_managed_bot_is_protected_even_if_configuration_changes(
    adapter, monkeypatch
):
    monkeypatch.setattr(automatic, "is_managed_account", lambda _: True)
    with pytest.raises(ProtocolError):
        await automatic.mute_and_escalate(adapter, "m1", "5", "2", "1")
    adapter.execute_moderation.assert_not_awaited()


@pytest.mark.asyncio
async def test_upstream_admin_role_still_required_for_automatic_kick(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(moderation, "instance_dir", lambda _: tmp_path)
    client = AsyncMock()
    client.fields = [0, 0, 0, "1"]
    client.request.return_value = {
        "member": [{"groupId": 5, "me": {"role": "GROUP_ROLE_MEMBER"}}]
    }
    with pytest.raises(ProtocolError, match="permission"):
        await moderation.execute(
            client, "bot", "1", "automatic-kick/fixture", "kick", 5, 2
        )
    assert client.request.await_count == 1
