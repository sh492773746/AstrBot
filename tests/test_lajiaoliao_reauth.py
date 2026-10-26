import asyncio
import copy
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_lajiaoliao_plugin_api import call, make_plugin

from data.plugins.astrbot_plugin_lajiaoliao.reauth import Reauthorization
from data.plugins.astrbot_plugin_lajiaoliao.storage import SessionError, atomic_json


def provision(tmp_path, monkeypatch, enabled=False):
    plugin = make_plugin(tmp_path, monkeypatch)
    ref = plugin.store.create("alice")["session_ref"]
    plugin.store.complete(ref, "alice", "original-user")
    plugin.store.bind(ref, "alice", "bot-one")
    atomic_json(
        plugin.store.directory(ref) / "token.json",
        {
            "user_id": "original-user",
            "im_token": "old-im",
            "chat_token": "old-chat",
            "version": 3,
        },
    )
    atomic_json(
        plugin.store.directory(ref) / "cursor.json",
        {
            "user_id": "original-user",
            "sequences": {"group-test": 42},
        },
    )
    config = {
        "id": "bot-one",
        "type": "lajiaoliao",
        "session_ref": ref,
        "enable": enabled,
        "message_policy": {
            "group_mode": "allowlist",
            "group_ids": ["group-test"],
            "private_enabled": False,
        },
    }
    plugin.context.get_config = lambda: {"platform": [config]}
    plugin.context.platform_manager = SimpleNamespace(
        terminate_platform=AsyncMock(), reload=AsyncMock()
    )
    plugin.reauthorization = Reauthorization(plugin.store, Path("/not-used"))
    plugin.reauthorization.manager.start = AsyncMock()
    plugin.reauthorization.manager.stop = AsyncMock()
    plugin.reauthorization.manager.request = AsyncMock(return_value={"required": False})
    return plugin, config, {"session_ref": ref, "instance_id": "bot-one"}


@pytest.mark.parametrize("enabled", [False, True])
def test_reauth_keeps_identity_cursor_policy_and_enable(tmp_path, monkeypatch, enabled):
    async def run():
        plugin, config, body = provision(tmp_path, monkeypatch, enabled)
        ref = body["session_ref"]
        original_config = copy.deepcopy(config)
        original_binding = plugin.store._read(ref)
        cursor = (plugin.store.directory(ref) / "cursor.json").read_bytes()
        status, first = await call(plugin, "prepare_reauth", "alice", body)
        assert status == 200
        _, again = await call(plugin, "prepare_reauth", "alice", body)
        assert first["operation_ref"] == again["operation_ref"]
        assert "credential_revision" not in first and "expected_account_id" not in first
        plugin.reauthorization.manager.start.assert_awaited_with(
            first["operation_ref"], connect=False
        )
        op = first["operation_ref"]

        async def request(ref_, method, path, **kwargs):
            if path == "/v1/status":
                return {"authenticated": False}
            assert kwargs["json"]["expected_user_id"] == "original-user"
            atomic_json(
                plugin.reauthorization.staging.directory(ref_) / "token.json",
                {
                    "user_id": "original-user",
                    "im_token": "new-im",
                    "chat_token": "new-chat",
                },
            )
            return {"status": "logged_in", "account_id": "original-user"}

        plugin.reauthorization.manager.request.side_effect = request
        request_body = {
            **body,
            "operation_ref": op,
            "account": "13800000000",
            "password": "synthetic-password",
            "captcha_verify_param": "",
        }
        status, result = await call(plugin, "reauthenticate", "alice", request_body)
        assert status == 200 and result["state"] == "completed"
        assert config == original_config and plugin.store._read(ref) == original_binding
        assert (plugin.store.directory(ref) / "cursor.json").read_bytes() == cursor
        tokens = json.loads((plugin.store.directory(ref) / "token.json").read_text())
        assert tokens["im_token"] == "new-im" and tokens["version"] == 4
        assert not (
            plugin.reauthorization.staging.directory(op) / "token.json"
        ).exists()
        assert "new-im" not in json.dumps(
            result
        ) and "synthetic-password" not in json.dumps(result)
        plugin.context.platform_manager.terminate_platform.assert_awaited_once_with(
            "bot-one"
        )
        assert plugin.context.platform_manager.reload.await_count == int(enabled)
        # Completion is queryable and a duplicate request never submits a password.
        plugin.reauthorization.manager.request.reset_mock()
        assert (await call(plugin, "reauthenticate", "alice", request_body))[1][
            "state"
        ] == "completed"
        plugin.reauthorization.manager.request.assert_not_awaited()

    asyncio.run(run())


def test_reauth_foreign_owner_and_cross_instance_references(tmp_path, monkeypatch):
    async def run():
        plugin, _, body = provision(tmp_path, monkeypatch)
        assert (await call(plugin, "prepare_reauth", "bob", body))[0] == 403
        assert (
            await call(
                plugin, "prepare_reauth", "alice", {**body, "instance_id": "other"}
            )
        )[0] == 409
        plugin.reauthorization.manager.start.assert_not_awaited()
        _, first = await call(plugin, "prepare_reauth", "alice", body)
        foreign = plugin.reauthorization.staging.create("bob")
        assert (
            await call(
                plugin,
                "reauth_status",
                "alice",
                {
                    **body,
                    "operation_ref": foreign["session_ref"],
                },
            )
        )[0] == 403
        assert (
            await call(
                plugin,
                "cancel_reauth",
                "bob",
                {
                    **body,
                    "operation_ref": first["operation_ref"],
                },
            )
        )[0] == 403

    asyncio.run(run())


@pytest.mark.parametrize("scenario", ["mismatch", "generation", "deleted", "timeout"])
def test_failed_reauth_never_replaces_old_credentials(tmp_path, monkeypatch, scenario):
    async def run():
        plugin, config, body = provision(tmp_path, monkeypatch, True)
        ref = body["session_ref"]
        _, prepared = await call(plugin, "prepare_reauth", "alice", body)
        op = prepared["operation_ref"]
        if scenario == "generation":
            atomic_json(
                plugin.store.directory(ref) / "token.json",
                {
                    "user_id": "original-user",
                    "im_token": "renewed-while-verifying",
                    "chat_token": "old-chat",
                    "version": 4,
                },
            )
        if scenario == "deleted":
            plugin.context.get_config = lambda: {"platform": []}
        expected = (plugin.store.directory(ref) / "token.json").read_bytes()
        atomic_json(
            plugin.reauthorization.staging.directory(op) / "token.json",
            {
                "user_id": "wrong-user" if scenario == "mismatch" else "original-user",
                "im_token": "new-im",
                "chat_token": "new-chat",
            },
        )
        if scenario == "timeout":
            # The upstream response was lost, but credentials are already staged.
            plugin.reauthorization.manager.request.side_effect = SessionError(
                "protocol_request_failed"
            )
            status, result = await call(
                plugin, "reauth_status", "alice", {**body, "operation_ref": op}
            )
            assert status == 200 and result["state"] == "completed"
            return
        status, _ = await call(
            plugin, "reauth_status", "alice", {**body, "operation_ref": op}
        )
        assert status == 409
        assert (plugin.store.directory(ref) / "token.json").read_bytes() == expected
        assert config["enable"] is True
        plugin.context.platform_manager.terminate_platform.assert_not_awaited()

    asyncio.run(run())


def test_cancellation_expiry_and_recovery_do_not_touch_live_session(
    tmp_path, monkeypatch
):
    async def run():
        plugin, _, body = provision(tmp_path, monkeypatch)
        _, prepared = await call(plugin, "prepare_reauth", "alice", body)
        op = prepared["operation_ref"]
        expected = (
            plugin.store.directory(body["session_ref"]) / "token.json"
        ).read_bytes()
        assert (
            await call(plugin, "cancel_reauth", "alice", {**body, "operation_ref": op})
        )[0] == 200
        assert (await call(plugin, "reauth_status", "alice", body))[1][
            "state"
        ] == "none"
        _, prepared = await call(plugin, "prepare_reauth", "alice", body)
        op = prepared["operation_ref"]
        row = plugin.reauthorization.staging._read(op)
        row["expires_at"] = time.time() - 1
        plugin.reauthorization.staging._write(row)
        await plugin.reauthorization.expire()
        assert plugin.reauthorization.staging._read(op)["state"] == "revoked"
        assert (
            plugin.store.directory(body["session_ref"]) / "token.json"
        ).read_bytes() == expected
        _, prepared = await call(plugin, "prepare_reauth", "alice", body)
        row = plugin.reauthorization.staging._read(prepared["operation_ref"])
        row.update(
            state="committing", staged_revision=hashlib.sha256(expected).hexdigest()
        )
        plugin.reauthorization.staging._write(row)
        recovered = Reauthorization(plugin.store, Path("/not-used"))
        assert recovered.staging._read(row["session_ref"])["state"] == "completed"

    asyncio.run(run())
