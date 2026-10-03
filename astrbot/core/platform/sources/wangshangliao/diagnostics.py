"""Bounded, payload-free diagnostics through the AstrBot logging pipeline."""

import hashlib
import logging
import re
import time

from astrbot.core import logger

FAILURES: dict[tuple[str, str, str, bool], tuple[float, int]] = {}
MAX_FAILURE_WINDOWS = 512
ERROR_LABELS = frozenset(
    {
        "transport",
        "http_rejected",
        "business_rejected",
        "rate_limited",
        "reauth_required",
        "account_conflict",
        "nim_kicked",
        "nim_disconnected",
        "connection_failed",
        "startup_failed",
        "invalid_credentials",
        "invalid_sms",
        "sms_expired",
        "registration_failed",
        "response_invalid",
        "response_shape",
        "refresh_shape",
        "sync_boundary",
        "account_already_active",
        "deployment_missing",
        "deployment_invalid",
        "message_key_missing",
        "receipt_shape",
        "permission_denied",
        "validation_failed",
        "provider_unavailable",
        "model_timeout",
        "model_response_invalid",
        "operation_failed",
        "result_unknown",
        "configuration_conflict",
        "confirmation_invalid",
    }
)


def reference(value: str) -> str:
    """Opaque correlation only; never put platform identities in log messages."""
    return hashlib.sha256(str(value).encode()).hexdigest()[:16] if value else "-"


def label(value: str) -> str:
    """Bound fixed internal labels and prevent multiline log injection."""
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value)
        else "other"
    )


class Diagnostics:
    """Share bounded failure windows across transactions for each instance."""

    def __init__(self, instance: str):
        self.instance = (
            instance
            if isinstance(instance, str)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", instance)
            else "ref_" + reference(instance)
        )

    def emit(
        self,
        stage: str,
        outcome: str,
        correlation: str = "",
        *,
        failed: bool = False,
        terminal: bool = False,
        error: str = "",
        route: str = "none",
        action: str = "none",
        group: str = "",
        actor: str = "",
        session: str = "",
        duration_ms: int | None = None,
        aggregate: bool = True,
    ) -> None:
        """Emit fixed operation labels without exception text or message payloads.

        Args:
            stage: Internal operation label, never provider text.
            outcome: Internal result label, never provider text.
            correlation: Internal identifier hashed before logging.
            failed: Whether to aggregate this failure for sixty seconds.
            terminal: Whether manual intervention is required.
            error: Error label checked against an allowlist before logging.
        """
        count = 1
        stage, outcome, route, action = map(label, (stage, outcome, route, action))
        error = error if error in ERROR_LABELS else "other" if error else "none"
        if failed and aggregate:
            now = time.monotonic()
            for expired in [
                key for key, value in FAILURES.items() if now - value[0] >= 60
            ]:
                if expired != (
                    self.instance,
                    stage,
                    error if error != "none" else outcome,
                    terminal,
                ):
                    self._flush(expired)
            key = (
                self.instance,
                stage,
                error if error != "none" else outcome,
                terminal,
            )
            previous, suppressed = FAILURES.get(key, (float("-inf"), 0))
            if now - previous < 60:
                FAILURES[key] = (previous, suppressed + 1)
                return
            count += suppressed
            if key not in FAILURES and len(FAILURES) >= MAX_FAILURE_WINDOWS:
                self._flush(min(FAILURES, key=lambda k: FAILURES[k][0]))
            FAILURES[key] = (now, 0)
        logger.log(
            logging.ERROR if terminal else logging.WARNING if failed else logging.INFO,
            "Wangshangliao instance=%s stage=%s outcome=%s error=%s correlation=%s "
            "route=%s action=%s group=%s actor=%s session=%s duration_ms=%s count=%s",
            self.instance,
            stage,
            outcome,
            error,
            reference(correlation),
            route,
            action,
            reference(group),
            reference(actor),
            reference(session),
            max(0, min(duration_ms, 86400000)) if type(duration_ms) is int else "-",
            count,
        )

    @staticmethod
    def _flush(key):
        """Report suppressed failures on expiry/eviction, without payload retention."""
        _, suppressed = FAILURES.pop(key)
        if suppressed:
            instance, stage, error, terminal = key
            logger.log(
                logging.ERROR if terminal else logging.WARNING,
                "Wangshangliao instance=%s stage=%s outcome=suppressed_summary "
                "error=%s correlation=- count=%s",
                instance,
                stage,
                error,
                suppressed,
            )
