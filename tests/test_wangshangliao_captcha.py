"""Worker lifecycle tests without external verification requests."""

import asyncio
import sys

import pytest

from astrbot.core.platform.sources.wangshangliao.captcha import CaptchaSolver


@pytest.mark.asyncio
async def test_worker_reuse_timeout_and_restart(monkeypatch):
    spawn = asyncio.create_subprocess_exec

    async def fixture_worker(*args, **kwargs):
        # Only replace the worker; Windows cleanup must execute the real taskkill.
        if args[0] != sys.executable:
            return await spawn(*args, **kwargs)
        return await spawn(
            sys.executable, "-u", "-c",
            "import sys,json,time\n"
            "for line in sys.stdin:\n"
            " r=json.loads(line)\n"
            " if r['id']=='slow': time.sleep(10)\n"
            " print(json.dumps({'validate':'fixture'}),flush=True)\n",
            **kwargs,
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fixture_worker)
    solver = CaptchaSolver()
    assert solver.process is None
    try:
        assert await solver.solve("fixture", "https://example.test", 5) == "fixture"
        first = solver.process
        assert await solver.solve("fixture", "https://example.test", 5) == "fixture"
        assert solver.process is first
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                solver.solve("slow", "https://example.test", 0.1), timeout=5
            )
        assert first.returncode is not None
        assert solver.process is None
        assert await solver.solve("fixture", "https://example.test", 5) == "fixture"
        last = solver.process
        assert last.pid != first.pid
    finally:
        await asyncio.wait_for(solver.close(), timeout=5)
    assert last.returncode is not None
