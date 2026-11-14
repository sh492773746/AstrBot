"""Hot reload must recover failed startup without stealing a live lock."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from filelock import FileLock, Timeout

from data.plugins.astrbot_plugin_superbot import runtime_lifecycle
from data.plugins.astrbot_plugin_superbot.main import Main
from data.plugins.astrbot_plugin_superbot.store import Rejected


class Runtime:
    terminate = Main.terminate

    def __init__(self, path, context):
        self.context = context
        self.instance_lock = FileLock(path)
        self.application = self.platform = self.keno = None
        self.tasks = []
        self.active_updates = set()
        self.store = SimpleNamespace(close=Mock())
        self.logger = Mock()
        self.stopping = False
        self.worker_stopped = False

    async def worker(self):
        try:
            await asyncio.Event().wait()
        finally:
            self.worker_stopped = True


@pytest.mark.asyncio
async def test_failed_startup_is_drained_before_reacquiring_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_lifecycle, "star_registry", [])
    old = Runtime(tmp_path / "runtime.lock", object())
    old.instance_lock.acquire(timeout=0)
    old.tasks.append(asyncio.create_task(old.worker()))
    await asyncio.sleep(0)
    new = Runtime(tmp_path / "runtime.lock", old.context)
    with pytest.raises(Timeout):
        new.instance_lock.acquire(timeout=0)
    await runtime_lifecycle.recover_failed_startup(new)
    assert old.worker_stopped and old.stopping
    assert old.store is None and not old.tasks
    with new.instance_lock.acquire(timeout=0):
        assert new.instance_lock.is_locked


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "protected", ["application", "platform", "registered", "context"]
)
async def test_active_or_unrelated_runtime_is_not_recovered(
    tmp_path, monkeypatch, protected
):
    registry = []
    monkeypatch.setattr(runtime_lifecycle, "star_registry", registry)
    old = Runtime(tmp_path / "runtime.lock", object())
    old.instance_lock.acquire(timeout=0)
    worker = asyncio.create_task(old.worker())
    old.tasks.append(worker)
    await asyncio.sleep(0)
    new = Runtime(tmp_path / "runtime.lock", old.context)
    if protected == "registered":
        registry.append(SimpleNamespace(star_cls=old))
    elif protected == "context":
        new.context = object()
    else:
        setattr(old, protected, object())
    try:
        with pytest.raises(Rejected):
            await runtime_lifecycle.recover_failed_startup(new)
        assert old.instance_lock.is_locked and not worker.done()
        assert not old.stopping
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        old.instance_lock.release()


@pytest.mark.asyncio
async def test_unidentified_lock_holder_is_never_forcibly_unlocked(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(runtime_lifecycle, "star_registry", [])
    lock = FileLock(tmp_path / "runtime.lock")
    new = Runtime(tmp_path / "runtime.lock", object())
    with lock.acquire(timeout=0):
        with pytest.raises(Rejected):
            await runtime_lifecycle.recover_failed_startup(new)
        with pytest.raises(Timeout):
            new.instance_lock.acquire(timeout=0)


@pytest.mark.asyncio
async def test_initialize_failure_closes_tasks_and_releases_lock(tmp_path):
    runtime = Runtime(tmp_path / "runtime.lock", object())

    async def fail():
        runtime.instance_lock.acquire(timeout=0)
        runtime.tasks.append(asyncio.create_task(runtime.worker()))
        await asyncio.sleep(0)
        raise ValueError("feature initialization failed")

    runtime._initialize = fail
    with pytest.raises(ValueError, match="feature initialization failed"):
        await Main.initialize(runtime)
    assert runtime.worker_stopped
    assert runtime.store is None and not runtime.instance_lock.is_locked


@pytest.mark.asyncio
async def test_client_close_error_does_not_leak_database_or_lock(tmp_path):
    runtime = Runtime(tmp_path / "runtime.lock", object())
    runtime.instance_lock.acquire(timeout=0)
    runtime.keno = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("close")))
    runtime.payments = SimpleNamespace(close=AsyncMock())
    store = runtime.store
    await runtime.terminate()
    runtime.payments.close.assert_awaited_once()
    store.close.assert_called_once()
    assert not runtime.instance_lock.is_locked


@pytest.mark.asyncio
async def test_termination_waits_for_inflight_handler_before_unlocking(tmp_path):
    runtime = Runtime(tmp_path / "runtime.lock", object())
    runtime.instance_lock.acquire(timeout=0)
    application = SimpleNamespace(remove_handler=Mock(), bot=object())
    runtime.application = application
    runtime.handler = object()
    finished = asyncio.get_running_loop().create_future()
    runtime.active_updates.add(finished)
    task = asyncio.create_task(runtime.terminate())
    await asyncio.sleep(0)
    assert runtime.instance_lock.is_locked and not task.done()
    assert runtime.application is application
    runtime.store.close.assert_not_called()
    finished.set_result(None)
    await task
    assert not runtime.instance_lock.is_locked
    assert runtime.application is None
