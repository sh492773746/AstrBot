"""Synthetic 3.7.3 authentication, rotation and compatibility regressions."""

import asyncio
import base64
import json
import struct
import time
from collections import deque
from unittest.mock import AsyncMock

import aiohttp
import nacl.bindings
import pytest
import pytest_asyncio
import xxhash
import zstandard
from aiohttp import web
from nacl.public import PrivateKey, SealedBox
from nacl.signing import VerifyKey

from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.business import (
    SECURITY_ERRORS,
    BusinessClient,
    BusinessError,
    Deployment,
)
from astrbot.core.platform.sources.wangshangliao.registration import (
    RegistrationManager,
)


@pytest.fixture
def current_deployment():
    """Return disposable protocol material, never production credentials."""
    return Deployment(
        origin="https://example.invalid",
        headers={},
        metadata=[1] * 14,
        signing_seed=base64.b64encode(bytes(32)).decode(),
        body_key=base64.b64encode(bytes(32)).decode(),
        server_key=base64.b64encode(
            bytes(PrivateKey(bytes([3]) * 32).public_key)
        ).decode(),
        captcha_id="fixture",
        app_key="fixture",
        device_id="fixture-device",
        protocol_version="3.7.3",
    )


@pytest_asyncio.fixture
async def current_http(current_deployment):
    """Validate signed traffic against an independently constructed AAD."""
    seen, replies = [], deque()

    async def provider(request):
        metadata = wire.unb64(request.headers["x-request"])
        public = wire.unb64(request.headers["x-seed"])
        VerifyKey(public).verify(metadata, wire.unb64(request.headers["x-hash"]))
        fields, offset = {}, 0
        while offset < len(metadata):
            tag, offset = wire.read_varint(metadata, offset)
            value, offset = wire.read_varint(metadata, offset, 64)
            fields[tag >> 3] = value
        assert request.headers["x-version"] == "1.1703240058615.0"
        assert request.headers["x-device"] == "3"
        assert request.headers["x-client"] == "2"
        assert fields.get(2) == 3 and fields.get(3) == 1
        assert fields[1] == xxhash.xxh3_64_intdigest(public)
        assert abs(fields[6] - int(time.time() * 1000)) < 20000
        assert fields.get(13) == 1
        assert all(fields.get(index, 0) == 0 for index in (11, 12, 14))
        aad = xxhash.xxh3_128_intdigest(
            struct.pack("<QQQQ", fields[6], fields[8], fields[1], 1703240058615)
        ).to_bytes(16, "little")
        raw = await request.read()
        plain = nacl.bindings.crypto_aead_chacha20poly1305_decrypt(
            raw[32:] + raw[:16], aad, raw[16:24], bytes(32)
        )
        decoded = zstandard.ZstdDecompressor().decompress(plain)
        assert len(decoded) == fields[7]
        assert xxhash.xxh3_64_intdigest(decoded) == fields[8]
        expected_mac = nacl.bindings.crypto_shorthash_siphash24(
            struct.pack("<QQQ", fields[1], fields[6], fields[10]), public[16:32]
        )
        assert fields.get(9, 0) == (
            int.from_bytes(expected_mac, "little") if fields.get(4, 0) > 0 else 0
        )
        seen.append(
            {
                "route": request.path,
                "params": json.loads(decoded),
                "headers": dict(request.headers),
                "fields": fields,
            }
        )
        if request.path == "/v1/user/login":
            context = wire.Exchange.FromString(
                SealedBox(PrivateKey(bytes([3]) * 32)).decrypt(
                    wire.unb64(request.headers["x-context"])
                )
            )
            assert context.kind == 3
            assert context.device == fields[1]
            assert context.public_key == public
        response, headers = replies.popleft()
        compressed = zstandard.ZstdCompressor().compress(json.dumps(response).encode())
        sealed = nacl.bindings.crypto_aead_chacha20poly1305_encrypt(
            compressed, aad, bytes(8), bytes(32)
        )
        return web.Response(
            body=sealed[-16:] + bytes(16) + sealed[:-16], headers=headers
        )

    app = web.Application()
    app.router.add_post("/{path:.*}", provider)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    deployment = current_deployment.model_copy(
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
async def test_current_login_security_rotation_and_im_token(current_http):
    client, seen, replies = current_http
    replies.extend(
        [
            (
                {
                    "code": 0,
                    "data": {
                        "uid": 99,
                        "nimId": "nim-99",
                        "jwtToken": "pending-jwt",
                        "safeCodeSet": True,
                        "safeCodeRequired": True,
                    },
                },
                {},
            ),
            (
                {"code": 0, "data": {"jwtToken": "verified-jwt"}},
                {"x-set-jwt": "verified-jwt"},
            ),
            ({"code": 0, "data": {"nimToken": "verified-nim-token"}}, {}),
        ]
    )
    result = await client.login({"type": "LOGIN_TYPE_SMS"})
    assert result["nimId"] == "nim-99" and client.security_required
    with pytest.raises(BusinessError, match="unverified"):
        await client.refresh_nim_token()
    await client.verify_security_code("123456")
    assert not client.security_required
    assert await client.refresh_nim_token() == "verified-nim-token"
    assert seen[-1]["params"] == {
        "nimDeviceId": "fixture-device",
        "uid": 99,
        "loginToken": "verified-jwt",
    }
    assert seen[-1]["headers"]["x-jwt"] == "verified-jwt"
    assert "123456" not in json.dumps(client.export())
    with pytest.raises(BusinessError, match="security_code_input"):
        await client.verify_security_code("123456")
    assert len(seen) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["password", "sms"])
async def test_registration_sends_verified_release_fields_for_both_login_methods(
    current_http, method
):
    """Exercise registration through encrypted HTTP, not a mocked login method."""
    client, seen, replies = current_http
    manager = RegistrationManager(
        lambda: client.deployment, lambda deployment, http: client
    )
    try:
        view = await manager.action(
            "admin", {"action": "start", "instance_id": "fixture"}
        )
        base = {
            "instance_id": "fixture",
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
                "verification_code": "123456",
            }
        else:
            payload = {
                **base,
                "action": "login",
                "account": "fixture",
                "password": "fixture-password",
            }
        replies.append(
            (
                {
                    "code": 0,
                    "data": {
                        "uid": 99,
                        "nimId": "nim-99",
                        "jwtToken": "fixture-jwt",
                        "safeCodeRequired": True,
                    },
                },
                {},
            )
        )
        view = await manager.action("admin", payload)
        assert view["status"] == "security_required"
        assert not view["session_ref"]
        request = seen[-1]
        assert request["route"] == "/v1/user/login"
        params = request["params"]
        assert params["version"] == "3.7.3" and params["appVersion"] == 335
        certificate, apk = params["appHash"].split("=")
        assert certificate == (
            "3C:F2:35:2F:6D:B2:85:67:98:25:B1:2F:06:5E:FC:5C:"
            "7A:AC:0F:6D:74:67:DA:4E:F9:50:05:58:C4:42:F1:96"
        )
        assert apk == (
            "02ED673B263F7EA80B307ADE1967C49A7264F955CDF33EB36F7673591029E195"
        )
        assert params["type"] == (
            "LOGIN_TYPE_SMS" if method == "sms" else "LOGIN_TYPE_ACCOUNT_PWD"
        )
        assert "appHash" not in json.dumps(view)
        assert "fixture-password" not in json.dumps(view)
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_current_login_release_metadata_cannot_be_overridden(current_http):
    client, seen, replies = current_http
    params = {"version": "stale", "appVersion": 1, "appHash": "untrusted"}
    replies.append(({"code": 0, "data": {"uid": 99, "jwtToken": "fixture-jwt"}}, {}))
    await client.login(params)
    assert params == {"version": "stale", "appVersion": 1, "appHash": "untrusted"}
    assert seen[0]["params"]["version"] == "3.7.3"
    assert seen[0]["params"]["appVersion"] == 335
    assert seen[0]["params"]["appHash"] != "untrusted"


@pytest.mark.asyncio
async def test_generic_login_rejection_preserves_code_without_retry_or_secrets(
    current_http,
):
    client, seen, replies = current_http
    manager = RegistrationManager(
        lambda: client.deployment, lambda deployment, http: client
    )
    try:
        view = await manager.action(
            "admin", {"action": "start", "instance_id": "fixture"}
        )
        replies.append(
            ({"code": 1001, "msg": "fixture-password\nprivate provider detail"}, {})
        )
        view = await manager.action(
            "admin",
            {
                "instance_id": "fixture",
                "registration_code": view["registration_code"],
                "action": "login",
                "account": "fixture",
                "password": "fixture-password",
                "validate_str": "fixture-proof",
            },
        )
        assert view["status"] == "failed"
        assert view["error"] == "login_rejected_unspecified"
        assert view["provider_code"] == 1001
        assert view["error_action"] == "login"
        assert not view["session_ref"] and not client.jwt
        assert len(seen) == 1
        assert not any(
            value in json.dumps(view)
            for value in ("fixture-password", "private provider detail")
        )
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("number,label", SECURITY_ERRORS.items())
async def test_security_error_codes_do_not_collapse_into_1400(
    current_http, number, label
):
    client, seen, replies = current_http
    client.jwt, client.fields[3] = "fixture-jwt", 99
    replies.append(({"code": number, "msg": "private provider detail"}, {}))
    with pytest.raises(BusinessError, match=f"^{label}$") as failure:
        await client.request("/v1/user/get_safe_code", {})
    assert failure.value.number == number
    assert len(seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers,data,expected",
    [
        ({"x-set-jwt": "header-jwt"}, {}, "header-jwt"),
        ({}, {"jwtToken": "body-jwt"}, "body-jwt"),
        ({"x-set-jwt": "new-jwt"}, {"jwtToken": "new-jwt"}, "new-jwt"),
        (
            {"x-set-jwt": "header-jwt"},
            {"jwtToken": "body-jwt"},
            "header-jwt",
        ),
        ({"x-set-jwt": "pending-jwt"}, {}, "pending-jwt"),
        ({}, {}, "pending-jwt"),
        ({}, {"jwtToken": ""}, "pending-jwt"),
        ({}, None, "pending-jwt"),
        ({"x-set-jwt": "header-jwt"}, None, "header-jwt"),
    ],
)
async def test_successful_verification_follows_optional_official_jwt_rotation(
    current_http, headers, data, expected
):
    """Successful verification need not supply a new, duplicated credential."""
    client, seen, replies = current_http
    client.jwt, client.security_required, client.fields[3] = "pending-jwt", True, 99
    replies.append(({"code": 0, "data": data}, headers))
    await client.verify_security_code("123456")
    assert client.jwt == expected and not client.security_required
    assert len(seen) == 1
    assert "123456" not in json.dumps(client.export())
    replies.append(({"code": 0, "data": {"nimToken": "verified-nim"}}, {}))
    assert await client.refresh_nim_token() == "verified-nim"
    assert seen[-1]["params"]["loginToken"] == expected
    assert seen[-1]["headers"]["x-jwt"] == expected
    with pytest.raises(BusinessError, match="security_code_input"):
        await client.verify_security_code("123456")
    assert len(seen) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers,data,expected",
    [
        ({"x-set-jwt": "header-jwt"}, {}, "header-jwt"),
        ({}, {"jwtToken": "body-jwt"}, "body-jwt"),
        (
            {"x-set-jwt": "header-jwt"},
            {"jwtToken": "body-jwt"},
            "header-jwt",
        ),
        ({"x-set-jwt": "pending-jwt"}, {}, "pending-jwt"),
        ({}, {}, "pending-jwt"),
        ({}, None, "pending-jwt"),
    ],
)
async def test_registration_completes_optional_rotation_only_after_im_token(
    current_http, headers, data, expected
):
    """Exercise the complete registration state machine over encrypted HTTP."""
    client, seen, replies = current_http
    manager = RegistrationManager(
        lambda: client.deployment, lambda deployment, http: client
    )
    try:
        view = await manager.action(
            "admin", {"action": "start", "instance_id": "fixture"}
        )
        base = {
            "instance_id": "fixture",
            "registration_code": view["registration_code"],
        }
        replies.append(
            (
                {
                    "code": 0,
                    "data": {
                        "uid": 99,
                        "nimId": "nim-99",
                        "nickName": "Fixture",
                        "jwtToken": "pending-jwt",
                        "safeCodeRequired": True,
                    },
                },
                {},
            )
        )
        view = await manager.action(
            "admin",
            {
                **base,
                "action": "login",
                "account": "fixture",
                "password": "fixture-password",
                "validate_str": "fixture-proof",
            },
        )
        assert view["status"] == "security_required" and not view["session_ref"]
        replies.extend(
            [
                ({"code": 0, "data": data}, headers),
                ({"code": 0, "data": {"nimToken": "verified-nim"}}, {}),
            ]
        )
        view = await manager.action(
            "admin",
            {**base, "action": "verify_security_code", "security_code": "123456"},
        )
        assert view["status"] == "authenticated" and not view["error"]
        tx = manager.claim("admin", "fixture", view["session_ref"])
        assert tx.saved["business"]["jwt"] == expected
        assert tx.saved["nim_token"] == "verified-nim"
        assert not tx.saved["business"]["security_required"]
        assert not tx.pending_login
        assert "123456" not in json.dumps(tx.saved)
        assert not any(
            secret in json.dumps(view)
            for secret in ("pending-jwt", expected, "verified-nim", "123456")
        )
        assert [request["route"] for request in seen] == [
            "/v1/user/login",
            "/v1/user/verify_safe_code",
            "/v1/user/get-nim-token",
        ]
        assert seen[-1]["params"]["loginToken"] == expected
        with pytest.raises(wire.ProtocolError, match="registration_conflict"):
            await manager.action(
                "admin",
                {**base, "action": "verify_security_code", "security_code": "123456"},
            )
        assert len(seen) == 3
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers,data",
    [
        ({"x-set-jwt": "invalid jwt"}, {}),
        ({}, {"jwtToken": "invalid jwt"}),
        ({}, {"jwtToken": 42}),
        ({}, {"jwtToken": "x" * 8193}),
        ({}, "invalid receipt"),
        ({}, []),
    ],
)
async def test_malformed_verification_receipt_never_completes(
    current_http, headers, data
):
    client, _, replies = current_http
    client.jwt, client.security_required, client.fields[3] = "pending-jwt", True, 99
    replies.append(({"code": 0, "data": data}, headers))
    with pytest.raises(BusinessError, match="security_rotation_missing"):
        await client.verify_security_code("123456")
    assert client.jwt == "pending-jwt" and client.security_required


@pytest.mark.asyncio
@pytest.mark.parametrize("number", [1400, 1401, 1402, 1403, 1404, 1405])
async def test_rejected_security_code_cannot_install_a_returned_token(
    current_http, number
):
    client, seen, replies = current_http
    client.jwt, client.security_required, client.fields[3] = "pending-jwt", True, 99
    replies.append(
        (
            {"code": number, "data": {"jwtToken": "must-not-install"}},
            {"x-set-jwt": "must-not-install"},
        )
    )
    with pytest.raises(BusinessError) as failure:
        await client.verify_security_code("123456")
    assert failure.value.number == number
    assert client.jwt == "pending-jwt" and client.security_required
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_refresh_credentials_are_bound_after_http_lock(current_http):
    client, seen, replies = current_http
    client.jwt, client.fields[3] = "old-jwt", 99
    replies.append(({"code": 0, "data": {"nimToken": "new-nim"}}, {}))
    await client.lock.acquire()
    task = asyncio.create_task(client.refresh_nim_token())
    await asyncio.sleep(0)
    client.jwt = "new-jwt"
    client.lock.release()
    assert await task == "new-nim"
    assert seen[0]["params"]["loginToken"] == seen[0]["headers"]["x-jwt"] == "new-jwt"


@pytest.mark.asyncio
async def test_verification_challenge_is_rechecked_after_http_lock(current_http):
    client, seen, _ = current_http
    client.jwt, client.security_required = "pending-jwt", True
    await client.lock.acquire()
    task = asyncio.create_task(client.verify_security_code("123456"))
    await asyncio.sleep(0)
    client.security_required = False
    client.lock.release()
    with pytest.raises(BusinessError, match="security_code_input") as failure:
        await task
    assert failure.value.submitted is False
    assert not seen


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [False, True])
async def test_current_login_without_legacy_response_token(current_http, flag):
    client, _, replies = current_http
    data = {"uid": 99, "jwtToken": "fixture-jwt"}
    if flag:
        data.update(safeCodeSet=True, safeCodeRequired=False)
    replies.append(({"code": 0, "data": data}, {}))
    await client.login({"type": "LOGIN_TYPE_ACCOUNT_PWD"})
    assert client.jwt == "fixture-jwt" and not client.security_required


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("uid", True),
        ("uid", 0),
        ("uid", "99"),
        ("jwtToken", "invalid jwt"),
        ("jwtToken", ""),
        ("safeCodeRequired", "false"),
        ("safeCodeSet", 1),
    ],
)
async def test_current_login_rejects_malformed_authentication_evidence(
    current_http, field, value
):
    client, _, replies = current_http
    data = {"uid": 99, "jwtToken": "fixture-jwt", field: value}
    replies.append(({"code": 0, "data": data}, {}))
    with pytest.raises(BusinessError, match="login_binding"):
        await client.login({"type": "LOGIN_TYPE_ACCOUNT_PWD"})
    assert not client.jwt and not client.security_required


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route",
    [
        "/v1/user/set_safe_code",
        "/v1/user/reset_safe_code",
        "/arbitrary",
    ],
)
async def test_security_adaptation_does_not_expose_setting_or_reset(
    current_http, route
):
    client, seen, _ = current_http
    with pytest.raises(BusinessError, match="unsupported_route"):
        await client.request(route, {})
    assert not seen


@pytest.mark.asyncio
async def test_legacy_saved_session_keeps_legacy_wire_version(current_deployment):
    async with aiohttp.ClientSession() as http:
        client = BusinessClient(current_deployment, http)
        saved = {
            "origin": current_deployment.origin,
            "uid": 99,
            "kind": 1,
            "device": 123,
            "jwt": "legacy-jwt",
            "mac": wire.b64(bytes(16)),
        }
        client.restore(saved)
        assert client.protocol_version == "3.7.1"
        assert client.fields[1:3] == current_deployment.metadata[1:3]
        client.request = AsyncMock(return_value={"nimToken": "legacy-nim"})
        assert await client.refresh_nim_token() == "legacy-nim"
        client.request.assert_awaited_once_with("/v1/user/RefreshToken", {})


class CurrentLoginFixture:
    """Keep disposable secrets inside an owner-bound in-memory client."""

    def __init__(self, deployment, http):
        self.deployment = deployment
        self.protocol_version = "3.7.3"
        self.security_required = False
        self.jwt = ""
        self.login = AsyncMock(side_effect=self.authenticate)
        self.verify_security_code = AsyncMock(side_effect=self.verify)
        self.refresh_nim_token = AsyncMock(return_value="fixture-nim-token")

    async def authenticate(self, params):
        self.jwt = "pending-secret"
        self.security_required = True
        return {"uid": 99, "nimId": "nim-99", "nickName": "Fixture"}

    async def verify(self, code):
        self.jwt, self.security_required = "verified-secret", False

    def export(self):
        return {"uid": 99, "jwt": self.jwt, "security_required": self.security_required}


@pytest_asyncio.fixture
async def challenged_registration(current_deployment):
    manager = RegistrationManager(lambda: current_deployment, CurrentLoginFixture)
    try:
        started = await manager.action(
            "admin", {"action": "start", "instance_id": "bot"}
        )
        base = {"instance_id": "bot", "registration_code": started["registration_code"]}
        view = await manager.action(
            "admin",
            {
                **base,
                "action": "login",
                "account": "fixture",
                "password": "fixture",
                "validate_str": "human-fixture",
            },
        )
        tx = manager.transactions[base["registration_code"]]
        assert view["status"] == "security_required"
        assert not view["session_ref"] and not tx.saved
        assert tx.nickname == "Fixture"
        yield manager, tx, base
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_challenge_owner_claim_and_ephemeral_secret(challenged_registration):
    manager, tx, base = challenged_registration
    with pytest.raises(wire.ProtocolError, match="not_found"):
        await manager.action(
            "other",
            {
                **base,
                "action": "verify_security_code",
                "security_code": "123456",
            },
        )
    with pytest.raises(wire.ProtocolError, match="reference_invalid"):
        manager.claim("admin", "bot", "")
    view = await manager.action(
        "admin",
        {
            **base,
            "action": "verify_security_code",
            "security_code": "123456",
        },
    )
    assert view["status"] == "authenticated"
    assert manager.claim("admin", "bot", view["session_ref"]) is tx
    assert "secret" not in json.dumps(view)
    assert "123456" not in json.dumps(tx.saved)
    assert not tx.pending_login
    tx.client.refresh_nim_token.assert_awaited_once()
    with pytest.raises(wire.ProtocolError, match="conflict"):
        await manager.action(
            "admin",
            {
                **base,
                "action": "verify_security_code",
                "security_code": "123456",
            },
        )
    tx.client.verify_security_code.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code,state",
    [
        (1400, "security_setup_required"),
        (1401, "failed"),
        (1402, "security_required"),
        (1403, "security_required"),
        (1404, "security_locked"),
        (1405, "security_setup_required"),
    ],
)
async def test_challenge_rejection_is_not_saved(challenged_registration, code, state):
    manager, tx, base = challenged_registration
    tx.client.verify_security_code.side_effect = BusinessError(
        SECURITY_ERRORS[code], code
    )
    view = await manager.action(
        "admin",
        {
            **base,
            "action": "verify_security_code",
            "security_code": "123456",
        },
    )
    assert view["status"] == state and view["provider_code"] == code
    assert not view["session_ref"] and not tx.saved
    if code == 1403:
        assert view["security_retry_after"] >= 29
        with pytest.raises(wire.ProtocolError, match="cooldown"):
            await manager.action(
                "admin",
                {
                    **base,
                    "action": "verify_security_code",
                    "security_code": "123456",
                },
            )
        tx.client.verify_security_code.assert_awaited_once()


@pytest.mark.asyncio
async def test_token_setup_retry_does_not_resubmit_security_code(
    challenged_registration,
):
    manager, tx, base = challenged_registration
    tx.client.refresh_nim_token.side_effect = [
        BusinessError("transport"),
        "fixture-nim-token",
    ]
    view = await manager.action(
        "admin",
        {
            **base,
            "action": "verify_security_code",
            "security_code": "123456",
        },
    )
    assert view["status"] == "connection_pending" and not view["session_ref"]
    tx.security_retry_at = 0
    view = await manager.action("admin", {**base, "action": "retry_connection"})
    assert view["status"] == "authenticated"
    tx.client.verify_security_code.assert_awaited_once()
    assert tx.client.refresh_nim_token.await_count == 2


@pytest.mark.asyncio
async def test_unknown_verification_is_terminal_until_new_login(
    challenged_registration,
):
    manager, tx, base = challenged_registration
    error = BusinessError("transport")
    error.submitted = True
    tx.client.verify_security_code.side_effect = error
    view = await manager.action(
        "admin",
        {
            **base,
            "action": "verify_security_code",
            "security_code": "123456",
        },
    )
    assert view["status"] == "security_uncertain" and not view["session_ref"]
    tx.security_retry_at = 0
    with pytest.raises(wire.ProtocolError, match="conflict"):
        await manager.action(
            "admin",
            {
                **base,
                "action": "verify_security_code",
                "security_code": "123456",
            },
        )
    tx.client.verify_security_code.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_during_verification_cannot_issue_save_reference(
    challenged_registration,
):
    manager, tx, base = challenged_registration
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(code):
        entered.set()
        await release.wait()

    tx.client.verify_security_code.side_effect = delayed
    task = asyncio.create_task(
        manager.action(
            "admin",
            {
                **base,
                "action": "verify_security_code",
                "security_code": "123456",
            },
        )
    )
    await entered.wait()
    assert (await manager.action("admin", {**base, "action": "poll"}))[
        "status"
    ] == "security_verifying"
    await manager.action("admin", {**base, "action": "cancel"})
    release.set()
    with pytest.raises(wire.ProtocolError, match="cancelled"):
        await task
    assert not tx.session_ref and not tx.saved and not tx.pending_login
    assert not tx.client.jwt


@pytest.mark.asyncio
async def test_expiry_during_verification_never_issues_capability(
    challenged_registration,
):
    manager, tx, base = challenged_registration
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(code):
        entered.set()
        await release.wait()
        tx.client.security_required = False

    tx.client.verify_security_code.side_effect = delayed
    task = asyncio.create_task(
        manager.action(
            "admin",
            {
                **base,
                "action": "verify_security_code",
                "security_code": "123456",
            },
        )
    )
    await entered.wait()
    tx.expires = 0
    release.set()
    view = await task
    assert view["error"] == "registration_expired"
    assert not tx.session_ref and not tx.saved
    tx.client.refresh_nim_token.assert_not_awaited()


@pytest.mark.asyncio
async def test_starting_reauthentication_does_not_terminate_old_connection(
    current_deployment,
    monkeypatch,
):
    from types import SimpleNamespace

    from astrbot.core.platform.sources.wangshangliao import dashboard, registration

    manager = RegistrationManager(lambda: current_deployment, CurrentLoginFixture)
    monkeypatch.setattr(registration, "registrations", manager)
    platform_manager = SimpleNamespace(
        terminate_platform=AsyncMock(),
        astrbot_config={
            "platform": [
                {
                    "id": "bot",
                    "type": "wangshangliao",
                    "account_id": "99",
                }
            ]
        },
    )
    try:
        view = await dashboard.handle_registration(
            platform_manager,
            {"action": "start", "instance_id": "bot"},
            "admin",
        )
        assert view["status"] == "pending"
        platform_manager.terminate_platform.assert_not_awaited()
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["１２３４５６", "12345", "abcdef", "", "1234567"])
async def test_bad_security_input_never_calls_provider(challenged_registration, code):
    manager, tx, base = challenged_registration
    try:
        view = await manager.action(
            "admin",
            {
                **base,
                "action": "verify_security_code",
                "security_code": code,
            },
        )
        assert view["error"] == "security_code_input"
    except wire.ProtocolError as error:
        assert str(error) == "registration_input"
    tx.client.verify_security_code.assert_not_awaited()
