"""Request-local native Gemini accounting; no global provider mutation."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from contextlib import asynccontextmanager

from google.genai import _common, models, types

from astrbot.core.provider.event_guard import AIRequestDenied, generation_meter


def provider_fingerprint(provider):
    """Authenticate billing configuration without publishing a credential digest.

    Args:
        provider: Native Gemini provider with its selected API credential.

    Returns:
        A deterministic, domain-separated HMAC-SHA256 billing identifier.

    Raises:
        AIRequestDenied: If the provider has no usable signing credential.
    """
    config = provider.provider_config
    credential = getattr(provider, "chosen_api_key", None)
    if not isinstance(credential, str) or not credential.strip():
        raise AIRequestDenied("ai_assistant_unavailable")
    material = {
        "type": config.get("type"),
        "id": config.get("id"),
        "model": provider.get_model(),
        "base": getattr(provider, "api_base", config.get("api_base")),
        "proxy": config.get("proxy"),
        "headers": getattr(provider, "request_headers", {}),
    }
    return hmac.new(
        credential.encode(),
        b"lajiaoliao/provider-fingerprint/v2\0"
        + json.dumps(material, sort_keys=True, default=str).encode(),
        hashlib.sha256,
    ).hexdigest()


def gemini_usage(response):
    """Distinguish missing counters from explicit zero and avoid cache double count."""
    meta = getattr(response, "usage_metadata", None)
    names = (
        "prompt_token_count",
        "cached_content_token_count",
        "candidates_token_count",
        "thoughts_token_count",
        "total_token_count",
    )
    values = [getattr(meta, key, None) for key in names]
    if any(type(value) is not int or value < 0 for value in values):
        return None
    prompt, cached, candidates, thinking, total = values
    extra = getattr(meta, "tool_use_prompt_token_count", None)
    if extra is None and total == prompt + candidates + thinking:
        extra = 0
    if (
        type(extra) is not int
        or extra < 0
        or cached > prompt + extra
        or total != prompt + extra + candidates + thinking
    ):
        return None
    usage = {
        "ordinary": prompt + extra - cached,
        "cached": cached,
        "candidates": candidates,
        "thinking": thinking,
    }
    if extra:
        usage["tool_input"] = extra
    return usage


def count_config(client, model, contents, config):
    """Use the pinned SDK serializer for the complete official countTokens input."""
    parameters = types._GenerateContentParameters(
        model=model,
        contents=contents,
        config=config,
    )
    body = models._GenerateContentParameters_to_mldev(
        client._api_client,
        parameters,
        None,
        parameters,
    )
    body["model"] = body.pop("_url")["model"]
    body = _common.encode_unserializable_types(_common.convert_to_dict(body))
    return types.CountTokensConfig(
        http_options=types.HttpOptions(
            retry_options=types.HttpRetryOptions(attempts=1),
            extra_body={"generateContentRequest": body},
        )
    )


@asynccontextmanager
async def metering_context(plugin, event, provider, purpose):
    """Install a meter only for currently licensed, noninternal instances."""
    commerce = getattr(plugin, "commerce", None)
    if commerce is None:
        yield
        return
    row = plugin.store._read(event.platform.session_ref)
    try:
        binding = commerce.require(row["instance_id"], ai=True)
    except Exception:
        event.stop_event()
        raise AIRequestDenied("ai_assistant_unavailable") from None
    if binding is None:
        yield
        return
    if (
        provider is None
        or provider.provider_config.get("type") != "googlegenai_chat_completion"
    ):
        if purpose != "review":
            event.stop_event()
        raise AIRequestDenied("ai_assistant_unavailable")
    meter = NativeMeter(plugin, event, provider, purpose, row["instance_id"])
    token = generation_meter.set(meter)
    try:
        yield
    except asyncio.CancelledError:
        raise
    except Exception:
        if purpose != "review":
            event.stop_event()
        raise AIRequestDenied("ai_assistant_unavailable") from None
    finally:
        generation_meter.reset(token)
        await meter.close()


class NativeMeter:
    """Reserve once per real SDK attempt and settle raw usage before parsing."""

    def __init__(self, plugin, event, provider, purpose, instance):
        self.plugin, self.event, self.provider = plugin, event, provider
        self.purpose, self.instance = purpose, instance
        self.fingerprint = provider_fingerprint(provider)
        self.client = provider.client
        channel = str(event.get_group_id() or "private:" + event.get_sender_id())
        self.logical = hashlib.sha256(
            json.dumps(
                [
                    instance,
                    channel,
                    str(event.message_obj.message_id),
                ]
            ).encode()
        ).hexdigest()
        self.streams = set()

    async def prepare(self, provider, model, contents, config):
        if provider is not self.provider or model != provider.get_model():
            raise AIRequestDenied("ai_assistant_unavailable")
        if provider_fingerprint(provider) != self.fingerprint:
            raise AIRequestDenied("ai_assistant_unavailable")
        binding = self.plugin.commerce.require(self.instance, ai=True)
        rate_key = json.dumps([provider.provider_config["id"], model, self.fingerprint])
        if rate_key not in json.loads(binding["period"]["rate_snapshot"]):
            raise AIRequestDenied("ai_assistant_unavailable")
        config = config.model_copy(deep=True)
        if config.response_modalities and any(
            str(m) != "TEXT" for m in config.response_modalities
        ):
            raise AIRequestDenied("ai_output_not_supported")
        if config.candidate_count not in (None, 1):
            raise AIRequestDenied("ai_output_not_supported")
        if config.cached_content is not None:
            raise AIRequestDenied("ai_output_not_supported")
        for tool in config.tools or []:
            if any(
                value is not None
                for key, value in tool.model_dump().items()
                if key != "function_declarations"
            ):
                raise AIRequestDenied("ai_output_not_supported")
        config.candidate_count = 1
        config.max_output_tokens = min(config.max_output_tokens or 4096, 4096)
        if config.max_output_tokens <= 0:
            raise AIRequestDenied("ai_output_not_supported")
        thinking = getattr(config.thinking_config, "thinking_budget", None)
        if thinking is not None and thinking > config.max_output_tokens:
            raise AIRequestDenied("ai_output_not_supported")
        config.http_options = types.HttpOptions(
            retry_options=types.HttpRetryOptions(attempts=1)
        )
        try:
            count = await self.client.models.count_tokens(
                model=model,
                # The SDK requires nonempty contents; the API ignores this field
                # when the complete generateContentRequest is supplied.
                contents=" ",
                config=count_config(self.client, model, contents, config),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise AIRequestDenied("ai_assistant_unavailable") from None
        count = getattr(count, "total_tokens", None)
        if (
            provider_fingerprint(provider) != self.fingerprint
            or provider.client is not self.client
        ):
            raise AIRequestDenied("ai_assistant_unavailable")
        sequence = self.event.get_extra("_lajiao_commerce_sequence", 0) + 1
        reserve_task = asyncio.create_task(
            self.plugin.commerce.db(
                "reserve",
                self.instance,
                self.logical,
                sequence,
                self.purpose,
                provider.provider_config["id"],
                model,
                self.fingerprint,
                count,
                config.max_output_tokens,
            )
        )
        try:
            reservation = await asyncio.shield(reserve_task)
        except asyncio.CancelledError:

            async def release_unstarted():
                try:
                    reserved = await reserve_task
                except Exception:
                    return
                await self.plugin.commerce.db("release", reserved["call_id"])

            await asyncio.shield(release_unstarted())
            raise
        except Exception:
            raise AIRequestDenied("ai_assistant_unavailable") from None
        identifier = reservation["call_id"]
        self.event.set_extra("_lajiao_commerce_sequence", sequence)
        try:
            await self.plugin.commerce.db("dispatch", identifier)
            if self.purpose == "review":
                self.plugin.business.instance(
                    self.event.platform.session_ref, str(self.event.get_group_id())
                )
                settings = (
                    await self.plugin.business.settings(
                        self.event.platform.session_ref, str(self.event.get_group_id())
                    )
                )["settings"]
                if (
                    self.event.is_stopped()
                    or not settings["review_enabled"]
                    or settings["review_provider_id"] != provider.provider_config["id"]
                    or self.plugin.business.administrator(
                        self.event.get_sender_id(), self.event.unified_msg_origin
                    )
                    or str(self.event.get_sender_id()) in settings["manager_user_ids"]
                ):
                    raise AIRequestDenied("review_not_authorized")
            else:
                await self.plugin.ai.authorize(self.event, self.purpose, commit=True)
        except BaseException:
            await asyncio.shield(
                self.plugin.commerce.db("release", identifier, unsent=True)
            )
            raise
        return identifier, config

    async def finish(self, identifier, response):
        try:
            result = await self.plugin.commerce.db(
                "settle", identifier, gemini_usage(response)
            )
            await self.plugin.commerce.refresh()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Fail closed even when the ledger executor becomes unavailable.
            customer = self.plugin.commerce.snapshot["bindings"][self.instance][
                "customer_id"
            ]
            self.plugin.commerce.snapshot["customers"][customer]["ai_blocked"] = (
                "ledger_unavailable"
            )
            raise AIRequestDenied("ai_assistant_unavailable") from None
        if result["state"] != "settled":
            raise AIRequestDenied("ai_assistant_unavailable")

    async def generate(self, provider, model, contents, config):
        identifier, config = await self.prepare(provider, model, contents, config)
        try:
            response = await self.client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
        except BaseException as exc:
            try:
                await asyncio.shield(self.finish(identifier, None))
            except AIRequestDenied:
                pass
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise AIRequestDenied("ai_assistant_unavailable") from None
        await self.finish(identifier, response)
        return response

    async def stream(self, provider, model, contents, config):
        identifier, config = await self.prepare(provider, model, contents, config)
        try:
            stream = await self.client.models.generate_content_stream(
                model=model,
                contents=contents,
                config=config,
            )
        except BaseException as exc:
            try:
                await asyncio.shield(self.finish(identifier, None))
            except AIRequestDenied:
                pass
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise AIRequestDenied("ai_assistant_unavailable") from None
        stream = MeteredStream(self, identifier, stream)
        self.streams.add(stream)
        return stream

    async def close(self):
        for stream in tuple(self.streams):
            await stream.aclose()


class MeteredStream:
    """Close and settle on normal completion, cancellation or early tool return."""

    def __init__(self, meter, identifier, stream):
        self.meter, self.identifier, self.stream = meter, identifier, stream
        self.response = None
        self.complete = False
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            chunk = await self.stream.__anext__()
        except StopAsyncIteration:
            self.complete = True
            await self.aclose()
            raise
        except BaseException as exc:
            try:
                await asyncio.shield(self.aclose())
            except AIRequestDenied:
                pass
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise AIRequestDenied("ai_assistant_unavailable") from None
        if getattr(chunk, "usage_metadata", None) is not None:
            self.response = chunk
        if any(
            getattr(candidate, "finish_reason", None)
            for candidate in (chunk.candidates or [])
        ):
            self.complete = True
        return chunk

    async def aclose(self):
        if self.closed:
            return
        self.closed = True
        self.meter.streams.discard(self)
        try:
            await self.stream.aclose()
        finally:
            await self.meter.finish(
                self.identifier, self.response if self.complete else None
            )
