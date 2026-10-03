"""Exercise transport outcomes without contacting Telegram."""

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import NetworkError, RetryAfter

spec = importlib.util.spec_from_file_location("domain_plugin", Path(__file__).parents[1] / "main.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.asyncio
@pytest.mark.parametrize("error,status", [(None, "sent"), (NetworkError("test"), "uncertain"), (RetryAfter(2), "rejected")])
async def test_receipt_before_next_claim(error, status):
    plugin = object.__new__(module.DomainAnalytics)
    plugin.receipt = None
    plugin.application = SimpleNamespace(running=True, bot=SimpleNamespace(_post=AsyncMock(return_value={"message_id": 1}, side_effect=error)))
    plugin.put_kv_data = AsyncMock()
    calls = []

    async def api(path, data):
        calls.append((path, data))
        if path == "/ack":
            assert plugin.put_kv_data.await_args_list[0].args[0] == "pending_receipt"
            raise asyncio.CancelledError
        return {"job": {"id": "1", "lease": "lease", "method": "sendMessage", "payload": {"params": {"chat_id": 1, "text": "test"}}}}

    plugin.api = api
    with pytest.raises(asyncio.CancelledError):
        await plugin.egress()
    assert calls[-1][0] == "/ack"
    assert calls[-1][1]["status"] == status
    assert plugin.application.bot._post.await_count == 1
