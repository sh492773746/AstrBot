import asyncio
from unittest.mock import AsyncMock

from test_lajiaoliao_plugin_api import call, make_plugin


def test_inline_login_binds_without_exposing_credentials(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path, monkeypatch)
        plugin.manager.start = AsyncMock()
        plugin.manager.request.return_value = {"required": True, "scene_id": "test", "region": "cn", "prefix": "test"}
        status, prepared = await call(plugin, "prepare_login", "alice", {"instance_id": "bot-one"})
        assert status == 200 and prepared["challenge"]["required"] is True
        ref = prepared["session_ref"]
        body = {"session_ref": ref, "instance_id": "bot-one", "account": "13800000000",
                "password": "synthetic-password", "captcha_verify_param": "synthetic-proof"}
        assert (await call(plugin, "login", "bob", body))[0] == 403
        assert (await call(plugin, "login", "alice", {**body, "instance_id": "bot-two"}))[0] == 409
        plugin.manager.request.side_effect = [{"authenticated": False}, {"status": "logged_in", "account_id": "test-account", "token": "never-return"}]
        status, result = await call(plugin, "login", "alice", body)
        assert status == 200 and result["state"] == "bound"
        assert result["instance_id"] == "bot-one" and result["account_hint"] == "138****0000"
        assert "token" not in result and "password" not in result
        assert "synthetic-password" not in (plugin.store.directory(ref) / "session.json").read_text()
        assert (await call(plugin, "login", "alice", body))[0] == 409
        plugin.manager.stop.assert_awaited_once_with(ref)
    asyncio.run(run())


def test_prepare_is_idempotent_and_poll_recovers_completed_login(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path, monkeypatch)
        plugin.manager.start = AsyncMock()
        plugin.manager.request.return_value = {"required": False}
        _, first = await call(plugin, "prepare_login", "alice", {"instance_id": "bot-one"})
        _, second = await call(plugin, "prepare_login", "alice", {"instance_id": "bot-one"})
        assert first["session_ref"] == second["session_ref"]
        plugin.manager.request.return_value = {"authenticated": True, "account_id": "test-account"}
        status, result = await call(plugin, "poll", "alice", {"session_ref": first["session_ref"]})
        assert status == 200 and result["state"] == "bound" and result["instance_id"] == "bot-one"
    asyncio.run(run())


def test_multiple_instance_drafts_do_not_block_each_other(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path, monkeypatch)
        plugin.manager.start = AsyncMock()
        plugin.manager.request.return_value = {"required": False}
        legacy = plugin.store.create("alice")
        refs = []
        for index in range(7):
            status, result = await call(plugin, "prepare_login", "alice", {"instance_id": f"bot-{index}"})
            assert status == 200
            refs.append(result["session_ref"])
        assert len(set(refs + [legacy["session_ref"]])) == 8
        status, resumed = await call(plugin, "prepare_login", "alice", {"instance_id": "bot-0"})
        assert status == 200 and resumed["session_ref"] == refs[0]
        status, _ = await call(plugin, "prepare_login", "alice", {"instance_id": "bot-over-limit"})
        assert status == 409
        plugin.store.revoke(refs[1], "alice")
        assert (await call(plugin, "prepare_login", "alice", {"instance_id": "bot-new"}))[0] == 200
        assert plugin.store.get(refs[0], "alice")["state"] == "draft"
    asyncio.run(run())


def test_slow_prepare_does_not_block_another_instance(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path, monkeypatch)
        plugin.manager.start = AsyncMock()
        entered, release = asyncio.Event(), asyncio.Event()

        async def request(ref, *args, **kwargs):
            if plugin.store._read(ref)["pending_instance_id"] == "slow":
                entered.set()
                await release.wait()
            return {"required": False}

        plugin.manager.request.side_effect = request
        slow = asyncio.create_task(call(plugin, "prepare_login", "alice", {"instance_id": "slow"}))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            result = await asyncio.wait_for(call(plugin, "prepare_login", "alice", {"instance_id": "fast"}), 1)
            assert result[0] == 200
        finally:
            release.set()
            await slow
    asyncio.run(run())


def test_cancel_only_owned_unfinished_draft(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path, monkeypatch)
        ref = plugin.store.create("alice", pending_instance_id="one")["session_ref"]
        body = {"session_ref": ref}
        assert (await call(plugin, "cancel_login", "bob", body))[0] == 403
        plugin.manager.workers[ref] = object()
        plugin.manager.request.return_value = {"authenticated": True}
        assert (await call(plugin, "cancel_login", "alice", body))[0] == 409
        plugin.manager.revoke.assert_not_awaited()
        plugin.manager.request.return_value = {"authenticated": False}
        assert (await call(plugin, "cancel_login", "alice", body))[0] == 200
        plugin.manager.revoke.assert_awaited_once_with(ref, "alice")
        plugin.store.complete(ref, "alice", "account")
        assert (await call(plugin, "cancel_login", "alice", body))[0] == 409
    asyncio.run(run())


def test_existing_account_cannot_be_overwritten_and_orphan_repair_is_explicit(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path, monkeypatch)
        ref = plugin.store.create("alice")["session_ref"]
        plugin.store.complete(ref, "alice", "old-account")
        plugin.store.bind(ref, "alice", "removed-bot")
        configs = [{"id": "current-bot", "type": "lajiaoliao", "session_ref": ref}]
        plugin.context.get_config = lambda: {"platform": configs}
        assert (await call(plugin, "prepare_login", "alice", {"instance_id": "current-bot"}))[0] == 409
        body = {"session_ref": ref, "instance_id": "current-bot"}
        assert (await call(plugin, "connect_session", "alice", body))[0] == 409
        body["confirm"] = True
        assert (await call(plugin, "connect_session", "bob", body))[0] == 403
        configs.append({"id": "removed-bot", "type": "lajiaoliao"})
        assert (await call(plugin, "connect_session", "alice", body))[0] == 409
        configs.pop()
        plugin.manager.workers[ref] = object()
        assert (await call(plugin, "connect_session", "alice", body))[0] == 409
        plugin.manager.workers.clear()
        status, result = await call(plugin, "connect_session", "alice", body)
        assert status == 200 and result["instance_id"] == "current-bot"
        assert result["account_id"] == "old-account"
    asyncio.run(run())


def test_revoke_only_unconfigured_sessions(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path, monkeypatch)
        ref = plugin.store.create("alice", pending_instance_id="former")["session_ref"]
        plugin.store.complete(ref, "alice", "account")
        plugin.store.bind(ref, "alice", "former")
        config = {"platform": [{"type": "lajiaoliao", "id": "former", "session_ref": ref, "enable": False}]}
        plugin.context.get_config = lambda: config
        body = {"session_ref": ref, "confirm": True}
        assert (await call(plugin, "revoke", "bob", body))[0] == 403
        assert (await call(plugin, "revoke", "alice", body))[0] == 409
        plugin.manager.revoke.assert_not_awaited()
        config["platform"].clear()
        assert (await call(plugin, "revoke", "alice", body))[0] == 200
        plugin.manager.revoke.assert_awaited_once_with(ref, "alice")
    asyncio.run(run())
