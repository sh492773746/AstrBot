import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from data.plugins.astrbot_plugin_lajiaoliao.runtime import Runtime
from data.plugins.astrbot_plugin_lajiaoliao.storage import SessionError, SessionStore

BINARY = Path(__file__).resolve().parents[1] / "data/plugins/astrbot_plugin_lajiaoliao/bin/lajiaoliao-bridge"


def test_managed_worker_lifecycle_and_auth(tmp_path):
    async def run():
        store = SessionStore(tmp_path)
        ref = store.create("test-owner")["session_ref"]
        runtime = Runtime(store, BINARY)
        try:
            worker = await runtime.start(ref, connect=False)
            assert await runtime.start(ref, connect=False) is worker
            status = await runtime.request(ref, "GET", "/v1/status")
            assert status["authenticated"] is False
            assert status["protocol_version"] == 2
            assert status["connection_state"] != "online"
            async with aiohttp.ClientSession() as client:
                async with client.get(worker.endpoint + "/v1/status") as response:
                    assert response.status == 401
                async with client.get(
                    worker.endpoint + "/v1/status",
                    headers={"Authorization": f"Bearer {worker.login_key}"},
                ) as response:
                    assert response.status == 401
                async with client.get(
                    worker.endpoint + "/v1/events",
                    headers={"Authorization": f"Bearer {worker.secret}"},
                ) as response:
                    assert response.status == 409
            # No upstream requests: the token-less authorization worker stays local.
            worker.process.stdin.close()
            await asyncio.wait_for(worker.process.wait(), 7)
            restarted = await runtime.start(ref, connect=False)
            assert restarted.process.pid != worker.process.pid
            await runtime.stop(ref)
            assert restarted.process.returncode is not None
            await runtime.stop(ref)
        finally:
            await runtime.close()
        with pytest.raises(SessionError, match="plugin_stopping"):
            await runtime.start(ref, connect=False)
    asyncio.run(run())


def test_checksum_failure_does_not_start_worker(tmp_path):
    async def run():
        store = SessionStore(tmp_path / "state")
        ref = store.create("owner")["session_ref"]
        fake = tmp_path / "worker"
        fake.write_bytes(b"not a program")
        fake.with_suffix(".sha256").write_text("0" * 64)
        runtime = Runtime(store, fake)
        with pytest.raises(SessionError, match="checksum"):
            await runtime.start(ref, connect=False)
        assert runtime.workers == {}
    asyncio.run(run())


def test_two_managers_cannot_own_same_consumer(tmp_path):
    async def run():
        store = SessionStore(tmp_path)
        ref = store.create("owner")["session_ref"]
        first, second = Runtime(store, BINARY), Runtime(store, BINARY)
        try:
            await first.start(ref, connect=False)
            with pytest.raises(SessionError, match="consumer_already_connected"):
                await second.start(ref, connect=False)
        finally:
            await first.close()
            await second.close()
    asyncio.run(run())


def test_directory_worker_never_consumes_or_sends(tmp_path):
    async def run():
        store = SessionStore(tmp_path)
        ref = store.create("owner")["session_ref"]
        store.complete(ref, "owner", "synthetic-account")
        runtime = Runtime(store, BINARY)
        try:
            worker = await runtime.start(ref, connect=False, directory_only=True)
            assert not worker.connected and worker.directory_only
            async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {worker.secret}"}) as client:
                for method, path in [("GET", "/v1/events"), ("POST", "/v1/messages"), ("POST", "/v1/session/login")]:
                    async with client.request(method, worker.endpoint + path, json={}) as response:
                        assert response.status == 409
                async with client.get(worker.endpoint + "/v1/groups") as response:
                    assert response.status == 401
            assert (await runtime.request(ref, "GET", "/v1/status"))["connection_state"] != "online"
        finally:
            await runtime.close()
        assert worker.process.returncode is not None
    asyncio.run(run())


def test_stalled_directory_does_not_block_another_account(tmp_path):
    async def run():
        store = SessionStore(tmp_path)
        first = store.create("first-owner")["session_ref"]
        store.complete(first, "first-owner", "synthetic-account")
        second = store.create("second-owner")["session_ref"]
        runtime = Runtime(store, BINARY)
        entered, release = asyncio.Event(), asyncio.Event()
        worker = await runtime.start(first, connect=False, directory_only=True)

        async def slow_request(*args, **kwargs):
            entered.set()
            await release.wait()
            return {"groups": []}

        runtime.request = slow_request
        reading = asyncio.create_task(runtime.directory(first, "/v1/groups", page=1))
        stopping = None
        try:
            await asyncio.wait_for(entered.wait(), 3)
            other = await asyncio.wait_for(runtime.start(second, connect=False), 3)
            await asyncio.wait_for(runtime.stop(second), 3)
            assert other.process.returncode is not None
            stopping = asyncio.create_task(runtime.stop(first))
            await asyncio.sleep(0)
            assert not stopping.done(), "Same-account cleanup must wait for its directory read"
        finally:
            release.set()
            await reading
            if stopping:
                await stopping
            await runtime.close()
        assert worker.process.returncode is not None
        assert len(runtime._session_locks) == 0
    asyncio.run(run())


def test_close_waits_for_inflight_start_and_remains_idempotent(tmp_path):
    async def run():
        store = SessionStore(tmp_path)
        ref = store.create("owner")["session_ref"]
        runtime = Runtime(store, BINARY)
        # Hold launch admission while start already owns its account lock.
        for _ in range(4):
            await runtime._launch_slots.acquire()
        starting = asyncio.create_task(runtime.start(ref, connect=False))
        await asyncio.sleep(0)
        closing = asyncio.create_task(runtime.close())
        await asyncio.sleep(0)
        assert not closing.done()
        for _ in range(4):
            runtime._launch_slots.release()
        with pytest.raises(SessionError, match="plugin_stopping"):
            await starting
        await closing
        await runtime.close()
        assert runtime.workers == {}
        assert len(runtime._session_locks) == 0
    asyncio.run(run())


def test_status_requests_have_short_deadline(tmp_path):
    async def run():
        store = SessionStore(tmp_path)
        ref = store.create("owner")["session_ref"]
        runtime = Runtime(store, BINARY)
        try:
            worker = await runtime.start(ref, connect=False)
            assert worker.http.connector.limit == 8
            with patch.object(worker.http, "request", wraps=worker.http.request) as request:
                status = await runtime.request(ref, "GET", "/v1/status")
                assert request.call_args.kwargs["timeout"].total == 3
            assert not status["authenticated"]
            assert not worker.connected
        finally:
            await runtime.close()
    asyncio.run(run())


def test_cleanup_reaps_even_when_http_close_fails(tmp_path):
    async def run():
        store = SessionStore(tmp_path)
        ref = store.create("owner")["session_ref"]
        runtime = Runtime(store, BINARY)
        worker = await runtime.start(ref, connect=False)
        actual_http = worker.http
        worker.http = type("BrokenHTTP", (), {"close": AsyncMock(side_effect=RuntimeError("synthetic close failure"))})()
        try:
            with pytest.raises(RuntimeError, match="synthetic close failure"):
                await runtime.close()
            assert worker.process.returncode is not None
            fd = store.acquire_consumer(ref)
            import os
            os.close(fd)
        finally:
            await actual_http.close()
            await runtime.close()
    asyncio.run(run())


def test_parallel_close_is_idempotent_for_multiple_accounts(tmp_path):
    async def run():
        store = SessionStore(tmp_path)
        refs = [store.create(f"owner-{index}")["session_ref"] for index in range(3)]
        runtime = Runtime(store, BINARY)
        workers = await asyncio.gather(*(runtime.start(ref, connect=False) for ref in refs))
        await asyncio.gather(runtime.close(), runtime.close())
        assert not runtime.workers
        assert len(runtime._session_locks) == 0
        assert all(worker.process.returncode is not None for worker in workers)
    asyncio.run(run())
