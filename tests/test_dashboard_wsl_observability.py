from types import SimpleNamespace

import pytest

from astrbot.core.log import LogBroker
from astrbot.dashboard.services.conversation_service import ConversationService
from astrbot.dashboard.services.log_service import LogService


def test_archive_recovers_multiline_logs_and_trace_after_restart(tmp_path):
    import json
    trace = tmp_path / "trace.log"
    log = tmp_path / "system.log"
    payload = {"type": "trace", "time": 1, "span_id": "test", "fields": {"text": "long" * 1000}}
    trace.write_text('[2026-09-19 21:00:00.123] ' + json.dumps(payload) + '\n', encoding="utf-8")
    log.write_text('[2026-09-19 21:00:00.124] [Core] [ERROR]: failed\nTraceback line\n', encoding="utf-8")
    service = LogService(LogBroker(), {"log_file_enable": True, "log_file_path": str(log), "trace_log_enable": True, "trace_log_path": str(trace)})
    rows = service.get_log_history()["logs"]
    assert payload in rows
    assert any(row.get("level") == "ERROR" and "Traceback line" in row["data"] for row in rows)


def test_archive_disabled_does_not_read_files(tmp_path):
    service = LogService(LogBroker(), {"log_file_path": str(tmp_path / "missing")})
    assert service.get_log_history() == {"logs": []}


def test_archive_preserves_category_and_marks_legacy_unknown(tmp_path):
    log = tmp_path / "system.log"
    log.write_text("[2026-09-19 21:00:00.124] [INFO] [category=user_chat]: private\n[2026-09-19 21:00:00.125] [INFO]: legacy\n")
    rows = LogService(LogBroker(), {"log_file_enable": True, "log_file_path": str(log)}).get_log_history()["logs"]
    assert [r["category"] for r in rows] == ["user_chat", "unknown"]


@pytest.mark.asyncio
async def test_context_only_selection_uses_capability_not_plugin_name(monkeypatch):
    from astrbot.core.pipeline.waking_check import stage

    enabled = SimpleNamespace(extras_configs={"context_only": True})
    ordinary = SimpleNamespace(extras_configs={})
    monkeypatch.setattr(stage.star_handlers_registry, "get_handlers_by_event_type", lambda _: [enabled, ordinary])
    extras = {"_context_only": True}
    event = SimpleNamespace(get_extra=extras.get, set_extra=extras.__setitem__)
    await stage.WakingCheckStage.process(None, event)
    assert extras["activated_handlers"] == [enabled]
    assert event.is_wake is False


@pytest.mark.parametrize("session,expected", [("979/private/236/abc", "旺商聊私聊 · 236"), ("979/1143980", "旺商聊群聊 · 1143980")])
def test_wsl_title_fallback_keeps_manual_title(session, expected):
    service = ConversationService(None, SimpleNamespace(conversation_manager=None))
    conversation = SimpleNamespace(platform_id="wangshangliao_test", user_id=f"wangshangliao_test:FriendMessage:{session}", cid="c", title=None, persona_id=None, token_usage=0, created_at=None, updated_at=None)
    assert service._serialize_conversation(conversation, {}, include_history=False)["title"] == expected
    conversation.title = "Manual title"
    assert service._serialize_conversation(conversation, {}, include_history=False)["title"] == "Manual title"


@pytest.mark.asyncio
async def test_log_subscription_precedes_replay():
    broker = LogBroker()
    service = LogService(broker, {})

    async def replay(_):
        assert len(broker.subscribers) == 1
        broker.publish({"time": 2, "data": "during replay"})
        yield "history"

    service.replay_cached_logs = replay
    stream = service.stream_log_events("1")
    assert await anext(stream) == "history"
    assert "during replay" in await anext(stream)
    await stream.aclose()
    assert not broker.subscribers
