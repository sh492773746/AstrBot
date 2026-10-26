"""Offline migration bundles. Never starts a worker or changes live config."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

from astrbot.core.platform.message_policy import migrate_legacy_policy

from .storage import SessionError, SessionStore, atomic_json

LEGACY_KEYS = (
    "account_id",
    "bridge_base_url",
    "bridge_events_url",
    "enabled_groups",
    "reply_private",
    "reply_groups",
    "request_timeout",
    "reconnect_delay",
)


def prepare_bundle(
    config: dict,
    token: dict,
    cursor: dict | None,
    *,
    owner: str,
    instance_id: str,
    output: Path,
    confirm_account_change: bool = False,
    defer_binding: bool = False,
) -> dict:
    bots = [
        item for item in config.get("platform", []) if item.get("id") == instance_id
    ]
    if len(bots) != 1 or bots[0].get("type") != "lajiaoliao":
        raise SessionError("migration_instance_not_unique")
    original = bots[0]
    if original.get("session_ref"):
        raise SessionError("instance_already_migrated")
    if not owner or not all(
        isinstance(token.get(key), str) and token[key]
        for key in (
            "user_id",
            "chat_token",
            "im_token",
        )
    ):
        raise SessionError("migration_session_invalid")
    account = token["user_id"]
    configured = str(original.get("account_id") or "").strip()
    if (
        configured
        and configured != account
        and not confirm_account_change
        and not defer_binding
    ):
        raise SessionError("migration_account_confirmation_required")
    if cursor is not None and cursor.get("user_id") != account:
        raise SessionError("migration_cursor_account_mismatch")
    if output.exists():
        raise SessionError("migration_output_exists")
    policy = migrate_legacy_policy(original)
    output.mkdir(mode=0o700, parents=True)
    store = SessionStore(output / "sessions")
    row = store.create(owner)
    ref = row["session_ref"]
    store.complete(ref, owner, account)
    if not defer_binding:
        store.bind(ref, owner, instance_id)
    atomic_json(store.directory(ref) / "token.json", token)
    if cursor is not None:
        atomic_json(store.directory(ref) / "cursor.json", cursor)
    migrated = copy.deepcopy(original)
    for key in LEGACY_KEYS:
        migrated.pop(key, None)
    migrated.update(session_ref=ref, message_policy=policy)
    if defer_binding:
        migrated["enable"] = False
    atomic_json(output / "bot.before.json", original)
    atomic_json(output / "bot.after.json", migrated)
    manifest = {
        "format": 1,
        "instance_id": instance_id,
        "session_ref": ref,
        "state": "prepared_not_applied",
        "history_baseline": cursor is None,
        "binding_pending": defer_binding,
        "original_bot_sha256": hashlib.sha256(
            json.dumps(original, sort_keys=True).encode()
        ).hexdigest(),
    }
    atomic_json(output / "manifest.json", manifest)
    return manifest


def rollback_bundle(
    store: SessionStore, ref: str, before: dict, *, output: Path
) -> None:
    """Export CURRENT tokens/cursor, never an old authorization snapshot."""
    row = store.for_instance(ref, str(before["id"]))
    if output.exists():
        raise SessionError("rollback_output_exists")
    fd = store.acquire_consumer(ref)
    try:
        source = store.directory(ref)
        token = json.loads((source / "token.json").read_text())
        if token.get("user_id") != row["account_id"]:
            raise SessionError("rollback_account_mismatch")
        cursor_path = source / "cursor.json"
        cursor = json.loads(cursor_path.read_text()) if cursor_path.exists() else None
        if cursor is not None and cursor.get("user_id") != row["account_id"]:
            raise SessionError("rollback_cursor_account_mismatch")
        output.mkdir(mode=0o700, parents=True)
        atomic_json(output / "token.json", token)
        if cursor is not None:
            atomic_json(output / "cursor.json", cursor)
        restored = copy.deepcopy(before)
        restored["account_id"] = row["account_id"]
        # No automatic restart or enabling as part of credential export.
        restored["enable"] = False
        atomic_json(output / "bot.restore.json", restored)
    finally:
        import os

        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--config", type=Path, required=True)
    prepare.add_argument("--token", type=Path, required=True)
    prepare.add_argument("--cursor", type=Path)
    prepare.add_argument("--owner", required=True)
    prepare.add_argument("--instance", required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--confirm-account-change", action="store_true")
    prepare.add_argument("--defer-binding", action="store_true")
    rollback = commands.add_parser("rollback-export")
    rollback.add_argument("--store", type=Path, required=True)
    rollback.add_argument("--ref", required=True)
    rollback.add_argument("--before", type=Path, required=True)
    rollback.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    def read(path):
        return json.loads(path.read_text(encoding="utf-8-sig"))

    if args.command == "prepare":
        prepare_bundle(
            read(args.config),
            read(args.token),
            read(args.cursor) if args.cursor else None,
            owner=args.owner,
            instance_id=args.instance,
            output=args.output,
            confirm_account_change=args.confirm_account_change,
            defer_binding=args.defer_binding,
        )
        print("Private migration bundle prepared; live config and services unchanged.")
    else:
        rollback_bundle(
            SessionStore(args.store), args.ref, read(args.before), output=args.output
        )
        print("Current credentials exported privately; no service started.")


if __name__ == "__main__":
    main()
