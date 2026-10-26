import asyncio
import copy
import time
from unittest.mock import AsyncMock

import pytest
from test_lajiaoliao_businesses import prepared_plugin
from test_lajiaoliao_plugin_api import call

from astrbot.core.platform.instance_control import instance_revision
from data.plugins.astrbot_plugin_lajiaoliao.business import DEFAULTS, BusinessStore
from data.plugins.astrbot_plugin_lajiaoliao.storage import SessionError
from data.plugins.astrbot_plugin_lajiaoliao.test_window import NativeTestPermit


def test_invitation_baseline_fresh_verified_unique_and_restart(tmp_path):
    store = BusinessStore(tmp_path)
    value = {
        **copy.deepcopy(DEFAULTS),
        "invitations_enabled": True,
        "invitation_rewards_enabled": True,
        "invitation_points": 3,
    }
    with pytest.raises(SessionError, match="baseline"):
        store.save_settings("g", 0, value, "owner")
    store.save_settings("g", 0, value, "owner", ["existing", "inviter"])
    now = time.time() + 1
    members = {
        uid: {
            "groupID": "g",
            "userID": uid,
            "inviterUserID": "inviter",
            "joinSource": 2,
            "joinTime": int(now * 1000),
        }
        for uid in ["new", "existing", "robot", "unproven", "wrong"]
    }
    members["inviter"] = {"userID": "inviter", "groupID": "g"}
    members["unproven"]["joinSource"] = 1
    members["wrong"]["groupID"] = "other"
    note = {
        "message_id": "event",
        "timestamp": now,
        "inviter_id": "inviter",
        "member_ids": ["new", "existing", "robot", "unproven", "wrong"],
    }
    assert store.invitation_credit("g", note, members, {"robot"}) == 1
    assert (
        store.invitation_credit(
            "g", {**note, "message_id": "duplicate"}, members, {"robot"}
        )
        == 0
    )
    assert store.invitation_summary("g") == [
        {"inviter_id": "inviter", "count": 1, "points": 3}
    ]
    assert not store.invitation_summary("other")
    restored = BusinessStore(tmp_path)
    assert len(restored.invitation_summary("g", "inviter")) == 1
    assert restored.invitation_credit("g", note, members, {"robot"}) == 0
    value["invitations_enabled"] = False
    value["invitation_rewards_enabled"] = False
    restored.save_settings("g", 1, value, "owner")
    members["later"] = {**members["new"], "userID": "later"}
    assert (
        restored.invitation_credit(
            "g", {**note, "member_ids": ["later"]}, members, set()
        )
        == 0
    )


@pytest.mark.parametrize(
    "fault", ["old", "future", "inviter", "joined", "source", "excluded"]
)
def test_invitation_rejects_unverified_or_historical_evidence(tmp_path, fault):
    store = BusinessStore(tmp_path)
    value = {**copy.deepcopy(DEFAULTS), "invitations_enabled": True}
    store.save_settings("g", 0, value, "owner", ["inviter"])
    now = time.time()
    note = {
        "message_id": "n",
        "timestamp": now,
        "inviter_id": "inviter",
        "member_ids": ["new"],
    }
    member = {
        "groupID": "g",
        "userID": "new",
        "joinSource": 2,
        "inviterUserID": "inviter",
        "joinTime": int(now * 1000),
    }
    excluded = set()
    if fault == "old":
        note["timestamp"] -= 600
    if fault == "future":
        note["timestamp"] += 60
    if fault == "inviter":
        note["inviter_id"] = "someone"
    if fault == "joined":
        member["joinTime"] -= 600000
    if fault == "source":
        member["joinSource"] = 1
    if fault == "excluded":
        excluded.add("inviter")
    assert (
        store.invitation_credit("g", note, {"new": member, "inviter": {}}, excluded)
        == 0
    )


def test_native_draft_human_scope_version_and_one_shot(tmp_path):
    store = BusinessStore(tmp_path)
    payload = {"action": "kick", "user_id": "ordinary", "operation_id": "op"}
    draft = store.prepare_native("g", "admin", "private-one", 0, payload)
    for owner, umo, version, kick in [
        ("other", "private-one", 0, True),
        ("admin", "other", 0, True),
        ("admin", "private-one", 1, True),
        ("admin", "private-one", 0, False),
    ]:
        with pytest.raises(SessionError, match="preview"):
            store.claim_native("g", owner, umo, draft, version, kick)
    assert store.claim_native("g", "admin", "private-one", draft, 0, True) == payload
    with pytest.raises(SessionError):
        store.claim_native("g", "admin", "private-one", draft, 0, True)


def test_native_manual_api_actions_are_removed(tmp_path, monkeypatch):
    async def run():
        plugin, config, instance = prepared_plugin(tmp_path, monkeypatch)
        instance.platform_action = AsyncMock(return_value={"status": "succeeded"})
        account = plugin.store._read(config["session_ref"])["account_id"]
        roster = {
            account: {"roleLevel": 20},
            "ordinary": {"roleLevel": 20},
            "admin": {"roleLevel": 20},
            "native-owner": {"roleLevel": 100},
        }
        plugin.business.roster = AsyncMock(return_value=roster)
        body = {
            "session_ref": config["session_ref"],
            "instance_id": config["id"],
            "revision": instance_revision(config),
            "operation_id": "operation-test",
            "action": "mute",
            "kind": "group",
            "target_id": "g",
            "business_version": 0,
            "confirm": True,
            "user_id": "ordinary",
            "muted_seconds": 600,
        }
        assert (await call(plugin, "platform_action", "bob", body))[0] == 403
        value = {
            **copy.deepcopy(DEFAULTS),
            "native_actions": ["mute", "kick", "announce"],
        }
        await plugin.business.db(
            config["session_ref"], "save_settings", "g", 0, value, "owner"
        )
        for action in ("mute", "unmute", "kick", "announce", "mute_all", "unmute_all"):
            result = await call(
                plugin, "platform_action", "alice", {**body, "action": action}
            )
            assert (
                result[0] == 409
                and result[1]["message"] == "manual_group_actions_removed"
            )
        instance.platform_action.assert_not_awaited()
        await plugin.business.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("action", "grant", "fields"),
    [
        ("announce", "announce", {"announcement": "test", "expected_announcement": ""}),
        ("revoke_member_message", "revoke_others", {"message_id": "fresh-message"}),
        (
            "set_member_group_nickname",
            "rename_others",
            {"nickname": "acceptance", "expected_nickname": "original"},
        ),
    ],
)
def test_native_local_member_probe_exact_grant_and_versions(
    tmp_path, monkeypatch, action, grant, fields
):
    from test_lajiaoliao_instance_management import provision

    async def run():
        plugin, config, instance = prepared_plugin(tmp_path, monkeypatch)
        target = provision(plugin, "alice", "test-member")
        ref = config["session_ref"]
        uid = plugin.store._read(target["session_ref"])["account_id"]
        account = plugin.store._read(ref)["account_id"]
        plugin.business.roster = AsyncMock(
            return_value={
                account: {"roleLevel": 100},
                uid: {"roleLevel": 20, "nickname": "original"},
            }
        )
        instance.platform_action = AsyncMock(return_value={"status": "succeeded"})
        settings = {**copy.deepcopy(DEFAULTS), "native_actions": [grant]}
        await plugin.business.db(ref, "save_settings", "g", 0, settings, "owner")
        body = {
            "session_ref": ref,
            "instance_id": config["id"],
            "revision": instance_revision(config),
            "business_version": 1,
            "operation_id": "local-member-probe",
            "action": action,
            "kind": "group",
            "target_id": "g",
            "confirm": True,
            **fields,
        }
        if action != "announce":
            body["user_id"] = uid
        permit = NativeTestPermit(plugin.store)
        try:
            permit.create(
                ref, "alice", target["session_ref"], "g", [body], confirm=True
            )
            with pytest.raises(SessionError, match="already_present"):
                permit.create(
                    ref, "alice", target["session_ref"], "g", [body], confirm=True
                )
            assert (await call(plugin, "platform_action", "bob", body))[0] == 403
            assert (
                await call(
                    plugin,
                    "platform_action",
                    "alice",
                    {
                        **body,
                        "operation_id": "unapproved-probe",
                    },
                )
            )[1]["message"] == "manual_group_actions_removed"
            instance.platform_action.assert_not_awaited()
            assert (await call(plugin, "platform_action", "alice", body))[0] == 200
            payload = instance.platform_action.await_args.args[0]
            assert payload["action"] == action
            assert "session_ref" not in payload and "revision" not in payload
            # An exact permit cannot override a revoked grant or a newer settings version.
            await plugin.business.db(
                ref,
                "save_settings",
                "g",
                1,
                {**settings, "native_actions": []},
                "owner",
            )
            instance.platform_action.reset_mock()
            assert (await call(plugin, "platform_action", "alice", body))[1][
                "message"
            ] == "business_changed_refresh_required"
            instance.platform_action.assert_not_awaited()
        finally:
            permit.close(ref)
            await plugin.business.close()

    asyncio.run(run())


def test_invitation_callback_disabled_does_not_fetch_members(tmp_path, monkeypatch):
    async def run():
        plugin, config, _ = prepared_plugin(tmp_path, monkeypatch)
        plugin.business.roster = AsyncMock()
        await plugin.business.notification(
            config["session_ref"],
            {
                "action": "members_invited",
                "group_id": "g",
                "timestamp": time.time(),
                "message_id": "n",
                "member_ids": ["new"],
            },
        )
        plugin.business.roster.assert_not_awaited()
        await plugin.business.close()

    asyncio.run(run())


def test_native_test_permit_exact_owned_target_expiry_and_cleanup(
    tmp_path, monkeypatch
):
    from test_lajiaoliao_instance_management import provision

    async def run():
        plugin, config, instance = prepared_plugin(tmp_path, monkeypatch)
        target = provision(plugin, "alice", "test-member")
        ref = config["session_ref"]
        target_ref = target["session_ref"]
        uid = plugin.store._read(target_ref)["account_id"]
        account = plugin.store._read(ref)["account_id"]
        plugin.business.roster = AsyncMock(
            return_value={account: {"roleLevel": 100}, uid: {"roleLevel": 20}}
        )
        instance.platform_action = AsyncMock(return_value={"status": "succeeded"})
        await plugin.business.db(
            ref,
            "save_settings",
            "g",
            0,
            {**copy.deepcopy(DEFAULTS), "native_actions": ["mute", "unmute", "kick"]},
            "owner",
        )
        body = {
            "session_ref": ref,
            "instance_id": config["id"],
            "revision": instance_revision(config),
            "business_version": 1,
            "operation_id": "native-test-one",
            "action": "mute",
            "kind": "group",
            "target_id": "g",
            "user_id": uid,
            "muted_seconds": 60,
            "confirm": True,
        }
        permit = NativeTestPermit(plugin.store)
        assert not permit.allows(ref, "g", body)
        assert (await call(plugin, "platform_action", "alice", body))[1][
            "message"
        ] == "manual_group_actions_removed"
        with pytest.raises(SessionError):
            permit.create(ref, "bob", target_ref, "g", [body], confirm=True)
        with pytest.raises(SessionError):
            permit.create(ref, "alice", target_ref, "other", [body], confirm=True)
        foreign = provision(plugin, "bob", "foreign-member")
        with pytest.raises(SessionError):
            permit.create(
                ref, "alice", foreign["session_ref"], "g", [body], confirm=True
            )
        with pytest.raises(SessionError):
            permit.create(ref, "alice", ref, "g", [body], confirm=True)
        with pytest.raises(SessionError):
            permit.create(ref, "alice", target_ref, "g", [body, body], confirm=True)
        permit.create(ref, "alice", target_ref, "g", [body], confirm=True)
        assert (
            plugin.store.directory(ref) / "native-test-permit.json"
        ).stat().st_mode & 0o777 == 0o600
        assert permit.allows(ref, "g", body)
        for changes in [
            {"operation_id": "native-test-two"},
            {"muted_seconds": 600},
            {"action": "kick"},
            {"target_id": "other"},
            {"user_id": account},
            {"instance_id": "other"},
            {"confirm": False},
        ]:
            assert not permit.allows(ref, "g", {**body, **changes})
        status, result = await call(plugin, "platform_action", "alice", body)
        assert status == 200
        assert result["status"] == "succeeded"
        assert instance.platform_action.await_args.args[0]["action"] == "mute"
        # Explicit test consent never authorizes an AstrBot administrator.
        current_config = plugin.context.get_config()
        current_config["admins_id"].append(uid)
        plugin.context.get_config = lambda umo=None: current_config
        assert (await call(plugin, "platform_action", "alice", body))[1][
            "message"
        ] == "protected_member"
        current_config["admins_id"].remove(uid)
        plugin.business.roster.return_value[uid]["roleLevel"] = 60
        assert (await call(plugin, "platform_action", "alice", body))[1][
            "message"
        ] == "protected_or_unverified_member"
        plugin.business.roster.return_value[uid]["roleLevel"] = 20
        target_metadata = plugin.store._read(target_ref)
        plugin.store._write({**target_metadata, "state": "revoked"})
        assert not permit.allows(ref, "g", body)
        plugin.store._write(target_metadata)
        permit_path = plugin.store.directory(ref) / "native-test-permit.json"
        permit_path.chmod(0o644)
        assert not permit.allows(ref, "g", body)
        permit_path.chmod(0o600)
        with monkeypatch.context() as clock:
            clock.setattr(
                "data.plugins.astrbot_plugin_lajiaoliao.test_window.time.time",
                lambda: time.time_ns() / 1e9 + 301,
            )
            assert not permit.allows(ref, "g", body)
        permit.close(ref)
        assert not permit.allows(ref, "g", body)
        await plugin.business.close()

    asyncio.run(run())
