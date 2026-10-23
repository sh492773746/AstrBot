"""Cooperative execution limits, explicit recovery and legacy-safe streaming."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from astrbot.core.star.component_runtime import (
    ComponentRuntime,
    ExecutionContext,
    RuntimeRejected,
    report_execution_fault,
)
from astrbot.core.star.components import CapabilitySpec, ComponentRegistry, PageSpec


def setup_runtime(handler, **settings):
    registry = ComponentRegistry()
    registry.register(
        CapabilitySpec(id="test.read", plugin="test", **settings),
        handler=handler,
        authorize=lambda context: context.check(),
    )
    return ComponentRuntime(registry)


def context(**settings):
    return ExecutionContext(("account", "group"), lambda: None, **settings)


@pytest.mark.asyncio
async def test_policy_rejections_do_not_trip_but_three_faults_do():
    async def fail(ctx):
        raise RuntimeRejected("permission_denied")

    runtime = setup_runtime(fail)
    for _ in range(4):
        with pytest.raises(RuntimeRejected):
            await runtime.invoke("test.read", context())
    assert runtime.status()[0]["failures"] == 0

    async def broken(ctx):
        raise RuntimeError("private text must not be exposed")

    runtime.registry.bindings["test.read"] = broken
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await runtime.invoke("test.read", context())
    assert runtime.status()[0]["state"] == "open"
    assert "private text" not in str(runtime.status())
    with pytest.raises(RuntimeRejected, match="circuit_open"):
        await runtime.invoke("test.read", context())
    with pytest.raises(RuntimeError):
        await runtime.invoke("test.read", ExecutionContext(("another",), lambda: None))
    assert runtime.status()[-1]["state"] == "closed"
    assert runtime.status()[-1]["failures"] == 1


@pytest.mark.asyncio
async def test_timeout_retains_slot_and_fences_uncooperative_worker():
    started, release = asyncio.Event(), asyncio.Event()
    fenced = []

    async def stubborn(ctx):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        with pytest.raises(RuntimeRejected):
            ctx.check()
        fenced.append(True)

    runtime = setup_runtime(stubborn, timeout=0.02, concurrency=1)
    task = asyncio.create_task(runtime.invoke("test.read", context()))
    await started.wait()
    with pytest.raises(TimeoutError):
        await task
    assert runtime.status()[0]["in_flight"] == 1
    with pytest.raises(RuntimeRejected, match="busy"):
        await runtime.invoke("test.read", context())
    release.set()
    await asyncio.gather(*tuple(runtime.tasks), return_exceptions=True)
    await asyncio.sleep(0)
    assert fenced and runtime.status()[0]["in_flight"] == 0


@pytest.mark.asyncio
async def test_streaming_and_early_consumer_exit_close_generator():
    closed = []

    async def stream(ctx):
        try:
            yield 1
            yield 2
        finally:
            closed.append(True)

    runtime = setup_runtime(stream)
    assert [item async for item in runtime.iterate("test.read", context())] == [1, 2]
    iterator = runtime.iterate("test.read", context())
    assert await anext(iterator) == 1
    await iterator.aclose()
    await asyncio.sleep(0)
    assert closed == [True, True]
    assert not runtime.tasks


@pytest.mark.asyncio
async def test_safe_probe_cas_and_review_cannot_execute_a_write():
    runtime = setup_runtime(AsyncMock(return_value="ok"), probe=True)
    _, key, circuit = runtime.key("test.read", ("a",))
    circuit.state, circuit.failures = "open", 3
    probe = AsyncMock(return_value=True)
    runtime.registry.probes["test.read"] = probe
    with pytest.raises(RuntimeRejected, match="state_changed"):
        await runtime.recover("test.read", ("a",), 99, "server")
    result = await runtime.recover("test.read", ("a",), 0, "server")
    assert result["state"] == "closed" and probe.await_count == 1
    assert runtime.audit[-1]["actor"] == "server"
    registry = ComponentRegistry()
    with pytest.raises(ValueError, match="unsafe"):
        registry.register(
            CapabilitySpec(id="ad", plugin="test", effect="send", probe=True),
            probe=probe,
        )
    assert "test.read" not in runtime.registry.catalog()[0].get("handler", "")
    assert key in runtime.circuits


@pytest.mark.asyncio
async def test_recovery_without_probe_requires_review_and_preserves_business_state():
    runtime = setup_runtime(AsyncMock())
    _, _, circuit = runtime.key("test.read", ("a",))
    circuit.state = "open"
    with pytest.raises(RuntimeRejected, match="no_safe_probe"):
        await runtime.recover("test.read", ("a",), 0, "server")
    with pytest.raises(RuntimeRejected, match="review_required"):
        await runtime.recover("test.read", ("a",), 0, "server", reset=True)
    await runtime.recover(
        "test.read", ("a",), 0, "server", reset=True, reason="Dependency verified"
    )
    assert circuit.state == "closed"


def test_schema_catalog_serializes_only_declarations():
    registry = ComponentRegistry()
    registry.register(PageSpec(id="page", plugin="test", capability="read", fields={}))
    assert registry.catalog()[0]["schema_version"] == 1
    assert not registry.bindings


@pytest.mark.parametrize("effect", ["write", "send", "model"])
def test_no_side_effecting_operation_can_register_as_probe(effect):
    registry = ComponentRegistry()
    with pytest.raises(ValueError, match="unsafe"):
        registry.register(
            CapabilitySpec(id="unsafe", plugin="fixture", effect=effect, probe=True),
            probe=AsyncMock(return_value=True),
        )


@pytest.mark.asyncio
async def test_handled_fault_preserves_return_but_trips_dependency():
    async def handled(ctx):
        report_execution_fault(OSError("redacted supplier details"))
        return "fixed feedback"

    runtime = setup_runtime(handled)
    for _ in range(3):
        assert await runtime.invoke("test.read", context()) == "fixed feedback"
    assert runtime.status()[0]["state"] == "open"


@pytest.mark.asyncio
async def test_capability_limit_is_shared_across_groups_and_nested_lease_is_reused():
    release = asyncio.Event()

    async def wait(ctx):
        await release.wait()

    runtime = setup_runtime(wait, concurrency=1)
    task = asyncio.create_task(runtime.invoke("test.read", context()))
    await asyncio.sleep(0.01)
    with pytest.raises(RuntimeRejected, match="busy"):
        await runtime.invoke("test.read", ExecutionContext(("another",), lambda: None))
    release.set()
    await task

    async def nested(ctx):
        if ctx.operation is None:
            return await runtime.invoke("test.read", context(operation="nested"))
        assert runtime.plugin_counts["test"] == 1
        assert runtime.capability_slots["test.read"] == 1
        ctx.parent.revoked = True
        with pytest.raises(RuntimeRejected, match="revoked"):
            ctx.fence()
        ctx.parent.revoked = False
        return 7

    runtime.registry.bindings["test.read"] = nested
    assert await runtime.invoke("test.read", context()) == 7
    await asyncio.sleep(0)
    assert not runtime.slot_refs


@pytest.mark.asyncio
async def test_single_half_open_probe_failure_backoff_and_unload():
    runtime = setup_runtime(AsyncMock(), probe=True)
    _, _, circuit = runtime.key("test.read", ("a",))
    circuit.state = "open"
    started, release = asyncio.Event(), asyncio.Event()

    async def probe(scope):
        started.set()
        await release.wait()
        return False

    runtime.registry.probes["test.read"] = probe
    task = asyncio.create_task(runtime.recover("test.read", ("a",), 0, "server"))
    await started.wait()
    with pytest.raises(RuntimeRejected, match="state_changed"):
        await runtime.recover("test.read", ("a",), circuit.version, "server")
    release.set()
    with pytest.raises(RuntimeRejected, match="probe_failed"):
        await task
    assert circuit.delay == 60 and runtime.audit[-1]["state"] == "open"
    await runtime.close("test")
    assert not runtime.tasks and not runtime.probe_tasks and not runtime.circuits


@pytest.mark.asyncio
async def test_probe_timeout_retains_single_probe_and_late_success_cannot_recover(
    monkeypatch,
):
    runtime = setup_runtime(AsyncMock(), probe=True)
    _, _, circuit = runtime.key("test.read", ("a",))
    circuit.state = "open"
    release = asyncio.Event()
    real_wait = asyncio.wait

    async def probe(scope):
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return True

    async def shortened_wait(tasks, *, timeout):
        assert timeout == 3
        return await real_wait(tasks, timeout=0.01)

    runtime.registry.probes["test.read"] = probe
    monkeypatch.setattr(asyncio, "wait", shortened_wait)
    with pytest.raises(RuntimeRejected, match="probe_failed"):
        await runtime.recover("test.read", ("a",), 0, "server")
    assert circuit.probing and circuit.state == "open"
    with pytest.raises(RuntimeRejected, match="state_changed"):
        await runtime.recover(
            "test.read",
            ("a",),
            circuit.version,
            "server",
            reset=True,
            reason="not sufficient while probe is still active",
        )
    release.set()
    await asyncio.gather(*runtime.probe_tasks)
    await asyncio.sleep(0)
    assert not circuit.probing and circuit.state == "open"


@pytest.mark.asyncio
async def test_unload_drops_bindings_but_never_unregisters_a_new_generation():
    runtime = setup_runtime(AsyncMock(return_value=True))
    generation = runtime.registry.generations["test"]
    runtime.registry.register(
        CapabilitySpec(id="test.read", plugin="test"),
        handler=AsyncMock(return_value=True),
    )
    assert not runtime.registry.unregister("test", generation)
    assert runtime.registry.catalog()
    await runtime.close("test", unregister=True)
    assert not runtime.registry.catalog() and not runtime.registry.bindings
