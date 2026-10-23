"""Async business authentication with explicit deployment configuration."""

import asyncio
import base64
import os
import stat
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import aiohttp
import xxhash
from nacl.public import PublicKey, SealedBox
from nacl.signing import SigningKey
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from . import wire

SECURITY_ERRORS = {
    1400: "secondary_security_code_required",
    1401: "account_clearing",
    1402: "secondary_security_unverified",
    1403: "secondary_security_code_invalid",
    1404: "secondary_security_locked",
    1405: "secondary_security_phone_required",
}


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
    protocol_version: Literal["3.7.1", "3.7.3", "2.8.3"] = "3.7.1"

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
        self.protocol_version = getattr(deployment, "protocol_version", "3.7.1")
        self.security_required = False
        self.auth_error = ""
        if self.protocol_version in {"3.7.3", "2.8.3"}:
            public = bytes(SigningKey(deployment.key("signing_seed")).verify_key)
            self.fields[0] = xxhash.xxh3_64_intdigest(public)
            self.fields[1:3] = [3 if self.protocol_version == "3.7.3" else 1, 1]
            if self.protocol_version == "3.7.3":
                self.mac = public[16:32]

    async def request(
        self, route: str, params: dict, exchange: bytes | None = None, *, authorize=None
    ):
        """Send a bounded signed request without redirects or automatic retries.

        Args:
            route: Reviewed business route.
            params: Operation-specific JSON object.
            exchange: Sealed login exchange, only for login.
            authorize: Optional lifecycle check repeated after HTTP lock admission.

        Returns:
            Validated provider data.

        Raises:
            BusinessError: On expiry, rejection, transport or malformed response.
        """
        from .business_http import request_business

        return await request_business(
            self, route, params, exchange, authorize=authorize
        )

    async def login(self, params: dict):
        """Authenticate and bind the sealed reply before installing credentials."""
        self.jwt, self.mac = "", b""
        self.security_required = False
        self.auth_error = ""
        self.fields[3] = self.fields[4] = 0
        signer = SigningKey(self.deployment.key("signing_seed"))
        public = bytes(signer.verify_key)
        self.fields[0] = xxhash.xxh3_64_intdigest(public)
        current = self.protocol_version == "3.7.3"
        if current:
            self.fields[1:3] = [3, 1]
            self.mac = public[16:32]
            # Public release metadata from the signature-verified 3.7.3 APK.
            # SecurityUtil uses uppercase SHA-256: colon-separated signer, then APK.
            params = {
                **params,
                "version": "3.7.3",
                "appVersion": 335,
                "appHash": (
                    "3C:F2:35:2F:6D:B2:85:67:98:25:B1:2F:06:5E:FC:5C:"
                    "7A:AC:0F:6D:74:67:DA:4E:F9:50:05:58:C4:42:F1:96="
                    "02ED673B263F7EA80B307ADE1967C49A7264F955CDF33EB36F7673591029E195"
                ),
            }
        elif self.protocol_version == "2.8.3":
            self.fields[1:3] = [1, 1]
        now = time.time_ns()
        exchange = wire.Exchange(
            device=self.fields[0],
            kind=3 if current else 1,
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
            if current:
                # The current SDK uses the authenticated response and device key,
                # not the legacy sealed response token or its server-issued MAC.
                if (
                    not isinstance(data, dict)
                    or type(data.get("uid")) is not int
                    or not 0 < data["uid"] < 1 << 31
                    or not isinstance(data.get("jwtToken"), str)
                    or not 0 < len(data["jwtToken"]) <= 8192
                    or any(c.isspace() for c in data["jwtToken"])
                    or type(data.get("safeCodeRequired", False)) is not bool
                    or type(data.get("safeCodeSet", False)) is not bool
                ):
                    raise ValueError
                self.fields[3], self.fields[4] = data["uid"], 0
                self.jwt, self.mac = data["jwtToken"], public[16:32]
                self.security_required = data.get("safeCodeRequired", False)
                return data
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
            if self.protocol_version == "2.8.3":
                if any(
                    type(data.get(name, False)) is not bool
                    for name in ("safeCodeRequired", "safeCodeSet")
                ):
                    raise ValueError
                self.security_required = data.get("safeCodeRequired", False)
            return data
        except Exception:
            self.jwt, self.mac = "", b""
            raise BusinessError("login_binding") from None

    async def verify_security_code(self, code: str) -> None:
        """Complete the provider's current login challenge without retaining code.

        Args:
            code: Six ASCII digits entered in the authenticated Dashboard.

        Raises:
            BusinessError: If the challenge, response or credential rotation fails.
        """
        if (
            self.protocol_version not in {"3.7.3", "2.8.3"}
            or not self.security_required
            or not isinstance(code, str)
            or len(code) != 6
            or not code.isascii()
            or not code.isdigit()
        ):
            raise BusinessError("security_code_input")
        await self.request("/v1/user/verify_safe_code", {"code": code})
        if not self.jwt or self.security_required:
            raise BusinessError("security_rotation_missing")

    async def refresh_nim_token(self) -> str:
        """Fetch a validated IM token using the saved session's protocol.

        Returns:
            The current IM login token.

        Raises:
            BusinessError: If authentication is incomplete or the response is invalid.
        """
        if self.security_required:
            raise BusinessError("secondary_security_unverified", 1402)
        if self.protocol_version in {"3.7.3", "2.8.3"}:
            params = {}

            def bind_credentials():
                if self.security_required:
                    raise BusinessError("secondary_security_unverified", 1402)
                params.update(
                    nimDeviceId=self.deployment.device_id,
                    uid=self.fields[3],
                    loginToken=self.jwt,
                )

            result = await self.request(
                "/v1/user/get-nim-token",
                params,
                authorize=bind_credentials,
            )
        else:
            result = await self.request("/v1/user/RefreshToken", {})
        token = result.get("nimToken") if isinstance(result, dict) else None
        if (
            not isinstance(token, str)
            or not 0 < len(token) <= 8192
            or any(c.isspace() for c in token)
        ):
            raise BusinessError("refresh_shape")
        kind = result.get("kind", "NIM_LOGIN_KIND_STATIC")
        auth_types = {
            "NIM_LOGIN_KIND_STATIC": 0,
            "NIM_LOGIN_KIND_DYNAMIC": 1,
            "NIM_LOGIN_KIND_CALLBACK": 2,
        }
        if not isinstance(kind, str) or kind not in auth_types:
            raise BusinessError("nim_auth_type_invalid")
        from .nim import LoginToken

        return LoginToken(token, auth_types[kind])

    def export(self) -> dict:
        """Return credentials exclusively for encrypted persistence."""
        return {
            "origin": self.deployment.origin,
            "uid": self.fields[3],
            "kind": self.fields[4],
            "device": self.fields[0],
            "jwt": self.jwt,
            "mac": wire.b64(self.mac),
            "protocol_version": self.protocol_version,
            "security_required": self.security_required,
        }

    def restore(self, saved: dict) -> None:
        """Restore credentials only for the same deployment."""
        if saved["origin"] != self.deployment.origin or not 0 < saved["uid"] < 1 << 31:
            raise BusinessError("session_binding")
        mac = wire.unb64(saved["mac"], 16)
        if len(mac) != 16 or not saved["jwt"] or any(c in saved["jwt"] for c in "\r\n"):
            raise BusinessError("session_binding")
        protocol = saved.get("protocol_version", "3.7.1")
        if (
            not isinstance(protocol, str)
            or protocol not in {"3.7.1", "3.7.3", "2.8.3"}
            or saved.get("security_required", False) is not False
        ):
            raise BusinessError("session_binding")
        self.fields[0], self.fields[3], self.fields[4] = (
            saved["device"],
            saved["uid"],
            saved["kind"],
        )
        self.jwt, self.mac = saved["jwt"], mac
        self.protocol_version = protocol
        self.auth_error = ""
        if protocol == "3.7.3":
            self.fields[1:3] = [3, 1]
        elif protocol == "2.8.3":
            self.fields[1:3] = [1, 1]
        else:
            self.fields[1:3] = self.deployment.metadata[1:3]
