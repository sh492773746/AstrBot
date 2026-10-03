"""Owner-bound, expiring native login transactions."""

import asyncio
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import aiohttp
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .business import BusinessClient, BusinessError, Deployment
from .captcha import CaptchaSolver
from .diagnostics import Diagnostics
from .wire import ProtocolError


class RegistrationInput(BaseModel):
    """Strict fields accepted by the password registration endpoint."""

    model_config = ConfigDict(extra="forbid")
    action: str
    instance_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    registration_code: str = Field(default="", max_length=128)
    account: str = Field(default="", max_length=128)
    password: SecretStr = SecretStr("")
    validate_str: SecretStr = SecretStr("")
    verification_code: SecretStr = SecretStr("")


@dataclass
class Transaction:
    """A backend-only transaction; secrets never enter its public view."""

    owner: str
    instance: str
    client: BusinessClient
    http: aiohttp.ClientSession
    expires: float
    state: str = "pending"
    error: str = ""
    session_ref: str = ""
    account_id: str = ""
    nickname: str = ""
    saved: dict = field(default_factory=dict, repr=False)
    challenge: dict = field(default_factory=dict, repr=False)
    attempts: list[float] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    timer: asyncio.TimerHandle | None = None
    diagnostics: Diagnostics = field(init=False, repr=False)

    def __post_init__(self):
        """Bind diagnostics to this transaction without retaining credentials."""
        self.diagnostics = Diagnostics(self.instance)

    def view(self, code: str) -> dict:
        """Return only public registration state and an opaque save capability."""
        return {
            "status": self.state,
            "registration_code": code,
            "instance_id": self.instance,
            "expires_in": max(0, int(self.expires - time.monotonic())),
            "resend_after": max(
                0, int(self.challenge.get("resend_at", 0) - time.monotonic())
            ),
            "captcha_id": self.client.deployment.captcha_id,
            "account_id": self.account_id,
            "nickname": self.nickname,
            "session_ref": self.session_ref,
            "error": self.error,
        }


class RegistrationManager:
    """Keep temporary sessions separate from saved platform configuration."""

    def __init__(
        self,
        deployment_loader: Callable = Deployment.load,
        client_factory: Callable = BusinessClient,
    ):
        self.transactions: dict[str, Transaction] = {}
        self.deployment_loader = deployment_loader
        self.client_factory = client_factory
        self.commit_lock = asyncio.Lock()
        self.solver = CaptchaSolver()

    async def discard(self, code: str) -> None:
        """Invalidate references before closing transport, including in-flight work."""
        tx = self.transactions.pop(code, None)
        if tx:
            tx.state = "cancelled"
            tx.session_ref = ""
            tx.saved.clear()
            tx.challenge.clear()
            if tx.timer:
                tx.timer.cancel()
            await tx.http.close()

    async def action(self, owner: str, payload: dict) -> dict:
        """Run an ownership-checked login action without returning provider secrets.

        Args:
            owner: Authenticated Dashboard principal supplied by the server.
            payload: Strict platform-specific input.

        Returns:
            Public transaction state.

        Raises:
            ProtocolError: On invalid ownership, expired transactions or bad input.
        """
        try:
            data = RegistrationInput.model_validate(payload)
        except Exception:
            raise ProtocolError("registration_input") from None
        if (
            not owner
            or len(data.password.get_secret_value()) > 1024
            or len(data.validate_str.get_secret_value()) > 8192
        ):
            raise ProtocolError("registration_input")
        if data.action == "start":
            if (
                sum(tx.owner == owner for tx in self.transactions.values()) >= 4
                or len(self.transactions) >= 64
            ):
                raise ProtocolError("registration_limit")
            deployment = self.deployment_loader()
            http = aiohttp.ClientSession()
            code = secrets.token_urlsafe(32)
            tx = Transaction(
                owner,
                data.instance_id,
                self.client_factory(deployment, http),
                http,
                time.monotonic() + 600,
            )
            self.transactions[code] = tx
            tx.timer = asyncio.get_running_loop().call_later(
                600, lambda: asyncio.create_task(self.discard(code))
            )
            return tx.view(code)
        code = data.registration_code
        tx = self.transactions.get(code)
        if not tx or tx.owner != owner or tx.instance != data.instance_id:
            raise ProtocolError("registration_not_found")
        if time.monotonic() >= tx.expires:
            await self.discard(code)
            raise ProtocolError("registration_expired")
        if data.action == "cancel":
            await self.discard(code)
            return {"status": "cancelled"}
        if data.action == "poll":
            return tx.view(code)
        if data.action == "groups":
            if tx.state != "authenticated":
                raise ProtocolError("reauth_required")
            from .directory import group_directory

            async with tx.lock:
                return await group_directory(tx.client, tx.account_id)
        if tx.lock.locked() or tx.state == "authenticated":
            raise ProtocolError("registration_conflict")
        if data.action not in {"login", "request_sms", "verify_sms", "resend_sms"}:
            raise ProtocolError("registration_action")
        async with tx.lock:
            now = time.monotonic()
            tx.attempts = [t for t in tx.attempts if now - t < 60]
            if len(tx.attempts) >= 5:
                raise ProtocolError("registration_rate_limit")
            tx.attempts.append(now)
            tx.error = ""
            tx.state = "verifying"
            tx.diagnostics.emit("login", "started")
            result = None
            try:
                # Reject local failures before spending time on remote verification.
                if data.action == "login" and (
                    not data.account or not data.password.get_secret_value()
                ):
                    raise ProtocolError("login_input")
                if data.action == "request_sms" and (
                    len(data.account) != 11
                    or not data.account.isascii()
                    or not data.account.isdigit()
                ):
                    raise ProtocolError("sms_input")
                if data.action in {"verify_sms", "resend_sms"} and not tx.challenge:
                    raise ProtocolError("sms_challenge")
                if data.action in {
                    "request_sms",
                    "resend_sms",
                } and now < tx.challenge.get("resend_at", 0):
                    raise ProtocolError("sms_cooldown")
                if data.action == "verify_sms":
                    digits = data.verification_code.get_secret_value()
                    if len(digits) != 6 or not digits.isascii() or not digits.isdigit():
                        raise ProtocolError("sms_input")
                if not data.validate_str.get_secret_value():
                    solver_url = os.environ.get(
                        "ASTRBOT_YIDUN_SOLVER_URL",
                        "builtin",
                    ).strip()
                    if solver_url:
                        deadline = min(time.monotonic() + 30, tx.expires)
                        for attempt in range(3):
                            remaining = deadline - time.monotonic()
                            if remaining <= 0 or self.transactions.get(code) is not tx:
                                break
                            try:
                                if solver_url == "builtin":
                                    candidate = await self.solver.solve(
                                        tx.client.deployment.captcha_id,
                                        os.environ.get(
                                            "ASTRBOT_YIDUN_REFERER",
                                            tx.client.deployment.origin.rstrip("/")
                                            + "/",
                                        ),
                                        remaining,
                                    )
                                    if candidate:
                                        data.validate_str = SecretStr(candidate)
                                        break
                                    if (
                                        attempt < 2
                                        and deadline - time.monotonic() > 0.3
                                    ):
                                        await asyncio.sleep(0.3)
                                    continue
                                timeout = aiohttp.ClientTimeout(
                                    total=remaining, connect=min(3, remaining)
                                )
                                async with tx.http.post(
                                    solver_url,
                                    json={
                                        "id": tx.client.deployment.captcha_id,
                                        "referer": os.environ.get(
                                            "ASTRBOT_YIDUN_REFERER",
                                            tx.client.deployment.origin.rstrip("/")
                                            + "/",
                                        ),
                                        "width": 320,
                                        "allow_intellisense_precheck": True,
                                    },
                                    timeout=timeout,
                                ) as response:
                                    if response.status == 200:
                                        solved = await response.json(content_type=None)
                                        candidate = (
                                            solved.get("data", {}).get("validate", "")
                                            if isinstance(solved, dict)
                                            and isinstance(solved.get("data"), dict)
                                            and solved.get("data", {}).get("result")
                                            is True
                                            else ""
                                        )
                                        if (
                                            isinstance(candidate, str)
                                            and 0 < len(candidate) <= 8192
                                        ):
                                            data.validate_str = SecretStr(candidate)
                            except (
                                aiohttp.ClientError,
                                asyncio.TimeoutError,
                                ValueError,
                                OSError,
                            ):
                                pass
                            if data.validate_str.get_secret_value():
                                break
                            if attempt < 2 and deadline - time.monotonic() > 0.3:
                                await asyncio.sleep(0.3)
                # Verification may finish after cancellation or transaction expiry.
                if self.transactions.get(code) is not tx:
                    raise ProtocolError("registration_cancelled")
                if time.monotonic() >= tx.expires:
                    raise ProtocolError("registration_expired")
                if data.action == "request_sms":
                    if not data.validate_str.get_secret_value():
                        raise ProtocolError("sms_input")
                    phone = {
                        "countryCode": "86",
                        "nationalNumber": data.account,
                        "maskedNationalNumber": data.account[:3]
                        + "****"
                        + data.account[-4:],
                    }
                    tx.challenge = {"phone": phone, "resend_at": now + 60}
                    response = await tx.client.request(
                        "/v1/verify/sms-anon",
                        {
                            "ty": "VERIFY_FOR_LOGIN",
                            "phone": phone,
                            "validateStr": data.validate_str.get_secret_value(),
                        },
                    )
                    key = response.get("key")
                    if not isinstance(key, str) or not 0 < len(key) <= 8192:
                        raise ProtocolError("sms_challenge")
                    tx.challenge["key"] = key
                    tx.state = "sms_required"
                elif data.action == "login":
                    if not data.validate_str.get_secret_value():
                        raise ProtocolError("login_input")
                    kind = (
                        "LOGIN_TYPE_PHONE_PWD"
                        if len(data.account) == 11
                        and data.account.isascii()
                        and data.account.isdigit()
                        else "LOGIN_TYPE_ACCOUNT_PWD"
                    )
                    params = {
                        "account": data.account,
                        "passwd": data.password.get_secret_value(),
                        "validateStr": data.validate_str.get_secret_value(),
                        "type": kind,
                    }
                    try:
                        result = await tx.client.login(params)
                    except BusinessError as exc:
                        if exc.number != 1069:
                            raise
                        challenge = await tx.client.request(
                            "/v1/user/get-change-device-verify",
                            {
                                "account": data.account,
                                "passwd": data.password.get_secret_value(),
                                "type": kind,
                            },
                        )
                        phone, key = challenge["phone"], challenge["sms"]["key"]
                        if (
                            not isinstance(phone, dict)
                            or not isinstance(key, str)
                            or not 0 < len(key) <= 8192
                        ):
                            raise ProtocolError("sms_challenge")
                        tx.challenge = {
                            "phone": phone,
                            "key": key,
                            "original_key": key,
                            "resend_at": now + 60,
                        }
                        tx.state = "sms_required"
                    finally:
                        params.clear()
                else:
                    if not data.validate_str.get_secret_value():
                        raise ProtocolError("sms_challenge")
                    if data.action == "resend_sms":
                        tx.challenge["resend_at"] = now + 60
                        response = await tx.client.request(
                            "/v1/verify/sms-anon",
                            {
                                "ty": "VERIFY_FOR_LOGIN",
                                "phone": tx.challenge["phone"],
                                **(
                                    {"Key": tx.challenge["original_key"]}
                                    if "original_key" in tx.challenge
                                    else {}
                                ),
                                "validateStr": data.validate_str.get_secret_value(),
                            },
                        )
                        key = response["key"]
                        if not isinstance(key, str) or not 0 < len(key) <= 8192:
                            raise ProtocolError("sms_challenge")
                        tx.challenge["key"] = key
                        tx.state = "sms_required"
                    else:
                        result = await tx.client.login(
                            {
                                "phone": tx.challenge["phone"],
                                "key": tx.challenge["key"],
                                "verificationCode": digits,
                                "validateStr": data.validate_str.get_secret_value(),
                                "type": "LOGIN_TYPE_SMS",
                            }
                        )
                if self.transactions.get(code) is not tx:
                    raise ProtocolError("registration_cancelled")
                if result is not None:
                    uid, nim_id, nim_token = (
                        result["uid"],
                        str(result["nimId"]),
                        result["nimToken"],
                    )
                    if not nim_id or not isinstance(nim_token, str) or not nim_token:
                        raise ProtocolError("login_identity")
                    tx.account_id = str(uid)
                    tx.nickname = str(
                        result.get("userNick") or result.get("nickname") or ""
                    )[:128]
                    tx.saved = {
                        "business": tx.client.export(),
                        "nim_id": nim_id,
                        "nim_token": nim_token,
                    }
                    tx.session_ref = secrets.token_urlsafe(32)
                    tx.challenge.clear()
                    tx.state = "authenticated"
            except ProtocolError as exc:
                if self.transactions.get(code) is not tx:
                    raise ProtocolError("registration_cancelled") from None
                tx.error = str(exc)
                tx.state = "sms_required" if tx.challenge else "failed"
            except Exception:
                tx.error = "registration_failed"
                tx.state = "sms_required" if tx.challenge else "failed"
            tx.diagnostics.emit(
                "login", tx.state, failed=bool(tx.error), error=tx.error
            )
            return tx.view(code)

    def claim(self, owner: str, instance: str, reference: str) -> Transaction:
        """Validate a save capability; consume only after configuration is saved."""
        for tx in self.transactions.values():
            if (
                tx.owner == owner
                and tx.instance == instance
                and tx.state == "authenticated"
                and tx.expires > time.monotonic()
                and secrets.compare_digest(tx.session_ref, reference)
            ):
                return tx
        raise ProtocolError("session_reference_invalid")

    async def close(self) -> None:
        """Cancel all temporary registrations during Dashboard shutdown."""
        for code in list(self.transactions):
            await self.discard(code)
        await self.solver.close()


registrations = RegistrationManager()
