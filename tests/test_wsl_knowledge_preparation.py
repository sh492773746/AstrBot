"""Ensure exported knowledge drafts do not expose message credentials."""

import importlib.util
from pathlib import Path


def test_redaction_removes_credentials_and_destinations():
    path = Path(__file__).resolve().parents[1] / "scripts" / "prepare_wsl_knowledge.py"
    spec = importlib.util.spec_from_file_location("prepare_wsl_knowledge", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    text = "meeting\n会议 ID：123 456 789\n密码334455\nhttp://example.com/access?token=secret\nexample.vip\n@person 13800138000"
    result = module.redact(text)
    for secret in (
        "123 456 789",
        "334455",
        "token=secret",
        "example.vip",
        "@person",
        "13800138000",
    ):
        assert secret not in result
    assert "meeting" in result
