"""Isolated canary and private batching checks without native sends or model costs."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import private_batches
from astrbot.builtin_stars.wangshangliao_moderation.component_governance import (
    Governance,
)
from astrbot.builtin_stars.wangshangliao_moderation.open_store import (
    OpenError,
    OpenStore,
)
from astrbot.core.platform.sources.wangshangliao.identity import ActorIdentity


def fixture(tmp_path):
    active = [True]
    adapter = SimpleNamespace(
        account="43506527",
        config={"id": "offline", "_business_epoch": 1, "_business_active": True},
        test_window=SimpleNamespace(
            active=lambda: active[0],
            sender=SimpleNamespace(account="31846829"),
            groups=["1459282"],
        ),
    )
    service = SimpleNamespace(
        store=OpenStore(tmp_path),
        active=True,
        chat_generations={},
        profiles=SimpleNamespace(resolve=lambda *args: {"fingerprint": "profile"}),
        support=SimpleNamespace(fingerprint=lambda account: "support"),
        verify_actor=AsyncMock(),
        reject_reply=AsyncMock(),
        chat=AsyncMock(),
    )
    service.governance = Governance(service)
    service.governance.configure(1, True, "offline/server")
    batches = private_batches.PrivateBatches(service)
    return service, adapter, batches, active


def event(adapter, mid, text="你好"):
    extras = {"wsl_test_scope": "private_ai", "wsl_chat_fresh": mid == "1"}
    return SimpleNamespace(
        platform=adapter,
        actor_identity=ActorIdentity("43506527", "31846829", "123", "31846829"),
        message_obj=SimpleNamespace(message_id=mid),
        message_str=text,
        processing_completion=asyncio.get_running_loop().create_future(),
        get_extra=lambda key: extras.get(key),
        set_extra=lambda key, value: extras.__setitem__(key, value),
        get_sender_id=lambda: "31846829",
        is_private_chat=lambda: True,
        get_group_id=lambda: "",
    )


@pytest.mark.asyncio
async def test_private_fragments_acknowledge_then_make_one_batch(tmp_path, monkeypatch):
    service, adapter, batches, _ = fixture(tmp_path)
    monkeypatch.setattr(private_batches, "WINDOW", 0.01)
    monkeypatch.setattr(
        private_batches, "consultation_access", lambda *a: {"kind": "consultation"}
    )
    seen = []

    async def chat(source):
        seen.append((source.message_str, source.get_extra("wsl_chat_fresh")))
        source.set_extra("_wsl_native_reply_result", "accepted")

    service.chat.side_effect = chat
    first = event(adapter, "1", "你好")
    await batches.enqueue(first)
    await batches.enqueue(event(adapter, "2", "请问怎么使用"))
    assert not seen and len(batches.pending) == 1
    first.processing_completion.set_result(None)
    await asyncio.gather(*tuple(batches.running))
    assert seen == [("你好\n请问怎么使用", True)]
    assert first.message_str == "你好"
    with service.store.connect() as db:
        row = dict(db.execute("SELECT * FROM private_ai_batches").fetchone())
    assert row["state"] == "completed"
    assert "你好" not in str(row)
    await batches.enqueue(event(adapter, "1"))
    assert not batches.pending


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason", ["command", "window", "generation", "config", "role"]
)
async def test_pending_batch_is_invalidated_without_model_call(
    tmp_path, monkeypatch, reason
):
    service, adapter, batches, active = fixture(tmp_path)
    monkeypatch.setattr(private_batches, "WINDOW", 0.01)
    monkeypatch.setattr(
        private_batches, "consultation_access", lambda *a: {"kind": "admin_guidance"}
    )
    first = event(adapter, "1")
    await batches.enqueue(first)
    if reason == "command":
        batches.cancel(first)
    elif reason == "window":
        active[0] = False
    elif reason == "generation":
        service.chat_generations[("43506527", first.actor_identity.principal)] = 1
    elif reason == "role":
        monkeypatch.setattr(
            private_batches, "consultation_access", lambda *a: {"kind": "consultation"}
        )
    else:
        adapter.config["_business_epoch"] = 2
    await asyncio.gather(*tuple(batches.running), return_exceptions=True)
    service.chat.assert_not_awaited()
    assert not batches.pending


@pytest.mark.asyncio
async def test_overflow_rejects_whole_batch_and_restart_never_replays(
    tmp_path, monkeypatch
):
    service, adapter, batches, _ = fixture(tmp_path)
    monkeypatch.setattr(
        private_batches, "consultation_access", lambda *a: {"kind": "consultation"}
    )
    for number in range(13):
        await batches.enqueue(event(adapter, str(number)))
    await batches.close()
    service.chat.assert_not_awaited()
    service.reject_reply.assert_awaited_once()
    restored = private_batches.PrivateBatches(service)
    assert not restored.pending


@pytest.mark.asyncio
async def test_character_limit_and_source_deduplication_survive_restart(
    tmp_path, monkeypatch
):
    service, adapter, batches, _ = fixture(tmp_path)
    monkeypatch.setattr(
        private_batches, "consultation_access", lambda *a: {"kind": "consultation"}
    )
    await batches.enqueue(event(adapter, "oversized", "x" * 4097))
    service.reject_reply.assert_awaited_once()
    service.chat.assert_not_awaited()
    restored = private_batches.PrivateBatches(service)
    await restored.enqueue(event(adapter, "oversized", "retry"))
    assert not restored.pending
    await batches.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason", ["chat_cooldown", "component_circuit_open", "request_duplicate"]
)
async def test_batch_preserves_fixed_rejections_without_ai_fallback(
    tmp_path, monkeypatch, reason
):
    service, adapter, batches, _ = fixture(tmp_path)
    monkeypatch.setattr(private_batches, "WINDOW", 0.01)
    monkeypatch.setattr(
        private_batches, "consultation_access", lambda *a: {"kind": "consultation"}
    )
    service.chat.side_effect = OpenError(reason)
    await batches.enqueue(event(adapter, "rejected"))
    await asyncio.gather(*tuple(batches.running))
    assert service.chat.await_count == 1
    assert service.reject_reply.await_count == (
        0 if reason == "request_duplicate" else 1
    )
    with service.store.connect() as db:
        assert (
            db.execute("SELECT state FROM private_ai_batches").fetchone()["state"]
            == "not_sent"
        )


def test_only_fixed_native_test_scope_can_enable_governance(tmp_path):
    service, adapter, _, active = fixture(tmp_path)
    assert service.governance.eligible(adapter, "1459282")
    assert not service.governance.eligible(adapter, "1517858")
    active[0] = False
    assert not service.governance.eligible(adapter, "1459282")
