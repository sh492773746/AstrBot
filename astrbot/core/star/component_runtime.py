"""Opt-in cooperative execution isolation; this is not a process sandbox."""

import asyncio
import contextvars
import hashlib
import inspect
import time
from dataclasses import dataclass, field

from .components import CapabilitySpec, components


class RuntimeRejected(ValueError):
    """Represent an admission rejection that does not count as a fault."""


@dataclass
class ExecutionContext:
    """Carry server-owned authority and cancellation through nested calls."""

    scope: tuple[str, ...]
    authorize: object
    operation: object = None
    deadline: float = float("inf")
    claimed: bool = False
    revoked: bool = False
    plugin: str = ""
    leases: set = field(default_factory=set)
    emit: object = None
    parent: object = None
    failure: object = None
    slots: dict = field(default_factory=dict)

    def check(self):
        """Check the lease beside every durable commit and native send."""
        self.fence()
        self.authorize()

    def fence(self):
        """Check cancellation without recursively entering a domain authorizer."""
        if self.revoked or time.monotonic() >= self.deadline:
            raise RuntimeRejected("component_lease_revoked")
        if self.parent is not None:
            self.parent.fence()

    def claim(self):
        """Permanently reserve this message for a deterministic handler."""
        self.claimed = True


current_execution = contextvars.ContextVar("component_execution", default=None)


def check_execution():
    """Fence late IO without changing legacy callers outside an execution."""
    context = current_execution.get()
    if context is not None:
        context.fence()


def report_execution_fault(error):
    """Record a handled dependency failure without changing its reply contract."""
    context = current_execution.get()
    if context is not None:
        context.failure = error


@dataclass
class Circuit:
    failures: int = 0
    state: str = "closed"
    retry_at: float = 0
    delay: float = 30
    version: int = 0
    probing: bool = False
    error: str = ""


class ComponentRuntime:
    """Bound opted-in tasks and retain their slots until cancellation completes."""

    def __init__(self, registry=components, *, clock=time.monotonic):
        self.registry, self.clock = registry, clock
        self.circuits = {}
        self.tasks = {}
        self.contexts = {}
        self.plugin_counts = {}
        self.capability_counts = {}
        self.audit = []
        self.probe_tasks = {}
        self.slot_refs = {}
        self.capability_slots = {}
        self.closing = set()
        self.retired = {}

    def key(self, capability, scope):
        """Build a bounded dependency key without storing message text."""
        if len(scope) > 8 or any(not isinstance(s, str) or len(s) > 256 for s in scope):
            raise RuntimeRejected("component_scope_invalid")
        spec = self.registry.specs.get(capability)
        if not isinstance(spec, CapabilitySpec):
            raise RuntimeRejected("component_not_registered")
        key = (capability, tuple(scope))
        if key not in self.circuits:
            if len(self.circuits) >= 4096:
                raise RuntimeRejected("component_scope_capacity")
            self.circuits[key] = Circuit()
        return spec, key, self.circuits[key]

    def fault(self, circuit, error):
        """Trip only on runtime faults, not policy or business rejection."""
        circuit.failures += 1
        circuit.error = type(error).__name__
        circuit.version += 1
        if circuit.failures >= 3:
            circuit.state = "open"
            circuit.retry_at = self.clock() + circuit.delay

    async def invoke(self, capability, context):
        """Execute an explicitly bound capability without queues or retries.

        Args:
            capability: Registered server-owned capability identifier.
            context: Frozen authorization and exact dependency scope.

        Returns:
            The original handler result.
        """
        spec, key, circuit = self.key(capability, context.scope)
        context.check()
        authorizer = self.registry.authorizers.get(capability)
        if authorizer is None or capability not in self.registry.bindings:
            raise RuntimeRejected("component_unbound")
        authorizer(context)
        if circuit.state != "closed":
            raise RuntimeRejected("component_circuit_open")
        parent = current_execution.get()
        if parent is not None:
            parent.check()
        if spec.plugin in self.retired and self.retired[
            spec.plugin
        ] != self.registry.generations.get(spec.plugin, 0):
            self.retired.pop(spec.plugin)
        self.closing.discard(spec.plugin)
        inherited = parent.slots if parent else {}
        plugin_slot, capability_slot = (
            ("plugin", spec.plugin),
            ("capability", capability),
        )
        plugin_count = self.plugin_counts.get(spec.plugin, 0)
        capability_count = self.capability_slots.get(capability, 0)
        if (plugin_slot not in inherited and plugin_count >= 8) or (
            capability_slot not in inherited and capability_count >= spec.concurrency
        ):
            raise RuntimeRejected("component_busy")
        context.deadline = min(context.deadline, self.clock() + spec.timeout)
        if parent is not None:
            context.deadline = min(context.deadline, parent.deadline)
        context.plugin = spec.plugin
        context.parent = parent
        context.leases = set(parent.leases) if parent else set()
        context.leases.add(key)
        context.slots = dict(inherited)
        if plugin_slot not in inherited:
            self.plugin_counts[spec.plugin] = plugin_count + 1
        if capability_slot not in inherited:
            self.capability_slots[capability] = capability_count + 1
        acquired = []
        for slot in (plugin_slot, capability_slot):
            token = context.slots.setdefault(slot, object())
            self.slot_refs[token] = self.slot_refs.get(token, 0) + 1
            acquired.append((slot, token))
        self.capability_counts[key] = self.capability_counts.get(key, 0) + 1

        async def execute():
            token = current_execution.set(context)
            try:
                context.check()
                result = self.registry.bindings[capability](context)
                if inspect.isawaitable(result):
                    result = await result
                if inspect.isasyncgen(result):
                    generator = result
                    try:
                        if context.emit is None:
                            raise RuntimeRejected("component_stream_requires_consumer")
                        async for item in generator:
                            context.check()
                            await context.emit(item)
                    finally:
                        await generator.aclose()
                    result = None
                context.check()
                return result
            finally:
                current_execution.reset(token)

        task = asyncio.create_task(execute())
        self.tasks[task] = (spec.plugin, key)
        self.contexts[task] = context

        def finished(done):
            self.tasks.pop(done, None)
            self.contexts.pop(done, None)
            for slot, token in acquired:
                self.slot_refs[token] -= 1
                if not self.slot_refs[token]:
                    self.slot_refs.pop(token)
                    counts = (
                        self.plugin_counts
                        if slot[0] == "plugin"
                        else self.capability_slots
                    )
                    counts[slot[1]] -= 1
            self.capability_counts[key] -= 1
            if not done.cancelled():
                done.exception()
            if spec.plugin in self.closing:
                self._clear_idle(spec.plugin)

        task.add_done_callback(finished)
        try:
            done, _ = await asyncio.wait(
                {task}, timeout=max(0, context.deadline - self.clock())
            )
            if not done:
                context.revoked = True
                task.cancel()
                raise TimeoutError("component_timeout")
            result = task.result()
            if context.failure is not None:
                self.fault(circuit, context.failure)
            else:
                circuit.failures = 0
            return result
        except asyncio.CancelledError:
            context.revoked = True
            task.cancel()
            raise
        except RuntimeRejected:
            raise
        except Exception as exc:
            self.fault(circuit, exc)
            raise

    async def iterate(self, capability, context):
        """Stream one frame at a time and close the producer if its consumer exits."""
        queue, resume = asyncio.Queue(maxsize=1), asyncio.Event()

        async def emit(item):
            await queue.put(item)
            await resume.wait()
            resume.clear()

        context.emit = emit
        runner = asyncio.create_task(self.invoke(capability, context))
        receiver = None
        try:
            while True:
                receiver = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait(
                    {runner, receiver}, return_when=asyncio.FIRST_COMPLETED
                )
                if receiver in done:
                    token = current_execution.set(context)
                    try:
                        context.check()
                        yield receiver.result()
                    finally:
                        current_execution.reset(token)
                    resume.set()
                else:
                    receiver.cancel()
                    await asyncio.gather(receiver, return_exceptions=True)
                    runner.result()
                    break
        finally:
            context.revoked = True
            if receiver is not None and not receiver.done():
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)

    def status(self, plugin=None):
        """Expose non-secret counters and opaque scope references."""
        return [
            {
                "capability": capability,
                "plugin": self.registry.specs[capability].plugin,
                "scope": list(scope),
                "reference": hashlib.sha256(
                    repr((capability, scope)).encode()
                ).hexdigest()[:16],
                "state": circuit.state,
                "version": circuit.version,
                "failures": circuit.failures,
                "retry_after": max(0, circuit.retry_at - self.clock()),
                "in_flight": self.capability_counts.get((capability, scope), 0),
                "error": circuit.error,
            }
            for (capability, scope), circuit in self.circuits.items()
            if plugin is None or self.registry.specs[capability].plugin == plugin
        ]

    async def recover(
        self, capability, scope, version, actor, *, reset=False, reason=""
    ):
        """Probe a fixed read dependency, or rearm after explicit server review."""
        if (capability, tuple(scope)) not in self.circuits:
            raise RuntimeRejected("component_scope_unknown")
        spec, key, circuit = self.key(capability, tuple(scope))
        if circuit.version != version or circuit.probing:
            raise RuntimeRejected("component_state_changed")
        if self.capability_counts.get(key, 0):
            raise RuntimeRejected("component_scope_busy")
        if reset:
            if not reason.strip() or self.capability_counts.get(key, 0):
                raise RuntimeRejected("component_review_required")
            circuit.state, circuit.failures, circuit.error = "closed", 0, ""
            circuit.delay = 30
            circuit.version += 1
        else:
            probe = self.registry.probes.get(capability)
            if probe is None or spec.effect != "read":
                raise RuntimeRejected("component_no_safe_probe")
            if circuit.state != "open" or self.clock() < circuit.retry_at:
                raise RuntimeRejected("component_probe_not_due")
            circuit.probing, circuit.state = True, "half_open"
            circuit.version += 1
            task = asyncio.create_task(probe(tuple(scope)))
            self.probe_tasks[task] = (spec.plugin, key)

            def probe_finished(done):
                self.probe_tasks.pop(done, None)
                circuit.probing = False
                if not done.cancelled():
                    done.exception()
                if spec.plugin in self.closing:
                    self._clear_idle(spec.plugin)

            task.add_done_callback(probe_finished)
            try:
                done, _ = await asyncio.wait({task}, timeout=3)
                if not done:
                    task.cancel()
                    raise TimeoutError("component_probe_timeout")
                healthy = task.result()
                if healthy is not True:
                    raise RuntimeError("component_probe_failed")
                circuit.state, circuit.failures, circuit.error = "closed", 0, ""
                circuit.delay = 30
            except BaseException as exc:
                task.cancel()
                circuit.state, circuit.error = "open", type(exc).__name__
                circuit.delay = min(300, circuit.delay * 2)
                circuit.retry_at = self.clock() + circuit.delay
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise RuntimeRejected("component_probe_failed") from None
            finally:
                circuit.version += 1
                self._audit(actor, capability, scope, circuit, reset, reason)
        if reset:
            self._audit(actor, capability, scope, circuit, reset, reason)
        return {"state": circuit.state, "version": circuit.version}

    def _audit(self, actor, capability, scope, circuit, reset, reason):
        self.audit.append(
            {
                "actor": actor,
                "capability": capability,
                "scope": list(scope),
                "version": circuit.version,
                "reset": reset,
                "reason": reason[:300],
                "state": circuit.state,
            }
        )
        self.audit[:] = self.audit[-200:]

    async def close(self, plugin, *, unregister=False):
        """Revoke tasks without pretending uncooperative cancellation completed."""
        self.closing.add(plugin)
        if unregister:
            self.retired[plugin] = self.registry.generations.get(plugin, 0)
        tasks = [task for task, (owner, _) in self.tasks.items() if owner == plugin]
        for task in tasks:
            self.contexts[task].revoked = True
            task.cancel()
        probes = [
            task for task, (owner, _) in self.probe_tasks.items() if owner == plugin
        ]
        for task in probes:
            task.cancel()
        tasks += probes
        if tasks:
            await asyncio.wait(tasks, timeout=1)
        self._clear_idle(plugin)

    def _clear_idle(self, plugin):
        for key in list(self.circuits):
            if (
                self.registry.specs[key[0]].plugin == plugin
                and not self.capability_counts.get(key)
                and not self.circuits[key].probing
            ):
                self.circuits.pop(key)
        if plugin in self.retired and not any(
            owner == plugin
            for owner, _ in (*self.tasks.values(), *self.probe_tasks.values())
        ):
            self.registry.unregister(plugin, self.retired.pop(plugin))


component_runtime = ComponentRuntime()
