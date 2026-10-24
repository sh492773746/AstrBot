"""Native model statistics use real local HTTP receipts, never live model costs."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

from astrbot.builtin_stars.wangshangliao_moderation import model_stats
from astrbot.dashboard.services.stat_service import StatService
from tests.test_wangshangliao_group_ai import model_scope
from tests.test_wangshangliao_open_rpc_guards import rpc_scope as rpc_scope


@pytest_asyncio.fixture
async def metrics_headers_gate():
    return SimpleNamespace(received=asyncio.Event(), release=asyncio.Event())


@pytest_asyncio.fixture
async def metrics_model(metrics_headers_gate):
    payload = {
        "choices": [{"message": {"content": "local response"}}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "prompt_tokens_details": {"cached_tokens": 40},
        },
    }

    async def respond(request):
        body = await request.json()
        if body.get("delay_headers"):
            metrics_headers_gate.received.set()
            await metrics_headers_gate.release.wait()
        return web.json_response(payload)

    app = web.Application()
    app.router.add_post("/chat/completions", respond)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    try:
        yield url + "/chat/completions", payload
    finally:
        metrics_headers_gate.release.set()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_native_dashboard_counts_call_tokens_and_duration_not_fake_ttft(
    temp_db, metrics_model, monkeypatch
):
    monkeypatch.setattr(model_stats, "db_helper", temp_db)
    url, _ = metrics_model
    async with model_stats.track_model_call(
        "fixture:GroupMessage:99/5", "configured/deepseek", "mock-model"
    ) as (trace, measurement):
        async with aiohttp.ClientSession(trace_configs=[trace]) as http:
            async with http.post(url, json={"messages": []}) as response:
                data = await response.json()
                measurement.update(usage=data["usage"], completed=True)
    service = object.__new__(StatService)
    service.db_helper = temp_db
    service.config = {}
    result = await service.get_provider_token_stats(1)
    assert result["range_total_calls"] == 1
    assert result["range_total_tokens"] == 120
    assert result["range_success_rate"] == 1
    assert result["range_avg_duration_ms"] > 0
    assert result["range_avg_ttft_ms"] == 0
    assert result["range_by_umo"][0]["umo"] == "fixture:GroupMessage:99/5"


@pytest.mark.asyncio
async def test_real_group_ai_transport_records_native_statistics(
    rpc_scope, metrics_model, monkeypatch
):
    value, adapter, _ = rpc_scope
    url, _ = metrics_model
    resolve = value.resolve_model

    def local_model(selection=None):
        # Only replace the already-validated fixture transport with loopback HTTP.
        return {**resolve(selection), "base_url": url.removesuffix("/chat/completions")}

    monkeypatch.setattr(value, "resolve_model", local_model)
    row = await model_scope(value, adapter)
    await value.group_ai.run(adapter, row, "idle/0", "local synthetic topic")
    adapter.send_text.assert_awaited_once()
    writer = model_stats.db_helper.insert_provider_stat
    writer.assert_awaited_once()
    record = writer.call_args.kwargs
    assert record["umo"] == "rpc-offline:GroupMessage:99/5"
    assert record["provider_id"] == "existing/model"
    assert record["stats"]["token_usage"] == {
        "input_other": 60,
        "input_cached": 40,
        "output": 20,
    }
    assert record["status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [ValueError, aiohttp.InvalidURL, asyncio.CancelledError]
)
async def test_unsent_requests_never_count_as_model_calls(monkeypatch, error):
    writer = AsyncMock()
    monkeypatch.setattr(
        model_stats, "db_helper", SimpleNamespace(insert_provider_stat=writer)
    )
    with pytest.raises(error):
        async with model_stats.track_model_call("test", "provider", "model"):
            raise error("not dispatched")
    writer.assert_not_awaited()


@pytest.mark.asyncio
async def test_http_failure_after_headers_records_error_without_estimated_usage(
    monkeypatch, metrics_model
):
    writer = AsyncMock()
    monkeypatch.setattr(
        model_stats, "db_helper", SimpleNamespace(insert_provider_stat=writer)
    )
    url, _ = metrics_model
    with pytest.raises(asyncio.CancelledError):
        async with model_stats.track_model_call("test", "provider", "model") as (
            trace,
            _,
        ):
            async with aiohttp.ClientSession(trace_configs=[trace]) as http:
                async with http.post(url, json={}) as response:
                    await response.read()
                    raise asyncio.CancelledError
    row = writer.call_args.kwargs
    assert row["status"] == "error"
    assert sum(row["stats"]["token_usage"].values()) == 0
    assert "time_to_first_token" not in row["stats"]


@pytest.mark.asyncio
@pytest.mark.parametrize("before_headers", [False, True])
async def test_timeout_trace_identifies_phase_without_private_exception_text(
    monkeypatch, metrics_model, metrics_headers_gate, before_headers
):
    writer, span = AsyncMock(), Mock()
    monkeypatch.setattr(
        model_stats, "db_helper", SimpleNamespace(insert_provider_stat=writer)
    )
    url, _ = metrics_model
    with pytest.raises(TimeoutError):
        async with model_stats.track_model_call(
            "test", "provider", "model", span=span
        ) as (trace, _):
            async with aiohttp.ClientSession(
                trace_configs=[trace],
                timeout=aiohttp.ClientTimeout(total=None),
            ) as http:
                if before_headers:
                    pending = asyncio.create_task(
                        http.post(url, json={"delay_headers": True})
                    )
                    try:
                        await metrics_headers_gate.received.wait()
                        assert not pending.done()
                        await asyncio.wait_for(pending, timeout=0)
                    finally:
                        metrics_headers_gate.release.set()
                        pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)
                else:
                    async with http.post(url, json={}) as response:
                        await response.read()
                        raise TimeoutError("private credentials must not appear")
    result = span.record.call_args.kwargs
    assert span.record.call_args.args == ("wsl_model_result",)
    assert result["error_type"] == "TimeoutError"
    assert result["failure_phase"] == (
        "awaiting_response_headers" if before_headers else "response"
    )
    assert result["http_status"] == (None if before_headers else 200)
    assert (result["response_headers_ms"] is None) is before_headers
    assert "private" not in str(span.record.call_args_list)
    writer.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "usage",
    [
        None,
        {"prompt_tokens": True, "completion_tokens": 1},
        {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 999},
        {"prompt_tokens": 10, "completion_tokens": 1, "prompt_cache_hit_tokens": 11},
        {"prompt_tokens": 10, "completion_tokens": -1},
        {"prompt_tokens": 2**64, "completion_tokens": 1},
    ],
)
async def test_malformed_usage_is_not_guessed_or_recorded_as_success(
    monkeypatch, metrics_model, usage
):
    writer = AsyncMock()
    monkeypatch.setattr(
        model_stats, "db_helper", SimpleNamespace(insert_provider_stat=writer)
    )
    url, payload = metrics_model
    payload["usage"] = usage
    async with model_stats.track_model_call("test", "provider", "model") as (
        trace,
        sample,
    ):
        async with aiohttp.ClientSession(trace_configs=[trace]) as http:
            async with http.post(url, json={}) as response:
                data = await response.json()
                sample.update(usage=data["usage"], completed=True)
    row = writer.call_args.kwargs
    assert row["status"] == "error"
    assert sum(row["stats"]["token_usage"].values()) == 0


@pytest.mark.asyncio
async def test_stats_failure_does_not_retry_model_or_fail_the_reply(
    monkeypatch, metrics_model
):
    writer = AsyncMock(side_effect=RuntimeError("private error must not be logged"))
    monkeypatch.setattr(
        model_stats, "db_helper", SimpleNamespace(insert_provider_stat=writer)
    )
    url, _ = metrics_model
    async with model_stats.track_model_call("test", "provider", "model") as (
        trace,
        sample,
    ):
        async with aiohttp.ClientSession(trace_configs=[trace]) as http:
            async with http.post(url, json={}) as response:
                data = await response.json()
                sample.update(usage=data["usage"], completed=True)
    writer.assert_awaited_once()
