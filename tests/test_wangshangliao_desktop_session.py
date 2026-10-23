"""Desktop login uses its own signed contract, never relabeled Android tokens."""

import asyncio
import base64
import json
import struct
from collections import deque

import aiohttp
import nacl.bindings
import pytest
import pytest_asyncio
import xxhash
import zstandard
from aiohttp import web
from nacl.public import PrivateKey, SealedBox
from nacl.signing import SigningKey, VerifyKey

from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
from astrbot.core.platform.sources.wangshangliao.business import (
    BusinessClient,
    BusinessError,
    Deployment,
)
from astrbot.core.platform.sources.wangshangliao.registration import RegistrationManager


@pytest.fixture
def desktop_deployment():
    return Deployment(
        origin="https://example.invalid",
        headers={"x-device": "3", "x-version": "android-must-not-leak"},
        metadata=[1] * 14,
        signing_seed=base64.b64encode(bytes(32)).decode(),
        body_key=base64.b64encode(bytes(32)).decode(),
        server_key=base64.b64encode(
            bytes(PrivateKey(bytes([3]) * 32).public_key)
        ).decode(),
        captcha_id="fixture",
        app_key="fixture",
        device_id="persistent-desktop-device",
        protocol_version="2.8.3",
    )


@pytest_asyncio.fixture
async def desktop_http(desktop_deployment):
    """Validate independently decoded desktop requests against a loopback provider."""
    seen, replies = [], deque()
    server = PrivateKey(bytes([3]) * 32)

    async def provider(request):
        metadata = wire.unb64(request.headers["x-request"])
        public = wire.unb64(request.headers["x-seed"])
        VerifyKey(public).verify(metadata, wire.unb64(request.headers["x-hash"]))
        fields, offset = {}, 0
        while offset < len(metadata):
            tag, offset = wire.read_varint(metadata, offset)
            value, offset = wire.read_varint(metadata, offset, 64)
            fields[tag >> 3] = value
        assert fields[2] == 1
        assert fields[3] == 1
        assert fields[1] == xxhash.xxh3_64_intdigest(public)
        assert request.headers["x-device"] == "1"
        assert request.headers["x-client"] == "11"
        assert request.headers["x-version"] == "1.1770619133615.0"
        if fields.get(4):
            expected = nacl.bindings.crypto_shorthash_siphash24(
                struct.pack("<QQQ", fields[1], fields[6], fields[10]),
                bytes([7]) * 16,
            )
            assert fields[9] == int.from_bytes(expected, "little")
        aad = xxhash.xxh3_128_intdigest(
            struct.pack("<QQQQ", fields[6], fields[8], fields[1], 1770619133615)
        ).to_bytes(16, "little")
        raw = await request.read()
        decoded = wire.open_business(bytes(32), aad, raw)
        assert len(decoded) == fields[7]
        assert xxhash.xxh3_64_intdigest(decoded) == fields[8]
        params = json.loads(decoded)
        seen.append((request.path, params))
        response, headers = replies.popleft()
        if request.path == "/v1/user/login" and response["code"] == 0:
            context = wire.Exchange.FromString(
                SealedBox(server).decrypt(wire.unb64(request.headers["x-context"]))
            )
            assert context.kind == 1 and context.version == 1
            assert context.device == fields[1]
            assert context.public_key == public
            token = wire.LoginToken(
                uid=99,
                kind=1,
                mac=bytes([7]) * 16,
                seconds=context.seconds,
                correlation=context.correlation,
            )
            response["data"]["token"] = wire.b64(
                SealedBox(
                    SigningKey(bytes(32)).verify_key.to_curve25519_public_key()
                ).encrypt(token.SerializeToString())
            )
        # Response AAD binds the request digest, not the response digest.
        encoded = nacl.bindings.crypto_aead_chacha20poly1305_encrypt(
            zstandard.ZstdCompressor().compress(json.dumps(response).encode()),
            aad,
            bytes(8),
            bytes(32),
        )
        return web.Response(
            body=encoded[-16:] + bytes(16) + encoded[:-16], headers=headers
        )

    app = web.Application()
    app.router.add_post("/{path:.*}", provider)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    deployment = desktop_deployment.model_copy(
        update={
            "origin": f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        }
    )
    try:
        async with aiohttp.ClientSession() as http:
            yield BusinessClient(deployment, http), seen, replies
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_desktop_login_secondary_verification_and_current_im_token(desktop_http):
    client, seen, replies = desktop_http
    replies.extend(
        [
            (
                {
                    "code": 0,
                    "data": {
                        "uid": 99,
                        "jwtToken": "desktop-pending",
                        "safeCodeRequired": True,
                        "safeCodeSet": True,
                    },
                },
                {},
            ),
            ({"code": 0, "data": {}}, {"x-set-jwt": "desktop-verified"}),
            ({"code": 0, "data": {"nimToken": "desktop-im-token"}}, {}),
        ]
    )
    await client.login({"type": "LOGIN_TYPE_SMS"})
    assert client.fields[1:3] == [1, 1]
    assert client.mac == bytes([7]) * 16
    with pytest.raises(BusinessError, match="secondary_security_unverified"):
        await client.refresh_nim_token()
    await client.verify_security_code("123456")
    assert client.mac == bytes([7]) * 16
    assert await client.refresh_nim_token() == "desktop-im-token"
    assert seen == [
        ("/v1/user/login", {"type": "LOGIN_TYPE_SMS"}),
        ("/v1/user/verify_safe_code", {"code": "123456"}),
        (
            "/v1/user/get-nim-token",
            {
                "nimDeviceId": "persistent-desktop-device",
                "uid": 99,
                "loginToken": "desktop-verified",
            },
        ),
    ]
    assert "123456" not in json.dumps(client.export())


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["password", "sms"])
@pytest.mark.parametrize("security_required", [False, True])
async def test_desktop_registration_finishes_before_issuing_save_capability(
    desktop_http, method, security_required
):
    """Exercise both Dashboard login methods through the signed HTTP transport."""
    client, seen, replies = desktop_http
    manager = RegistrationManager(
        lambda: client.deployment, lambda deployment, http: client
    )
    try:
        view = await manager.action(
            "admin", {"action": "start", "instance_id": "desktop-fixture"}
        )
        base = {
            "instance_id": "desktop-fixture",
            "registration_code": view["registration_code"],
            "validate_str": "fixture-proof",
        }
        if method == "sms":
            replies.append(({"code": 0, "data": {"key": "fixture-key"}}, {}))
            view = await manager.action(
                "admin", {**base, "action": "request_sms", "account": "13800000000"}
            )
            assert view["status"] == "sms_required"
            payload = {
                **base,
                "action": "verify_sms",
                "verification_code": "654321",
            }
        else:
            payload = {
                **base,
                "action": "login",
                "account": "fixture-account",
                "password": "fixture-password",
            }
        replies.append(
            (
                {
                    "code": 0,
                    "data": {
                        "uid": 99,
                        "nimId": "nim-99",
                        "jwtToken": "desktop-pending",
                        "safeCodeRequired": security_required,
                        "safeCodeSet": True,
                    },
                },
                {},
            )
        )
        token_reply = ({"code": 0, "data": {"nimToken": "desktop-im-token"}}, {})
        if not security_required:
            replies.append(token_reply)
        view = await manager.action("admin", payload)
        if security_required:
            assert view["status"] == "security_required"
            assert not view["session_ref"]
            replies.extend(
                [
                    ({"code": 0, "data": {}}, {"x-set-jwt": "desktop-verified"}),
                    token_reply,
                ]
            )
            view = await manager.action(
                "admin",
                {**base, "action": "verify_security_code", "security_code": "123456"},
            )
        assert view["status"] == "authenticated" and not view["error"]
        tx = manager.claim("admin", "desktop-fixture", view["session_ref"])
        assert tx.saved["business"]["protocol_version"] == "2.8.3"
        assert tx.saved["nim_token"] == "desktop-im-token"
        assert not tx.saved["business"]["security_required"]
        assert not tx.pending_login
        login = next(params for path, params in seen if path == "/v1/user/login")
        assert not {"version", "appVersion", "appHash"}.intersection(login)
        assert seen[-1] == (
            "/v1/user/get-nim-token",
            {
                "nimDeviceId": "persistent-desktop-device",
                "uid": 99,
                "loginToken": (
                    "desktop-verified" if security_required else "desktop-pending"
                ),
            },
        )
        assert "123456" not in json.dumps(tx.saved)
        assert not any(
            secret in json.dumps(view)
            for secret in (
                "fixture-password",
                "desktop-pending",
                "desktop-verified",
                "desktop-im-token",
            )
        )
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_desktop_auth_expiry_is_visible_without_dropping_im_or_retrying(
    desktop_http,
):
    client, seen, replies = desktop_http
    client.jwt = "desktop-credential"
    adapter = WangshangliaoAdapter(
        {"id": "desktop-expiry", "account_id": "99", "enabled_groups": []},
        {},
        asyncio.Queue(),
    )
    adapter.business = client
    adapter.connection_state = "online"
    replies.append(({"code": 401, "data": None}, {}))
    with pytest.raises(BusinessError, match="reauth_required"):
        await client.request("/v1/group/get-group-list", {"v": "0"})
    with pytest.raises(BusinessError, match="reauth_required"):
        await client.refresh_nim_token()
    assert len(seen) == 1
    assert adapter.get_stats()["connection_state"] == "online"
    assert adapter.get_stats()["business_auth"] == {
        "state": "expired",
        "protocol_version": "2.8.3",
        "device_type": 1,
    }
    assert client.mac == b""


@pytest.mark.parametrize("saved_protocol", ["3.7.1", "3.7.3", "2.8.3"])
def test_deployment_changes_never_relabel_existing_sessions(
    desktop_deployment, saved_protocol
):
    source = BusinessClient(
        desktop_deployment.model_copy(update={"protocol_version": saved_protocol}), None
    )
    source.jwt, source.mac = "saved-credential", bytes([5]) * 16
    source.fields[3] = 99
    saved = source.export()
    restored = BusinessClient(desktop_deployment, None)
    restored.restore(saved)
    assert restored.export() == saved
    assert restored.protocol_version == saved_protocol
    assert restored.fields[1] == (3 if saved_protocol == "3.7.3" else 1)
    assert desktop_deployment.device_id == "persistent-desktop-device"


@pytest.mark.asyncio
async def test_desktop_preflight_and_transient_failure_do_not_claim_device_conflict(
    desktop_http,
):
    client, seen, replies = desktop_http
    client.jwt = "desktop-credential"
    replies.append(({"code": 1400, "data": None}, {}))
    with pytest.raises(BusinessError, match="secondary_security_code_required"):
        await client.request("/v1/group/get-group-list", {"v": "0"})
    assert client.auth_error == ""
    assert client.jwt == "desktop-credential"
    assert len(seen) == 1
