"""Lazy, bounded local solver process shared by login transactions."""

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

from astrbot.core.utils.astrbot_path import get_astrbot_temp_path


class CaptchaSolver:
    """Serialize model access and terminate the worker on timeout or cancellation."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.process = None

    async def solve(self, captcha_id: str, referer: str, timeout: float) -> str:
        """Solve within a budget including time spent waiting for the model.

        Args:
            captcha_id: Deployment captcha identifier.
            referer: Deployment origin or explicit override.
            timeout: Remaining transaction budget in seconds.

        Returns:
            Validation value, or an empty string on a failed challenge.
        """

        async def exchange():
            async with self.lock:
                try:
                    if self.process is None or self.process.returncode is not None:
                        self.process = await asyncio.create_subprocess_exec(
                            sys.executable,
                            str(Path(__file__).with_name("captcha_worker.py")),
                            env={
                                **os.environ,
                                "ASTRBOT_SOLVER_RUNTIME": str(
                                    Path(get_astrbot_temp_path())
                                    / "wangshangliao_solver"
                                ),
                            },
                            stdin=asyncio.subprocess.PIPE,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.DEVNULL,
                            start_new_session=os.name != "nt",
                        )
                    payload = json.dumps({"id": captcha_id, "referer": referer})
                    self.process.stdin.write((payload + "\n").encode())
                    await self.process.stdin.drain()
                    reply = json.loads(await self.process.stdout.readline())
                    value = reply.get("validate", "") if isinstance(reply, dict) else ""
                    return (
                        value if isinstance(value, str) and len(value) <= 8192 else ""
                    )
                except BaseException:
                    await self.close()
                    raise

        return await asyncio.wait_for(exchange(), timeout)

    async def close(self) -> None:
        """Release the model and child workers during shutdown or failed exchange."""
        process, self.process = self.process, None
        if process is None:
            return
        if process.returncode is None:
            try:
                if os.name == "nt":
                    killer = await asyncio.create_subprocess_exec(
                        "taskkill",
                        "/PID",
                        str(process.pid),
                        "/T",
                        "/F",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await killer.wait()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await process.wait()
