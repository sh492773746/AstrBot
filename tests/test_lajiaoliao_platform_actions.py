import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
from test_lajiaoliao_instance_management import adapter_class, provision  # noqa: F401
from test_lajiaoliao_plugin_api import call, make_plugin

from astrbot.core.platform.instance_control import instance_revision
from data.plugins.astrbot_plugin_lajiaoliao.protocol import LajiaoLiaoProtocolError


def test_manual_actions_removed_but_receipt_queries_remain(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path, monkeypatch)
        config = provision(plugin, "alice", "one")
        config.update(
            enable=True,
            message_policy={
                "group_mode": "allowlist",
                "group_ids": ["test"],
                "private_enabled": False,
            },
        )
        instance = SimpleNamespace(
            platform_action=AsyncMock(
                return_value={"status": "succeeded", "operation_id": "operation-test"}
            )
        )
        plugin.context.get_config = lambda: {"platform": [config]}
        plugin.context.get_platform_inst = lambda _: instance
        body = {
            "session_ref": config["session_ref"],
            "instance_id": "one",
            "revision": instance_revision(config),
            "operation_id": "operation-test",
            "action": "set_self_group_nickname",
            "kind": "group",
            "target_id": "test",
            "nickname": "new",
            "expected_nickname": "old",
            "confirm": True,
        }
        assert (await call(plugin, "platform_action", "bob", body))[0] == 403
        assert (
            await call(
                plugin, "platform_action", "alice", {**body, "instance_id": "two"}
            )
        )[0] == 409
        assert (
            await call(plugin, "platform_action", "alice", {**body, "confirm": False})
        )[0] == 409
        assert (
            await call(
                plugin, "platform_action", "alice", {**body, "revision": "stale"}
            )
        )[0] == 409
        assert (
            await call(
                plugin, "platform_action", "alice", {**body, "target_id": "denied"}
            )
        )[0] == 409
        assert (
            await call(
                plugin, "platform_action", "alice", {**body, "user_id": "someone"}
            )
        )[0] == 409
        assert (
            await call(
                plugin, "platform_action", "alice", {**body, "nickname": "bad\ncard"}
            )
        )[0] == 409
        instance.platform_action.assert_not_awaited()
        for action in ("set_self_group_nickname", "revoke_self_message", "mute", "kick", "announce"):
            status, data = await call(
                plugin, "platform_action", "alice", {**body, "action": action}
            )
            assert status == 409 and data["message"] == "manual_group_actions_removed"
        instance.platform_action.assert_not_awaited()
        await call(
            plugin,
            "platform_action",
            "alice",
            {
                "session_ref": config["session_ref"],
                "instance_id": "one",
                "operation_id": "operation-test",
                "query": True,
            },
        )
        assert instance.platform_action.call_args.args[0] == {
            "operation_id": "operation-test",
            "query": True,
        }

    asyncio.run(run())


def test_group_state_only_reads_owned_allowed_instance(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path, monkeypatch)
        config = provision(plugin, "alice", "one")
        config.update(
            enable=True,
            message_policy={
                "group_mode": "allowlist",
                "group_ids": ["test"],
                "private_enabled": False,
            },
        )
        plugin.context.get_config = lambda: {"platform": [config]}
        plugin.context.get_platform_inst = lambda _: SimpleNamespace(
            notifications=[
                {"group_id": "test", "message_id": "n1"},
                {"group_id": "other", "message_id": "n2"},
            ]
        )
        plugin.manager.request.return_value = {"nickname": "old", "messages": []}
        body = {"session_ref": config["session_ref"], "group_id": "test"}
        assert (await call(plugin, "group_state", "bob", body))[0] == 403
        assert (
            await call(plugin, "group_state", "alice", {**body, "group_id": "other"})
        )[0] == 403
        plugin.manager.request.assert_not_awaited()
        status, result = await call(plugin, "group_state", "alice", body)
        assert status == 200 and result["notifications"] == [
            {"group_id": "test", "message_id": "n1"}
        ]
        plugin.manager.request.assert_awaited_once_with(
            config["session_ref"], "GET", "/v1/actions/group-state?group_id=test"
        )

    asyncio.run(run())


def test_notifications_cancel_only_matching_pending_message_and_bound_caches(
    tmp_path,
    monkeypatch,
    adapter_class,  # noqa: F811
):
    async def run():
        plugin = make_plugin(tmp_path, monkeypatch)
        config = provision(plugin, "alice", "one")
        adapter = adapter_class(config, {}, asyncio.Queue())
        matching, other = (
            asyncio.get_running_loop().create_future(),
            asyncio.get_running_loop().create_future(),
        )
        adapter._pending_messages[("group", "test", "original")] = matching
        adapter._pending_messages[("group", "other", "original")] = other
        note = {
            "action": "message_revoked",
            "message_id": "n1",
            "kind": "group",
            "target_id": "test",
            "group_id": "test",
            "revoked_message_id": "original",
        }
        adapter.apply_notification(note)
        assert matching.cancelled() and not other.done()
        assert ("group", "test", "original") in adapter._revoked_messages
        adapter.apply_notification(note)
        assert len(adapter.notifications) == 1
        for index in range(600):
            adapter.apply_notification(
                {
                    "action": "group_member_updated",
                    "message_id": f"n{index + 2}",
                    "group_id": "test",
                    "user_id": str(index),
                    "nickname": "new card",
                }
            )
        assert len(adapter.group_nicknames) == 512 and len(adapter.notifications) == 100
        assert adapter.group_nicknames[("test", "599")] == "new card"
        other.cancel()

    asyncio.run(run())


def test_notification_ack_and_out_of_order_recall_never_enter_model(
    tmp_path,
    monkeypatch,
    adapter_class,  # noqa: F811
):
    async def run():
        plugin = make_plugin(tmp_path, monkeypatch)
        config = provision(plugin, "alice", "one")
        config["message_policy"]["group_ids"] = ["test"]
        queue = asyncio.Queue()
        adapter = adapter_class(config, {}, queue)
        notes = [
            {
                "action": "message_revoked",
                "message_id": "n1",
                "kind": "group",
                "target_id": "test",
                "group_id": "test",
                "revoked_message_id": "original",
            },
            {
                "action": "group_member_updated",
                "message_id": "n2",
                "group_id": "test",
                "user_id": "user",
                "nickname": "group card",
            },
        ]
        events = [{"type": "notification", "data": note} for note in notes]
        events.extend(
            [
                {
                    "type": "message",
                    "data": {
                        "message_id": mid,
                        "kind": "group",
                        "target_id": "test",
                        "sender_id": "user",
                        "sender_name": "global name",
                        "text": "hello",
                    },
                }
                for mid in ("original", "fresh")
            ]
        )
        acks = []

        class Socket:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not events:
                    raise StopAsyncIteration
                return SimpleNamespace(
                    type=aiohttp.WSMsgType.TEXT, data=json.dumps(events.pop(0))
                )

            async def send_json(self, body):
                acks.append(body)

        adapter.http = SimpleNamespace(ws_connect=lambda *args, **kwargs: Socket())
        try:
            await adapter._consume_events()
        except LajiaoLiaoProtocolError as exc:
            assert str(exc) == "bridge_events_closed"
        assert [item["message_id"] for item in acks] == [
            "n1",
            "n2",
            "original",
            "fresh",
        ]
        assert queue.qsize() == 1
        event = queue.get_nowait()
        assert event.message_obj.message_id == "fresh"
        assert event.message_obj.sender.nickname == "group card"
        event.processing_completion.cancel()
        await asyncio.sleep(0)
        assert not adapter._pending_messages

    asyncio.run(run())
