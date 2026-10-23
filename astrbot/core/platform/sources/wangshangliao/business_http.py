"""Shared signed HTTP exchange for reviewed native and plugin operations."""

import asyncio
import inspect
import json
import struct
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import aiohttp
import nacl.bindings
from nacl.signing import SigningKey

from . import wire
from .business import SECURITY_ERRORS, BusinessError


async def request_business(client, route, params, exchange=None, *, authorize=None):
    """Use an existing authenticated session without creating another login.

    Args:
        client: Native business client owning the shared HTTP lock and credentials.
        route: Fixed reviewed business route.
        params: Operation-specific JSON object.
        exchange: Sealed login exchange, only for login.
        authorize: Optional account/lifecycle guard checked after lock admission.

    Returns:
        Validated provider data, without exposing authentication material.

    Raises:
        BusinessError: On expiry, rejection, transport or malformed response.
    """
    allowed = {
        "/v1/user/login",
        "/v1/user/get-change-device-verify",
        "/v1/verify/sms-anon",
        "/v1/user/RefreshToken",
        "/v1/user/get-nim-token",
        "/v1/user/get_safe_code",
        "/v1/user/verify_safe_code",
        "/v1/group/get-group-list",
        "/v1/group/get-group-members",
        "/v1/group/get-group-apply-list",
        "/v1/group/get-apply-logs",
        "/v1/group/set-member-mute",
        "/v1/group/set-member-nickname",
        "/v1/group/remove-group-member",
        "/v1/group/member-mute-cancel",
        "/v1/group/set-group-mute",
        "/v1/group/add-notice",
        "/v1/group/notice-list",
        "/v1/group/leave-group",
        "/v1/friend/friend-apply-list",
        "/v1/friend/friend-apply-handler",
    }
    if route not in allowed:
        raise BusinessError("unsupported_route")
    if route == "/v1/user/verify_safe_code" and (
        client.protocol_version not in {"3.7.3", "2.8.3"}
        or not client.security_required
        or set(params) != {"code"}
        or not isinstance(params["code"], str)
        or len(params["code"]) != 6
        or not params["code"].isascii()
        or not params["code"].isdigit()
    ):
        raise BusinessError("security_code_input")
    submission = {"started": False, "rejected": False}
    try:
        return await _exchange_business(
            client, route, params, exchange, authorize=authorize, submission=submission
        )
    except BaseException as exc:
        # Only this request's pre-IO failures are definitive non-submissions.
        exc.submitted = submission["started"]
        exc.provider_rejected = submission["rejected"]
        raise


async def _exchange_business(
    client, route, params, exchange=None, *, authorize=None, submission=None
):
    """Perform signed IO after a caller has validated its fixed operation scope.

    Args:
        client: Credential-owning business client.
        route: Fixed route already checked by the production or offline-test caller.
        params: Validated operation-specific parameters.
        exchange: Login-only sealed exchange.
        authorize: Synchronous or asynchronous identity and lifecycle guard.
        submission: Internal request-local submission evidence.

    Returns:
        Validated provider data.

    Raises:
        BusinessError: On rejection, malformed responses or transport uncertainty.
    """
    anonymous = {
        "/v1/user/login",
        "/v1/user/get-change-device-verify",
        "/v1/verify/sms-anon",
    }
    response_limit = (
        wire.INVITATION_LOG_LIMIT if route == "/v1/group/get-apply-logs" else wire.LIMIT
    )
    task = asyncio.current_task()
    # An async authorization callback may refresh the roster through this client.
    # Only that same task may reuse the lock; unrelated requests still serialize.
    lock = (
        nullcontext()
        if getattr(client, "_authorization_task", None) is task
        else client.lock
    )
    async with lock:
        if authorize is not None:
            previous = getattr(client, "_authorization_task", None)
            client._authorization_task = task
            try:
                checked = authorize()
                if inspect.isawaitable(checked):
                    await checked
            finally:
                client._authorization_task = previous
        if not client.jwt and route not in anonymous:
            raise BusinessError("reauth_required", 401)
        if route == "/v1/user/verify_safe_code" and not client.security_required:
            raise BusinessError("security_code_input")
        now = time.time_ns()
        current = getattr(client, "protocol_version", "3.7.1") == "3.7.3"
        request_id = max(now // 1_000_000 if current else now, client.last_request + 1)
        client.last_request = request_id
        plain = json.dumps(params, ensure_ascii=False, separators=(",", ":")).encode()
        version = 1703240058615 if current else 0x019C411FDEAF
        body = wire.seal_business(
            client.deployment.key("body_key"),
            request_id,
            plain,
            client.fields[0],
            version=version,
        )
        fields = list(client.fields)
        if current:
            fields[1:3] = [3, 1]
            fields[10:12], fields[13] = [0, 0], 0
        digest, aad = wire.binding(request_id, plain, fields[0], version=version)
        fields[5:8] = [request_id, len(plain), digest]
        fields[9], fields[12] = now // 1_000_000_000, 1
        fields[8] = (
            int.from_bytes(
                nacl.bindings.crypto_shorthash_siphash24(
                    struct.pack("<QQQ", fields[0], request_id, fields[9]), client.mac
                ),
                "little",
            )
            if client.mac and (not current or fields[3] > 0)
            else 0
        )
        metadata = wire.metadata(fields)
        signer = SigningKey(client.deployment.key("signing_seed"))
        headers = {
            k: v
            for k, v in client.deployment.headers.items()
            if k in {"x-version", "x-device", "accept", "content-type"}
        }
        if current:
            headers.update(
                {"x-version": "1.1703240058615.0", "x-device": "3", "x-client": "2"}
            )
        elif client.protocol_version == "2.8.3":
            # Published desktop 2.8.3 uses the legacy signed envelope, not Android's.
            headers.update(
                {"x-version": "1.1770619133615.0", "x-device": "1", "x-client": "11"}
            )
        headers.update(
            {
                "x-request": wire.b64(metadata),
                "x-seed": wire.b64(bytes(signer.verify_key)),
                "x-hash": wire.b64(signer.sign(metadata).signature),
                "x-trace-id": f"{fields[0]}.{request_id}",
            }
        )
        if client.jwt:
            headers.update(
                {
                    "x-id": str(fields[3]),
                    "x-token": str(fields[3]),
                    "x-jwt": client.jwt,
                }
            )
        if exchange:
            headers["x-context"] = wire.b64(exchange)
        try:
            if submission is not None:
                submission["started"] = True
            async with client.http.post(
                client.deployment.origin.rstrip("/") + route,
                headers=headers,
                data=body,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                # Submission and reviewed rejection are independent evidence.
                if submission is not None and response.status in {401, 403, 429}:
                    submission["rejected"] = True
                if response.status == 401:
                    client.jwt, client.mac = "", b""
                    client.auth_error = "reauth_required"
                    raise BusinessError("reauth_required", 401)
                if response.status == 429:
                    retry_after = 30.0
                    value = response.headers.get("Retry-After", "")
                    try:
                        retry_after = float(value)
                    except ValueError:
                        try:
                            retry_after = (
                                parsedate_to_datetime(value)
                                - datetime.now(timezone.utc)
                            ).total_seconds()
                        except (ValueError, TypeError, OverflowError):
                            pass
                    raise BusinessError(
                        "rate_limited", 429, max(1, min(retry_after, 86400))
                    )
                if response.status != 200:
                    raise BusinessError("http_rejected", response.status)
                rotated = response.headers.get("x-set-jwt", "")
                chunks = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    chunks.extend(chunk)
                    if len(chunks) > response_limit + 4096:
                        raise BusinessError("response_limit")
            decoded = (
                bytes(chunks)
                if chunks.startswith(b'{"')
                else wire.open_business(
                    client.deployment.key("body_key"),
                    aad,
                    bytes(chunks),
                    limit=response_limit,
                )
            )
            if len(decoded) > response_limit:
                raise BusinessError("response_limit")
            value = json.loads(decoded)
            if type(value.get("code")) is not int:
                raise BusinessError("response_shape")
            if value["code"] != 0:
                if submission is not None and (
                    value["code"] in {401, 403, 429} or value["code"] in SECURITY_ERRORS
                ):
                    submission["rejected"] = True
                if value["code"] == 401:
                    client.jwt, client.mac = "", b""
                    client.auth_error = "reauth_required"
                    raise BusinessError("reauth_required", 401)
                if value["code"] == 429:
                    raise BusinessError("rate_limited", 429, 30)
                if value["code"] in SECURITY_ERRORS:
                    raise BusinessError(SECURITY_ERRORS[value["code"]], value["code"])
                if route == "/v1/user/login" and value["code"] == 1001:
                    # DEFAULT_CODE does not establish a password error or ban.
                    raise BusinessError("login_rejected_unspecified", 1001)
                feedback = {
                    "账号或密码错误": "invalid_credentials",
                    "密码错误": "invalid_credentials",
                    "验证码错误": "invalid_sms",
                    "短信验证码错误": "invalid_sms",
                    "验证码已过期": "sms_expired",
                }
                raise BusinessError(
                    "phone_not_found"
                    if value["code"] == 1039
                    else feedback.get(value.get("msg"), "business_rejected"),
                    value["code"],
                )
            if route == "/v1/user/verify_safe_code":
                # The official SDK gives x-set-jwt precedence, but rotation is
                # optional. The success code is the verification receipt.
                returned = value.get("data")
                if returned is None:
                    returned = {}
                token = (
                    rotated
                    if rotated
                    else returned.get("jwtToken", "")
                    if isinstance(returned, dict)
                    else None
                )
                if (
                    not isinstance(returned, dict)
                    or not isinstance(token, str)
                    or len(token) > 8192
                    or not token.isascii()
                    or any(c.isspace() for c in token)
                    or not client.jwt
                    or not client.security_required
                ):
                    raise BusinessError("security_rotation_missing")
                if token:
                    client.jwt = token
                client.security_required = False
            if current and client.jwt:
                client.fields[4] = (client.fields[4] + 1) & 0x7FFFFFFF
            # Once accepted, a mutation receipt remains evidence after revocation.
            return returned if route == "/v1/user/verify_safe_code" else value["data"]
        except BusinessError:
            raise
        except wire.ProtocolError as exc:
            raise BusinessError(str(exc)) from None
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise BusinessError("transport") from None
        except Exception:
            raise BusinessError("response_invalid") from None
