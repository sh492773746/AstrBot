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
            log=lambda level, template, *args: entries.append((level, template % args))
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
    for _, entry in logs:
        assert f"correlation={diagnostics.reference(e.message_obj.message_id)}" in entry
        assert "private-message-secret" not in entry
        assert "private-session-secret" not in entry
        assert "sensitive-user" not in entry
        assert "sensitive-group" not in entry
        assert "secret-body" not in entry


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
    assert json.loads(result)["reason"] == "private_admin_required"
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
    assert any(f"route={route} " in text for _, text in logs)
    assert tools.remove_tool.call_count == (0 if private and admin else 1)


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
