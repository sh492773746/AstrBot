import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from weakref import WeakValueDictionary

from astrbot.api.web import bind_request_context
from data.plugins.astrbot_plugin_lajiaoliao import runtime
from data.plugins.astrbot_plugin_lajiaoliao.main import LajiaoLiaoPlugin
from data.plugins.astrbot_plugin_lajiaoliao.storage import SessionStore


def make_plugin(tmp_path, monkeypatch):
    plugin = object.__new__(LajiaoLiaoPlugin)
    plugin.store = SessionStore(tmp_path)
    plugin.manager = SimpleNamespace(
        workers={}, request=AsyncMock(), directory=AsyncMock(), stop=AsyncMock(), revoke=AsyncMock(),
    )
    plugin.context = SimpleNamespace(get_platform_inst=lambda _: None, get_config=lambda: {})
    plugin._operations = WeakValueDictionary()
    plugin._stopping = False
    monkeypatch.setattr(runtime, "active", plugin.manager)
    return plugin


async def call(plugin, method, owner, body=None):
    context = SimpleNamespace(username=owner, json=AsyncMock(return_value=body or {}))
    with bind_request_context(context):
        response = await getattr(plugin, method)()
    return response.status_code, json.loads(response.body)


def test_api_does_not_expose_other_owners(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path, monkeypatch)
        row = plugin.store.create("alice")
        status, data = await call(plugin, "sessions", "bob")
        assert status == 200 and data["sessions"] == []
        status, _ = await call(plugin, "poll", "bob", {"session_ref": row["session_ref"]})
        assert status == 403
        plugin.manager.request.assert_not_called()
        status, _ = await call(plugin, "sessions", "")
        assert status == 403
    asyncio.run(run())


def test_poll_saves_identity_then_stops_authorization_worker(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path, monkeypatch)
        row = plugin.store.create("alice")
        ref = row["session_ref"]
        plugin.manager.request.return_value = {
            "authenticated": True, "account_id": "test-user",
            "token": "synthetic-never-return",
        }
        status, data = await call(plugin, "poll", "alice", {"session_ref": ref})
        assert status == 200 and data["state"] == "authorized"
        assert "token" not in data
        plugin.manager.stop.assert_awaited_once_with(ref)
        await call(plugin, "poll", "alice", {"session_ref": ref})
        assert plugin.manager.request.await_count == 1
    asyncio.run(run())


def test_recovery_requires_confirmation_and_preserves_owner(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path, monkeypatch)
        ref = plugin.store.create("alice")["session_ref"]
        plugin.store.complete(ref, "alice", "test-user")
        body = {"session_ref": ref, "instance_id": "test-bot"}
        assert (await call(plugin, "connect_session", "alice", body))[0] == 409
        body["confirm"] = True
        assert (await call(plugin, "connect_session", "bob", body))[0] == 403
        status, data = await call(plugin, "connect_session", "alice", body)
        assert status == 200 and data["instance_id"] == "test-bot"
    asyncio.run(run())


def test_staged_plugin_does_not_register_adapter(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path, monkeypatch)
        plugin.logger = SimpleNamespace(info=lambda *_: None)
        plugin.context.get_config = lambda: {"platform": [{"type": "lajiaoliao"}]}
        monkeypatch.setattr(runtime, "active", None)
        await plugin.initialize()
        assert runtime.active is None
    asyncio.run(run())


def test_terminate_stops_owned_instances_before_runtime(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path, monkeypatch)
        calls = []
        owned = SimpleNamespace(manager=plugin.manager, meta=lambda: SimpleNamespace(name="lajiaoliao", id="ours"))
        other = SimpleNamespace(meta=lambda: SimpleNamespace(name="other", id="untouched"))
        async def terminate(instance):
            calls.append(instance)
        async def close():
            calls.append("runtime")
        plugin.context.platform_manager = SimpleNamespace(
            get_insts=lambda: [owned, other], terminate_platform=terminate,
        )
        plugin.manager.close = close
        plugin._sweeper = None
        await plugin.terminate()
        assert calls == ["ours", "runtime"]
        assert plugin.context._lajiaoliao_reload_ids == ["ours"]
        assert runtime.active is None
    asyncio.run(run())


def test_directory_requires_owned_bound_session(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path, monkeypatch)
        ref = plugin.store.create("alice")["session_ref"]
        body = {"session_ref": ref}
        assert (await call(plugin, "groups", "bob", body))[0] == 403
        assert (await call(plugin, "groups", "alice", body))[0] == 409
        plugin.manager.request.assert_not_called()
        plugin.store.complete(ref, "alice", "test-user")
        plugin.store.bind(ref, "alice", "test-bot")
        plugin.manager.directory.return_value = {"groups": []}
        assert (await call(plugin, "groups", "alice", body))[0] == 200
        plugin.manager.directory.assert_awaited_once_with(ref, "/v1/groups", page=1)
    asyncio.run(run())


def test_official_extension_dispatch_preserves_identity_and_errors(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from astrbot.dashboard.api.auth import AuthContext
    from astrbot.dashboard.api.plugins import require_plugin_scope, router
    from astrbot.dashboard.asgi_runtime import FastAPIAppAdapter

    plugin = make_plugin(tmp_path, monkeypatch)
    ref = plugin.store.create("alice")["session_ref"]
    plugin.store.complete(ref, "alice", "synthetic-test-account")
    app = FastAPI()
    FastAPIAppAdapter(app)
    app.state.core_lifecycle = SimpleNamespace(star_context=SimpleNamespace(
        registered_web_apis=[
            (f"/astrbot_plugin_lajiaoliao/{name}", getattr(plugin, name), [method], name)
            for name, method in [("sessions", "GET"), ("connect_session", "POST")]
        ],
    ))
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[require_plugin_scope] = lambda: AuthContext("alice", ["plugin"])
    base = "/api/v1/plugins/extensions/astrbot_plugin_lajiaoliao"
    with TestClient(app) as client:
        response = client.get(f"{base}/sessions")
        assert response.status_code == 200
        assert response.json()["sessions"][0]["session_ref"] == ref
        body = {"session_ref": ref, "instance_id": "fixture-bot", "confirm": True}
        app.dependency_overrides[require_plugin_scope] = lambda: AuthContext("bob", ["plugin"])
        response = client.post(f"{base}/connect_session", json=body)
        assert response.status_code == 403
        assert response.json()["message"] == "session_access_denied"
        app.dependency_overrides[require_plugin_scope] = lambda: AuthContext("alice", ["plugin"])
        response = client.post(f"{base}/connect_session", json=body)
        assert response.status_code == 200
        assert response.json()["state"] == "bound"


def test_prepare_login_is_owner_scoped(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path, monkeypatch)
        ref = plugin.store.create("alice", pending_instance_id="test-bot")["session_ref"]
        plugin.manager.start = AsyncMock()
        plugin.manager.request.return_value = {"required": False}
        assert (await call(plugin, "prepare_login", "bob", {"session_ref": ref, "instance_id": "test-bot"}))[0] == 403
        plugin.manager.start.assert_not_awaited()
        status, data = await call(plugin, "prepare_login", "alice", {"session_ref": ref, "instance_id": "test-bot"})
        assert status == 200 and data["session_ref"] == ref
        assert len(plugin.store.list_owned("alice")) == 1
        plugin.store.complete(ref, "alice", "account")
        assert (await call(plugin, "prepare_login", "alice", {"session_ref": ref, "instance_id": "test-bot"}))[0] == 409
    asyncio.run(run())


def test_directory_before_binding_and_pagination(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path, monkeypatch)
        ref = plugin.store.create("alice")["session_ref"]
        plugin.store.complete(ref, "alice", "synthetic")
        plugin.manager.directory.return_value = {"groups": []}
        body = {"session_ref": ref, "page": 2}
        assert (await call(plugin, "groups", "alice", body))[0] == 200
        plugin.manager.directory.assert_awaited_once_with(ref, "/v1/groups", page=2)
        for page in [True, 0, 1001, "2"]:
            assert (await call(plugin, "groups", "alice", {**body, "page": page}))[0] == 400
    asyncio.run(run())


def test_slow_account_does_not_serialize_other_account_api(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path, monkeypatch)
        first = plugin.store.create("alice")["session_ref"]
        plugin.store.complete(first, "alice", "first-account")
        second = plugin.store.create("alice")["session_ref"]
        plugin.store.complete(second, "alice", "second-account")
        entered, release = asyncio.Event(), asyncio.Event()

        async def directory(ref, *args, **kwargs):
            if ref == first:
                entered.set()
                await release.wait()
            return {"groups": []}

        plugin.manager.directory = directory
        slow = asyncio.create_task(call(plugin, "groups", "alice", {"session_ref": first}))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            status, _ = await asyncio.wait_for(call(plugin, "groups", "alice", {"session_ref": second}), 1)
            assert status == 200
            assert (await asyncio.wait_for(call(plugin, "sessions", "alice"), 1))[0] == 200
            assert (await call(plugin, "groups", "bob", {"session_ref": first}))[0] == 403
        finally:
            release.set()
            await slow
        assert len(plugin._operations) == 0
    asyncio.run(run())


def test_queued_operation_is_rejected_after_termination_starts(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path, monkeypatch)
        ref = plugin.store.create("alice")["session_ref"]
        plugin.store.complete(ref, "alice", "first-account")
        lock = plugin._operations.setdefault("session:" + ref, asyncio.Lock())
        await lock.acquire()
        pending = asyncio.create_task(call(plugin, "connect_session", "alice", {"session_ref": ref, "instance_id": "test", "confirm": True}))
        await asyncio.sleep(0)
        plugin._stopping = True
        lock.release()
        assert (await pending)[0] == 409
        assert plugin.store._read(ref)["state"] == "authorized"
    asyncio.run(run())


def test_expiry_only_deletes_the_locked_draft(tmp_path, monkeypatch):
    async def run():
        plugin = make_plugin(tmp_path, monkeypatch)
        refs = []
        for owner in ("alice", "bob"):
            row = plugin.store.create(owner)
            row["expires_at"] = 0
            plugin.store._write(row)
            refs.append(row["session_ref"])
        await plugin._expire_one(refs[0])
        assert plugin.store._read(refs[0])["state"] == "revoked"
        assert plugin.store._read(refs[1])["state"] == "draft"
        plugin.manager.stop.assert_awaited_once_with(refs[0])
    asyncio.run(run())
