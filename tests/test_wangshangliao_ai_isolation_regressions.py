"""Offline regressions for cancellation and isolated AI continuation authority."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import group_ai, private_batches
from astrbot.builtin_stars.wangshangliao_moderation.native_conversations import (
    NativeConversations,
)
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from astrbot.core.platform.sources.wangshangliao.test_window import TestWindow as Window
from tests.fixtures.wsl_native_conversations import conversation_manager
from tests.test_wangshangliao_component_batches import event, fixture
from tests.test_wangshangliao_group_ai import fake_http, model_scope
from tests.test_wangshangliao_group_ai_window import (
    GROUP,
    RECEIVER,
    SENDER,
    control,
    control_group_ai,
    inbound,
)
from tests.test_wangshangliao_group_ai_window import scope as scope
from tests.test_wangshangliao_open_rpc_guards import rpc_scope as rpc_scope


@pytest.fixture(autouse=True)
def offline_http(monkeypatch):
    """Require every model request to use an explicitly installed fake transport."""

    def forbidden(*args, **kwargs):
        raise AssertionError("Real HTTP is forbidden in isolation regressions")

    monkeypatch.setattr(group_ai.aiohttp, "ClientSession", forbidden)


@pytest.fixture
def tool_http(monkeypatch):
    """Expose a two-response model with controllable continuation failures."""
    transport = SimpleNamespace(requests=[], status=200, hook=None)

    class Response:
        def __init__(self, number):
            self.number, self.content = number, self
            self.status = 200 if number == 1 else transport.status

        async def __aenter__(self):
            if self.number == 2 and transport.hook is not None:
                await transport.hook()
            return self

        async def __aexit__(self, *args):
            return False

        async def iter_chunked(self, size):
            message = (
                {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "offline-tool",
                            "type": "function",
                            "function": {
                                "name": "pc28_analysis",
                                "arguments": '{"query":"latest","window":20}',
                            },
                        }
                    ],
                }
                if self.number == 1
                else {"content": "Verified public result."}
            )
            yield json.dumps(
                {
                    "choices": [{"message": message, "finish_reason": "stop"}],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "total_tokens": 120,
                    },
                }
            ).encode()

    class HTTP:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def post(self, url, **kwargs):
            transport.requests.append(kwargs["json"])
            return Response(len(transport.requests))

    monkeypatch.setattr(group_ai.aiohttp, "ClientSession", HTTP)
    return transport


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["delete", "switch", "edit"])
async def test_changed_native_dialogue_cannot_authorize_tool_continuation(
    rpc_scope, tool_http, change
):
    value, adapter, _ = rpc_scope
    row = await model_scope(value, adapter)
    manager = value.context.conversation_manager

    async def query(arguments, *, guard):
        current = next(iter(manager.db.rows.values()))
        if change == "delete":
            await manager.delete_conversation(current.user_id, current.conversation_id)
        elif change == "switch":
            await manager.new_conversation(current.user_id, title="replacement")
        else:
            await manager.update_conversation(
                current.user_id,
                current.conversation_id,
                history=[{"role": "user", "content": "edited"}],
            )
        return {"query": "latest", "status": "ok", "reason": "", "data": {}}

    value.pc28.query = AsyncMock(side_effect=query)
    await value.group_ai.run(adapter, row, "mention/revoked-tool/2/1002", "PC28 latest")
    assert len(tool_http.requests) == 1
    adapter.send_text.assert_not_awaited()
    with value.store.connect() as db:
        charges = db.execute("SELECT state FROM ai_charges").fetchall()
    assert all(charge["state"] in {"settled", "not_sent"} for charge in charges)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["http", "disconnect", "cancel", "timeout"])
async def test_unknown_continuation_blocks_active_test_and_survives_close(
    scope, tool_http, monkeypatch, failure
):
    value, receiver, sender, _ = scope
    await control(value, "open")
    isolated = value.group_ai.test_fixture
    value.pc28.query = AsyncMock(
        return_value={"query": "latest", "status": "ok", "reason": "", "data": {}}
    )
    if failure == "http":
        tool_http.status = 500
    else:
        if failure == "timeout":
            timeout = asyncio.timeout(None)
            monkeypatch.setattr(group_ai.asyncio, "timeout", lambda seconds: timeout)

        async def fail():
            if failure == "cancel":
                raise asyncio.CancelledError()
            if failure == "timeout":
                timeout.reschedule(asyncio.get_running_loop().time())
                await asyncio.sleep(3600)
            raise group_ai.aiohttp.ServerDisconnectedError("offline unknown result")

        tool_http.hook = fail
    await value.group_ai.observe(await inbound(scope, text="PC28 latest"))
    await asyncio.gather(*isolated.group_ai.tasks.values(), return_exceptions=True)
    assert len(tool_http.requests) == 2
    receiver.send_text.assert_not_awaited()
    assert value.chat_count == 0
    saved = isolated.group_ai.configs[(RECEIVER, GROUP)]
    assert saved["blocked"] == "model_result_unknown"
    with isolated.store.connect() as db:
        child = db.execute(
            "SELECT * FROM ai_charges WHERE request LIKE '%/tool-answer'"
        ).fetchone()
        assert child["state"] == "unknown"
        assert child["charged_micro"] == child["reserved_micro"] > 0
    saved["last_attempt"] = 0
    isolated.group_ai.launch(
        isolated.adapter, saved, "mention/another/31846829/1938341073", "PC28 latest"
    )
    assert not isolated.group_ai.tasks
    with value.store.connect() as db:
        assert db.execute("SELECT count(*) FROM ai_charges").fetchone()[0] == 0
    await control(value, "close")
    receiver.test_window = Window(receiver, sender, [GROUP], ["group_ai"], [], 300, 3)
    with pytest.raises(OpenError, match="test_group_ai_unknown"):
        await control(value, "open")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["delete", "switch", "edit"])
async def test_changed_native_dialogue_suppresses_late_group_delivery(
    rpc_scope, monkeypatch, change
):
    value, adapter, _ = rpc_scope
    row = await model_scope(value, adapter)
    entered, release = asyncio.Event(), asyncio.Event()
    requests = fake_http(monkeypatch, barrier=(entered, release))
    task = asyncio.create_task(
        value.group_ai.run(adapter, row, "mention/late-native/2/1002", "hello")
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        manager = value.context.conversation_manager
        current = next(iter(manager.db.rows.values()))
        if change == "delete":
            await manager.delete_conversation(current.user_id, current.conversation_id)
        elif change == "switch":
            await manager.new_conversation(current.user_id, title="replacement")
        else:
            await manager.update_conversation(
                current.user_id,
                current.conversation_id,
                history=[{"role": "user", "content": "edited"}],
            )
    finally:
        release.set()
        await asyncio.wait_for(task, 3)
    assert len(requests) == 1
    adapter.send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_selection_change_during_record_read_is_rejected(monkeypatch):
    manager = conversation_manager()
    native = NativeConversations(SimpleNamespace(conversation_manager=manager))
    origin = "offline:GroupMessage:99/5/wsl-ai/group"
    dialogue = await native.load(origin, "test", "")
    await native.start(dialogue, lambda: None)
    original = manager.get_conversation

    async def changed(*args):
        result = await original(*args)
        await manager.switch_conversation(origin, "another-conversation")
        return result

    monkeypatch.setattr(manager, "get_conversation", changed)
    with pytest.raises(OpenError, match="conversation_changed"):
        await native.current(dialogue)


@pytest.mark.asyncio
async def test_reopened_test_keeps_budget_but_never_imports_production_history(
    scope, monkeypatch
):
    value, receiver, sender, _ = scope
    await control(value, "open")
    isolated = value.group_ai.test_fixture
    profile = isolated.profiles.resolve("group", RECEIVER, GROUP)
    production_origin = (
        f"{receiver.config['id']}:GroupMessage:{RECEIVER}/{GROUP}"
        f"/wsl-ai/{RECEIVER}/group/{profile['fingerprint']}"
        f"/mention/{SENDER}/{sender.nim_account}"
    )
    manager = value.context.conversation_manager
    production = await manager.new_conversation(
        production_origin,
        content=[{"role": "user", "content": "PRODUCTION_ONLY_SECRET"}],
    )
    requests = fake_http(monkeypatch, content="TEST_ONLY_REPLY")
    await value.group_ai.observe(await inbound(scope))
    await asyncio.gather(*isolated.group_ai.tasks.values())
    assert len(requests) == 1 and "PRODUCTION_ONLY_SECRET" not in str(requests)
    with isolated.store.connect() as db:
        charge = dict(db.execute("SELECT * FROM ai_charges").fetchone())
        assert charge["state"] == "settled" and charge["charged_micro"] > 0
    await control(value, "close")
    receiver.test_window = Window(receiver, sender, [GROUP], ["group_ai"], [], 300, 3)
    operation = "WSL_TEST_WARM_" + "b" * 32
    try:
        await control_group_ai(
            value, "dashboard/test", {"operation_id": operation, "action": "open"}
        )
        reopened = value.group_ai.test_fixture
        with reopened.store.connect() as db:
            assert dict(db.execute("SELECT * FROM ai_charges").fetchone()) == charge
        with value.store.connect() as db:
            assert db.execute("SELECT count(*) FROM ai_charges").fetchone()[0] == 0
        original = await manager.get_conversation(production_origin, production)
        assert "PRODUCTION_ONLY_SECRET" in original.history
        assert "TEST_ONLY_REPLY" not in original.history
    finally:
        await control_group_ai(
            value, "dashboard/test", {"operation_id": operation, "action": "close"}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["exit", "policy", "completion"])
async def test_private_enqueue_cannot_adopt_authority_changed_during_verification(
    tmp_path, monkeypatch, change
):
    service, adapter, batches, _ = fixture(tmp_path)
    monkeypatch.setattr(private_batches, "WINDOW", 0)
    monkeypatch.setattr(
        private_batches, "consultation_access", lambda *args: {"kind": "consultation"}
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def verify(source):
        entered.set()
        await release.wait()

    service.verify_actor.side_effect = verify
    source = event(adapter, "cancelled-before-buffer")
    task = asyncio.create_task(batches.enqueue(source))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        if change == "exit":
            batches.cancel(source)
            service.chat_generations[
                (adapter.account, source.actor_identity.principal)
            ] = 1
        elif change == "policy":
            adapter.config["_business_epoch"] += 1
        else:
            source.processing_completion.cancel()
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.gather(*tuple(batches.running), return_exceptions=True)
        service.chat.assert_not_awaited()
        assert not batches.pending
        with service.store.connect() as db:
            assert (
                db.execute("SELECT count(*) FROM private_ai_batches").fetchone()[0] == 0
            )
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await batches.close()
