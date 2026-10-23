"""Assigned bot-admin controls are narrow, authenticated and independently gated."""

import asyncio
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.core.pipeline.rate_limit_check.stage import RateLimitStage
from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
from astrbot.core.platform.sources.wangshangliao.event import managed_group_control
from astrbot.core.platform.sources.wangshangliao.runtime_policy import runtime
from astrbot.core.platform.sources.wangshangliao.storage import Ledger
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from tests.test_wangshangliao_plain_commands import native_event

pytestmark = pytest.mark.usefixtures("wsl_active_policy", "wsl_command_classifier")


def config():
    return {
        "id": "offline-managed-admin",
        "account_id": "1",
        "enable": True,
        "enabled_groups": ["5"],
        "reply_groups": {"5": True},
        "_open_integrated": True,
        "_open_paused": False,
        "_business_active": True,
        "_open_groups": {"5": {"flood": True, "advertising": False, "keywords": []}},
        "_open_managers": {"5": ["2"]},
        "_open_revisions": {"5": "revision-1"},
    }


def payload(text="黑名单 @Target"):
    return {
        "sender": "2",
        "name": "Manager",
        "text": text,
        "created_at": int(time.time() * 1000),
        "mentions": ["903"],
        "mention_spans": [{"uid": "903", "nick": "Target", "start": 4, "end": 11}],
        "recall_route": {"peer": "902", "client": "command-client"},
        "_open_revision": "revision-1",
    }


@pytest.fixture
def managed_accounts(monkeypatch):
    from astrbot.core import astrbot_config

    monkeypatch.setitem(
        astrbot_config,
        "platform",
        [{"type": "wangshangliao", "account_id": uid} for uid in ("1", "2")],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["黑名单 @Target", "@Target 黑名单", "黑名单"])
async def test_assigned_managed_admin_dispatches_and_replies_without_test_window(
    tmp_path, managed_accounts, text
):
    adapter = WangshangliaoAdapter(config(), {}, asyncio.Queue())
    adapter.ledger = Ledger(tmp_path / "messages.sqlite3")
    await adapter.ledger.open()
    adapter.connection_state = "online"
    adapter.nim_account = "901"
    adapter.members = {"5": {"2": "902", "3": "903"}}
    adapter.send_reply_text = AsyncMock(return_value="accepted")
    incoming = payload(text)
    if text == "黑名单":
        incoming["mentions"] = []
        incoming["mention_spans"] = []
        incoming["reply"] = {"server": "original", "client": "original-client"}
    await adapter.ledger.ingest("1", "5", "command", incoming)
    worker = asyncio.create_task(adapter.process_group("5", single=True))
    try:
        event = await asyncio.wait_for(adapter._event_queue.get(), 2)
        assert not adapter.test_window
        assert event.actor_identity.verified_uid == "2"
        assert event.get_extra("_wsl_managed_group_control")
        event.set_extra("wsl_plain_command", True)
        event.set_extra("wsl_command_result", True)
        await event.send(MessageChain([Plain("已处理")]))
        await event.send(MessageChain([Plain("不得发送第二份回复")]))
        adapter.send_reply_text.assert_awaited_once()
        await adapter.send_reply_text.call_args.kwargs["authorize"]()
        event.processing_completion.set_result(None)
        await asyncio.wait_for(worker, 2)
        async with adapter.ledger.db.execute(
            "SELECT state FROM inbox WHERE message='command'"
        ) as cursor:
            assert (await cursor.fetchone())[0] == "processed"
    finally:
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await adapter.ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "chat",
        "private",
        "unknown_group",
        "unassigned",
        "peer",
        "revision",
        "paused",
        "disabled",
        "inactive",
        "media",
        "unrelated_command",
        "self",
        "missing_text",
    ],
)
async def test_admission_does_not_authorize_general_bot_chat(fault):
    event = native_event("黑名单")
    adapter = event.platform
    adapter.config = config()
    incoming, group = payload(), "5"
    if fault == "chat":
        incoming["text"] = "你好"
    elif fault == "private":
        group = "private/2/OTAy"
    elif fault == "unknown_group":
        group = "6"
    elif fault == "unassigned":
        adapter.config["_open_managers"]["5"] = []
    elif fault == "peer":
        incoming["recall_route"]["peer"] = "904"
    elif fault == "revision":
        incoming["_open_revision"] = "old"
    elif fault == "paused":
        adapter.config["_open_paused"] = True
    elif fault == "disabled":
        adapter.config["enable"] = False
    elif fault == "inactive":
        adapter.config["_business_active"] = False
    elif fault == "media":
        incoming["kind"] = "image"
    elif fault == "unrelated_command":
        incoming["text"] = "禁言 @Target"
    elif fault == "self":
        incoming["sender"] = "1"
    else:
        incoming["text"] = None
    assert managed_group_control(adapter, group, incoming) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["manager", "revision", "peer", "cancelled"])
async def test_managed_control_reply_rechecks_scope_at_submission(
    managed_accounts, fault
):
    event = native_event("黑名单")
    adapter = event.platform
    adapter.config = config()
    incoming = payload("黑名单")
    event.set_extra("wangshangliao_payload", incoming)
    event.set_extra("_wsl_managed_group_control", "黑名单")
    event.set_extra("wsl_plain_command", True)
    await event.send(MessageChain([Plain("结果")]))
    guard = adapter.send_reply_text.call_args.kwargs["authorize"]
    if fault == "manager":
        adapter.config["_open_managers"]["5"] = []
    elif fault == "revision":
        adapter.config["_open_revisions"]["5"] = "new"
    elif fault == "peer":
        incoming["recall_route"] = {"peer": "spoof"}
    else:
        event.processing_completion.cancel()
    with pytest.raises(ProtocolError, match="management_reply_scope_changed"):
        await guard()


@pytest.mark.asyncio
async def test_public_native_dispatch_does_not_stall_behind_group_chat_limit(
    monkeypatch,
):
    event = native_event("黑名单")
    event.platform.config.update(
        _open_integrated=True, _business_active=True, _business_epoch="epoch"
    )
    event.policy_epoch = "epoch"
    monkeypatch.setattr(
        runtime, "open_service", SimpleNamespace(integrated=True, active=True)
    )
    limiter = RateLimitStage()
    limiter.rate_limit_count = 1
    limiter.rate_limit_time = timedelta(seconds=60)
    limiter.rl_strategy = "stall"
    origin = event.unified_msg_origin
    limiter.event_timestamps[origin].append(datetime.now())
    await limiter.locks[origin].acquire()
    try:
        await asyncio.wait_for(
            asyncio.gather(*(limiter.process(event) for _ in range(100))), timeout=0.5
        )
        assert len(limiter.event_timestamps[origin]) == 1
        assert not event.is_stopped()
    finally:
        limiter.locks[origin].release()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["legacy", "inactive", "not_integrated", "stale"])
async def test_shared_limiter_remains_for_other_native_modes(monkeypatch, fault):
    event = native_event("ordinary")
    event.platform.config.update(_open_integrated=True, _business_active=True)
    service = SimpleNamespace(integrated=True, active=True)
    monkeypatch.setattr(runtime, "open_service", service)
    if fault == "legacy":
        monkeypatch.setattr(runtime, "open_service", None)
    elif fault == "inactive":
        service.active = False
    elif fault == "not_integrated":
        event.platform.config["_open_integrated"] = False
    else:
        event.platform.config["_business_epoch"] = "changed"
    limiter = RateLimitStage()
    limiter.rate_limit_count = 1
    limiter.rate_limit_time = timedelta(seconds=60)
    limiter.rl_strategy = "discard"
    limiter.event_timestamps[event.unified_msg_origin].append(datetime.now())
    await limiter.process(event)
    assert event.is_stopped()
