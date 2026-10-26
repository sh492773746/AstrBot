"""Run separately from legacy adapter tests to avoid duplicate registry entries."""

import asyncio
import json
import tempfile
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain, Reply
from astrbot.core.event_bus import EventBus
from astrbot.core.pipeline.scheduler import PipelineScheduler
from astrbot.core.platform.message_policy import MessagePolicyDenied
from data.plugins.astrbot_plugin_lajiaoliao import runtime
from data.plugins.astrbot_plugin_lajiaoliao.adapter import LajiaoLiaoAdapter
from data.plugins.astrbot_plugin_lajiaoliao.storage import SessionStore


async def main():
    with tempfile.TemporaryDirectory() as directory:
        store = SessionStore(Path(directory))
        ref = store.create("owner")["session_ref"]
        store.complete(ref, "owner", "bot")
        store.bind(ref, "owner", "test")
        runtime.active = SimpleNamespace(store=store)
        queue = asyncio.Queue()
        config = {
            "id": "test",
            "session_ref": ref,
            "message_policy": {
                "group_mode": "allowlist",
                "group_ids": ["allowed"],
                "private_enabled": False,
            },
        }
        adapter = LajiaoLiaoAdapter(config, {}, queue)
        events = [{"type": "ready", "data": {"status": "online", "account_id": "bot"}}]
        for mid, target in [("m1", "allowed"), ("m1", "allowed"), ("m2", "denied")]:
            events.append(
                {
                    "type": "message",
                    "data": {
                        "message_id": mid,
                        "sender_id": "user",
                        "kind": "group",
                        "target_id": target,
                        "text": "synthetic",
                        "components": [
                            {
                                "type": "quote",
                                "message_id": "quoted-original",
                                "sender_id": "bot",
                                "text": "quoted context",
                                "timestamp": 123,
                            }
                        ],
                    },
                }
            )
        acks, sends = [], []

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
                acks.append(value)

        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def json(self, **kwargs):
                return {"status": "sent", "message_id": "receipt"}

        def post(url, **kwargs):
            sends.append(kwargs["json"])
            return Response()

        adapter.http = SimpleNamespace(
            ws_connect=lambda *args, **kwargs: Socket(), post=post
        )
        try:
            await adapter._consume_events()
        except RuntimeError as exc:
            assert str(exc) == "bridge_events_closed"
        assert len(acks) == 3
        assert queue.qsize() == 1
        event = queue.get_nowait()
        assert event.get_group_id() == "allowed"
        await adapter.send_parts(
            "allowed", "", [{"type": "text", "text": "reply"}], "group"
        )
        assert len(sends) == 1
        model = AsyncMock(return_value="synthetic model reply")

        class ModelStage:
            async def process(self, incoming):
                quoted = next(
                    item
                    for item in incoming.message_obj.message
                    if isinstance(item, Reply)
                )
                text = await model(incoming.message_str, quoted.message_str)
                await incoming.send(
                    MessageChain(
                        [Reply(id=incoming.message_obj.message_id), Plain(text)]
                    )
                )

        scheduler = object.__new__(PipelineScheduler)
        scheduler.stages = [ModelStage()]
        bus = EventBus(
            queue,
            {"default": scheduler},
            SimpleNamespace(get_conf_info=lambda _: {"id": "default", "name": "test"}),
        )
        queue.put_nowait(event)
        with patch.object(AstrMessageEvent, "send", AsyncMock()):
            task = asyncio.create_task(bus.dispatch())
            try:
                await asyncio.wait_for(asyncio.shield(event.processing_completion), 5)
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        model.assert_awaited_once_with("synthetic", "quoted context")
        assert len(sends) == 2
        assert sends[-1]["parts"] == [
            {"type": "quote", "message_id": "m1"},
            {"type": "text", "text": "synthetic model reply"},
        ]
        config["message_policy"]["group_mode"] = "deny_all"
        try:
            await event.send(MessageChain([Plain("blocked reply")]))
            raise AssertionError("policy was bypassed")
        except MessagePolicyDenied:
            pass
        assert len(sends) == 2
        await asyncio.sleep(0)
        assert adapter._slots._value == 32
        runtime.active = None
        print(
            "Plugin adapter: identity, ACK, dedupe, EventBus/pipeline/quoted context/mock quoted reply, hot policy passed."
        )


if __name__ == "__main__":
    asyncio.run(main())
