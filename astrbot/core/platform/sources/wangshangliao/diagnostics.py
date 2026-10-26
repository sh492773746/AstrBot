"""Bounded, payload-free diagnostics through the AstrBot logging pipeline."""

import hashlib
import logging
import re
import time
from collections import deque

from astrbot.core import logger

FAILURES: dict[tuple[str, str, str, bool], tuple[float, int]] = {}
MAX_FAILURE_WINDOWS = 512
ERROR_LABELS = frozenset(
    {
        "transport",
        "http_rejected",
        "business_rejected",
        "login_rejected_unspecified",
        "secondary_security_code_required",
        "secondary_security_unverified",
        "secondary_security_code_invalid",
        "secondary_security_locked",
        "secondary_security_phone_required",
        "account_clearing",
        "security_rotation_missing",
        "security_code_input",
        "security_code_cooldown",
        "login_binding",
        "login_identity",
        "rate_limited",
        "reauth_required",
        "account_conflict",
        "nim_kicked",
        "nim_disconnected",
        "nim_auth_rejected",
        "nim_auth_type_invalid",
        "nim_push_backpressure",
        "connection_failed",
        "startup_failed",
        "invalid_credentials",
        "invalid_sms",
        "phone_not_found",
        "sms_expired",
        "registration_failed",
        "registration_expired",
        "registration_cancelled",
        "registration_rate_limit",
        "captcha_required",
        "login_input",
        "sms_input",
        "sms_challenge",
        "sms_cooldown",
        "response_invalid",
        "response_shape",
        "refresh_shape",
        "sync_boundary",
        "account_already_active",
        "deployment_missing",
        "deployment_invalid",
        "message_key_missing",
        "message_fields",
        "message_trailing",
        "message_count",
        "message_decode",
        "message_unverified",
        "recall_notification",
        "notification",
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
        "card_rule_changed",
        "card_name_too_long",
        "card_name_collision",
        "card_result_unresolved",
        "card_automation_disabled",
        "moderation_permission",
        "moderation_identity",
        "member_pagination_incomplete",
        "members_shape",
        "member_identity",
        "member_cursor",
        "member_pagination",
        "member_limit",
        "body_limit",
        "response_limit",
        "body_authentication_or_compression",
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
        self.samples = {}
        self.sample_counts = {}
        self.gauges = {}

    def observe(self, metric, seconds):
        """Record bounded, payload-free timing samples for runtime acceptance."""
        if (
            metric
            not in {
                "queue_wait",
                "event_total",
                "model_review",
                "member_directory",
                "event_loop",
            }
            or not isinstance(seconds, (int, float))
            or not 0 <= seconds < 86400
        ):
            return
        if metric not in self.samples:
            self.samples[metric] = deque(maxlen=512)
        self.samples[metric].append(seconds * 1000)
        self.sample_counts[metric] = self.sample_counts.get(metric, 0) + 1

    def snapshot(self):
        """Return rolling timing percentiles and fixed operational gauges."""
        timings = {}
        for key, values in self.samples.items():
            ordered = sorted(values)
            timings[key] = {
                "count": self.sample_counts[key],
                "samples": len(ordered),
                "p50_ms": round(ordered[int((len(ordered) - 1) * 0.5)], 3),
                "p95_ms": round(ordered[int((len(ordered) - 1) * 0.95)], 3),
            }
        return {"timings": timings, "gauges": dict(self.gauges)}

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
        provider_code: int | None = None,
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
            provider_code: Bounded numeric provider status, never response text.
        """
        count = 1
        if stage == "semantic" and type(duration_ms) is int:
            self.observe("model_review", duration_ms / 1000)
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
        level = (
            logging.ERROR
            if terminal
            else logging.WARNING
            if failed
            else logging.DEBUG
            if (
                stage in {"receive", "process"}
                and outcome in {"received", "started", "completed"}
                or stage == "recall"
                and outcome == "observed"
            )
            else logging.INFO
        )
        if not logger.isEnabledFor(level):
            return
        logger.log(
            level,
            "Wangshangliao instance=%s stage=%s outcome=%s error=%s correlation=%s "
            "route=%s action=%s group=%s actor=%s session=%s duration_ms=%s "
            "provider_code=%s count=%s",
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
            provider_code
            if type(provider_code) is int and -(1 << 31) <= provider_code < 1 << 31
            else "-",
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
