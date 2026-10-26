import copy
import importlib.util
import json
from pathlib import Path

import pytest

from data.plugins.astrbot_plugin_lajiaoliao.business import DEFAULTS, BusinessStore


@pytest.fixture
def tool(monkeypatch):
    directory = Path(__file__).resolve().parents[1] / "services/lajiaoliao-bridge"
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location(
        "lajiaoliao_live_recall_tool", directory / "reply_recall_acceptance_live.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_existing_ledger_does_not_run_startup_recovery(tool, tmp_path):
    store = BusinessStore(tmp_path)
    settings = copy.deepcopy(DEFAULTS)
    settings["maintenance"]["reply_recall_enabled"] = True
    settings["native_actions"] = ["reply_recall"]
    store.save_settings("g", 0, settings, "dashboard:operator")
    store.schedule_reply("g", "running-message", "group", "g", "dashboard:operator")
    with store.connect() as db:
        db.execute("UPDATE scheduled_jobs SET state='running'")
    ledger = tool.ExistingReplyLedger(store.path)
    ledger.schedule_reply("g", "new-message", "group", "g", "dashboard:operator")
    ledger.schedule_reply("g", "new-message", "group", "g", "dashboard:operator")
    with ledger.connect() as db:
        rows = db.execute("SELECT * FROM scheduled_jobs").fetchall()
    assert len(rows) == 2
    assert (
        next(
            row["state"]
            for row in rows
            if json.loads(row["payload"])["message_id"] == "running-message"
        )
        == "running"
    )
    assert (
        next(
            row["state"]
            for row in rows
            if json.loads(row["payload"])["message_id"] == "new-message"
        )
        == "pending"
    )


def test_private_ledger_required(tool, tmp_path):
    path = tmp_path / "business.sqlite3"
    with pytest.raises(RuntimeError, match="existing_private_ledger_required"):
        tool.ExistingReplyLedger(path)
    store = BusinessStore(tmp_path)
    store.path.chmod(0o644)
    with pytest.raises(RuntimeError, match="existing_private_ledger_required"):
        tool.ExistingReplyLedger(store.path)
    store.path.chmod(0o600)
    symlink = tmp_path / "link.sqlite3"
    symlink.symlink_to(store.path)
    with pytest.raises(RuntimeError, match="existing_private_ledger_required"):
        tool.ExistingReplyLedger(symlink)


def test_disabled_feature_never_schedules_probe(tool, tmp_path):
    store = BusinessStore(tmp_path)
    store.save_settings("g", 0, copy.deepcopy(DEFAULTS), "dashboard:operator")
    ledger = tool.ExistingReplyLedger(store.path)
    ledger.schedule_reply("g", "message", "private", "uid", "dashboard:operator")
    with ledger.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM scheduled_jobs").fetchone()[0] == 0
