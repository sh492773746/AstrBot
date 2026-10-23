import asyncio
import copy
from unittest.mock import AsyncMock

from test_lajiaoliao_businesses import prepared_plugin
from test_lajiaoliao_plugin_api import call


def test_read_only_inspection_owner_policy_and_existing_worker(tmp_path, monkeypatch):
    async def run():
        plugin, config, _ = prepared_plugin(tmp_path, monkeypatch)
        plugin.manager.request = AsyncMock(return_value={"messages": []})
        body = {"session_ref": config["session_ref"], "kind": "group", "target_id": "g"}
        assert (await call(plugin, "message_inspect", "bob", body))[0] == 403
        assert (await call(plugin, "message_inspect", "alice", {**body, "target_id": "other"}))[0] == 403
        plugin.manager.request.assert_not_awaited()
        assert (await call(plugin, "message_inspect", "alice", body))[0] == 200
        plugin.manager.request.assert_awaited_once_with(
            config["session_ref"], "GET", "/v1/diagnostics/messages?kind=group&target_id=g"
        )
        config["message_policy"]["private_enabled"] = False
        assert (await call(plugin, "message_inspect", "alice", {
            **body, "kind": "private", "target_id": "member",
        }))[0] == 403
        config["enable"] = False
        assert (await call(plugin, "message_inspect", "alice", body))[0] == 409
    asyncio.run(run())


def test_inspection_rechecks_replaced_config_after_await(tmp_path, monkeypatch):
    async def run():
        plugin, config, _ = prepared_plugin(tmp_path, monkeypatch)

        async def request(*args):
            changed = copy.deepcopy(config)
            changed["message_policy"]["group_ids"] = []
            monkeypatch.setattr(plugin.context, "get_config", lambda: {"platform": [changed]})
            return {"messages": [{"text": "must not escape revoked scope"}]}

        plugin.manager.request = AsyncMock(side_effect=request)
        response = await call(plugin, "message_inspect", "alice", {
            "session_ref": config["session_ref"], "kind": "group", "target_id": "g",
        })
        assert response[0] == 403
        assert "messages" not in response[1]
    asyncio.run(run())
