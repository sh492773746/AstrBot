import asyncio
import time

import pytest
from test_lajiaoliao_businesses import prepared_plugin, settings
from test_lajiaoliao_instance_management import provision
from test_lajiaoliao_plugin_api import call

from astrbot.core.platform.instance_control import instance_revision
from data.plugins.astrbot_plugin_lajiaoliao.storage import SessionError, atomic_json
from data.plugins.astrbot_plugin_lajiaoliao.test_window import GroupManagerTestPermit


def test_local_lease_owned_scope_expiry_permissions_and_identity(tmp_path, monkeypatch):
    plugin, config, _ = prepared_plugin(tmp_path, monkeypatch)
    other = provision(plugin, "alice", "two")
    ref, target_ref = config["session_ref"], other["session_ref"]
    uid = plugin.store._read(target_ref)["account_id"]
    permit = GroupManagerTestPermit(plugin.store)
    assert not permit.allows(ref, "g", uid)
    with pytest.raises(SessionError):
        permit.create(ref, "alice", target_ref, "g")
    with pytest.raises(SessionError):
        permit.create(ref, "bob", target_ref, "g", confirm=True)
    permit.create(ref, "alice", target_ref, "g", confirm=True)
    assert permit.allows(ref, "g", uid)
    assert not permit.allows(target_ref, "g", uid)
    assert not permit.allows(ref, "other", uid)
    assert not permit.allows(ref, "g", "ordinary")
    path = plugin.store.directory(ref) / "group-manager-test-permit.json"
    assert path.stat().st_mode & 0o777 == 0o600
    path.chmod(0o644)
    assert not permit.allows(ref, "g", uid)
    path.chmod(0o600)
    import json

    value = json.loads(path.read_text())
    value.update(created_at=time.time() - 301, expires_at=time.time() - 1)
    atomic_json(path, value)
    assert not permit.allows(ref, "g", uid)
    permit.close(ref)
    assert not path.exists()


def test_permit_does_not_grant_itself_or_change_global_roles(tmp_path, monkeypatch):
    async def run():
        plugin, config, _ = prepared_plugin(tmp_path, monkeypatch)
        other = provision(plugin, "alice", "two")
        ref, target_ref = config["session_ref"], other["session_ref"]
        uid = plugin.store._read(target_ref)["account_id"]
        body = {
            "session_ref": ref, "group_id": "g", "action": "save-managers",
            "confirm": True, "version": 0, "manager_user_ids": [uid],
            "instance_revision": instance_revision(config),
        }
        from unittest.mock import AsyncMock

        plugin.business.roster = AsyncMock(return_value={uid: {}})
        assert (await call(plugin, "business_page", "alice", body))[0] == 409
        permit = GroupManagerTestPermit(plugin.store)
        permit.create(ref, "alice", target_ref, "g", confirm=True)
        assert not await plugin.business.group_administrator(ref, "g", uid, "private")
        assert (await call(plugin, "business_page", "alice", body))[0] == 200
        assert await plugin.business.group_administrator(ref, "g", uid, "private")
        assert not plugin.business.administrator(uid, "private")
        assert await plugin.business.accessible_groups(ref, uid, "private") == ["g"]
        assert not await plugin.business.group_administrator(ref, "other", uid, "private")
        permit.close(ref)
        assert not await plugin.business.group_administrator(ref, "g", uid, "private")
        assert not await plugin.business.accessible_groups(ref, uid, "private")
        # Even a leftover test grant fails closed after lease removal.
        assert (await plugin.business.settings(ref, "g"))["settings"]["manager_user_ids"] == [uid]
        await plugin.business.db(ref, "save_settings", "g", 1, settings(), "cleanup")
    asyncio.run(run())
