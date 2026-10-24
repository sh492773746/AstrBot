"""Opt-in, event-local model admission without changing shared providers."""

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar

from astrbot.core.utils.session_lock import session_lock_manager


class AIRequestDenied(Exception):
    """Stop a guarded run without exposing an internal error to the chat."""


generation_meter = ContextVar("astrbot_generation_meter", default=None)


@asynccontextmanager
async def model_meter_scope(event, provider, purpose):
    """Opt into event-local accounting without importing a plugin into core."""
    callback = getattr(event, "ai_metering_context", None)
    if callable(callback):
        async with callback(provider, purpose):
            yield
    else:
        yield


@asynccontextmanager
async def event_session_lock(event, origin):
    """Bound guarded session-lock waits without timing out model execution."""
    lock = session_lock_manager.acquire_lock(origin)
    if callable(getattr(event, "authorize_ai_model_request", None)):
        try:
            budget = getattr(event, "ai_queue_timeout", None)
            timeout = budget() if callable(budget) else 30
            await asyncio.wait_for(lock.__aenter__(), timeout=max(0, timeout))
        except TimeoutError:
            event.stop_event()
            raise AIRequestDenied("ai_session_queue_timeout") from None
    else:
        await lock.__aenter__()
    try:
        yield
    finally:
        await lock.__aexit__(None, None, None)


class EventProvider:
    """Delegate a provider while guarding each actual model invocation."""

    def __init__(self, provider, event, purpose):
        self.provider = provider
        self.event = event
        self.purpose = purpose

    def __getattr__(self, name):
        return getattr(self.provider, name)

    async def text_chat(self, *args, **kwargs):
        async with model_meter_scope(self.event, self.provider, self.purpose):
            await self.event.authorize_ai_model_request(self.purpose)
            try:
                response = await self.provider.text_chat(*args, **kwargs)
            except (Exception, asyncio.CancelledError):
                await self.event.record_ai_model_response(self.purpose, None)
                raise
            await self.event.record_ai_model_response(self.purpose, response)
            return response

    async def text_chat_stream(self, *args, **kwargs):
        async with model_meter_scope(self.event, self.provider, self.purpose):
            await self.event.authorize_ai_model_request(self.purpose)
            response = None
            stream = self.provider.text_chat_stream(*args, **kwargs)
            try:
                async for chunk in stream:
                    if not chunk.is_chunk:
                        response = chunk
                    yield chunk
            finally:
                try:
                    await stream.aclose()
                finally:
                    await self.event.record_ai_model_response(self.purpose, response)


def guard_provider(provider, event, purpose="chat"):
    """Wrap only events explicitly providing a model authorization callback.

    Args:
        provider: Shared provider; never mutated.
        event: Optional event supplying the admission and metric callbacks.
        purpose: Fixed internal call classification.

    Returns:
        Event-local delegate, or the original unguarded provider.
    """
    if provider is None or not callable(
        getattr(event, "authorize_ai_model_request", None)
    ):
        return provider
    if isinstance(provider, EventProvider):
        return provider
    return EventProvider(provider, event, purpose)
