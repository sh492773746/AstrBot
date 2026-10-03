"""Payload-free plugin diagnostics in AstrBot's existing log pipeline."""

import asyncio
import json
import time
from functools import wraps

from astrbot.core.platform.sources.wangshangliao.diagnostics import Diagnostics

ACTIONS = frozenset(
    {
        "groups",
        "select_group",
        "members",
        "search_members",
        "next_page",
        "permissions",
        "capabilities",
        "rules",
        "violations",
        "result",
        "mute",
        "unmute",
        "kick",
        "announce",
        "mute_all",
        "unmute_all",
        "card_preview",
        "card_member_preview",
        "card_execute",
        "card_status",
        "card_stop",
        "config_preview",
        "config_confirm",
    }
)
OUTCOMES = frozenset(
    {
        "rejected",
        "unknown",
        "query_failed",
        "query",
        "preview",
        "saved",
        "started",
        "not_confirmed",
        "schedule_confirmation_required",
        "queued",
        "running",
        "completed",
        "partial",
        "stopped",
        "needs_review",
        "accepted",
        "verified",
    }
)


def record(adapter, stage, outcome, correlation="", **fields):
    config = getattr(adapter, "config", {})
    Diagnostics(config.get("id", "unknown")).emit(stage, outcome, correlation, **fields)


def record_event(event, stage, outcome, **fields):
    """Only server identities, never message text, tool arguments or output."""
    record(
        getattr(event, "platform", None),
        stage,
        outcome,
        str(getattr(getattr(event, "message_obj", None), "message_id", "")),
        group=getattr(event, "get_group_id", lambda: "")(),
        actor=getattr(event, "get_sender_id", lambda: "")(),
        session=getattr(event, "unified_msg_origin", ""),
        **fields,
    )


def trace_management(callback):
    """Trace every tool exit, including guards, without changing authorization."""

    @wraps(callback)
    async def traced(self, event, action, value=""):
        if event.get_platform_name() != "wangshangliao":
            return await callback(self, event, action, value)
        started = time.monotonic()
        action_label = action if action in ACTIONS else "invalid"
        try:
            result = await callback(self, event, action, value)
        except asyncio.CancelledError:
            record_event(event, "admin_tool", "cancelled", action=action_label)
            raise
        except Exception:
            record_event(
                event,
                "admin_tool",
                "failed",
                action=action_label,
                failed=True,
                error="operation_failed",
                aggregate=False,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            raise
        try:
            data = json.loads(result)
            if not isinstance(data, dict):
                data = {}
        except (ValueError, TypeError):
            data = {}
        outcome = data.get("status")
        outcome = (
            outcome if isinstance(outcome, str) and outcome in OUTCOMES else "returned"
        )
        reason = data.get("reason")
        error = (
            "permission_denied"
            if reason
            in (
                "private_admin_required",
                "manual_kick_only",
                "card_private_admin_required",
                "authorization_or_identity_invalid",
            )
            else "configuration_conflict"
            if reason == "configuration_changed_preview_again"
            else "confirmation_invalid"
            if reason == "invalid_expired_or_nonexplicit_confirmation"
            else "validation_failed"
            if outcome == "rejected"
            else "result_unknown"
            if outcome in {"unknown", "not_confirmed", "needs_review"}
            else "operation_failed"
            if outcome == "query_failed"
            else ""
        )
        record_event(
            event,
            "admin_tool",
            outcome,
            action=action_label,
            error=error,
            failed=outcome
            in {"unknown", "not_confirmed", "needs_review", "query_failed"},
            aggregate=False,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return result

    return traced
