"""Offline imports retain AstrBot logging without opening live log files."""

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("disabled", ["1", "TRUE", "0", None])
def test_tool_logging_override_does_not_change_saved_configuration(tmp_path, disabled):
    """Exercise a fresh import against an isolated real configuration and SQLite DB."""
    root = tmp_path / "application"
    data = root / "data"
    data.mkdir(parents=True)
    logs = tmp_path / "service-logs"
    config = {
        "log_file_enable": True,
        "log_file_path": str(logs / "astrbot.log"),
        "trace_log_enable": True,
        "trace_log_path": str(logs / "astrbot.trace.log"),
    }
    (data / "cmd_config.json").write_text(json.dumps(config))
    environment = dict(os.environ, ASTRBOT_ROOT=str(root), ASTRBOT_DISABLE_METRICS="1")
    if disabled is None:
        environment.pop("ASTRBOT_DISABLE_FILE_LOGGING", None)
    else:
        environment["ASTRBOT_DISABLE_FILE_LOGGING"] = disabled
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import astrbot.core as core; "
            "assert core.astrbot_config['log_file_enable']; "
            "assert core.astrbot_config['trace_log_enable']; "
            "assert core.logger.name == 'astrbot'; "
            "assert core.LogManager is not None; "
            "core.LogManager.configure_logger(core.logger, core.astrbot_config); "
            "from astrbot.core.utils.trace import TraceSpan; "
            "TraceSpan('offline-test').record('offline-record'); "
            "core.LogManager.configure_trace_logger(core.astrbot_config)",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    expected_files = disabled not in {"1", "TRUE"}
    assert (logs / "astrbot.log").exists() == expected_files
    assert (logs / "astrbot.trace.log").exists() == expected_files
    saved = json.loads((data / "cmd_config.json").read_text(encoding="utf-8-sig"))
    assert saved["log_file_enable"] is True
    assert saved["trace_log_enable"] is True


def test_trace_file_permission_failure_does_not_abort_startup(monkeypatch, caplog):
    """Keep the service alive without broadening permissions or exposing paths."""
    from astrbot.core.log import LogManager

    def denied(**_kwargs):
        raise PermissionError("private-file-path-must-not-be-logged")

    monkeypatch.setattr(LogManager, "_add_file_sink", denied)
    monkeypatch.setattr(LogManager, "_trace_sink_id", None)
    monkeypatch.delenv("ASTRBOT_DISABLE_FILE_LOGGING", raising=False)
    logger = logging.getLogger("astrbot")
    logger.addHandler(caplog.handler)
    try:
        LogManager.configure_trace_logger({"trace_log_enable": True})
    finally:
        logger.removeHandler(caplog.handler)
    assert LogManager._trace_sink_id is None
    messages = [record.getMessage() for record in caplog.records]
    assert any("PermissionError" in message for message in messages)
    assert all(
        "private-file-path-must-not-be-logged" not in message for message in messages
    )
