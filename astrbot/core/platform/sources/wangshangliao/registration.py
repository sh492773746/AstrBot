"""Owner-bound, expiring native login transactions."""

import asyncio
import math
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
    security_code: SecretStr = SecretStr("")


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
    error_action: str = ""
    provider_code: int | None = None
    sms_resend_at: float = 0
    security_retry_at: float = 0
    session_ref: str = ""
    account_id: str = ""
    nickname: str = ""
    saved: dict = field(default_factory=dict, repr=False)
    challenge: dict = field(default_factory=dict, repr=False)
    pending_login: dict = field(default_factory=dict, repr=False)
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
            "resend_after": max(0, math.ceil(self.sms_resend_at - time.monotonic())),
            "security_retry_after": max(
                0, math.ceil(self.security_retry_at - time.monotonic())
            ),
            "captcha_id": self.client.deployment.captcha_id,
            "account_id": self.account_id,
            "nickname": self.nickname,
            "session_ref": self.session_ref,
            "error": self.error,
            "error_action": self.error_action,
            "provider_code": self.provider_code,
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
            tx.pending_login.clear()
            tx.client.jwt = ""
            tx.client.security_required = False
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
            or len(data.security_code.get_secret_value()) > 6
            or (
                data.security_code.get_secret_value()
                and data.action != "verify_security_code"
            )
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
        if data.action in {"verify_security_code", "retry_connection"}:
            async with tx.lock:
                expected = (
                    "security_required"
                    if data.action == "verify_security_code"
                    else "connection_pending"
                )
                if tx.state != expected or not tx.pending_login:
                    raise ProtocolError("registration_conflict")
                if time.monotonic() < tx.security_retry_at:
                    raise ProtocolError("security_code_cooldown")
                tx.error = ""
                tx.provider_code = None
                tx.security_retry_at = time.monotonic() + 3
                submitted = False
                verification_finished = False
                try:
                    if data.action == "verify_security_code":
                        digits = data.security_code.get_secret_value()
                        if (
                            len(digits) != 6
                            or not digits.isascii()
                            or not digits.isdigit()
                        ):
                            raise ProtocolError("security_code_input")
                        tx.state = "security_verifying"
                        submitted = True
                        await tx.client.verify_security_code(digits)
                        verification_finished = True
                    await self._complete_login(code, tx, tx.pending_login)
                except ProtocolError as exc:
                    if self.transactions.get(code) is not tx:
                        raise ProtocolError("registration_cancelled") from None
                    tx.error = str(exc)
                    if isinstance(exc, BusinessError):
                        tx.provider_code = exc.number or None
                    if tx.error == "secondary_security_code_invalid":
                        tx.state = "security_required"
                        tx.security_retry_at = time.monotonic() + 30
                    elif tx.error == "secondary_security_locked":
                        tx.state = "security_locked"
                    elif tx.error in {
                        "secondary_security_code_required",
                        "secondary_security_phone_required",
                    }:
                        tx.state = "security_setup_required"
                    elif tx.error == "secondary_security_unverified":
                        tx.client.security_required = True
                        tx.state = "security_required"
                    elif tx.error in {"reauth_required", "account_clearing"}:
                        tx.state = "failed"
                        tx.pending_login.clear()
                    elif (
                        submitted
                        and not verification_finished
                        and (
                            tx.error
                            in {
                                "transport",
                                "security_rotation_missing",
                                "response_invalid",
                            }
                            or getattr(exc, "submitted", False)
                        )
                    ):
                        tx.state = "security_uncertain"
                    elif tx.error == "security_code_input":
                        tx.state = expected
                    else:
                        tx.state = (
                            "connection_pending"
                            if not tx.client.security_required
                            else expected
                        )
                except Exception:
                    tx.error = "registration_failed"
                    tx.state = (
                        "connection_pending"
                        if verification_finished
                        else "security_uncertain"
                        if submitted
                        else expected
                    )
                finally:
                    data.security_code = SecretStr("")
                tx.error_action = data.action if tx.error else ""
                tx.diagnostics.emit(
                    "login",
                    tx.state,
                    failed=bool(tx.error),
                    error=tx.error,
                    route="verify_safe_code" if submitted else "nim_token",
                    action=data.action,
                    provider_code=tx.provider_code,
                    aggregate=False,
                )
                return tx.view(code)
        if tx.state in {
            "security_required",
            "security_locked",
            "security_setup_required",
            "security_uncertain",
            "connection_pending",
        }:
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
            tx.error_action = ""
            tx.provider_code = None
            tx.state = "verifying"
            tx.diagnostics.emit("login", "started", action=data.action)
            result = None
            route = "none"
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
                if data.action in {"verify_sms", "resend_sms"} and (
                    not tx.challenge.get("key") or not tx.challenge.get("phone")
                ):
                    raise ProtocolError("sms_challenge")
                if (
                    data.action
                    in {
                        "request_sms",
                        "resend_sms",
                    }
                    and now < tx.sms_resend_at
                ):
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
                if not data.validate_str.get_secret_value():
                    raise ProtocolError("captcha_required")
                if data.action == "request_sms":
                    phone = {
                        "countryCode": 86,
                        "nationalNumber": int(data.account),
                        "maskedNationalNumber": "",
                    }
                    # A cooldown does not prove that an SMS challenge was issued.
                    tx.challenge.clear()
                    tx.sms_resend_at = time.monotonic() + 60
                    route = "sms_anon"
                    response = await tx.client.request(
                        "/v1/verify/sms-anon",
                        {
                            "ty": "VERIFY_FOR_LOGIN",
                            "phone": phone,
                            "validateStr": data.validate_str.get_secret_value(),
                        },
                    )
                    key = response.get("key") if isinstance(response, dict) else None
                    if not isinstance(key, str) or not 0 < len(key) <= 8192:
                        raise ProtocolError("sms_challenge")
                    tx.challenge = {"phone": phone, "key": key}
                    tx.state = "sms_required"
                elif data.action == "login":
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
                        tx.challenge.clear()
                        route = "user_login"
                        result = await tx.client.login(params)
                    except BusinessError as exc:
                        if exc.number != 1069:
                            raise
                        tx.sms_resend_at = time.monotonic() + 60
                        route = "change_device_verify"
                        challenge = await tx.client.request(
                            "/v1/user/get-change-device-verify",
                            {
                                "account": data.account,
                                "passwd": data.password.get_secret_value(),
                                "type": kind,
                            },
                        )
                        phone = (
                            challenge.get("phone")
                            if isinstance(challenge, dict)
                            else None
                        )
                        sms = (
                            challenge.get("sms")
                            if isinstance(challenge, dict)
                            else None
                        )
                        key = sms.get("key") if isinstance(sms, dict) else None
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
                        }
                        tx.state = "sms_required"
                    finally:
                        params.clear()
                else:
                    if data.action == "resend_sms":
                        tx.sms_resend_at = time.monotonic() + 60
                        route = "sms_anon"
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
                        key = (
                            response.get("key") if isinstance(response, dict) else None
                        )
                        if not isinstance(key, str) or not 0 < len(key) <= 8192:
                            raise ProtocolError("sms_challenge")
                        tx.challenge["key"] = key
                        tx.state = "sms_required"
                    else:
                        route = "user_login"
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
                    await self._complete_login(code, tx, result)
            except ProtocolError as exc:
                if self.transactions.get(code) is not tx:
                    raise ProtocolError("registration_cancelled") from None
                tx.error = str(exc)
                if isinstance(exc, BusinessError):
                    if type(exc.number) is int and -(1 << 31) <= exc.number < 1 << 31:
                        tx.provider_code = exc.number or None
                    if str(exc) == "rate_limited" and type(exc.retry_after) in (
                        int,
                        float,
                    ):
                        tx.sms_resend_at = max(
                            tx.sms_resend_at,
                            time.monotonic() + min(86400, max(1, exc.retry_after)),
                        )
                if tx.pending_login:
                    tx.state = (
                        "failed"
                        if str(exc) in {"reauth_required", "account_clearing"}
                        else "security_required"
                        if str(exc) == "secondary_security_unverified"
                        else "security_setup_required"
                        if str(exc)
                        in {
                            "secondary_security_code_required",
                            "secondary_security_phone_required",
                        }
                        else "connection_pending"
                    )
                    if tx.state == "security_required":
                        tx.client.security_required = True
                    if tx.state == "failed":
                        tx.pending_login.clear()
                else:
                    tx.state = "sms_required" if tx.challenge else "failed"
            except Exception:
                tx.error = "registration_failed"
                tx.state = (
                    "connection_pending"
                    if tx.pending_login
                    else "sms_required"
                    if tx.challenge
                    else "failed"
                )
            tx.error_action = data.action if tx.error else ""
            tx.diagnostics.emit(
                "login",
                tx.state,
                failed=bool(tx.error),
                error=tx.error,
                route=route,
                action=data.action,
                provider_code=tx.provider_code,
                aggregate=False,
            )
            return tx.view(code)

    async def _complete_login(self, code: str, tx: Transaction, result: dict) -> None:
        """Finish authentication before issuing a save capability.

        Args:
            code: Owner-bound transaction reference.
            tx: The still-current login transaction.
            result: Primary identity retained only until authentication ends.

        Raises:
            ProtocolError: If identity, expiry, verification or IM setup fails.
        """
        if self.transactions.get(code) is not tx:
            raise ProtocolError("registration_cancelled")
        if time.monotonic() >= tx.expires:
            raise ProtocolError("registration_expired")
        uid, nim_id = result.get("uid"), result.get("nimId")
        if (
            type(uid) is not int
            or not 0 < uid < 1 << 31
            or not isinstance(nim_id, (str, int))
            or isinstance(nim_id, bool)
            or not 0 < len(str(nim_id)) <= 128
        ):
            raise ProtocolError("login_identity")
        tx.account_id = str(uid)
        tx.nickname = str(
            result.get("nickName")
            or result.get("userNick")
            or result.get("nickname")
            or tx.nickname
        )[:128]
        tx.pending_login = {
            "uid": uid,
            "nimId": str(nim_id),
            "nimToken": result.get("nimToken", ""),
        }
        tx.session_ref = ""
        tx.saved.clear()
        tx.challenge.clear()
        tx.sms_resend_at = 0
        if getattr(tx.client, "security_required", False):
            tx.state = "security_required"
            return
        if getattr(tx.client, "protocol_version", "3.7.1") in {"3.7.3", "2.8.3"}:
            nim_token = await tx.client.refresh_nim_token()
        else:
            nim_token = result.get("nimToken")
        if not isinstance(nim_token, str) or not 0 < len(nim_token) <= 8192:
            raise ProtocolError("login_identity")
        if self.transactions.get(code) is not tx:
            raise ProtocolError("registration_cancelled")
        if time.monotonic() >= tx.expires:
            raise ProtocolError("registration_expired")
        tx.saved = {
            "business": tx.client.export(),
            "nim_id": str(nim_id),
            "nim_token": nim_token,
        }
        tx.session_ref = secrets.token_urlsafe(32)
        tx.pending_login.clear()
        tx.state = "authenticated"

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
