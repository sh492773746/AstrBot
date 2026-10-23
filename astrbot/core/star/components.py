"""Versioned declarations shared by dispatchers, forms and documentation."""

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ComponentSpec(BaseModel):
    """Describe public metadata without serializing executable objects."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    id: str = Field(pattern=r"^[A-Za-z0-9_.:/-]{1,160}$")
    plugin: str = Field(min_length=1, max_length=160)


class CapabilitySpec(ComponentSpec):
    """Declare the bounds and side effects of one server-owned capability."""

    effect: Literal["read", "write", "send", "model"] = "read"
    timeout: float = Field(default=10, gt=0, le=300)
    concurrency: int = Field(default=2, ge=1, le=8)
    critical: bool = True
    probe: bool = False


class CommandSpec(ComponentSpec):
    """Declare one native command and its compatible execution entry."""

    command: str
    aliases: tuple[str, ...] = ()
    locations: tuple[Literal["group", "private"], ...] = ("group", "private")
    role: str = "group_manager"
    preview: bool = False
    retired: bool = False
    capability: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    effect: Literal["read", "write", "send", "model"] = "write"


class PageSpec(ComponentSpec):
    """Expose a Pydantic schema with bounded presentation hints."""

    capability: str
    fields: dict[str, Any]
    confirmation: bool = True


class ComponentRecoveryRequest(BaseModel):
    """Select only a registered scope and a versioned server recovery action."""

    model_config = ConfigDict(extra="forbid", strict=True)
    capability: str = Field(min_length=1, max_length=160)
    scope: list[str] = Field(max_length=8)
    version: int = Field(ge=0)
    operation: Literal["probe", "reset_after_review"]
    reason: str = Field(default="", max_length=300)


class ComponentRegistry:
    """Own immutable declarations and non-public server bindings."""

    def __init__(self):
        self.specs: dict[str, ComponentSpec] = {}
        self.bindings: dict[str, Callable] = {}
        self.authorizers: dict[str, Callable] = {}
        self.probes: dict[str, Callable] = {}
        self.generations: dict[str, int] = {}

    def register(self, spec, *, handler=None, authorize=None, probe=None):
        """Register trusted code, rejecting collisions and unsafe probes.

        Args:
            spec: Validated component metadata.
            handler: Server-owned asynchronous entry point.
            authorize: Server-owned synchronous authorization check.
            probe: Explicitly side-effect-free dependency health check.
        """
        if spec.id in self.specs:
            if self.specs[spec.id] != spec:
                raise ValueError("component_collision")
        if probe is not None and (
            not isinstance(spec, CapabilitySpec)
            or spec.effect != "read"
            or not spec.probe
        ):
            raise ValueError("unsafe_component_probe")
        self.specs[spec.id] = spec
        if handler is not None:
            self.bindings[spec.id] = handler
        if authorize is not None:
            self.authorizers[spec.id] = authorize
        if probe is not None:
            self.probes[spec.id] = probe
        self.generations[spec.plugin] = self.generations.get(spec.plugin, 0) + 1

    def unregister(self, plugin, generation):
        """Drop retired bindings only if a new lifecycle has not replaced them."""
        if self.generations.get(plugin, 0) != generation:
            return False
        for name in [
            name for name, spec in self.specs.items() if spec.plugin == plugin
        ]:
            self.specs.pop(name)
            self.bindings.pop(name, None)
            self.authorizers.pop(name, None)
            self.probes.pop(name, None)
        self.generations[plugin] = generation + 1
        return True

    def catalog(self, plugin=None):
        """Return metadata only, never handler or authorizer objects."""
        return [
            spec.model_dump(mode="json")
            for spec in self.specs.values()
            if plugin is None or spec.plugin == plugin
        ]


components = ComponentRegistry()
