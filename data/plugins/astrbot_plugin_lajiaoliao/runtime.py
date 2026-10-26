"""One private, parent-leased worker per session. No upstream traffic on import."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from weakref import WeakValueDictionary

import aiohttp

from .storage import SessionError, SessionStore

active: Runtime | None = None


@dataclass
class Worker:
    process: asyncio.subprocess.Process
    endpoint: str
    secret: str
    login_key: str
    lock_fd: int
    connected: bool
    directory_only: bool
    http: aiohttp.ClientSession


class Runtime:
    def __init__(
        self,
        store: SessionStore,
        binary: Path,
        *,
        launch_slots: asyncio.Semaphore | None = None,
    ):
        self.store = store
        self.binary = binary
        self.workers: dict[str, Worker] = {}
        self._session_locks: WeakValueDictionary[str, asyncio.Lock] = (
            WeakValueDictionary()
        )
        self._launch_slots = (
            launch_slots if launch_slots is not None else asyncio.Semaphore(4)
        )
        self._directory_slots = asyncio.Semaphore(4)
        self._closed = False

    def _session_lock(self, ref: str) -> asyncio.Lock:
        """Get the lock shared by all operations on this session.

        Args:
            ref: Valid session reference.

        Returns:
            A lock retained while an operation is active or waiting.
        """
        self.store.directory(ref)
        return self._session_locks.setdefault(ref, asyncio.Lock())

    def verify_binary(self):
        try:
            expected = self.binary.with_suffix(".sha256").read_text().split()[0]
            digest = hashlib.sha256()
            with self.binary.open("rb") as stream:
                for chunk in iter(lambda: stream.read(512 * 1024), b""):
                    digest.update(chunk)
            actual = digest.hexdigest()
        except (OSError, IndexError) as exc:
            raise SessionError("protocol_binary_unavailable") from exc
        if not secrets.compare_digest(expected, actual):
            raise SessionError("protocol_binary_checksum_mismatch")

    async def start(
        self, ref: str, *, connect: bool, directory_only: bool = False
    ) -> Worker:
        async with self._session_lock(ref), self._launch_slots:
            if self._closed:
                raise SessionError("plugin_stopping")
            row = self.store._read(ref)
            if directory_only and (
                connect or row["state"] not in {"authorized", "bound"}
            ):
                raise SessionError("session_binding_mismatch")
            if connect and row["state"] != "bound":
                raise SessionError("session_binding_mismatch")
            commerce = getattr(self, "commerce", None)
            if connect and commerce is not None:
                commerce.require(row["instance_id"])
            if not connect:
                self.store.get(ref, row["owner"])
            existing = self.workers.get(ref)
            if existing:
                if (
                    existing.process.returncode is None
                    and existing.connected == connect
                    and existing.directory_only == directory_only
                ):
                    return existing
                await self._stop(ref)
            await asyncio.to_thread(self.verify_binary)
            fd = self.store.acquire_consumer(ref)
            process = None
            try:
                process = await asyncio.create_subprocess_exec(
                    str(self.binary),
                    "--managed",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    # No inherited endpoint overrides or account credentials.
                    env={"PATH": "/usr/bin:/bin", "HOME": str(self.store.root)},
                    limit=8192,
                )
                secret, login_key = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                process.stdin.write(
                    (
                        json.dumps(
                            {
                                "secret": secret,
                                "login_key": ""
                                if connect or directory_only
                                else login_key,
                                "token_path": str(
                                    self.store.directory(ref) / "token.json"
                                ),
                                "connect": connect,
                                "directory_only": directory_only,
                                "expires_at": int(row["expires_at"])
                                if not connect
                                else 0,
                            }
                        )
                        + "\n"
                    ).encode()
                )
                await process.stdin.drain()
                raw = await asyncio.wait_for(process.stdout.readline(), 10)
                hello = json.loads(raw)
                url = urlsplit(hello["endpoint"])
                if (
                    hello.get("protocol_version") != 2
                    or url.scheme != "http"
                    or url.hostname != "127.0.0.1"
                    or not url.port
                    or url.username
                    or url.password
                    or url.path
                    or url.query
                    or url.fragment
                ):
                    raise SessionError("protocol_handshake_incompatible")
                worker = Worker(
                    process,
                    hello["endpoint"],
                    secret,
                    login_key,
                    fd,
                    connect,
                    directory_only,
                    aiohttp.ClientSession(
                        headers={"Authorization": f"Bearer {secret}"},
                        timeout=aiohttp.ClientTimeout(total=70),
                        connector=aiohttp.TCPConnector(limit=8, limit_per_host=8),
                    ),
                )
                self.workers[ref] = worker
                return worker
            except BaseException:
                if process:
                    await self._reap(process)
                os.close(fd)
                raise

    async def request(self, ref: str, method: str, path: str, **kwargs) -> dict:
        worker = self.workers.get(ref)
        if not worker or worker.process.returncode is not None:
            raise SessionError("protocol_worker_stopped")
        if path == "/v1/status":
            kwargs.setdefault("timeout", aiohttp.ClientTimeout(total=3))
        try:
            async with worker.http.request(
                method,
                worker.endpoint + path,
                **kwargs,
            ) as response:
                data = await response.json()
                if response.status >= 400:
                    raise SessionError(
                        str(data.get("error", "protocol_request_failed"))
                    )
                return data
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise SessionError("protocol_request_failed") from exc

    async def directory(self, ref: str, path: str, *, page: int) -> dict:
        """Read an owned directory without starting a message consumer.

        Args:
            ref: Owner-checked session reference.
            path: Fixed group or member endpoint.
            page: Validated directory page number.

        Returns:
            Upstream directory response.
        """
        async with self._session_lock(ref), self._directory_slots:
            if self._closed:
                raise SessionError("plugin_stopping")
            existing = self.workers.get(ref)
            if existing and existing.process.returncode is None:
                return await self.request(
                    ref,
                    "GET",
                    path,
                    params={"page": page},
                    timeout=aiohttp.ClientTimeout(total=10),
                )
            temporary = Runtime(
                self.store, self.binary, launch_slots=self._launch_slots
            )
            try:
                await temporary.start(ref, connect=False, directory_only=True)
                return await temporary.request(
                    ref,
                    "GET",
                    path,
                    params={"page": page},
                    timeout=aiohttp.ClientTimeout(total=10),
                )
            finally:
                await temporary.close()

    @staticmethod
    async def _reap(process):
        if process.stdin:
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except (TimeoutError, asyncio.CancelledError) as exc:
            if process.returncode is None:
                process.kill()
            await process.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise

    async def _stop(self, ref: str):
        worker = self.workers.pop(ref, None)
        if worker:
            try:
                try:
                    await worker.http.close()
                finally:
                    await self._reap(worker.process)
            finally:
                os.close(worker.lock_fd)

    async def stop(self, ref: str):
        async with self._session_lock(ref):
            await self._stop(ref)

    async def revoke(self, ref: str, owner: str):
        async with self._session_lock(ref):
            self.store.get(ref, owner)
            await self._stop(ref)
            self.store.revoke(ref, owner)

    async def close(self):
        self._closed = True
        # Include starts and directory reads that have not published a worker yet.
        refs = set(self.workers) | set(self._session_locks)
        results = await asyncio.gather(
            *(self.stop(ref) for ref in refs), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
