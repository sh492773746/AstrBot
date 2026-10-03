"""Offline receipt diagnostics without credentials or real chat writes."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

from data.plugins.astrbot_plugin_rebo_live.client import Client, RemoteError


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,reason",
    [
        (TimeoutError(), "receipt_timeout"),
        (ConnectionError(), "connection_error"),
        (asyncio.CancelledError(), "task_cancelled"),
        (ValueError("SECRET"), "invalid_receipt"),
    ],
)
async def test_errors_are_sanitized(failure, reason):
    ws = SimpleNamespace(
        send_json=AsyncMock(), receive=AsyncMock(side_effect=failure), close_code=1006
    )
    diagnostic = {}
    with pytest.raises(type(failure)):
        await Client(None, "", {}).send(ws, "SECRET", "request", diagnostic)
    assert diagnostic["reason"] == reason
    assert diagnostic["stage"] == "receipt"
    assert diagnostic["close_code"] == 1006
    assert "SECRET" not in json.dumps(diagnostic)
    assert ws.send_json.await_count == 1


@pytest.mark.asyncio
async def test_matching_and_missing_ids():
    frames = [
        {"Type": "MESSAGE", "Content": "SECRET"},
        {"Type": "MESSAGE", "RequestId": "other"},
        {"Type": "MESSAGE", "RequestId": "request", "Id": "message"},
    ]
    ws = SimpleNamespace(
        send_json=AsyncMock(),
        receive=AsyncMock(
            side_effect=[
                SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(f))
                for f in frames
            ]
        ),
        close_code=None,
    )
    diagnostic = {}
    assert (
        await Client(None, "", {}).send(ws, "SECRET", "request", diagnostic)
        == "message"
    )
    assert diagnostic["frames"] == 3
    assert diagnostic["unmatched"] == 2
    assert diagnostic["missing_request"] == 1
    assert "SECRET" not in json.dumps(diagnostic)
    assert diagnostic["samples"][0]["type"] == "MESSAGE"
    assert diagnostic["samples"][-1]["request_match"]


@pytest.mark.asyncio
async def test_content_match_alone_cannot_confirm_delivery():
    frame = SimpleNamespace(
        type=aiohttp.WSMsgType.TEXT,
        data=json.dumps(
            {
                "Type": "MESSAGE",
                "RequestId": "different",
                "Id": "some-id",
                "Sender": {"UserId": "private-user"},
                "Content": json.dumps({"client_event_id": "r", "content": "SECRET"}),
            }
        ),
    )
    ws = SimpleNamespace(
        send_json=AsyncMock(),
        receive=AsyncMock(side_effect=[frame, TimeoutError()]),
        close_code=None,
    )
    diagnostic = {}
    with pytest.raises(TimeoutError):
        await Client(None, "", {}).send(ws, "SECRET", "r", diagnostic)
    assert diagnostic["samples"][0]["content_match"]
    assert diagnostic["samples"][0]["event_match"]
    assert not diagnostic["samples"][0]["request_match"]
    assert "private-user" not in json.dumps(diagnostic)
    assert "SECRET" not in json.dumps(diagnostic)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload", ["[]", "{", '{"Type":"MESSAGE","RequestId":"request"}']
)
async def test_invalid_receipts(payload):
    ws = SimpleNamespace(
        send_json=AsyncMock(),
        receive=AsyncMock(
            return_value=SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=payload)
        ),
        close_code=None,
    )
    diagnostic = {}
    with pytest.raises(ValueError):
        await Client(None, "", {}).send(ws, "text", "request", diagnostic)
    assert diagnostic["reason"] == "invalid_receipt"


@pytest.mark.asyncio
async def test_explicit_rejection_stays_rejection():
    ws = SimpleNamespace(
        send_json=AsyncMock(),
        receive=AsyncMock(
            return_value=SimpleNamespace(
                type=aiohttp.WSMsgType.TEXT,
                data=json.dumps({"Type": "ERROR", "RequestId": "r", "ErrorCode": 403}),
            )
        ),
        close_code=None,
    )
    with pytest.raises(RemoteError):
        await Client(None, "", {}).send(ws, "text", "r", {})
