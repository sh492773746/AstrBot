"""Synthetic native SDK attempts; these tests never call a remote provider."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from google import genai
from google.genai import types
from test_lajiaoliao_commerce import NOW, RATE, prepared

from astrbot.core.provider.event_guard import AIRequestDenied, generation_meter
from data.plugins.astrbot_plugin_lajiaoliao.metering import (
    NativeMeter,
    gemini_usage,
    metering_context,
    provider_fingerprint,
)
from data.plugins.astrbot_plugin_lajiaoliao.storage import SessionError


def response(**changes):
    values = {
        "prompt_token_count": 100,
        "cached_content_token_count": 20,
        "candidates_token_count": 50,
        "thoughts_token_count": 25,
        "total_token_count": 175,
    }
    values.update(changes)
    return types.GenerateContentResponse(
        usage_metadata=types.GenerateContentResponseUsageMetadata(**values),
        candidates=[types.Candidate(finish_reason="STOP")],
    )


class FakeCommerce:
    def __init__(self, store, customer, period):
        self.store, self.customer, self.period = store, customer, period
        self.snapshot = store.snapshot()

    def require(self, instance, *, ai=False):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM commerce_customers WHERE id=?", (self.customer,)
            ).fetchone()
            period = db.execute(
                "SELECT * FROM commerce_periods WHERE id=?", (self.period,)
            ).fetchone()
            if row["ai_blocked"] or row["state"] != "active":
                raise SessionError("commerce_ai_paused")
            return {"customer_id": self.customer, "period": dict(period)}

    async def db(self, method, *args, **kwargs):
        if method in {"reserve", "dispatch", "settle"}:
            kwargs["now"] = NOW
        return getattr(self.store, method)(*args, **kwargs)

    async def refresh(self):
        self.snapshot = self.store.snapshot()


def fixture(tmp_path):
    store, customer, period = prepared(tmp_path)
    provider = SimpleNamespace(
        provider_config={"type": "googlegenai_chat_completion", "id": "native"},
        api_base="https://synthetic.invalid",
        chosen_api_key="test-key",
        get_model=lambda: "synthetic",
        client=SimpleNamespace(
            _api_client=SimpleNamespace(vertexai=False),
            models=SimpleNamespace(
                count_tokens=AsyncMock(return_value=SimpleNamespace(total_tokens=1000)),
                generate_content=AsyncMock(return_value=response()),
                generate_content_stream=AsyncMock(),
            ),
        ),
    )
    fingerprint = provider_fingerprint(provider)
    with store.connect() as db:
        db.execute("UPDATE commerce_rates SET fingerprint=?", (fingerprint,))
        snapshot = {
            json.dumps(["native", "synthetic", fingerprint]): {
                **RATE,
                "rate_id": "test-rate",
                "version": 1,
            }
        }
        db.execute(
            "UPDATE commerce_periods SET rate_snapshot=? WHERE id=?",
            (json.dumps(snapshot), period),
        )
    extras = {}
    event = SimpleNamespace(
        platform=SimpleNamespace(session_ref="ref"),
        message_obj=SimpleNamespace(message_id="message"),
        get_group_id=lambda: "g",
        get_sender_id=lambda: "u",
        get_extra=lambda key, default=None: extras.get(key, default),
        set_extra=lambda key, value: extras.__setitem__(key, value),
        stop_event=lambda: extras.__setitem__("stopped", True),
    )
    plugin = SimpleNamespace(
        commerce=FakeCommerce(store, customer, period),
        store=SimpleNamespace(_read=lambda ref: {"instance_id": "bot"}),
        ai=SimpleNamespace(authorize=AsyncMock()),
        business=SimpleNamespace(instance=lambda *args: None),
    )
    return plugin, event, provider, store


def test_usage_cached_thoughts_explicit_zero():
    assert gemini_usage(response()) == {
        "ordinary": 80,
        "cached": 20,
        "candidates": 50,
        "thinking": 25,
    }
    assert gemini_usage(
        response(
            prompt_token_count=0,
            cached_content_token_count=0,
            candidates_token_count=0,
            thoughts_token_count=0,
            total_token_count=0,
        )
    ) == {
        "ordinary": 0,
        "cached": 0,
        "candidates": 0,
        "thinking": 0,
    }
    assert gemini_usage(
        response(tool_use_prompt_token_count=3, total_token_count=178)
    ) == {
        "ordinary": 83,
        "cached": 20,
        "candidates": 50,
        "thinking": 25,
        "tool_input": 3,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"thoughts_token_count": None},
        {"cached_content_token_count": None},
        {"total_token_count": None},
        {"prompt_token_count": None},
        {"candidates_token_count": None},
        {"cached_content_token_count": 101},
        {"total_token_count": 200},
        {"tool_use_prompt_token_count": 3},
    ],
)
def test_missing_and_inconsistent_usage_unknown(changes):
    assert gemini_usage(response(**changes)) is None
    assert gemini_usage(None) is None


@pytest.mark.asyncio
async def test_exact_final_payload_local_cap_no_global_mutation(tmp_path):
    plugin, event, provider, store = fixture(tmp_path)
    meter = NativeMeter(plugin, event, provider, "chat", "bot")
    config = types.GenerateContentConfig(
        max_output_tokens=8192, system_instruction="Synthetic persona"
    )
    result = await meter.generate(provider, "synthetic", ["final contents"], config)
    assert result.usage_metadata
    assert config.max_output_tokens == 8192 and config.http_options is None
    request = provider.client.models.generate_content.call_args.kwargs
    assert request["contents"] == ["final contents"]
    assert request["config"].max_output_tokens == 4096
    assert request["config"].http_options.retry_options.attempts == 1
    counted = provider.client.models.count_tokens.call_args.kwargs
    complete = counted["config"].http_options.extra_body["generateContentRequest"]
    assert complete["systemInstruction"]["parts"][0]["text"] == "Synthetic persona"
    assert complete["contents"][0]["parts"][0]["text"] == "final contents"
    assert counted["contents"] == " "
    plugin.ai.authorize.assert_awaited_once_with(event, "chat", commit=True)
    with store.connect() as db:
        call = db.execute("SELECT * FROM commerce_calls").fetchone()
        assert call["state"] == "settled"
        assert call["cost_nano"] == 457_000


@pytest.mark.asyncio
async def test_preflight_rejection_no_daily_charge_or_generation(tmp_path):
    plugin, event, provider, store = fixture(tmp_path)
    meter = NativeMeter(plugin, event, provider, "chat", "bot")
    provider.client.models.count_tokens.return_value = SimpleNamespace(
        total_tokens=32769
    )
    with pytest.raises(AIRequestDenied):
        await meter.generate(
            provider, "synthetic", ["test"], types.GenerateContentConfig()
        )
    plugin.ai.authorize.assert_not_awaited()
    provider.client.models.generate_content.assert_not_awaited()
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM commerce_calls").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_daily_rejection_releases_unsent_reservation(tmp_path):
    plugin, event, provider, store = fixture(tmp_path)
    plugin.ai.authorize.side_effect = AIRequestDenied("daily-limit")
    meter = NativeMeter(plugin, event, provider, "chat", "bot")
    with pytest.raises(AIRequestDenied):
        await meter.generate(
            provider, "synthetic", ["test"], types.GenerateContentConfig()
        )
    provider.client.models.generate_content.assert_not_awaited()
    with store.connect() as db:
        call = db.execute("SELECT * FROM commerce_calls").fetchone()
        assert call["state"] == "released"
        assert call["cost_nano"] == 0


@pytest.mark.asyncio
async def test_request_failure_unknown_keeps_reservation_no_retry(tmp_path):
    plugin, event, provider, store = fixture(tmp_path)
    provider.client.models.generate_content.side_effect = RuntimeError(
        "synthetic failure"
    )
    meter = NativeMeter(plugin, event, provider, "chat", "bot")
    with pytest.raises(AIRequestDenied, match="ai_assistant_unavailable"):
        await meter.generate(
            provider, "synthetic", ["test"], types.GenerateContentConfig()
        )
    assert provider.client.models.generate_content.await_count == 1
    with store.connect() as db:
        call = db.execute("SELECT * FROM commerce_calls").fetchone()
        assert call["state"] == "unknown" and call["held_nano"] > 0
    with pytest.raises(SessionError):
        plugin.commerce.require("bot", ai=True)


@pytest.mark.asyncio
async def test_source_switch_denied_before_accounting(tmp_path):
    plugin, event, provider, _ = fixture(tmp_path)
    meter = NativeMeter(plugin, event, provider, "chat", "bot")
    provider.chosen_api_key = "another-key"
    with pytest.raises(AIRequestDenied):
        await meter.generate(
            provider, "synthetic", ["test"], types.GenerateContentConfig()
        )
    provider.client.models.count_tokens.assert_not_awaited()
    plugin.ai.authorize.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_output_images_or_multiple_candidates(tmp_path):
    plugin, event, provider, _ = fixture(tmp_path)
    meter = NativeMeter(plugin, event, provider, "chat", "bot")
    for config in (
        types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"]),
        types.GenerateContentConfig(candidate_count=2),
        types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())]
        ),
    ):
        with pytest.raises(AIRequestDenied):
            await meter.generate(provider, "synthetic", [], config)
    provider.client.models.generate_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_stream_completed_and_early_cancel(tmp_path):
    plugin, event, provider, store = fixture(tmp_path)
    meter = NativeMeter(plugin, event, provider, "chat", "bot")

    async def completed():
        yield response()

    provider.client.models.generate_content_stream.return_value = completed()
    stream = await meter.stream(
        provider, "synthetic", ["test"], types.GenerateContentConfig()
    )
    chunks = [chunk async for chunk in stream]
    assert len(chunks) == 1 and not meter.streams
    with store.connect() as db:
        assert db.execute("SELECT state FROM commerce_calls").fetchone()[0] == "settled"

    async def incomplete():
        yield types.GenerateContentResponse(candidates=[])

    provider.client.models.generate_content_stream.return_value = incomplete()
    stream = await meter.stream(
        provider, "synthetic", ["test"], types.GenerateContentConfig()
    )
    await stream.__anext__()
    with pytest.raises(AIRequestDenied):
        await stream.aclose()
    with store.connect() as db:
        assert (
            db.execute("SELECT state FROM commerce_calls WHERE sequence=2").fetchone()[
                0
            ]
            == "unknown"
        )


@pytest.mark.asyncio
async def test_scope_failure_generic_and_no_context_leak(tmp_path):
    plugin, event, provider, _ = fixture(tmp_path)
    with pytest.raises(AIRequestDenied, match="ai_assistant_unavailable"):
        async with metering_context(plugin, event, provider, "chat"):
            assert generation_meter.get() is not None
            raise RuntimeError("private-source-error")
    assert generation_meter.get() is None
    assert event.get_extra("stopped")


@pytest.mark.asyncio
async def test_internal_instances_keep_existing_behavior(tmp_path):
    plugin, event, provider, _ = fixture(tmp_path)
    plugin.commerce.require = lambda *args, **kwargs: None
    async with metering_context(plugin, event, provider, "chat"):
        assert generation_meter.get() is None
    provider.client.models.count_tokens.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_sdk_http_payload_includes_persona_tools_and_image(tmp_path):
    plugin, event, provider, store = fixture(tmp_path)
    requests = []

    def transport(request):
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        if request.url.path.endswith(":countTokens"):
            return httpx.Response(200, json={"totalTokens": 1000})
        assert request.url.path.endswith(":generateContent")
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "role": "model",
                            "parts": [{"text": "synthetic result"}],
                        },
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 100,
                    "cachedContentTokenCount": 20,
                    "candidatesTokenCount": 50,
                    "thoughtsTokenCount": 25,
                    "totalTokenCount": 175,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        client = genai.Client(
            api_key="synthetic-test-key",
            http_options=types.HttpOptions(
                base_url="https://synthetic.invalid",
                httpx_async_client=http,
            ),
        )
        provider.client = client.aio
        contents = [
            types.Content(
                role="user",
                parts=[
                    types.Part(text="synthetic text"),
                    types.Part(
                        inline_data=types.Blob(
                            mime_type="image/png", data=b"synthetic-bytes"
                        )
                    ),
                ],
            )
        ]
        config = types.GenerateContentConfig(
            system_instruction="synthetic persona",
            max_output_tokens=8192,
            tools=[
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name="fixed_status", description="Read current status"
                        ),
                    ]
                )
            ],
        )
        meter = NativeMeter(plugin, event, provider, "chat", "bot")
        await meter.generate(provider, "synthetic", contents, config)
        assert len(requests) == 2
        counted = requests[0][1]["generateContentRequest"]
        generated = requests[1][1]
        assert counted["model"] == "models/synthetic"
        assert {
            key: value for key, value in counted.items() if key != "model"
        } == generated
        assert generated["generationConfig"]["maxOutputTokens"] == 4096
        assert generated["systemInstruction"]["parts"][0]["text"] == "synthetic persona"
        assert (
            generated["tools"][0]["functionDeclarations"][0]["name"] == "fixed_status"
        )
        assert (
            generated["contents"][0]["parts"][1]["inlineData"]["data"]
            == "c3ludGhldGljLWJ5dGVz"
        )
        await client.aio.aclose()
    with store.connect() as db:
        assert db.execute("SELECT state FROM commerce_calls").fetchone()[0] == "settled"


@pytest.mark.asyncio
async def test_review_revoked_during_count_releases_without_generation(tmp_path):
    plugin, event, provider, store = fixture(tmp_path)
    event.is_stopped = lambda: False
    plugin.business.settings = AsyncMock(
        return_value={
            "settings": {
                "review_enabled": False,
                "review_provider_id": "native",
                "manager_user_ids": [],
            }
        }
    )
    meter = NativeMeter(plugin, event, provider, "review", "bot")
    with pytest.raises(AIRequestDenied, match="review_not_authorized"):
        await meter.generate(
            provider, "synthetic", ["test"], types.GenerateContentConfig()
        )
    provider.client.models.generate_content.assert_not_awaited()
    plugin.ai.authorize.assert_not_awaited()
    with store.connect() as db:
        assert (
            db.execute("SELECT state FROM commerce_calls").fetchone()[0] == "released"
        )


@pytest.mark.asyncio
async def test_actual_sdk_503_has_one_attempt_and_unknown_hold(tmp_path):
    plugin, event, provider, store = fixture(tmp_path)
    generated = []

    def transport(request):
        if request.url.path.endswith(":countTokens"):
            return httpx.Response(200, json={"totalTokens": 1000})
        generated.append(request)
        return httpx.Response(
            503,
            json={
                "error": {
                    "code": 503,
                    "status": "UNAVAILABLE",
                    "message": "synthetic failure",
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        client = genai.Client(
            api_key="synthetic",
            http_options=types.HttpOptions(
                base_url="https://synthetic.invalid",
                httpx_async_client=http,
            ),
        )
        provider.client = client.aio
        meter = NativeMeter(plugin, event, provider, "chat", "bot")
        with pytest.raises(AIRequestDenied, match="ai_assistant_unavailable"):
            await meter.generate(
                provider, "synthetic", ["test"], types.GenerateContentConfig()
            )
        assert len(generated) == 1
        await client.aio.aclose()
    with store.connect() as db:
        assert db.execute("SELECT state FROM commerce_calls").fetchone()[0] == "unknown"
        assert db.execute("SELECT held_nano FROM commerce_periods").fetchone()[0] > 0


@pytest.mark.asyncio
async def test_native_recitation_does_not_retry_paid_generation():
    from astrbot.core.provider.sources.gemini_source import ProviderGoogleGenAI

    provider = ProviderGoogleGenAI.__new__(ProviderGoogleGenAI)
    provider.provider_config = {}
    provider.get_model = lambda: "synthetic"
    provider._prepare_conversation = AsyncMock(return_value=["test"])
    provider._prepare_query_config = AsyncMock(
        return_value=types.GenerateContentConfig()
    )
    meter = SimpleNamespace(
        generate=AsyncMock(
            return_value=SimpleNamespace(
                candidates=[
                    SimpleNamespace(finish_reason=types.FinishReason.RECITATION)
                ],
            )
        )
    )
    token = generation_meter.set(meter)
    try:
        with pytest.raises(AIRequestDenied, match="ai_assistant_unavailable"):
            await provider._query({"messages": []}, None)
        meter.generate.assert_awaited_once()
    finally:
        generation_meter.reset(token)


@pytest.mark.asyncio
async def test_cancel_during_reservation_releases_committed_unstarted_budget(tmp_path):
    plugin, event, provider, store = fixture(tmp_path)
    db = plugin.commerce.db
    committed, allow_return = asyncio.Event(), asyncio.Event()

    async def delayed(method, *args, **kwargs):
        result = await db(method, *args, **kwargs)
        if method == "reserve":
            committed.set()
            await allow_return.wait()
        return result

    plugin.commerce.db = delayed
    meter = NativeMeter(plugin, event, provider, "chat", "bot")
    task = asyncio.create_task(
        meter.generate(
            provider,
            "synthetic",
            ["test"],
            types.GenerateContentConfig(),
        )
    )
    await committed.wait()
    task.cancel()
    allow_return.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    provider.client.models.generate_content.assert_not_awaited()
    plugin.ai.authorize.assert_not_awaited()
    with store.connect() as connection:
        assert (
            connection.execute("SELECT state FROM commerce_calls").fetchone()[0]
            == "released"
        )
        assert (
            connection.execute("SELECT held_nano FROM commerce_periods").fetchone()[0]
            == 0
        )
