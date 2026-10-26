import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def load_tool():
    path = Path(__file__).resolve().parents[1] / "services/lajiaoliao-bridge/rollback_plugin_code.py"
    spec = importlib.util.spec_from_file_location("lajiaoliao_code_rollback", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def layout(tmp_path):
    root, backup = tmp_path / "astrbot", tmp_path / "backup"
    root.mkdir()
    backup.mkdir(mode=0o700)
    for base in (root / "data/plugins/astrbot_plugin_lajiaoliao", backup / "plugin"):
        (base / "bin").mkdir(parents=True)
        binary = base / "bin/lajiaoliao-bridge"
        binary.write_bytes(b"synthetic-binary")
        binary.with_suffix(".sha256").write_text(hashlib.sha256(binary.read_bytes()).hexdigest())
        (base / "metadata.yaml").write_text("old" if base.parent == backup else "new")
    for base in (root / "dashboard/dist", backup / "dist"):
        base.mkdir(parents=True)
        (base / "index.html").write_text("old" if base.parent == backup else "new")
    (root / "data/cmd_config.json").write_text('{"platform":[]}')
    state = root / "data/plugin_data/astrbot_plugin_lajiaoliao"
    state.mkdir(parents=True)
    (state / "token.json").write_text("latest-credential-placeholder")
    return root, backup, state


def test_rollback_code_preserves_config_and_latest_account_files(tmp_path, monkeypatch):
    tool = load_tool()
    root, backup, state = layout(tmp_path)
    monkeypatch.setattr(tool.subprocess, "run", Mock(return_value=SimpleNamespace(returncode=3)))
    tool.restore(root, backup)
    assert (root / "dashboard/dist/index.html").read_text() == "old"
    assert (root / "data/plugins/astrbot_plugin_lajiaoliao/metadata.yaml").read_text() == "old"
    assert (state / "token.json").read_text() == "latest-credential-placeholder"
    assert (root / "data/cmd_config.json").read_text() == '{"platform":[]}'


@pytest.mark.parametrize("scenario", ["running", "public", "checksum"])
def test_rollback_checks_do_not_modify_code(tmp_path, monkeypatch, scenario):
    tool = load_tool()
    root, backup, _ = layout(tmp_path)
    monkeypatch.setattr(tool.subprocess, "run", Mock(return_value=SimpleNamespace(returncode=0 if scenario == "running" else 3)))
    if scenario == "public":
        backup.chmod(0o755)
    if scenario == "checksum":
        (backup / "plugin/bin/lajiaoliao-bridge").write_bytes(b"modified")
    with pytest.raises(RuntimeError):
        tool.restore(root, backup)
    assert (root / "dashboard/dist/index.html").read_text() == "new"
    assert (root / "data/plugins/astrbot_plugin_lajiaoliao/metadata.yaml").read_text() == "new"


def test_plugin_only_rollback_keeps_dashboard_and_business_ledger(tmp_path, monkeypatch):
    tool = load_tool()
    root, backup, state = layout(tmp_path)
    (state / "business.sqlite3").write_bytes(b"latest-business-placeholder")
    (backup / "dist/index.html").unlink()
    monkeypatch.setattr(
        tool.subprocess, "run",
        Mock(return_value=SimpleNamespace(returncode=3)),
    )
    tool.restore(root, backup, plugin_only=True)
    assert (root / "dashboard/dist/index.html").read_text() == "new"
    assert (root / "data/plugins/astrbot_plugin_lajiaoliao/metadata.yaml").read_text() == "old"
    assert (state / "business.sqlite3").read_bytes() == b"latest-business-placeholder"
    assert (state / "token.json").read_text() == "latest-credential-placeholder"
