import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest
from test_lajiaoliao_instance_management import adapter_class as adapter_class
from test_lajiaoliao_instance_management import provision
from test_lajiaoliao_plugin_api import make_plugin

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from data.plugins.astrbot_plugin_lajiaoliao.event import incoming_to_message
from data.plugins.astrbot_plugin_lajiaoliao.storage import SessionStore


def test_acceptance_reports_do_not_label_ignored_bot_media_as_handed_off(monkeypatch):
    monkeypatch.syspath_prepend(
        str(Path(__file__).resolve().parents[1] / "services/lajiaoliao-bridge")
    )
    from acceptance_client import image_disposition

    assert image_disposition({}, "m") == "pending"
    assert (
        image_disposition(
            {
                "ignored_bot_messages": [
                    {"message_id": "m", "status": "ignored_bound_bot"}
                ]
            },
            "m",
        )
        == "ignored_bound_bot"
    )
    assert (
        image_disposition(
            {"received_images": [{"message_id": "m", "status": "handed_off"}]}, "m"
        )
        == "handed_off"
    )


def test_bound_bot_cache_changes_on_bind_revoke_and_includes_other_owner(tmp_path):
    store = SessionStore(tmp_path)
    assert store.bound_account_ids() == frozenset()
    refs = []
    for owner, uid in (("alice", "bot-a"), ("bob", "bot-b")):
        ref = store.create(owner)["session_ref"]
        store.complete(ref, owner, uid)
        assert uid not in store.bound_account_ids()
        store.bind(ref, owner, uid)
        refs.append((ref, owner))
    assert store.bound_account_ids() == frozenset({"bot-a", "bot-b"})
    cached = store.bound_account_ids()
    original = store._all
    store._all = Mock(side_effect=AssertionError("no per-message directory scan"))
    assert store.bound_account_ids() is cached
    store._all = original
    store.revoke(*refs[1])
    assert store.bound_account_ids() == frozenset({"bot-a"})
    assert SessionStore(tmp_path).bound_account_ids() == frozenset({"bot-a"})


def test_two_bots_ack_peer_group_text_quote_image_and_self_without_queueing(
    tmp_path, monkeypatch, adapter_class
):
    async def run():
        plugin = make_plugin(tmp_path, monkeypatch)
        configs = [provision(plugin, "alice", name) for name in ("one", "two")]
        for config in configs:
            config["message_policy"].update(group_ids=["g"], private_enabled=True)
        for index, config in enumerate(configs):
            queue = asyncio.Queue()
            adapter = adapter_class(config, {}, queue)
            adapter.images.materialize = AsyncMock(
                side_effect=AssertionError("bound bot images must not be downloaded")
            )
            peer = "account-" + ("two" if index == 0 else "one")

            def message(mid, sender, kind="group", components=None):
                return {
                    "type": "message",
                    "data": {
                        "message_id": mid,
                        "sender_id": sender,
                        "target_id": "g" if kind == "group" else sender,
                        "kind": kind,
                        "text": "/quoted command",
                        "components": components or [],
                    },
                }

            events = [
                message("text", peer),
                message(
                    "quote",
                    peer,
                    components=[
                        {
                            "type": "quote",
                            "message_id": "old",
                            "sender_id": adapter.account_id,
                            "text": "bot reply",
                        }
                    ],
                ),
                message(
                    "image", peer, components=[{"type": "image", "url": "malicious"}]
                ),
                message("self", adapter.account_id),
                message("text", peer),
                message(
                    "human-quote",
                    "human",
                    components=[
                        {
                            "type": "quote",
                            "message_id": "old",
                            "sender_id": adapter.account_id,
                            "text": "bot reply",
                        }
                    ],
                ),
                message("private-test", peer, "private"),
            ]
            sent = []

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

                async def send_json(self, value):
                    sent.append(value)

            adapter.http = SimpleNamespace(ws_connect=lambda *args, **kwargs: Socket())
            with pytest.raises(RuntimeError, match="bridge_events_closed"):
                await adapter._consume_events()
            assert queue.qsize() == 2
            assert [queue.get_nowait().message_obj.message_id for _ in range(2)] == [
                "human-quote",
                "private-test",
            ]
            assert len(sent) == 7 and all(row["type"] == "ack" for row in sent)
            assert len(adapter.ignored_bot_messages) == 4
            adapter.images.materialize.assert_not_awaited()
            for completion in tuple(adapter._pending_completions):
                completion.cancel()

    asyncio.run(run())


@pytest.mark.parametrize("sender,allowed", [("account-two", False), ("human", True)])
def test_pipeline_and_reply_guards_preserve_human_quotes(
    tmp_path, monkeypatch, adapter_class, sender, allowed
):
    async def run():
        plugin = make_plugin(tmp_path, monkeypatch)
        config = provision(plugin, "alice", "one")
        provision(plugin, "bob", "two")
        adapter = adapter_class(config, {}, asyncio.Queue())
        adapter.send_parts = AsyncMock()
        message = incoming_to_message(
            {
                "message_id": "source",
                "sender_id": sender,
                "target_id": "g",
                "kind": "group",
                "text": "question",
                "components": [
                    {
                        "type": "quote",
                        "message_id": "old",
                        "sender_id": adapter.account_id,
                        "text": "reply",
                    }
                ],
            },
            adapter.account_id,
        )
        event = adapter.create_event(message)
        if allowed:
            assert await event.prepare_waking_check({}, []) is True
        else:
            # Even bypassing the pipeline does not permit a stale queued reply.
            await event.send(MessageChain([Plain("should not send")]))
            adapter.send_parts.assert_not_awaited()
            assert await event.prepare_waking_check({}, []) is False
            assert event.processing_completion.cancelled()
        event.processing_completion.cancel()

    asyncio.run(run())


def test_binding_new_bot_blocks_existing_queued_reply(
    tmp_path, monkeypatch, adapter_class
):
    async def run():
        plugin = make_plugin(tmp_path, monkeypatch)
        config = provision(plugin, "alice", "one")
        adapter = adapter_class(config, {}, asyncio.Queue())
        adapter.send_parts = AsyncMock()
        event = adapter.create_event(
            incoming_to_message(
                {
                    "message_id": "source",
                    "sender_id": "account-two",
                    "target_id": "g",
                    "kind": "group",
                    "text": "question",
                },
                adapter.account_id,
            )
        )
        assert await event.prepare_waking_check({}, []) is True
        provision(plugin, "bob", "two")
        await event.send(MessageChain([Plain("late reply")]))
        adapter.send_parts.assert_not_awaited()
        event.processing_completion.cancel()

    asyncio.run(run())


def test_real_waking_stage_stops_bound_bot_before_handlers_or_model(
    tmp_path, monkeypatch, adapter_class
):
    from astrbot.core.pipeline.waking_check.stage import WakingCheckStage

    async def run():
        plugin = make_plugin(tmp_path, monkeypatch)
        config = provision(plugin, "alice", "one")
        provision(plugin, "alice", "two")
        adapter = adapter_class(config, {}, asyncio.Queue())
        event = adapter.create_event(
            incoming_to_message(
                {
                    "message_id": "source",
                    "sender_id": "account-two",
                    "target_id": "g",
                    "kind": "group",
                    "text": "/test",
                    "components": [
                        {
                            "type": "quote",
                            "message_id": "own",
                            "sender_id": adapter.account_id,
                            "text": "bot reply",
                        }
                    ],
                },
                adapter.account_id,
            )
        )
        stage = WakingCheckStage()
        # No later pipeline state is provided: any handler/model path would fail.
        stage.ctx = SimpleNamespace(astrbot_config={"admins_id": ["account-two"]})
        await stage.process(event)
        assert event.is_stopped() and event.processing_completion.cancelled()
        assert not event.is_wake

    asyncio.run(run())
