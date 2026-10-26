import asyncio
import copy
import time
from unittest.mock import AsyncMock

import pytest
from test_lajiaoliao_businesses import prepared_plugin

from data.plugins.astrbot_plugin_lajiaoliao.business import DEFAULTS
from data.plugins.astrbot_plugin_lajiaoliao.capabilities import (
    execution_verified,
    load_capabilities,
)
from data.plugins.astrbot_plugin_lajiaoliao.storage import SessionError


@pytest.mark.parametrize("explicit", [False, None, 1, "true"])
def test_explicit_execution_gate_does_not_inherit_live_status(explicit):
    assert not execution_verified(
        {
            "execution_verified": explicit,
            "live_acceptance": True,
        }
    )
    assert execution_verified({"live_acceptance": True})
    assert execution_verified({"execution_verified": True, "live_acceptance": False})


def test_matrix_native_evidence_is_not_complete_workflow_acceptance():
    features = {row["name"]: row for row in load_capabilities()["features"]}
    for name in ("revoke_others", "rename_others"):
        assert execution_verified(features[name])
        assert features[name]["live_native_test_action"] is True
        assert features[name]["live_acceptance"] is False
        assert features[name]["default_enabled"] is False
    assert not execution_verified(features["cleanup"])


def test_native_execution_keeps_permissions_and_protected_identity(
    tmp_path, monkeypatch
):
    async def run():
        plugin, config, instance = prepared_plugin(tmp_path, monkeypatch)
        ref = config["session_ref"]
        account = plugin.store._read(ref)["account_id"]
        roster = {
            account: {"roleLevel": 100, "nickname": "Bot"},
            "ordinary": {"roleLevel": 20, "nickname": "Original"},
            "admin": {"roleLevel": 20, "nickname": "Admin"},
        }
        plugin.business.roster = AsyncMock(return_value=roster)
        value = copy.deepcopy(DEFAULTS)
        value["native_actions"] = ["revoke_others", "rename_others"]
        await plugin.business.db(ref, "save_settings", "g", 0, value, "owner")
        try:
            for action, fields in [
                ("revoke_member_message", {"message_id": "fresh"}),
                (
                    "set_member_group_nickname",
                    {
                        "nickname": "NewName",
                        "expected_nickname": "Original",
                    },
                ),
            ]:
                body = {
                    "action": action,
                    "kind": "group",
                    "target_id": "g",
                    "user_id": "ordinary",
                    "operation_id": "automatic-test",
                    "business_version": 1,
                    **fields,
                }
                assert (await plugin.business.native_payload(ref, "g", body))[
                    "user_id"
                ] == "ordinary"
                for uid in ("admin", account):
                    with pytest.raises(SessionError, match="protected_member"):
                        await plugin.business.native_payload(
                            ref, "g", {**body, "user_id": uid}
                        )
                roster[account]["roleLevel"] = 20
                with pytest.raises(SessionError, match="native_admin_required"):
                    await plugin.business.native_payload(ref, "g", body)
                roster[account]["roleLevel"] = 100
            value["maintenance"]["cleanup_enabled"] = True
            with pytest.raises(SessionError, match="native_capability_unverified"):
                await plugin.business.validate_capabilities(ref, value)
        finally:
            await plugin.business.close()

    asyncio.run(run())


def test_progressive_recall_uses_verified_native_action_once(tmp_path, monkeypatch):
    async def run():
        plugin, config, instance = prepared_plugin(tmp_path, monkeypatch)
        ref = config["session_ref"]
        account = plugin.store._read(ref)["account_id"]
        plugin.business.roster = AsyncMock(
            return_value={
                account: {"roleLevel": 100},
                "ordinary": {"roleLevel": 20},
            }
        )
        instance.platform_action = AsyncMock(return_value={"status": "succeeded"})
        value = copy.deepcopy(DEFAULTS)
        value.update(review_enabled=True, native_actions=["mute", "revoke_others"])
        value["automation"].update(enabled=True, punishment_mode="recall_progressive")
        await plugin.business.db(ref, "save_settings", "g", 0, value, "owner")
        try:
            await plugin.business.validate_capabilities(ref, value)
            now = time.time()
            assert await plugin.business.apply_automatic(
                ref, "g", "ordinary", "fresh", now, "keyword", 1
            )
            instance.platform_action.assert_awaited_once()
            sent = instance.platform_action.await_args.args[0]
            assert sent["action"] == "revoke_member_message"
            assert sent["user_id"] == "ordinary" and sent["message_id"] == "fresh"
            await plugin.business.apply_automatic(
                ref, "g", "ordinary", "fresh", now, "keyword", 1
            )
            await plugin.business.apply_automatic(
                ref, "g", account, "bot-message", now, "keyword", 1
            )
            instance.platform_action.assert_awaited_once()
        finally:
            await plugin.business.close()

    asyncio.run(run())


def test_card_sync_uses_granted_action_and_preserves_bot(tmp_path, monkeypatch):
    async def run():
        plugin, config, instance = prepared_plugin(tmp_path, monkeypatch)
        ref = config["session_ref"]
        account = plugin.store._read(ref)["account_id"]
        roster = {
            account: {"roleLevel": 100, "nickname": "Bot"},
            "ordinary": {"roleLevel": 20, "nickname": "Original"},
        }
        plugin.business.roster = AsyncMock(return_value=roster)

        async def apply(body):
            assert body["user_id"] == "ordinary"
            roster["ordinary"]["nickname"] = body["nickname"]
            return {"status": "succeeded"}

        instance.platform_action = AsyncMock(side_effect=apply)
        value = copy.deepcopy(DEFAULTS)
        value["native_actions"] = ["rename_others"]
        value["maintenance"].update(cards_enabled=True, prefix="T", name_characters=2)
        await plugin.business.db(ref, "save_settings", "g", 0, value, "owner")
        try:
            await plugin.business.poll_maintenance(ref)
            instance.platform_action.assert_awaited_once()
            assert roster["ordinary"]["nickname"] == "TOr"
            assert roster[account]["nickname"] == "Bot"
            await plugin.business.poll_maintenance(ref)
            instance.platform_action.assert_awaited_once()
        finally:
            await plugin.business.close()

    asyncio.run(run())
