"""Active group canary boundaries without the removed private batch worker."""

import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from astrbot.builtin_stars.wangshangliao_moderation import component_governance
from astrbot.builtin_stars.wangshangliao_moderation.component_governance import (
    GROUP,
    PLUGIN,
    Governance,
)
from astrbot.builtin_stars.wangshangliao_moderation.open_store import (
    OpenError,
    OpenStore,
)
from astrbot.core.platform.sources.wangshangliao.identity import ActorIdentity
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from astrbot.core.star.component_runtime import (
    ComponentRuntime,
    RuntimeRejected,
    current_execution,
)
from astrbot.core.star.components import ComponentRegistry


@pytest_asyncio.fixture(autouse=True)
async def governance_runtime(monkeypatch):
    """Isolate registry, runtime tasks and network access for each regression.

    Args:
        monkeypatch: Test-scoped replacement of shared bindings and sockets.

    Yields:
        Temporary component executor using only this test's declarations.
    """

    def forbidden(*args, **kwargs):
        raise AssertionError("real_network_forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    registry = ComponentRegistry()
    runtime = ComponentRuntime(registry)
    monkeypatch.setattr(component_governance, "components", registry)
    monkeypatch.setattr(component_governance, "component_runtime", runtime)
    try:
        yield runtime
    finally:
        await runtime.close(PLUGIN)


def fixture(tmp_path):
    active = [True]
    adapter = SimpleNamespace(
        account="43506527",
        config={"id": "offline", "_business_epoch": 1, "_business_active": True},
        test_window=SimpleNamespace(
            active=lambda: active[0],
            sender=SimpleNamespace(account="31846829"),
            groups=[GROUP],
            scopes={"group_commands", "group_rules", "group_ai"},
        ),
    )
    service = SimpleNamespace(
        store=OpenStore(tmp_path),
        active=True,
        chat_generations={},
        verify_actor=AsyncMock(),
        reject_reply=AsyncMock(),
        chat=AsyncMock(),
    )
    service.governance = Governance(service)
    service.governance.configure(1, True, "offline/server")
    return service, adapter, active


def event(adapter, mid, text="你好", *, private=True, scope=None):
    extras = {
        "wsl_test_scope": scope or ("private_ai" if private else "group_commands"),
        "wsl_chat_fresh": mid == "1",
    }
    return SimpleNamespace(
        platform=adapter,
        actor_identity=ActorIdentity("43506527", "31846829", "123", "31846829"),
        message_obj=SimpleNamespace(message_id=mid),
        message_str=text,
        processing_completion=asyncio.get_running_loop().create_future(),
        get_extra=lambda key: extras.get(key),
        set_extra=lambda key, value: extras.__setitem__(key, value),
        get_sender_id=lambda: "31846829",
        is_private_chat=lambda: private,
        get_group_id=lambda: "" if private else GROUP,
    )


def test_governance_registers_group_capabilities_and_business_rules_only(
    tmp_path, governance_runtime
):
    service, _, _ = fixture(tmp_path)
    registry = governance_runtime.registry
    assert "wsl.private_ai" not in registry.specs
    assert "wsl.private_ai" not in registry.bindings
    assert "wsl.page.support_settings" not in registry.specs
    assert {
        "wsl.commands",
        "wsl.rules",
        "wsl.advertisements",
        "wsl.control",
        "wsl.scheduled",
        "wsl.activities",
        "wsl.group_ai",
        "wsl.pc28",
    } <= registry.specs.keys()
    assert service.governance.page("business_rules")["capability"] == "wsl.rules"


def test_support_page_rejected_before_service_or_registry_access():
    governance = object.__new__(Governance)
    with pytest.raises(OpenError, match="^private_messages_retired$"):
        governance.page("support_settings")


@pytest.mark.parametrize(
    "scope", ["private_ai", "private_commands", "group_ai", "group_commands", None]
)
@pytest.mark.parametrize("group", ["", GROUP])
def test_private_event_never_eligible_before_policy_access(scope, group):
    governance = object.__new__(Governance)
    source = SimpleNamespace(
        is_private_chat=lambda: True,
        get_extra=Mock(return_value=scope),
    )
    assert not governance.eligible(object(), group, source)
    source.get_extra.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "private"),
    [
        ("private_ai", None),
        ("private_ai", False),
        ("control", True),
        ("commands", True),
        ("group_ai", True),
    ],
)
async def test_private_governance_rejected_before_fallback_or_policy(name, private):
    governance = object.__new__(Governance)
    governance.eligible = Mock(side_effect=AssertionError("policy_must_not_run"))
    operation = AsyncMock(side_effect=AssertionError("operation_must_not_run"))
    source = (
        None if private is None else SimpleNamespace(is_private_chat=lambda: private)
    )
    with pytest.raises(OpenError, match="^private_messages_retired$"):
        await governance.run(name, object(), operation, event=source)
    operation.assert_not_awaited()
    governance.eligible.assert_not_called()


def test_only_fixed_native_test_scope_can_enable_governance(tmp_path):
    service, adapter, active = fixture(tmp_path)
    assert service.governance.eligible(adapter, GROUP)
    assert not service.governance.eligible(adapter, "1517858")
    adapter.test_window.scopes = {"private_commands"}
    assert not service.governance.eligible(adapter)
    active[0] = False
    assert not service.governance.eligible(adapter, GROUP)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope",
    ["group_ai", "group_commands", "group_rules", "private_ai", "private_commands"],
)
async def test_group_event_requires_group_scope_and_exact_window_sender(
    tmp_path, scope
):
    service, adapter, _ = fixture(tmp_path)
    source = event(adapter, "group", private=False, scope=scope)
    assert service.governance.eligible(adapter, GROUP, source) is scope.startswith(
        "group_"
    )
    source.get_sender_id = lambda: "different-sender"
    assert not service.governance.eligible(adapter, GROUP, source)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name", ["commands", "rules", "scheduled", "activities", "group_ai", "pc28"]
)
async def test_group_governance_executes_registered_capability(
    tmp_path, governance_runtime, name
):
    service, adapter, _ = fixture(tmp_path)
    source = event(adapter, "group", private=False)
    guard = Mock()

    async def operation():
        context = current_execution.get()
        assert context is not None
        assert context.scope == (adapter.account, GROUP)
        assert context.plugin == PLUGIN and context.claimed
        return "group-result"

    assert (
        await service.governance.run(
            name, adapter, operation, group=GROUP, event=source, guard=guard, claim=True
        )
        == "group-result"
    )
    assert guard.call_count > 1
    assert source.get_extra("_component_claimed") is True
    assert governance_runtime.status()[0]["state"] == "closed"
    assert governance_runtime.status()[0]["failures"] == 0
    assert not governance_runtime.tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("pilot_enabled", [False, True])
async def test_no_group_webui_control_retains_existing_operation(
    tmp_path, governance_runtime, pilot_enabled
):
    service, adapter, _ = fixture(tmp_path)
    if not pilot_enabled:
        service.governance.configure(2, False, "offline/server")
    adapter.test_window.scopes = {"private_commands"}
    operation = AsyncMock(return_value="webui-control-result")
    assert not service.governance.eligible(adapter)
    assert (
        await service.governance.run("control", adapter, operation)
        == "webui-control-result"
    )
    operation.assert_awaited_once_with()
    assert not governance_runtime.circuits


@pytest.mark.asyncio
async def test_non_canary_group_retains_original_domain_checks(
    tmp_path, governance_runtime
):
    service, adapter, _ = fixture(tmp_path)
    operation = AsyncMock(side_effect=OpenError("native_group_permission_denied"))
    with pytest.raises(OpenError, match="^native_group_permission_denied$"):
        await service.governance.run("commands", adapter, operation, group="1517858")
    operation.assert_awaited_once_with()
    assert not governance_runtime.circuits


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["window", "config", "event", "role", "pilot"])
async def test_group_authority_rechecked_before_operation(
    tmp_path, monkeypatch, governance_runtime, change
):
    service, adapter, active = fixture(tmp_path)
    source = event(adapter, "group", private=False)
    operation = AsyncMock(side_effect=AssertionError("revoked_operation_must_not_run"))
    guard = Mock()
    invoke = governance_runtime.invoke

    async def revoke_before_invoke(capability, context):
        if change == "window":
            active[0] = False
        elif change == "config":
            adapter.config["_business_epoch"] = 2
        elif change == "event":
            source.processing_completion.cancel()
        elif change == "role":
            guard.side_effect = OpenError("manager_revoked")
        else:
            service.governance.configure(2, False, "offline/server")
        return await invoke(capability, context)

    monkeypatch.setattr(governance_runtime, "invoke", revoke_before_invoke)
    expected = {
        "window": (OpenError, "component_pilot_revoked"),
        "config": (ProtocolError, "database_authority_changed"),
        "event": (OpenError, "component_event_revoked"),
        "role": (OpenError, "manager_revoked"),
        "pilot": (OpenError, "component_pilot_revoked"),
    }
    error, reason = expected[change]
    with pytest.raises(error, match="^" + reason + "$"):
        await service.governance.run(
            "commands", adapter, operation, group=GROUP, event=source, guard=guard
        )
    operation.assert_not_awaited()
    assert not governance_runtime.tasks
    assert all(row["failures"] == 0 for row in governance_runtime.status())


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["window", "role", "pilot"])
async def test_running_group_operation_fenced_before_late_effect(
    tmp_path, governance_runtime, change
):
    service, adapter, active = fixture(tmp_path)
    source = event(adapter, "group", private=False)
    entered, release = asyncio.Event(), asyncio.Event()
    commit = AsyncMock()
    guard = Mock()

    async def operation():
        entered.set()
        await release.wait()
        current_execution.get().check()
        await commit()

    task = asyncio.create_task(
        service.governance.run(
            "commands", adapter, operation, group=GROUP, event=source, guard=guard
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if change == "window":
            active[0] = False
            error, reason = RuntimeRejected, "component_pilot_revoked"
        elif change == "role":
            guard.side_effect = OpenError("manager_revoked")
            error, reason = OpenError, "manager_revoked"
        else:
            service.governance.configure(2, False, "offline/server")
            error, reason = asyncio.CancelledError, None
        release.set()
        with pytest.raises(error, match=reason):
            await asyncio.wait_for(task, 1)
        commit.assert_not_awaited()
        assert not governance_runtime.tasks
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
