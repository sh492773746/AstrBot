"""Preserve the provider's login mode without falling back to a static token."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.business import (
    BusinessClient,
    BusinessError,
)
from astrbot.core.platform.sources.wangshangliao.nim import LoginToken, NimClient


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,expected",
    [
        ("NIM_LOGIN_KIND_STATIC", 0),
        ("NIM_LOGIN_KIND_DYNAMIC", 1),
        ("NIM_LOGIN_KIND_CALLBACK", 2),
        ("absent", 0),
    ],
)
async def test_fresh_token_preserves_mode_through_sdk_login(kind, expected):
    client = BusinessClient.__new__(BusinessClient)
    client.security_required = False
    client.protocol_version = "2.8.3"
    client.fields = [0, 1, 1, 99]
    client.jwt = "business-session-fixture"
    client.deployment = SimpleNamespace(device_id="fixture-device", app_key="fixture")
    result = {"nimToken": "fresh-token", "staticNimToken": "never-fallback"}
    if kind != "absent":
        result["kind"] = kind
    client.request = AsyncMock(return_value=result)
    token = await client.refresh_nim_token()
    assert token == "fresh-token"
    assert isinstance(token, str) and token.auth_type == expected
    assert json.loads(json.dumps({"token": token})) == {"token": "fresh-token"}

    async def chunks(_size):
        yield b'{"common":{"link":["https://example.invalid"]}}'

    response = MagicMock()
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)
    response.content.iter_chunked = chunks
    transport = NimClient(SimpleNamespace(get=lambda *a, **k: response))
    transport.open_socket = AsyncMock()
    await transport.connect(client.deployment, "nim-fixture", token)
    fields = dict(transport.open_socket.await_args.args[1])
    assert fields[1000] == b"fresh-token"
    assert fields.get(115) == (str(expected).encode() if expected else None)
    assert b"never-fallback" not in fields.values()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [None, 0, True, [], {}, "", "NIM_LOGIN_KIND_UNKNOWN"])
async def test_unknown_mode_is_rejected_without_static_fallback(kind):
    client = BusinessClient.__new__(BusinessClient)
    client.security_required = False
    client.protocol_version = "3.7.1"
    client.request = AsyncMock(
        return_value={
            "kind": kind,
            "nimToken": "fresh-token",
            "staticNimToken": "never-fallback",
        }
    )
    with pytest.raises(BusinessError, match="nim_auth_type_invalid"):
        await client.refresh_nim_token()


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_type", [None, True, "2", -1, 3, {}, []])
async def test_invalid_transport_mode_never_reaches_discovery(auth_type):
    with pytest.raises(wire.ProtocolError, match="nim_auth_type_invalid"):
        LoginToken("fixture-token", auth_type)
    token = LoginToken("fixture-token", 2)
    token.auth_type = auth_type
    http = SimpleNamespace(get=MagicMock())
    with pytest.raises(wire.ProtocolError, match="nim_auth_type_invalid"):
        await NimClient(http).connect(None, "nim-fixture", token)
    http.get.assert_not_called()
