"""Local AstrBot statistics for the budget-guarded, tool-free model transport."""

import time
from contextlib import asynccontextmanager

import aiohttp

from astrbot.core import db_helper, logger


@asynccontextmanager
async def track_model_call(
    umo, provider_id, provider_model, *, conversation_id=None, span=None
):
    """Record one emitted HTTP request in AstrBot's existing provider statistics.

    Args:
        umo: Native AstrBot conversation origin, without message contents.
        provider_id: Configured AstrBot model provider identifier.
        provider_model: Model selected at admission, not a supplier-controlled label.
        conversation_id: Native conversation identifier for dashboard correlation.
        span: Native AstrBot trace; no prompts, knowledge text or credentials.

    Yields:
        Trace configuration and a measurement populated from the response receipt.
        Non-streamed calls leave TTFT unset; costs remain in the protected ledger.
    """
    measurement = {
        "sent_at": 0,
        "ended_at": 0,
        "headers_at": 0,
        "http_status": None,
        "error_type": None,
        "usage": None,
        "completed": False,
    }
    trace = aiohttp.TraceConfig()

    async def sent(_session, _context, _params):
        measurement["sent_at"] = time.time()
        if span is not None:
            try:
                span.record(
                    "wsl_model_request",
                    conversation_id=conversation_id,
                    provider_id=provider_id,
                    provider_model=provider_model,
                    tools_enabled=False,
                )
            except Exception:
                logger.warning("[Wangshangliao] Native model request trace failed.")

    async def headers_received(_session, _context, params):
        measurement["headers_at"] = time.time()
        measurement["http_status"] = params.response.status

    trace.on_request_headers_sent.append(sent)
    trace.on_request_end.append(headers_received)
    try:
        yield trace, measurement
    except BaseException as exc:
        measurement["error_type"] = type(exc).__name__
        raise
    finally:
        ended = measurement["ended_at"] or time.time()
        if measurement["sent_at"]:
            usage = measurement["usage"]
            tokens = {"input_other": 0, "input_cached": 0, "output": 0}
            valid = isinstance(usage, dict)
            if valid:
                inputs = usage.get("prompt_tokens")
                outputs = usage.get("completion_tokens")
                details = usage.get("prompt_tokens_details")
                cached = (
                    details.get("cached_tokens", 0)
                    if isinstance(details, dict)
                    else usage.get("prompt_cache_hit_tokens", 0)
                )
                valid = (
                    all(
                        type(n) is int and 0 <= n < 2**63
                        for n in (inputs, outputs, cached)
                    )
                    and cached <= inputs
                    and inputs + outputs < 2**63
                    and (
                        "total_tokens" not in usage
                        or type(usage["total_tokens"]) is int
                        and usage["total_tokens"] == inputs + outputs
                    )
                    and (
                        "prompt_cache_hit_tokens" not in usage
                        or type(usage["prompt_cache_hit_tokens"]) is int
                        and usage["prompt_cache_hit_tokens"] == cached
                    )
                )
                if valid:
                    tokens = {
                        "input_other": inputs - cached,
                        "input_cached": cached,
                        "output": outputs,
                    }
            if measurement["completed"] and not valid:
                logger.warning(
                    "[Wangshangliao] Model usage receipt missing or invalid; "
                    "recording the call without estimated tokens."
                )
            measurement["total_tokens"] = sum(tokens.values()) if valid else None
            try:
                await db_helper.insert_provider_stat(
                    umo=umo,
                    conversation_id=conversation_id,
                    provider_id=provider_id,
                    provider_model=provider_model,
                    agent_type="internal",
                    status="completed"
                    if measurement["completed"] and valid
                    else "error",
                    stats={
                        "token_usage": tokens,
                        "start_time": measurement["sent_at"],
                        "end_time": ended,
                    },
                )
            except Exception as exc:
                logger.warning(
                    "[Wangshangliao] Persist provider statistics failed (%s).",
                    type(exc).__name__,
                )
            if span is not None:
                try:
                    span.record(
                        "wsl_model_result",
                        conversation_id=conversation_id,
                        status="completed"
                        if measurement["completed"] and valid
                        else "error",
                        token_usage=tokens,
                        duration_ms=max(
                            0, round((ended - measurement["sent_at"]) * 1000)
                        ),
                        response_headers_ms=(
                            max(
                                0,
                                round(
                                    (measurement["headers_at"] - measurement["sent_at"])
                                    * 1000
                                ),
                            )
                            if measurement["headers_at"]
                            else None
                        ),
                        http_status=measurement["http_status"],
                        error_type=measurement["error_type"],
                        failure_phase=(
                            "response"
                            if measurement["headers_at"]
                            else "awaiting_response_headers"
                        )
                        if measurement["error_type"]
                        else None,
                    )
                except Exception:
                    logger.warning("[Wangshangliao] Native model result trace failed.")
