"""Automatic removal requires three distinct accepted, durable mute receipts."""

import json
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot.core.platform.sources.wangshangliao import automatic, moderation
from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError


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

    async def execute(operation, action, group, member):
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
    return instance


@pytest.mark.asyncio
async def test_three_mutes_then_fourth_violation_kicks_without_fourth_mute(adapter):
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
    assert result["action"] == "kick"
    assert [call.args[1] for call in adapter.execute_moderation.await_args_list] == [
        "mute",
        "mute",
        "mute",
        "kick",
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
    assert result["action"] == "kick"


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
async def test_unknown_kick_is_not_retried_or_followed_by_more_mutes(adapter):
    original = adapter.execute_moderation.side_effect

    async def execute(*args):
        if args[1] == "kick":
            return {"status": "unknown"}
        return await original(*args)

    adapter.execute_moderation.side_effect = execute
    for number in range(1, 6):
        await automatic.mute_and_escalate(adapter, f"m{number}", "5", "2", str(number))
    assert adapter.execute_moderation.await_count == 4


@pytest.mark.asyncio
async def test_no_kick_without_saved_opt_in_or_grant(adapter):
    adapter.config["moderation"]["auto_kick"] = {}
    for number in range(1, 5):
        await automatic.mute_and_escalate(adapter, f"m{number}", "5", "2", str(number))
    assert adapter.execute_moderation.await_count == 4
    adapter.config["moderation"]["auto_kick"] = {"5": True}
    adapter.config["moderation"]["permissions"]["5"] = ["mute"]
    with pytest.raises(ProtocolError, match="moderation_permission"):
        await automatic.mute_and_escalate(adapter, "m5", "5", "2", "5")
    assert adapter.execute_moderation.await_count == 4


@pytest.mark.asyncio
async def test_forged_kick_operation_and_wrong_identity_are_rejected(adapter):
    with pytest.raises(ProtocolError):
        automatic.authorize_kick(adapter, "automatic-kick/forged", "5", "2")
    for number in range(1, 5):
        result = await automatic.mute_and_escalate(
            adapter, f"m{number}", "5", "2", str(number)
        )
    key = result["operation"]
    with pytest.raises(ProtocolError, match="identity"):
        automatic.authorize_kick(adapter, key, "5", "3")
    adapter.members["5"]["2"] = "changed-peer"
    with pytest.raises(ProtocolError, match="identity"):
        automatic.authorize_kick(adapter, key, "5", "2")


@pytest.mark.asyncio
async def test_guard_checks_real_transport_receipts_not_only_counter(adapter):
    for number in range(1, 5):
        result = await automatic.mute_and_escalate(
            adapter, f"m{number}", "5", "2", str(number)
        )
    with closing(automatic.database(adapter)) as db, db:
        db.execute("DELETE FROM operations WHERE id='m1'")
    with pytest.raises(ProtocolError, match="evidence"):
        automatic.authorize_kick(adapter, result["operation"], "5", "2")


@pytest.mark.asyncio
async def test_guard_requires_distinct_fourth_message_evidence(adapter):
    for number in range(1, 5):
        result = await automatic.mute_and_escalate(adapter, f"m{number}", "5", "2", str(number))
    with closing(automatic.database(adapter)) as db, db:
        db.execute("UPDATE automatic_kick_events SET message='3'")
    with pytest.raises(ProtocolError, match="evidence"):
        automatic.authorize_kick(adapter, result["operation"], "5", "2")
    with closing(automatic.database(adapter)) as db, db:
        db.execute("DELETE FROM automatic_kick_events")
    with pytest.raises(ProtocolError, match="evidence"):
        automatic.authorize_kick(adapter, result["operation"], "5", "2")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["automatic-kick/old", "message/5/old", "ai/old"])
async def test_manual_only_blocks_automatic_and_legacy_transport_kicks(adapter, operation):
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
