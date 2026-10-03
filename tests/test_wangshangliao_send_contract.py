import asyncio
from unittest.mock import AsyncMock

import pytest

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
from astrbot.core.platform.sources.wangshangliao.storage import Ledger
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError


@pytest.mark.asyncio
async def test_logical_segments_retries_and_conflicts(tmp_path):
    adapter = WangshangliaoAdapter({"id": "test", "account_id": "1"}, {}, asyncio.Queue())
    adapter.ledger = Ledger(tmp_path / "ledger.sqlite")
    await adapter.ledger.open()
    adapter.send_text = AsyncMock(return_value="accepted")
    try:
        text = "中文" * 2000
        assert await adapter.send_reply_text("5", "op", text) == "accepted"
        calls = adapter.send_text.call_args_list
        assert len(calls) > 1
        assert "".join(call.args[2] for call in calls) == text
        assert all(len(call.args[2].encode()) <= 4096 for call in calls)
        before = len(calls)
        await adapter.send_reply_text("5", "op", text)
        assert adapter.send_text.await_count == before
        with pytest.raises(ProtocolError, match="send_key_conflict"):
            await adapter.send_reply_text("6", "op", text)
        adapter.send_text.reset_mock()
        adapter.send_text.return_value = "unknown"
        assert await adapter.send_reply_text("5", "uncertain", text) == "unknown"
        assert adapter.send_text.await_count == 1
        await adapter.send_reply_text("5", "uncertain", text)
        assert adapter.send_text.await_count == 1
    finally:
        await adapter.ledger.close()


@pytest.mark.asyncio
async def test_independent_proactive_ids_and_missing_grant(tmp_path):
    adapter = WangshangliaoAdapter({"id": "test", "account_id": "1", "enabled_groups": ["5"], "proactive_send": {"enabled": True}}, {}, asyncio.Queue())
    adapter.groups = {"5": "55"}
    adapter.connection_state = "online"
    adapter.ledger = Ledger(tmp_path / "ledger.sqlite")
    await adapter.ledger.open()
    try:
        with pytest.raises(ProtocolError, match="proactive_not_authorized"):
            await adapter.send_text("5", "denied", "hello", proactive=True)
        adapter.send_reply_text = AsyncMock(return_value="accepted")
        session = MessageSession("test", MessageType.GROUP_MESSAGE, "1/5")
        message = MessageChain([Plain("same reminder")])
        first = await adapter.send_by_session(session, message)
        second = await adapter.send_by_session(session, message)
        assert first["operation_id"] != second["operation_id"]
    finally:
        await adapter.ledger.close()
