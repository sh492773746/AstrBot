import copy
import importlib.util
import io
import tarfile
import time
from pathlib import Path

import pytest

from data.plugins.astrbot_plugin_lajiaoliao.business import DEFAULTS, BusinessStore
from data.plugins.astrbot_plugin_lajiaoliao.storage import SessionStore, atomic_json


def release_tool(monkeypatch):
    directory = Path(__file__).resolve().parents[1] / "services/lajiaoliao-bridge"
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location(
        "lajiaoliao_release_tool", directory / "reload_release.py"
    )
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    return tool


def fixture_account(tmp_path, monkeypatch):
    tool = release_tool(monkeypatch)
    monkeypatch.setattr(tool, "DATA", tmp_path)
    sessions = SessionStore(tmp_path)
    ref = sessions.create("owner")["session_ref"]
    sessions.complete(ref, "owner", "user")
    sessions.bind(ref, "owner", "instance")
    config = {
        "platform": [
            {
                "type": "lajiaoliao",
                "id": "instance",
                "session_ref": ref,
                "enable": True,
            },
            {"type": "telegram", "id": "other", "enable": True},
        ]
    }
    store = BusinessStore(sessions.directory(ref))
    store.save_settings("g", 0, copy.deepcopy(DEFAULTS), "dashboard:owner")
    return tool, sessions, ref, config, store


def test_release_compares_settings_and_bindings_not_live_credentials(
    tmp_path, monkeypatch
):
    tool, sessions, ref, config, store = fixture_account(tmp_path, monkeypatch)
    baseline = tool.metadata_snapshot(config)
    atomic_json(sessions.directory(ref) / "token.json", {"test_only": "new"})
    atomic_json(sessions.directory(ref) / "cursor.json", {"test_only": 2})
    assert tool.metadata_snapshot(config) == baseline
    updated = copy.deepcopy(DEFAULTS)
    updated["rankings_enabled"] = True
    store.save_settings("g", 1, updated, "dashboard:owner")
    assert tool.metadata_snapshot(config) != baseline


def test_release_rejects_authorization_and_nearby_tasks_without_mutation(
    tmp_path, monkeypatch
):
    tool, sessions, ref, config, store = fixture_account(tmp_path, monkeypatch)
    baseline = tool.metadata_snapshot(config)
    draft = sessions.create("owner")["session_ref"]
    with pytest.raises(RuntimeError, match="authorization_in_progress"):
        tool.metadata_snapshot(config)
    sessions.revoke(draft, "owner")
    with store.connect() as db:
        db.execute(
            "INSERT INTO scheduled_jobs VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "nearby",
                "g",
                "quiet",
                1,
                "dashboard:owner",
                time.time() + 20,
                "pending",
                "{}",
                "",
            ),
        )
    with pytest.raises(RuntimeError, match="nearby_scheduled"):
        tool.metadata_snapshot(config)
    assert tool.metadata_snapshot(config, check_work=False) == baseline
    assert store.scheduled("g")[0]["state"] == "pending"


@pytest.mark.parametrize("reference", ["../bad", "g" * 32, "a" * 33])
def test_release_rejects_external_session_paths(tmp_path, monkeypatch, reference):
    tool = release_tool(monkeypatch)
    monkeypatch.setattr(tool, "DATA", tmp_path)
    with pytest.raises(RuntimeError, match="invalid_session_reference"):
        tool.metadata_snapshot(
            {"platform": [{"type": "lajiaoliao", "session_ref": reference}]}
        )


@pytest.mark.parametrize(
    "name",
    [
        "astrbot_plugin_lajiaoliao/token.json",
        "astrbot_plugin_lajiaoliao/pages/accounts/index.html",
        "../outside",
        "/absolute",
        "astrbot_plugin_lajiaoliao/business.sqlite3",
    ],
)
def test_package_audit_refuses_private_and_unsafe_resources(tmp_path, name):
    path = (
        Path(__file__).resolve().parents[1]
        / "services/lajiaoliao-bridge/audit_release.py"
    )
    spec = importlib.util.spec_from_file_location("lajiaoliao_package_audit", path)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    package = tmp_path / "runtime.tar.gz"
    with tarfile.open(package, "w:gz") as archive:
        member = tarfile.TarInfo(name)
        member.size = 1
        archive.addfile(member, io.BytesIO(b"x"))
    with pytest.raises(RuntimeError, match="unsafe_or_private"):
        tool.inspect_archive(package, "astrbot_plugin_lajiaoliao")
