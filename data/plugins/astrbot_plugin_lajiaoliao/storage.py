"""Private session metadata. Credentials are only read by the protocol worker."""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path


class SessionError(RuntimeError):
    pass


def atomic_json(path: Path, value: dict) -> None:
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, ensure_ascii=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class SessionStore:
    def __init__(self, root: Path):
        self.root = root
        self._bound_account_ids: frozenset[str] | None = None
        self._cache_lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)

    @contextmanager
    def transaction(self):
        fd = os.open(self.root / ".registry.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def directory(self, ref: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", ref):
            raise SessionError("invalid_session_ref")
        return self.root / ref

    def _read(self, ref: str) -> dict:
        try:
            return json.loads((self.directory(ref) / "session.json").read_text())
        except (OSError, ValueError) as exc:
            raise SessionError("session_unavailable") from exc

    def _write(self, row: dict):
        atomic_json(self.directory(row["session_ref"]) / "session.json", row)
        with self._cache_lock:
            self._bound_account_ids = None

    def bound_account_ids(self) -> frozenset[str]:
        """Snapshot all locally bound bots; invalidate on registry writes."""
        with self._cache_lock:
            if self._bound_account_ids is None:
                self._bound_account_ids = frozenset(
                    str(row["account_id"])
                    for row in self._all()
                    if row["state"] == "bound" and row.get("account_id")
                )
            return self._bound_account_ids

    def _all(self) -> list[dict]:
        return [
            self._read(path.name)
            for path in self.root.iterdir()
            if path.is_dir() and re.fullmatch(r"[0-9a-f]{32}", path.name)
        ]

    def get(self, ref: str, owner: str) -> dict:
        row = self._read(ref)
        if not owner or row["owner"] != owner:
            raise SessionError("session_access_denied")
        if row["state"] == "draft" and row["expires_at"] <= time.time():
            raise SessionError("login_draft_expired")
        if row["state"] == "revoked":
            raise SessionError("session_revoked")
        return row

    def list_owned(self, owner: str) -> list[dict]:
        if not owner:
            raise SessionError("dashboard_login_required")
        with self.transaction():
            return [
                row
                for row in self._all()
                if row["owner"] == owner
                and row["state"] != "revoked"
                and not (row["state"] == "draft" and row["expires_at"] <= time.time())
            ]

    def create(self, owner: str, *, pending_instance_id: str = "") -> dict:
        if not owner:
            raise SessionError("dashboard_login_required")
        with self.transaction():
            drafts = [
                row
                for row in self._all()
                if row["owner"] == owner
                and row["state"] == "draft"
                and row["expires_at"] > time.time()
            ]
            if any(
                row.get("pending_instance_id", "") == pending_instance_id
                for row in drafts
            ):
                raise SessionError("login_draft_already_exists")
            # Bound abandoned workers without blocking unrelated instance logins.
            if len(drafts) >= 8:
                raise SessionError("login_draft_limit_reached")
            ref = secrets.token_hex(16)
            self.directory(ref).mkdir(mode=0o700)
            row = {
                "session_ref": ref,
                "owner": owner,
                "state": "draft",
                "created_at": time.time(),
                "expires_at": time.time() + 900,
                "account_id": "",
                "instance_id": "",
                "pending_instance_id": pending_instance_id,
            }
            self._write(row)
            return row

    def complete(self, ref: str, owner: str, account_id: str) -> dict:
        with self.transaction():
            row = self.get(ref, owner)
            if row["state"] != "draft":
                if row["account_id"] == account_id:
                    return row
                raise SessionError("account_change_requires_new_session")
            if not account_id:
                raise SessionError("missing_account_id")
            if any(
                other["session_ref"] != ref
                and other["account_id"] == account_id
                and other["state"] != "revoked"
                for other in self._all()
            ):
                raise SessionError("account_already_registered")
            row.update(state="authorized", account_id=account_id)
            self._write(row)
            return row

    def bind(self, ref: str, owner: str, instance_id: str) -> dict:
        if (
            not instance_id
            or len(instance_id) > 128
            or instance_id != instance_id.strip()
            or ":" in instance_id
            or "!" in instance_id
        ):
            raise SessionError("invalid_instance_id")
        with self.transaction():
            row = self.get(ref, owner)
            if row["state"] not in {"authorized", "bound"}:
                raise SessionError("login_required")
            if row["instance_id"] and row["instance_id"] != instance_id:
                raise SessionError("session_already_bound")
            if any(
                other["session_ref"] != ref
                and other["instance_id"] == instance_id
                and other["state"] == "bound"
                for other in self._all()
            ):
                raise SessionError("instance_already_bound")
            row.update(state="bound", instance_id=instance_id)
            self._write(row)
            return row

    def for_instance(self, ref: str, instance_id: str) -> dict:
        row = self._read(ref)
        if row["state"] != "bound" or row["instance_id"] != instance_id:
            raise SessionError("session_binding_mismatch")
        return row

    def revoke(self, ref: str, owner: str) -> None:
        with self.transaction():
            row = self.get(ref, owner)
            row["state"] = "revoked"
            self._write(row)
            for name in ("token.json", "cursor.json"):
                (self.directory(ref) / name).unlink(missing_ok=True)

    def expire(self, ref: str | None = None) -> list[str]:
        expired = []
        with self.transaction():
            for row in [self._read(ref)] if ref is not None else self._all():
                if row["state"] == "draft" and row["expires_at"] <= time.time():
                    row["state"] = "revoked"
                    self._write(row)
                    for name in ("token.json", "cursor.json"):
                        (self.directory(row["session_ref"]) / name).unlink(
                            missing_ok=True
                        )
                    expired.append(row["session_ref"])
        return expired

    def acquire_consumer(self, ref: str) -> int:
        fd = os.open(
            self.directory(ref) / ".consumer.lock", os.O_CREAT | os.O_RDWR, 0o600
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise SessionError("consumer_already_connected") from exc
        return fd
