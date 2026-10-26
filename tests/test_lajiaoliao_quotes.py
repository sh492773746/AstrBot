import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_lajiaoliao_instance_management import Config, provision
from test_lajiaoliao_instance_management import adapter_class as adapter_class
from test_lajiaoliao_plugin_api import call, make_plugin

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain, Reply
from astrbot.api.platform import PlatformMetadata
from data.plugins.astrbot_plugin_lajiaoliao.event import (
    LajiaoLiaoEvent,
    chain_to_parts,
    incoming_to_message,
)
from data.plugins.astrbot_plugin_lajiaoliao.protocol import LajiaoLiaoProtocolError
from data.plugins.astrbot_plugin_lajiaoliao.storage import SessionError
from data.plugins.astrbot_plugin_lajiaoliao.test_window import AcceptanceWindow


def test_incoming_quote_preserves_text_and_original_context():
    message = incoming_to_message(
        {
            "message_id": "new-message",
            "sender_id": "user",
            "target_id": "test-group",
            "kind": "group",
            "text": "/new question",
            "components": [
                {
                    "type": "quote",
                    "message_id": "original",
                    "text": "original answer",
                    "sender_id": "bot",
                    "sender_name": "Bot",
                    "timestamp": 123,
                }
            ],
        },
        "bot",
    )
    assert message.message_str == "/new question"
    assert message.session_id == "group:test-group"
    quote = next(part for part in message.message if isinstance(part, Reply))
    assert quote.id == "original"
    assert quote.message_str == "original answer"
    assert quote.chain[0].text == "original answer"
    assert quote.sender_id == "bot"
    assert quote.sender_nickname == "Bot"
    assert quote.time == 123
    assert chain_to_parts(MessageChain([Reply(id="original"), Plain("reply")])) == [
        {"type": "quote", "message_id": "original"},
        {"type": "text", "text": "reply"},
    ]


@pytest.mark.parametrize("kind", ["group", "private"])
def test_event_reply_sends_explicit_quote_only(kind, monkeypatch):
    async def run():
        monkeypatch.setattr(AstrMessageEvent, "send", AsyncMock())
        platform = SimpleNamespace(
            send_parts=AsyncMock(),
            member_ids={},
            meta=lambda: PlatformMetadata("lajiaoliao", "test", "instance-one"),
        )
        message = incoming_to_message(
            {
                "message_id": "original",
                "sender_id": "user",
                "target_id": "group" if kind == "group" else "user",
                "kind": kind,
                "text": "question",
            },
            "bot",
        )
        event = LajiaoLiaoEvent(message, platform)
        await event.send(MessageChain([Reply(id="original"), Plain("reply")]))
        platform.send_parts.assert_awaited_once_with(
            target_id="group" if kind == "group" else "user",
            reply_to="",
            parts=[
                {"type": "quote", "message_id": "original"},
                {"type": "text", "text": "reply"},
            ],
            kind=kind,
        )

    asyncio.run(run())


def test_quote_acceptance_digest_prevents_retargeted_retry(tmp_path, monkeypatch):
    plugin = make_plugin(tmp_path, monkeypatch)
    config = provision(plugin, "alice", "one")
    windows = AcceptanceWindow(plugin.store)
    ref = config["session_ref"]
    state = windows.update(
        ref,
        "alice",
        {
            "action": "open",
            "confirm": True,
            "account_id": "account-one",
            "targets": [{"kind": "group", "target_id": "test-group"}],
        },
    )
    body = {
        "window_ref": state["window_ref"],
        "operation_id": "quote-once",
        "kind": "group",
        "target_id": "test-group",
        "text": "reply",
        "reply_to": "original",
    }
    result, submit = windows.reserve(ref, "alice", body)
    assert submit and result["status"] == "unknown"
    assert windows.reserve(ref, "alice", body) == (result, False)
    for changed in ("other-original", ""):
        with pytest.raises(SessionError, match="operation_id_conflict"):
            windows.reserve(ref, "alice", {**body, "reply_to": changed})
    for invalid in (123, None, " original", "x" * 129):
        with pytest.raises(ValueError, match="invalid_quote_id"):
            windows.reserve(ref, "alice", {**body, "reply_to": invalid})


def test_api_quote_test_send_keeps_scope_and_idempotent_receipt(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path, monkeypatch)
        config = provision(plugin, "alice", "one")
        config["enable"] = True
        config["message_policy"]["group_ids"] = ["test-group"]
        plugin.context.get_config = lambda: Config(platform=[config])
        sender = AsyncMock(
            return_value={"status": "sent", "message_id": "quote-receipt"}
        )
        plugin.context.get_platform_inst = lambda _: SimpleNamespace(send_parts=sender)
        plugin.acceptance = AcceptanceWindow(plugin.store)
        ref = config["session_ref"]
        _, state = await call(
            plugin,
            "test_window",
            "alice",
            {
                "session_ref": ref,
                "action": "open",
                "confirm": True,
                "account_id": "account-one",
                "targets": [{"kind": "group", "target_id": "test-group"}],
            },
        )
        body = {
            "session_ref": ref,
            "window_ref": state["window_ref"],
            "operation_id": "quote-once",
            "kind": "group",
            "target_id": "test-group",
            "text": "reply",
            "reply_to": "original",
        }
        assert (await call(plugin, "test_send", "bob", body))[0] == 403
        assert (
            await call(
                plugin, "test_send", "alice", {**body, "target_id": "other-group"}
            )
        )[0] == 403
        status, result = await call(plugin, "test_send", "alice", body)
        assert status == 200 and result["status"] == "sent"
        assert (await call(plugin, "test_send", "alice", body))[1] == result
        sender.assert_awaited_once_with(
            "test-group", "original", [{"type": "text", "text": "reply"}], "group"
        )

    asyncio.run(run())


def test_quote_preflight_rejection_is_not_reported_as_unknown(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path, monkeypatch)
        config = provision(plugin, "alice", "one")
        config["enable"] = True
        config["message_policy"]["group_ids"] = ["test-group"]
        plugin.context.get_config = lambda: Config(platform=[config])
        sender = AsyncMock(
            side_effect=LajiaoLiaoProtocolError(
                "quote_not_found", delivery_status="rejected"
            )
        )
        plugin.context.get_platform_inst = lambda _: SimpleNamespace(send_parts=sender)
        plugin.acceptance = AcceptanceWindow(plugin.store)
        ref = config["session_ref"]
        _, state = await call(
            plugin,
            "test_window",
            "alice",
            {
                "session_ref": ref,
                "action": "open",
                "confirm": True,
                "account_id": "account-one",
                "targets": [{"kind": "group", "target_id": "test-group"}],
            },
        )
        body = {
            "session_ref": ref,
            "window_ref": state["window_ref"],
            "operation_id": "missing-quote",
            "kind": "group",
            "target_id": "test-group",
            "text": "reply",
            "reply_to": "missing",
        }
        status, result = await call(plugin, "test_send", "alice", body)
        assert status == 200 and result["status"] == "rejected"
        assert (await call(plugin, "test_send", "alice", body))[1] == result
        assert sender.await_count == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    ("http_status", "body", "expected"),
    [
        (422, {"error": "quote_not_found", "status": "rejected"}, "rejected"),
        (502, {"error": "quote_lookup_failed", "status": "rejected"}, "rejected"),
        (502, {"error": "connection_lost", "status": "unknown"}, "unknown"),
        (400, {"error": "invalid_quote_id"}, "rejected"),
    ],
)
def test_send_boundary_preserves_quote_failure_status(
    tmp_path, monkeypatch, adapter_class, http_status, body, expected
):
    async def run():
        plugin = make_plugin(tmp_path, monkeypatch)
        config = provision(plugin, "alice", "one")
        config["message_policy"]["group_ids"] = ["test-group"]
        adapter = adapter_class(config, {}, asyncio.Queue())

        class Response:
            status = http_status

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def json(self, **kwargs):
                return body

        adapter.http = SimpleNamespace(post=lambda *args, **kwargs: Response())
        with pytest.raises(LajiaoLiaoProtocolError) as error:
            await adapter.send_parts(
                "test-group", "original", [{"type": "text", "text": "reply"}], "group"
            )
        assert error.value.delivery_status == expected

    asyncio.run(run())
