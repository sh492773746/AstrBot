import asyncio
import base64
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from test_lajiaoliao_instance_management import Config, provision
from test_lajiaoliao_plugin_api import call, make_plugin

from data.plugins.astrbot_plugin_lajiaoliao.storage import SessionError
from data.plugins.astrbot_plugin_lajiaoliao.test_window import AcceptanceWindow


def test_test_window_is_closed_and_requires_account_and_targets(tmp_path, monkeypatch):
    plugin = make_plugin(tmp_path, monkeypatch)
    config = provision(plugin, "alice", "one")
    windows = AcceptanceWindow(plugin.store)
    ref = config["session_ref"]
    assert windows.update(ref, "alice", {})["open"] is False
    with pytest.raises(SessionError, match="explicit_test_target_required"):
        windows.update(ref, "alice", {"action": "open"})
    with pytest.raises(SessionError, match="session_access_denied"):
        windows.update(ref, "bob", {})
    for targets in ([], [{"kind": "all", "target_id": "any"}]):
        with pytest.raises(ValueError, match="invalid_test_targets"):
            windows.update(ref, "alice", {
                "action": "open", "confirm": True, "account_id": "account-one", "targets": targets,
            })


def test_window_budget_receipts_and_idempotency(tmp_path, monkeypatch):
    plugin = make_plugin(tmp_path, monkeypatch)
    config = provision(plugin, "alice", "one")
    ref = config["session_ref"]
    windows = AcceptanceWindow(plugin.store)
    state = windows.update(ref, "alice", {
        "action": "open", "confirm": True, "account_id": "account-one",
        "targets": [{"kind": "group", "target_id": "selected-test-group"}], "budget": 1,
    })
    body = {"window_ref": state["window_ref"], "operation_id": "test-one",
            "kind": "group", "target_id": "selected-test-group", "text": "synthetic"}
    with pytest.raises(SessionError, match="test_target_denied"):
        windows.reserve(ref, "alice", {**body, "target_id": "not-selected"})
    receipt, submit = windows.reserve(ref, "alice", body)
    assert submit and receipt["status"] == "unknown"
    # A crash between reservation and send/receipt never repeats an unknown send.
    restored = AcceptanceWindow(plugin.store)
    assert restored.reserve(ref, "alice", body) == (receipt, False)
    with pytest.raises(SessionError, match="operation_id_conflict"):
        restored.reserve(ref, "alice", {**body, "text": "changed"})
    with pytest.raises(SessionError, match="test_budget_exhausted"):
        restored.reserve(ref, "alice", {**body, "operation_id": "second"})
    windows.update(ref, "alice", {"action": "close"})
    assert windows.update(ref, "alice", {})["open"] is False


def test_window_allows_image_only_probe_and_binds_digest(tmp_path, monkeypatch):
    plugin = make_plugin(tmp_path, monkeypatch)
    config = provision(plugin, "alice", "one")
    ref = config["session_ref"]
    windows = AcceptanceWindow(plugin.store)
    state = windows.update(ref, "alice", {
        "action": "open", "confirm": True, "account_id": "account-one",
        "targets": [{"kind": "group", "target_id": "test-group"}], "budget": 1,
    })
    raw = io.BytesIO()
    Image.new("RGB", (1, 1), "red").save(raw, format="GIF")
    body = {
        "window_ref": state["window_ref"], "operation_id": "gif-only",
        "kind": "group", "target_id": "test-group",
        "image_base64": base64.b64encode(raw.getvalue()).decode(),
    }
    receipt, submit = windows.reserve(ref, "alice", body)
    assert submit and receipt["status"] == "unknown"
    assert windows.reserve(ref, "alice", body) == (receipt, False)
    raw = io.BytesIO()
    Image.new("RGB", (1, 1), "blue").save(raw, format="GIF")
    with pytest.raises(SessionError, match="operation_id_conflict"):
        windows.reserve(ref, "alice", {
            **body, "image_base64": base64.b64encode(raw.getvalue()).decode(),
        })


def test_api_image_only_probe_sends_no_extra_text(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path, monkeypatch)
        config = provision(plugin, "alice", "one")
        config["enable"] = True
        config["message_policy"]["group_ids"] = ["test-group"]
        plugin.context.get_config = lambda: Config(platform=[config])
        sender = AsyncMock(return_value={"status": "sent", "message_id": "gif-message"})
        plugin.context.get_platform_inst = lambda _: SimpleNamespace(send_parts=sender)
        plugin.acceptance = AcceptanceWindow(plugin.store)
        ref = config["session_ref"]
        _, window = await call(plugin, "test_window", "alice", {
            "session_ref": ref, "action": "open", "confirm": True,
            "account_id": "account-one",
            "targets": [{"kind": "group", "target_id": "test-group"}], "budget": 1,
        })
        raw = io.BytesIO()
        Image.new("RGB", (1, 1), "red").save(raw, format="GIF")
        image = base64.b64encode(raw.getvalue()).decode()
        body = {
            "session_ref": ref, "window_ref": window["window_ref"],
            "operation_id": "gif-only", "kind": "group", "target_id": "test-group",
            "image_base64": image,
        }
        status, receipt = await call(plugin, "test_send", "alice", body)
        assert status == 200 and receipt["status"] == "sent"
        sender.assert_awaited_once_with(
            "test-group", "", [{"type": "image", "url": "base64://" + image}], "group",
        )
        assert (await call(plugin, "test_send", "alice", body))[1] == receipt
        assert sender.await_count == 1

    asyncio.run(run())


def test_api_test_send_requires_window_policy_and_no_unknown_retry(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path, monkeypatch)
        config = provision(plugin, "alice", "one")
        config["enable"] = True
        config["message_policy"]["group_ids"] = ["test-group"]
        plugin.context.get_config = lambda: Config(platform=[config])
        sender = AsyncMock(side_effect=TimeoutError("unknown delivery"))
        plugin.context.get_platform_inst = lambda _: SimpleNamespace(send_parts=sender)
        plugin.acceptance = AcceptanceWindow(plugin.store)
        ref = config["session_ref"]
        body = {"session_ref": ref, "operation_id": "test-op", "kind": "group", "target_id": "test-group", "text": "synthetic"}
        assert (await call(plugin, "test_send", "alice", body))[0] == 409
        sender.assert_not_awaited()
        _, window = await call(plugin, "test_window", "alice", {
            "session_ref": ref, "action": "open", "confirm": True, "account_id": "account-one",
            "targets": [{"kind": "group", "target_id": "test-group"}],
        })
        body["window_ref"] = window["window_ref"]
        assert (await call(plugin, "test_send", "bob", body))[0] == 403
        assert (await call(plugin, "test_send", "alice", {**body, "target_id": "denied"}))[0] == 403
        status, result = await call(plugin, "test_send", "alice", body)
        assert status == 200 and result["status"] == "unknown"
        assert (await call(plugin, "test_send", "alice", body))[1] == result
        assert sender.await_count == 1
        config["message_policy"]["group_ids"] = []
        assert (await call(plugin, "test_send", "alice", body))[0] == 403
    asyncio.run(run())
