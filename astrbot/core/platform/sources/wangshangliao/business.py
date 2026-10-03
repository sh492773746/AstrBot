"""Async business authentication with explicit deployment configuration."""

import asyncio
import base64
import json
import os
import stat
import struct
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
import nacl.bindings
import xxhash
from nacl.public import PublicKey, SealedBox
from nacl.signing import SigningKey
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from . import wire


class Deployment(BaseModel):
    """Server-only protocol configuration; never serialized into platform settings."""

    model_config = ConfigDict(extra="forbid")
    origin: str
    headers: dict[str, str]
    metadata: list[int] = Field(min_length=14, max_length=14)
    signing_seed: SecretStr
    body_key: SecretStr
    message_key: SecretStr = SecretStr("")
    server_key: SecretStr
    captcha_id: str
    app_key: str
    device_id: str

    def key(self, name: str) -> bytes:
        """Decode a fixed-size deployment key without echoing its value."""
        try:
            key = base64.b64decode(
                getattr(self, name).get_secret_value(), validate=True
            )
            if len(key) != 32:
                raise ValueError
            return key
        except Exception:
            raise wire.ProtocolError("deployment_key") from None

    @classmethod
    def load(cls):
        """Load an explicit deployment file or the imported encrypted deployment.

        Returns:
            Validated server-only protocol configuration.

        Raises:
            wire.ProtocolError: If configuration is missing or invalid.
        """
        location = os.environ.get("ASTRBOT_WANGSHANGLIAO_CONFIG")
        try:
            if location:
                path = Path(location)
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or path.stat().st_size > 65536
                ):
                    raise ValueError
                if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
                    raise ValueError
                result = cls.model_validate_json(path.read_bytes())
            else:
                from astrbot.core.utils.astrbot_path import get_astrbot_data_path

                from .storage import Vault

                root = (
                    Path(get_astrbot_data_path())
                    / "platform_data/wangshangliao/deployment"
                )
                saved = Vault("wangshangliao-deployment-v1", root).load()
                if saved is None:
                    raise wire.ProtocolError("deployment_missing")
                result = cls.model_validate(saved)
            parsed = urlsplit(result.origin)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.path not in ("", "/")
            ):
                raise ValueError
            wire.metadata(result.metadata)
            for name in ("signing_seed", "body_key", "server_key"):
                result.key(name)
            # Legacy Rust login deployments do not contain a message key.
            if result.message_key.get_secret_value():
                result.key("message_key")
            return result
        except wire.ProtocolError as exc:
            if str(exc) == "deployment_missing":
                raise
            raise wire.ProtocolError("deployment_invalid") from None
        except Exception:
            raise wire.ProtocolError("deployment_invalid") from None


class BusinessError(wire.ProtocolError):
    """A sanitized provider rejection with an optional numeric code."""

    def __init__(self, code: str, number: int = 0, retry_after: float = 0):
        super().__init__(code)
        self.number = number
        self.retry_after = retry_after


class BusinessClient:
    """Own the signed HTTP session; expose only reviewed operations."""

    def __init__(self, deployment: Deployment, http: aiohttp.ClientSession):
        self.deployment = deployment
        self.http = http
        self.fields = list(deployment.metadata)
        self.fields[3] = self.fields[4] = self.fields[8] = 0
        self.jwt = ""
        self.mac = b""
        self.last_request = 0
        self.lock = asyncio.Lock()

    async def request(self, route: str, params: dict, exchange: bytes | None = None):
        """Send a bounded signed request without redirects or automatic retries.

        Args:
            route: Reviewed business route.
            params: Operation-specific JSON object.
            exchange: Sealed login exchange, only for login.

        Returns:
            Validated provider data.

        Raises:
            BusinessError: On expiry, rejection, transport or malformed response.
        """
        allowed = {
            "/v1/user/login",
            "/v1/user/get-change-device-verify",
            "/v1/verify/sms-anon",
            "/v1/user/RefreshToken",
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
        }
        anonymous = {
            "/v1/user/login",
            "/v1/user/get-change-device-verify",
            "/v1/verify/sms-anon",
        }
        if route not in allowed:
            raise BusinessError("unsupported_route")
        async with self.lock:
            # A preceding request may invalidate authentication while we wait.
            if not self.jwt and route not in anonymous:
                raise BusinessError("reauth_required", 401)
            now = time.time_ns()
            request_id = max(now, self.last_request + 1)
            self.last_request = request_id
            plain = json.dumps(
                params, ensure_ascii=False, separators=(",", ":")
            ).encode()
            body = wire.seal_business(
                self.deployment.key("body_key"), request_id, plain, self.fields[0]
            )
            fields = list(self.fields)
            digest, aad = wire.binding(request_id, plain, fields[0])
            fields[5:8] = [request_id, len(plain), digest]
            fields[9], fields[12] = now // 1_000_000_000, 1
            fields[8] = (
                int.from_bytes(
                    nacl.bindings.crypto_shorthash_siphash24(
                        struct.pack("<QQQ", fields[0], request_id, fields[9]), self.mac
                    ),
                    "little",
                )
                if self.mac
                else 0
            )
            metadata = wire.metadata(fields)
            signer = SigningKey(self.deployment.key("signing_seed"))
            headers = {
                k: v
                for k, v in self.deployment.headers.items()
                if k in {"x-version", "x-device", "accept", "content-type"}
            }
            headers.update(
                {
                    "x-request": wire.b64(metadata),
                    "x-seed": wire.b64(bytes(signer.verify_key)),
                    "x-hash": wire.b64(signer.sign(metadata).signature),
                    "x-trace-id": f"{fields[0]}.{request_id}",
                }
            )
            if self.jwt:
                headers.update(
                    {
                        "x-id": str(fields[3]),
                        "x-token": str(fields[3]),
                        "x-jwt": self.jwt,
                    }
                )
            if exchange:
                headers["x-context"] = wire.b64(exchange)
            try:
                async with self.http.post(
                    self.deployment.origin.rstrip("/") + route,
                    headers=headers,
                    data=body,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as response:
                    if response.status == 401:
                        self.jwt, self.mac = "", b""
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
                    chunks = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        chunks.extend(chunk)
                        if len(chunks) > wire.LIMIT + 4096:
                            raise BusinessError("response_limit")
                decoded = (
                    bytes(chunks)
                    if chunks.startswith(b'{"')
                    else wire.open_business(
                        self.deployment.key("body_key"), aad, bytes(chunks)
                    )
                )
                value = json.loads(decoded)
                if type(value.get("code")) is not int:
                    raise BusinessError("response_shape")
                if value["code"] != 0:
                    if value["code"] == 401:
                        self.jwt, self.mac = "", b""
                        raise BusinessError("reauth_required", 401)
                    feedback = {
                        "账号或密码错误": "invalid_credentials",
                        "密码错误": "invalid_credentials",
                        "验证码错误": "invalid_sms",
                        "短信验证码错误": "invalid_sms",
                        "验证码已过期": "sms_expired",
                    }
                    raise BusinessError(
                        feedback.get(value.get("msg"), "business_rejected"),
                        value["code"],
                    )
                return value["data"]
            except BusinessError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError):
                raise BusinessError("transport") from None
            except Exception:
                raise BusinessError("response_invalid") from None

    async def login(self, params: dict):
        """Authenticate and bind the sealed reply before installing credentials."""
        self.jwt, self.mac = "", b""
        self.fields[3] = self.fields[4] = 0
        signer = SigningKey(self.deployment.key("signing_seed"))
        public = bytes(signer.verify_key)
        self.fields[0] = xxhash.xxh3_64_intdigest(public)
        now = time.time_ns()
        exchange = wire.Exchange(
            device=self.fields[0],
            kind=1,
            public_key=public,
            seconds=now // 1_000_000_000,
            correlation=now,
            version=self.fields[2],
        )
        sealed = SealedBox(PublicKey(self.deployment.key("server_key"))).encrypt(
            exchange.SerializeToString()
        )
        data = await self.request("/v1/user/login", params, sealed)
        try:
            token = wire.LoginToken.FromString(
                SealedBox(signer.to_curve25519_private_key()).decrypt(
                    wire.unb64(data["token"], 65584)
                )
            )
            if (
                token.seconds != exchange.seconds
                or token.correlation != exchange.correlation
                or type(data["uid"]) is not int
                or token.uid != data["uid"]
                or not 0 < token.uid < 1 << 31
                or len(token.mac) < 16
                or not isinstance(data["jwtToken"], str)
                or not 0 < len(data["jwtToken"]) <= 8192
            ):
                raise ValueError
            if any(c in data["jwtToken"] for c in "\r\n"):
                raise ValueError
            self.fields[3], self.fields[4] = token.uid, token.kind & 0xFFFFFFFF
            self.jwt, self.mac = data["jwtToken"], token.mac[:16]
            return data
        except Exception:
            raise BusinessError("login_binding") from None

    def export(self) -> dict:
        """Return credentials exclusively for encrypted persistence."""
        return {
            "origin": self.deployment.origin,
            "uid": self.fields[3],
            "kind": self.fields[4],
            "device": self.fields[0],
            "jwt": self.jwt,
            "mac": wire.b64(self.mac),
        }

    def restore(self, saved: dict) -> None:
        """Restore credentials only for the same deployment."""
        if saved["origin"] != self.deployment.origin or not 0 < saved["uid"] < 1 << 31:
            raise BusinessError("session_binding")
        mac = wire.unb64(saved["mac"], 16)
        if len(mac) != 16 or not saved["jwt"] or any(c in saved["jwt"] for c in "\r\n"):
            raise BusinessError("session_binding")
        self.fields[0], self.fields[3], self.fields[4] = (
            saved["device"],
            saved["uid"],
            saved["kind"],
        )
        self.jwt, self.mac = saved["jwt"], mac
