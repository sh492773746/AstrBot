"""Server-controlled canary bindings for the common component runtime."""

import inspect
import time

from pydantic import ValidationError

from astrbot.core.platform.sources.wangshangliao.database import account_guard
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from astrbot.core.star.component_runtime import (
    ExecutionContext,
    RuntimeRejected,
    component_runtime,
)
from astrbot.core.star.components import CapabilitySpec, PageSpec, components
from astrbot.dashboard.responses import ApiError

from .command_capabilities import COMMAND_SPECS
from .open_store import OpenError
from .rule_replies import RuleConfiguration
from .service_settings import SupportConfiguration

PLUGIN = "wangshangliao_moderation"
ACCOUNTS = {"31846829", "43506527", "23691273"}
GROUP = "1459282"
FAULTS = {
    "model_unavailable",
    "model_oversized",
    "api_unavailable",
    "api_oversized",
    "api_invalid",
    "rate_limited",
}


class BusinessRejected(RuntimeRejected):
    """Preserve existing business rejection types outside the executor."""

    def __init__(self, original):
        self.original = original
        super().__init__(str(original))


class DependencyFault(RuntimeError):
    """Count a dependency fault while preserving its native caller contract."""

    def __init__(self, original):
        self.original = original
        super().__init__(type(original).__name__)


async def execute(context):
    """Invoke only the operation bound by trusted server code."""
    try:
        result = context.operation()
        return await result if inspect.isawaitable(result) else result
    except (OpenError, ProtocolError, ValidationError, ValueError) as exc:
        if str(exc) in FAULTS:
            raise DependencyFault(exc) from None
        raise BusinessRejected(exc) from None
    except ApiError as exc:
        if exc.status_code >= 500:
            raise DependencyFault(exc) from None
        raise BusinessRejected(exc) from None


class Governance:
    """Keep production untouched unless an exact active native test window admits it."""

    def __init__(self, service):
        self.service = service
        with service.store.connect(write=True) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS component_pilot("
                "id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER,enabled INTEGER)"
            )
            db.execute("INSERT OR IGNORE INTO component_pilot VALUES(1,1,0)")
        for name, effect, timeout in (
            ("commands", "write", 30),
            ("rules", "send", 30),
            ("advertisements", "send", 30),
            ("control", "write", 30),
            ("scheduled", "write", 30),
            ("activities", "send", 30),
            ("private_ai", "model", 45),
            ("group_ai", "model", 45),
            ("pc28", "read", 10),
        ):
            components.register(
                CapabilitySpec(
                    id="wsl." + name, plugin=PLUGIN, effect=effect, timeout=timeout
                ),
                handler=execute,
                authorize=lambda context: context.check(),
            )
        for spec in COMMAND_SPECS.values():
            components.register(spec)
        for name, model, capability in (
            ("business_rules", RuleConfiguration, "wsl.rules"),
            ("support_settings", SupportConfiguration, "wsl.control"),
        ):
            components.register(
                PageSpec(
                    id="wsl.page." + name,
                    plugin=PLUGIN,
                    capability=capability,
                    fields=model.model_json_schema(),
                )
            )

    def policy(self):
        """Read the additive server-owned pilot switch."""
        with self.service.store.connect() as db:
            row = dict(
                db.execute("SELECT * FROM component_pilot WHERE id=1").fetchone()
            )
        return {
            "version": row["version"],
            "enabled": bool(row["enabled"]),
            "group": GROUP,
            "accounts": sorted(ACCOUNTS),
        }

    def configure(self, version, enabled, actor):
        """CAS the canary switch without granting native roles or opening windows."""
        if type(version) is not int or type(enabled) is not bool:
            raise OpenError("component_pilot_invalid")
        with self.service.store.connect(write=True) as db:
            if (
                db.execute(
                    "UPDATE component_pilot SET enabled=?,version=version+1 WHERE id=1 AND version=?",
                    (enabled, version),
                ).rowcount
                != 1
            ):
                raise OpenError("component_pilot_changed")
            self.service.store.log(
                db, actor, "", GROUP, "component_pilot", {"enabled": enabled}
            )
        if not enabled:
            for task, context in tuple(component_runtime.contexts.items()):
                if context.plugin == PLUGIN:
                    context.revoked = True
                    task.cancel()
        return self.policy()

    def eligible(self, adapter, group="", event=None):
        """Require both an explicit server canary and the original live test window."""
        if not self.policy()["enabled"] or str(adapter.account) not in ACCOUNTS:
            return False
        window = getattr(adapter, "test_window", None)
        if window is None or not window.active():
            return False
        if event is not None:
            scope = event.get_extra("wsl_test_scope")
            if not scope or event.get_sender_id() != window.sender.account:
                return False
            if event.is_private_chat():
                return scope in {"private_ai", "private_commands"}
            if scope not in {"group_ai", "group_commands", "group_rules"}:
                return False
        if not group:
            return "private_commands" in getattr(window, "scopes", ())
        return group == GROUP and GROUP in window.groups

    async def run(
        self,
        name,
        adapter,
        operation,
        *,
        group="",
        event=None,
        guard=None,
        deadline=None,
        provider="",
        claim=False,
    ):
        """Add admission without replacing existing domain checks.

        Args:
            name: Fixed server-registered capability suffix.
            adapter: Authenticated native adapter.
            operation: Trusted bound operation, never a client function name.
            group: Exact business group, if any.
            event: Original authenticated incoming event.
            guard: Additional existing domain authorization.
            deadline: Existing monotonic deadline, never extended.
            provider: Credential-bound model or read dependency scope.
            claim: Whether this deterministic handler owns the message.

        Returns:
            Unmodified operation result.
        """
        if not self.eligible(adapter, group, event):
            return await operation()
        frozen = account_guard(adapter)

        def authorize():
            frozen()
            if not self.service.active or not self.eligible(adapter, group, event):
                raise RuntimeRejected("component_pilot_revoked")
            if event is not None and event.processing_completion.cancelled():
                raise RuntimeRejected("component_event_revoked")
            if guard is not None:
                guard()

        context = ExecutionContext(
            scope=("provider", provider) if provider else (str(adapter.account), group),
            authorize=authorize,
            operation=operation,
            claimed=claim,
            deadline=deadline if deadline is not None else time.monotonic() + 45,
        )
        if event is not None and claim:
            event.set_extra("_component_claimed", True)
        try:
            return await component_runtime.invoke("wsl." + name, context)
        except (BusinessRejected, DependencyFault) as exc:
            raise exc.original from None
        except RuntimeRejected as exc:
            raise OpenError(str(exc)) from None

    def page(self, name):
        """Return one fixed page schema; no function objects leave the registry."""
        return components.specs["wsl.page." + name].model_dump(mode="json")
