"""Staged same-account authorization with a recoverable credential commit."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from weakref import WeakValueDictionary

from .runtime import Runtime
from .storage import SessionError, SessionStore, atomic_json


class Reauthorization:
    def __init__(self, store: SessionStore, binary: Path):
        self.store = store
        self.staging = SessionStore(store.root / ".reauth")
        self.manager = Runtime(self.staging, binary)
        self.operations: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self.recover()

    def recover(self):
        """Resolve interrupted commits without submitting credentials again."""
        for row in self.staging._all():
            if row["state"] != "committing":
                continue
            current = self.credential_revision(row["target_ref"])
            if current == row["staged_revision"]:
                row["state"] = "completed"
                (self.staging.directory(row["session_ref"]) / "token.json").unlink(
                    missing_ok=True
                )
            else:
                row["state"] = "failed"
                row["error"] = "reauth_commit_interrupted"
            self.staging._write(row)

    def credential_revision(self, ref: str) -> str:
        """Return an opaque comparison value, never credential contents.

        Args:
            ref: Existing immutable session reference.

        Returns:
            SHA-256 of the credential file, or an empty marker when absent.
        """
        path = self.store.directory(ref) / "token.json"
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except FileNotFoundError:
            return ""

    def prepare(self, ref: str, owner: str, instance_id: str) -> dict:
        target = self.store.get(ref, owner)
        if target["state"] != "bound" or target["instance_id"] != instance_id:
            raise SessionError("session_binding_mismatch")
        active = next(
            (
                row
                for row in self.staging.list_owned(owner)
                if row.get("target_ref") == ref
                and row["state"] in {"draft", "ready", "committing"}
            ),
            None,
        )
        if active:
            if active["expires_at"] <= time.time():
                raise SessionError("reauth_expired")
            return active
        row = self.staging.create(owner, pending_instance_id=instance_id)
        row.update(
            target_ref=ref,
            expected_account_id=target["account_id"],
            credential_revision=self.credential_revision(ref),
        )
        self.staging._write(row)
        return row

    def get(
        self, ref: str, owner: str, operation_ref: str | None = None
    ) -> dict | None:
        target = self.store.get(ref, owner)
        rows = self.staging.list_owned(owner)
        if operation_ref:
            row = self.staging.get(operation_ref, owner)
            if row.get("target_ref") != ref:
                raise SessionError("session_binding_mismatch")
        else:
            matches = [row for row in rows if row.get("target_ref") == ref]
            row = max(matches, key=lambda item: item["created_at"], default=None)
        if row and (
            row["pending_instance_id"] != target["instance_id"]
            or row["expected_account_id"] != target["account_id"]
        ):
            raise SessionError("session_binding_mismatch")
        if (
            row
            and row["state"] not in {"completed", "failed"}
            and row["expires_at"] <= time.time()
        ):
            raise SessionError("reauth_expired")
        return row

    @staticmethod
    def public(row: dict | None) -> dict:
        if row is None:
            return {"state": "none"}
        return {
            "operation_ref": row["session_ref"],
            "state": row["state"],
            "expires_at": row["expires_at"],
            "error": row.get("error", ""),
        }

    async def commit(self, row: dict, context, live_manager: Runtime) -> dict:
        """Replace only credentials after acquiring the sole consumer lock.

        Args:
            row: Owner-checked authorization operation.
            context: AstrBot context used to stop and restore the saved instance.
            live_manager: Manager of the original message consumer.

        Returns:
            Sanitized authorization result.

        Raises:
            SessionError: On identity, generation or configuration conflicts.
        """
        op, ref, owner = row["session_ref"], row["target_ref"], row["owner"]
        if getattr(live_manager, "_closed", False) or self.manager._closed:
            raise SessionError("plugin_stopping")
        if row["state"] == "completed":
            return self.public(row)
        await self.manager.stop(op)
        token_path = self.staging.directory(op) / "token.json"
        if not token_path.is_file() or token_path.stat().st_size > 65536:
            raise SessionError("reauth_result_unavailable")
        tokens = json.loads(token_path.read_text())
        if (
            tokens.get("user_id") != row["expected_account_id"]
            or not tokens.get("im_token")
            or not tokens.get("chat_token")
        ):
            row.update(state="failed", error="reauth_account_mismatch")
            self.staging._write(row)
            token_path.unlink(missing_ok=True)
            raise SessionError("reauth_account_mismatch")
        config = next(
            (
                item
                for item in context.get_config().get("platform", [])
                if item.get("id") == row["pending_instance_id"]
                and item.get("type") == "lajiaoliao"
                and item.get("session_ref") == ref
            ),
            None,
        )
        if config is None:
            raise SessionError("session_binding_mismatch")
        if self.credential_revision(ref) != row["credential_revision"]:
            row.update(state="failed", error="credential_version_changed")
            self.staging._write(row)
            token_path.unlink(missing_ok=True)
            raise SessionError("credential_version_changed")
        old_path = self.store.directory(ref) / "token.json"
        old_version = (
            json.loads(old_path.read_text()).get("version", 0)
            if old_path.is_file()
            else 0
        )
        tokens["version"] = old_version + 1
        row.update(
            state="committing",
            staged_revision=hashlib.sha256(
                json.dumps(tokens, ensure_ascii=True).encode()
            ).hexdigest(),
        )
        self.staging._write(row)
        try:
            await context.platform_manager.terminate_platform(config["id"])
            await live_manager.stop(ref)
            with self.store.transaction():
                target = self.store.get(ref, owner)
                if (
                    target["account_id"] != tokens["user_id"]
                    or target["instance_id"] != config["id"]
                    or config not in context.get_config().get("platform", [])
                ):
                    raise SessionError("session_binding_mismatch")
                fd = self.store.acquire_consumer(ref)
                try:
                    if self.credential_revision(ref) != row["credential_revision"]:
                        raise SessionError("credential_version_changed")
                    # No binding, config or cursor file is rewritten.
                    atomic_json(self.store.directory(ref) / "token.json", tokens)
                    row["staged_revision"] = self.credential_revision(ref)
                    row.update(state="completed", completed_at=time.time())
                    self.staging._write(row)
                    token_path.unlink(missing_ok=True)
                finally:
                    os.close(fd)
        except BaseException:
            if self.credential_revision(ref) == row.get("staged_revision"):
                row["state"] = "completed"
            else:
                row.update(state="failed", error="reauth_commit_interrupted")
            self.staging._write(row)
            raise
        finally:
            current = next(
                (
                    item
                    for item in context.get_config().get("platform", [])
                    if item.get("id") == config["id"] and item.get("session_ref") == ref
                ),
                None,
            )
            if current and current.get("enable"):
                await context.platform_manager.reload(current)
        return self.public(row)

    async def expire(self):
        for row in self.staging._all():
            async with self.operations.setdefault(
                row.get("target_ref", row["session_ref"]), asyncio.Lock()
            ):
                row = self.staging._read(row["session_ref"])
                if row["expires_at"] <= time.time() and row["state"] in {
                    "draft",
                    "ready",
                    "failed",
                }:
                    await self.manager.stop(row["session_ref"])
                    row["state"] = "revoked"
                    self.staging._write(row)
                    (self.staging.directory(row["session_ref"]) / "token.json").unlink(
                        missing_ok=True
                    )
