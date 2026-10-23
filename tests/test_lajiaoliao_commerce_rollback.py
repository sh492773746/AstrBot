"""Never downgrade commercial accounts into a version with no entitlement gate."""

import importlib.util
import json
from pathlib import Path

import pytest
from test_lajiaoliao_commerce import mutation, prepared


def guard():
    path = (
        Path(__file__).resolve().parents[1]
        / "services/lajiaoliao-bridge/commerce_rollback_guard.py"
    )
    spec = importlib.util.spec_from_file_location("commerce_rollback_guard", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.require_safe_commerce_rollback


def test_commercial_legacy_downgrade_refused_preserves_ledger(tmp_path):
    root = tmp_path / "deployment"
    store, customer, _ = prepared(root / "data/plugin_data/astrbot_plugin_lajiaoliao")
    config = root / "data/cmd_config.json"
    config.write_text(json.dumps({"platform": [{"id": "bot", "enable": False}]}))
    backup = tmp_path / "plugin"
    backup.mkdir()
    before = store.path.read_bytes()
    with pytest.raises(RuntimeError, match="Legacy backup"):
        guard()(root, backup)
    assert store.path.read_bytes() == before
    (backup / "commerce.py").write_text("# gated backup fixture\n")
    with pytest.raises(RuntimeError, match="Pause commercial"):
        guard()(root, backup)
    mutation(store, "customers", "pause", 3, customer_id=customer)
    guard()(root, backup)
    config.write_text(json.dumps({"platform": [{"id": "bot", "enable": True}]}))
    with pytest.raises(RuntimeError, match="Disable all commercial"):
        guard()(root, backup)


def test_no_commercial_accounts_do_not_block_internal_rollback(tmp_path):
    guard()(tmp_path, tmp_path / "legacy-plugin")
