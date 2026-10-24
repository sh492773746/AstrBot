"""Logs preserve security outcomes without payloads or sensitive identifiers."""

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.builtin_stars.wangshangliao_moderation.main import Main
from astrbot.builtin_stars.wangshangliao_moderation.observability import (
    record_event,
    trace_management,
)
from astrbot.core.log import LogBroker, LogQueueHandler
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.platform.sources.wangshangliao import diagnostics
from astrbot.core.platform.sources.wangshangliao.event import WangshangliaoEvent


@pytest.fixture
def logs(monkeypatch):
    entries = []
    monkeypatch.setattr(diagnostics, "FAILURES", {})
    monkeypatch.setattr(
        diagnostics,
        "logger",
        SimpleNamespace(
            isEnabledFor=lambda _: True,
            log=lambda level, template, *args: entries.append((level, template % args)),
        ),
    )
    return entries


def event(*, admin=True, private=True):
    return SimpleNamespace(
        platform=SimpleNamespace(config={"id": "wangshangliao_test"}),
        get_platform_name=lambda: "wangshangliao",
        is_private_chat=lambda: private,
        is_admin=lambda: admin,
        get_group_id=lambda: "sensitive-group",
        get_sender_id=lambda: "sensitive-user",
        unified_msg_origin="private-session-secret",
        message_obj=SimpleNamespace(message_id="private-message-secret"),
        message_str="credential=secret-body",
        get_extra=lambda _: None,
    )


def test_identifiers_hashed_and_shared_correlation(logs):
    e = event()
    record_event(e, "ai_route", "customer_only", route="customer_private")
    record_event(e, "command", "rejected", error="permission_denied")
    record_event(e, "menu", "invalid_selection")
    record_event(
        e, "menu_reply", "unknown", failed=True, error="result_unknown", aggregate=False
    )
    for _, entry in logs:
        assert f"correlation={diagnostics.reference(e.message_obj.message_id)}" in entry
        assert "private-message-secret" not in entry
        assert "private-session-secret" not in entry
        assert "sensitive-user" not in entry
        assert "sensitive-group" not in entry
        assert "secret-body" not in entry


@pytest.mark.parametrize("private", [False, True])
def test_astrbot_event_log_never_reads_wangshangliao_body(monkeypatch, private):
    from astrbot.core import event_bus

    entries = []
    monkeypatch.setattr(
        event_bus,
        "logger",
        SimpleNamespace(
            info=lambda template, *args, **_: entries.append(template % args)
        ),
    )
    e = event(private=private)
    e.get_message_outline = Mock(side_effect=AssertionError("Body must not be read"))
    e.get_sender_name = Mock(side_effect=AssertionError("Name must not be read"))
    event_bus.EventBus._print_event(None, e, "private-config-secret")
    text = entries[0]
    assert f"route={'private' if private else 'group'}" in text
    assert f"correlation={diagnostics.reference(e.message_obj.message_id)}" in text
    for value in (
        "private-config-secret",
        "private-message-secret",
        "sensitive-user",
        "sensitive-group",
        "credential",
        "secret-body",
    ):
        assert value not in text


def test_event_log_preserves_other_platform_behavior(monkeypatch):
    from astrbot.core import event_bus

    logger = Mock()
    monkeypatch.setattr(event_bus, "logger", logger)
    e = SimpleNamespace(
        get_platform_name=lambda: "telegram",
        get_platform_id=lambda: "fixture",
        get_sender_name=lambda: "Sender",
        get_sender_id=lambda: "1",
        get_message_outline=lambda: "Other platform message",
    )
    event_bus.EventBus._print_event(None, e, "Fixture")
    assert "Sender/1: Other platform message" in logger.info.call_args.args[0]


def test_fields_reject_multiline_and_oversized_labels(logs):
    diagnostics.Diagnostics("bad\npassword=secret").emit(
        "a" * 1000,
        "bad\nsecret",
        "raw-message",
        action="token=secret",
        route="not a label",
        error="password=secret",
        duration_ms=-5,
    )
    text = logs[0][1]
    assert "\n" not in text and "secret" not in text
    assert "stage=other" in text and "duration_ms=0" in text
    assert len(text) < 512


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (1201, "1201"),
        (-(1 << 31), str(-(1 << 31))),
        ((1 << 31) - 1, str((1 << 31) - 1)),
        (1 << 31, "-"),
        (True, "-"),
        ("1201\npassword=secret", "-"),
        (None, "-"),
    ],
)
def test_provider_code_is_numeric_bounded_and_injection_safe(logs, code, expected):
    diagnostics.Diagnostics("fixture").emit(
        "login",
        "failed",
        action="request_sms",
        provider_code=code,
        failed=True,
        error="business_rejected",
        aggregate=False,
    )
    assert f"provider_code={expected}" in logs[0][1]
    assert "\n" not in logs[0][1]
    assert "secret" not in logs[0][1]


def test_secondary_security_code_diagnostic_preserves_only_safe_label(logs):
    diagnostics.Diagnostics("fixture").emit(
        "friend_application_read",
        "failed",
        failed=True,
        error="secondary_security_code_required",
        provider_code=1400,
        aggregate=False,
    )
    text = logs[0][1]
    assert "error=secondary_security_code_required " in text
    assert "provider_code=1400 " in text
    assert "未设置二级安全码" not in text and "\n" not in text


def test_failure_windows_are_bounded_and_report_suppression(logs, monkeypatch):
    monkeypatch.setattr(diagnostics, "MAX_FAILURE_WINDOWS", 2)
    monkeypatch.setattr(diagnostics.time, "monotonic", lambda: 100)
    d = diagnostics.Diagnostics("one")
    d.emit("connection", "failed", failed=True)
    d.emit("connection", "failed", failed=True)
    d.emit("login", "failed", failed=True)
    d.emit("send", "failed", failed=True)
    assert len(diagnostics.FAILURES) == 2
    assert any(
        "outcome=suppressed_summary" in text and "count=1" in text for _, text in logs
    )


def test_expired_other_failure_reports_suppression(logs, monkeypatch):
    clock = iter([100, 101, 161])
    monkeypatch.setattr(diagnostics.time, "monotonic", lambda: next(clock))
    d = diagnostics.Diagnostics("one")
    d.emit("connection", "failed", failed=True)
    d.emit("connection", "failed", failed=True)
    d.emit("login", "failed", failed=True)
    assert any(
        "stage=connection outcome=suppressed_summary" in text for _, text in logs
    )


def test_per_operation_results_are_not_aggregated(logs):
    d = diagnostics.Diagnostics("one")
    for message in ("first", "second"):
        d.emit("progressive_result", "unknown", message, failed=True, aggregate=False)
    assert len(logs) == 2 and not diagnostics.FAILURES


def test_routine_receive_uses_debug_but_failures_remain_visible(logs):
    d = diagnostics.Diagnostics("fixture")
    d.emit("receive", "received")
    d.emit("receive", "failed", failed=True, error="transport")
    assert logs[0][0] == logging.DEBUG
    assert logs[1][0] == logging.WARNING


def test_disabled_debug_avoids_hashing_identifiers(monkeypatch):
    destination = logging.Logger("disabled-wsl-debug", level=logging.INFO)
    monkeypatch.setattr(diagnostics, "logger", destination)
    d = diagnostics.Diagnostics("fixture")
    hashed = Mock(side_effect=AssertionError("Disabled logs must not hash IDs"))
    monkeypatch.setattr(diagnostics, "reference", hashed)
    for stage, outcome in (
        ("receive", "received"),
        ("process", "started"),
        ("process", "completed"),
        ("recall", "observed"),
    ):
        d.emit(stage, outcome, "private-message", group="private-group")
    hashed.assert_not_called()


def test_observed_recall_uses_debug_without_hiding_action_results(logs):
    d = diagnostics.Diagnostics("fixture")
    d.emit("recall", "observed", "incoming")
    d.emit("recall", "accepted", "outgoing")
    d.emit("recall", "unknown", "outgoing", failed=True)
    assert [level for level, _ in logs] == [
        logging.DEBUG,
        logging.INFO,
        logging.WARNING,
    ]


def test_timing_samples_reuse_the_bounded_buffer(monkeypatch):
    factory = Mock(wraps=diagnostics.deque)
    monkeypatch.setattr(diagnostics, "deque", factory)
    d = diagnostics.Diagnostics("fixture")
    for _ in range(1000):
        d.observe("event_loop", 0.001)
    assert factory.call_count == 1
    assert d.snapshot()["timings"]["event_loop"]["samples"] == 512
    assert d.snapshot()["timings"]["event_loop"]["count"] == 1000


@pytest.mark.parametrize(
    "code",
    [
        "message_fields",
        "message_trailing",
        "message_count",
        "message_decode",
        "notification",
    ],
)
def test_known_receive_failures_keep_distinct_safe_codes(logs, code):
    diagnostics.Diagnostics("fixture").emit(
        "receive", "unsupported", failed=True, error=code
    )
    assert f"error={code}" in logs[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "private,admin", [(True, False), (False, True), (False, False)]
)
async def test_permission_rejection_is_logged_without_changing_result(
    logs, private, admin
):
    plugin = object.__new__(Main)
    result = await plugin.private_management(
        event(admin=admin, private=private), "config_preview", "secret proposal"
    )
    assert json.loads(result)["reason"] == "ai_management_retired"
    level, text = logs[-1]
    assert level == logging.INFO
    assert "outcome=rejected" in text and "error=permission_denied" in text
    assert "secret proposal" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        "preview",
        "saved",
        "started",
        "schedule_confirmation_required",
        "accepted",
        "verified",
        "unknown",
        "not_confirmed",
        "query_failed",
        "needs_review",
    ],
)
async def test_tool_results_preserve_exact_outcome_without_logging_content(
    logs, status
):
    original = json.dumps(
        {"status": status, "text": "secret-result", "confirmation": "private-token"}
    )

    @trace_management
    async def tool(self, e, action, value):
        return original

    result = await tool(None, event(), "config_confirm", "private-token")
    assert result == original
    level, text = logs[-1]
    assert f"outcome={status} " in text and "action=config_confirm" in text
    assert "private-token" not in text and "secret-result" not in text
    assert level == (
        logging.WARNING
        if status in {"unknown", "not_confirmed", "query_failed", "needs_review"}
        else logging.INFO
    )


@pytest.mark.asyncio
async def test_untrusted_tool_action_and_exception_text_never_logged(logs):
    @trace_management
    async def tool(self, e, action, value):
        raise RuntimeError("token=secret-exception")

    with pytest.raises(RuntimeError, match="secret-exception"):
        await tool(None, event(), "secret-action", "private-token")
    assert "action=invalid" in logs[-1][1]
    assert "secret" not in logs[-1][1]


@pytest.mark.asyncio
async def test_cancellation_is_preserved(logs):
    @trace_management
    async def tool(self, e, action, value):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await tool(None, event(), "config_confirm")
    assert "outcome=cancelled" in logs[-1][1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "private,admin,route",
    [
        (True, True, "admin_private"),
        (True, False, "customer_private"),
        (False, True, "customer_group"),
        (False, False, "customer_group"),
    ],
)
async def test_actual_ai_request_route_is_logged(logs, private, admin, route):
    plugin = object.__new__(Main)
    tools = SimpleNamespace(tools=["wsl_private_management"])
    tools.get_tool = Mock(return_value=True)
    tools.remove_tool = Mock()
    request = SimpleNamespace(func_tool=tools, contexts=[], system_prompt="")
    await plugin.plain_text_request(event(admin=admin, private=private), request)
    assert any(
        f"route={'customer_private' if private else 'customer_group'} " in text
        for _, text in logs
    )
    assert request.func_tool.tools == []
    assert tools.tools == ["wsl_private_management"]


def test_structured_diagnostics_reach_astrbot_log_broker(monkeypatch):
    broker = LogBroker()
    logger = logging.Logger("wsl_diagnostics_fixture")
    logger.addHandler(LogQueueHandler(broker))
    monkeypatch.setattr(diagnostics, "logger", logger)
    diagnostics.Diagnostics("fixture").emit("ai_route", "customer_only")
    entry = broker.log_cache[-1]
    assert entry["level"] == "INFO"
    assert "Wangshangliao instance=fixture stage=ai_route" in entry["data"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,expected",
    [
        ("disabled", "reply_disabled"),
        ("ineligible", "not_eligible"),
        ("managed", "managed_account_blocked"),
        ("cancelled", "cancelled"),
        ("empty", "empty_reply"),
    ],
)
async def test_reply_gate_logs_without_sending(logs, monkeypatch, case, expected):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        "astrbot.core.platform.sources.wangshangliao.event.is_managed_account",
        lambda _: case == "managed",
    )
    message = AstrBotMessage()
    message.type = MessageType.FRIEND_MESSAGE
    message.self_id = "1"
    message.session_id = "1/private/2/peer"
    message.group_id = ""
    message.message_id = "private-message-secret"
    message.sender = MessageMember("2", "private-name")
    message.message_str = "secret body"
    message.message = [Plain(message.message_str)]
    adapter = SimpleNamespace(
        config={"id": "fixture", "reply_private": case != "disabled"},
        meta=lambda: PlatformMetadata("wangshangliao", "test", id="test"),
        send_reply_text=AsyncMock(),
    )
    e = WangshangliaoEvent(message, adapter, case != "ineligible")
    if case == "cancelled":
        e.processing_completion.cancel()
    await e.send(MessageChain([] if case == "empty" else [Plain("private response")]))
    adapter.send_reply_text.assert_not_awaited()
    assert f"outcome={expected} " in logs[-1][1]
    assert "private-message-secret" not in logs[-1][1]
    assert "private response" not in logs[-1][1]
    e.processing_completion.cancel()
