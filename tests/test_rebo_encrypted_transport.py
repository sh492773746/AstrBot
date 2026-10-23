"""Verify wire envelopes, authenticated responses and sanitized HTTP errors."""

import base64
import json
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pyhpke import AEADId, CipherSuite, KDFId, KEMId

from data.plugins.astrbot_plugin_rebo_live.client import Client, RemoteError


class Response:
    """Supply the small aiohttp response surface used by the client."""

    def __init__(self, payload, status=200, headers=None):
        self.payload = payload
        self.status = status
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return self.payload

    async def read(self):
        return self.payload


@pytest.fixture
def server():
    """Build a local HPKE peer, without any live network calls.

    Returns:
        Mock transport with the server's authenticated envelope handler.
    """
    suite = CipherSuite.new(
        KEMId.DHKEM_X25519_HKDF_SHA256, KDFId.HKDF_SHA256, AEADId.AES256_GCM
    )
    keys = suite.kem.derive_key_pair(b"isolated-test-key-material-32bytes")
    public = (
        base64.urlsafe_b64encode(keys.public_key.to_public_bytes())
        .rstrip(b"=")
        .decode()
    )
    obj = SimpleNamespace(
        suite=suite,
        keys=keys,
        envelope=None,
        status=200,
        response={"code": 0, "data": {"ok": True}},
        corrupt=False,
    )

    def handle(method, url, data=None, headers=None):
        parts = urlsplit(url)
        assert "SECRET" not in url
        assert "json" not in headers
        enc = base64.urlsafe_b64decode(headers["X-Enc-Enc"] + "==")
        receiver = suite.create_recipient_context(
            enc, keys.private_key, info=b"live-api-v1"
        )
        wire = (
            data
            if method == "POST"
            else base64.urlsafe_b64decode(parse_qs(parts.query)["e"][0] + "==")
        )
        assert b"SECRET" not in wire
        plaintext = receiver.open(
            wire, f"{method}\n{parts.path}\n{headers['X-Enc-Ts']}".encode()
        )
        obj.envelope = json.loads(plaintext)
        nonce = b"response-entropy"
        secret = receiver.export(b"live-api-v1 response", 32)
        key = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=enc + nonce, info=b"key"
        ).derive(secret)
        iv = HKDF(
            algorithm=hashes.SHA256(), length=12, salt=enc + nonce, info=b"nonce"
        ).derive(secret)
        encrypted = AESGCM(key).encrypt(iv, json.dumps(obj.response).encode(), None)
        return Response(
            b"corrupt" if obj.corrupt else encrypted,
            obj.status,
            {"X-Enc": "v1", "X-Enc-Nonce": base64.urlsafe_b64encode(nonce).decode()},
        )

    obj.transport = SimpleNamespace(
        get=Mock(return_value=Response({"code": 0, "data": {"public_key": public}})),
        request=Mock(side_effect=handle),
    )
    return obj


@pytest.mark.asyncio
async def test_get_and_post_use_official_authenticated_envelopes(server):
    client = Client(server.transport, "test-device", {"access_token": "token"})
    assert await client.request("room/info?room_id=SECRET") == {"ok": True}
    assert server.envelope == {"q": "room_id=SECRET", "b": None}
    assert (
        server.transport.request.call_args.kwargs["headers"]["Authorization"]
        == "Bearer token"
    )
    assert await client.request(
        "user/auth/login/sms", {"code": "SECRET"}, authenticated=False
    ) == {"ok": True}
    assert server.envelope == {"q": "", "b": {"code": "SECRET"}}
    assert "Authorization" not in server.transport.request.call_args.kwargs["headers"]
    server.transport.get.assert_called_once()


@pytest.mark.asyncio
async def test_encrypted_http_error_reports_numeric_code_without_raw_payload(server):
    server.status = 400
    server.response = {"code": 2001032, "message": "SECRET raw payload"}
    client = Client(server.transport, "device", {})
    with pytest.raises(RemoteError) as caught:
        await client.request(
            "user/auth/login/sms", {"code": "SECRET"}, authenticated=False
        )
    assert caught.value.code == 400
    assert caught.value.business_code == 2001032
    assert caught.value.endpoint == "user/auth/login/sms"
    assert "SECRET" not in str(caught.value)
    assert client.hpke_key is None
    server.transport.request.assert_called_once()


@pytest.mark.asyncio
async def test_corrupt_response_fails_closed_without_retry(server):
    server.corrupt = True
    client = Client(server.transport, "device", {})
    with pytest.raises(RemoteError, match="invalid_encrypted_response"):
        await client.request(
            "user/auth/login/sms", {"code": "SECRET"}, authenticated=False
        )
    server.transport.request.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["bd84.disabled", "unknown.key", "ae71.short"])
async def test_unsupported_key_never_sends_credentials(server, key):
    server.transport.get.return_value = Response(
        {"code": 0, "data": {"public_key": key}}
    )
    client = Client(server.transport, "device", {})
    with pytest.raises(RemoteError):
        await client.request(
            "user/auth/login/sms", {"code": "SECRET"}, authenticated=False
        )
    server.transport.request.assert_not_called()
