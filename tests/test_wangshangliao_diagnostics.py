from unittest.mock import Mock
from astrbot.core.platform.sources.wangshangliao import diagnostics


def test_redaction_and_aggregation(monkeypatch):
    monkeypatch.setattr(diagnostics, "FAILURES", {})
    output = Mock()
    monkeypatch.setattr(diagnostics, "logger", output)
    clock = iter([100, 101, 102, 161])
    monkeypatch.setattr(diagnostics.time, "monotonic", lambda: next(clock))
    d = diagnostics.Diagnostics("bot_test")
    for _ in range(4):
        d.emit(
            "connection", "retry_scheduled", "private-sensitive-identifier", failed=True
        )
    assert output.log.call_count == 2
    assert output.log.call_args.args[-1] == 3
    assert "private-sensitive-identifier" not in str(output.log.call_args_list)
    d.emit("connection", "online")
    assert output.log.call_count == 3


def test_instance_windows_and_error_redaction(monkeypatch):
    monkeypatch.setattr(diagnostics, "FAILURES", {})
    output = Mock()
    monkeypatch.setattr(diagnostics, "logger", output)
    monkeypatch.setattr(diagnostics.time, "monotonic", lambda: 100)
    for _ in range(2):
        diagnostics.Diagnostics("one").emit("login", "failed", failed=True, error="invalid_credentials")
    assert output.log.call_count == 1
    diagnostics.Diagnostics("two").emit("login", "failed", failed=True, error="invalid_credentials")
    diagnostics.Diagnostics("one").emit("login", "failed", failed=True, error="transport")
    assert output.log.call_count == 3
    diagnostics.Diagnostics("one").emit("login", "failed", failed=True, error="password=secret")
    assert "password=secret" not in str(output.log.call_args_list)
    assert "other" in output.log.call_args.args
