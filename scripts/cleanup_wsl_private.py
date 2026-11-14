"""Preview or remove explicitly scoped Wangshangliao private-message remnants.

Only operator-supplied paths are opened. No runtime imports, directory discovery,
network calls, whole-file deletion, VACUUM, or financial/audit table changes occur.
Apply requires a matching preview digest and an explicit offline assertion.
SQLite deletion is logical cleanup, not forensic erasure of pages or backups.
"""

import argparse
import hashlib
import json
import sqlite3
from contextlib import ExitStack, closing
from pathlib import Path

PRIVATE_TABLES = {
    "guidance_trials": {"account", "group_id", "uid", "principal", "quota"},
    "support_group_owners": {"account", "group_id", "epoch", "uid"},
    "support_owners": {"uid", "peer", "name", "version", "active"},
    "support_settings": {"account", "version", "config"},
    "support_editors": {"account", "uid", "peer", "version", "active"},
    "support_drafts": {"token", "account", "actor", "session", "config"},
    "private_ai_sources": {"account", "principal", "message_id", "batch_id"},
    "private_ai_batches": {
        "id",
        "account",
        "principal",
        "messages",
        "state",
        "created",
        "updated",
    },
    "chat_history": {"account", "sender", "messages", "updated"},
    "ai_quota_notices": {"id", "pool", "kind", "text", "state"},
}
INTERACTION_TABLES = {
    "onboarding": {"account", "sender", "created", "outcome"},
    "chat_sessions": {"account", "sender", "expires"},
}
FRIEND_APPROVAL_TABLES = {
    "approval_settings": {
        "account",
        "enabled",
        "version",
        "paused",
        "last_scan",
        "last_action",
        "pending",
        "retry_at",
        "read_failures",
        "last_request",
        "welcome_initialized",
        "welcome_checked_at",
    },
    "approval_attempts": {
        "account",
        "request_id",
        "member",
        "state",
        "created",
        "updated",
    },
    "welcome_observations": {"account", "request_id", "member", "peer", "state"},
}
KNOWLEDGE_TRIGGERS = {
    "knowledge_total_add": (
        "CREATE TRIGGER knowledge_total_add AFTER INSERT ON knowledge_entries BEGIN "
        "UPDATE knowledge_totals SET bytes=bytes+NEW.bytes,entries=entries+1; END"
    ),
    "knowledge_total_edit": (
        "CREATE TRIGGER knowledge_total_edit AFTER UPDATE OF bytes ON knowledge_entries BEGIN "
        "UPDATE knowledge_totals SET bytes=bytes+NEW.bytes-OLD.bytes; END"
    ),
    "knowledge_total_delete": (
        "CREATE TRIGGER knowledge_total_delete AFTER DELETE ON knowledge_entries BEGIN "
        "UPDATE knowledge_totals SET bytes=bytes-OLD.bytes,entries=entries-1; END"
    ),
}
PROTECTED = (
    "All group messages, group knowledge, group profiles, group_onboarding, tenants, group_ai, "
    "actor_bindings, account_labels, financial ledgers, subscriptions, quotas, "
    "calls, provider_stats, audit, unknown charges, and all unlisted tables/files "
    "are preserved. Personal knowledge uses exact kind='personal' and linked FTS "
    "rowids only; its known delete trigger maintains shared totals. "
    "No outbox, attachment, persona, or unknown knowledge-space guessing."
)


def private_origin(value, platform_ids):
    """Match an exact declared WSL platform and native private message type.

    Args:
        value: Stored origin, not message content.
        platform_ids: Exact WSL platform instance IDs declared by the operator.

    Returns:
        Whether the nonempty origin is exclusively private in that platform.
    """
    if not isinstance(value, str):
        return False
    parts = value.split(":", 2)
    return (
        len(parts) == 3
        and parts[0] in platform_ids
        and parts[1] == "FriendMessage"
        and bool(parts[2])
    )


def deletion_plan(db, kind, platform_ids=()):
    """Build a fixed allowlist plan without exposing private row contents.

    Args:
        db: Connection owning a consistent read snapshot or write lock.
        kind: Explicit database role, never inferred from arbitrary tables.
        platform_ids: Exact WSL instances allowed in shared framework storage.

    Returns:
        Deterministic actions with selected-row fingerprints.

    Raises:
        ValueError: If a known target has an unfamiliar schema or unsafe links.
    """
    tables = {
        row[0]: row[1]
        for row in db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    }
    columns = {
        table: {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
        for table in tables
        # Only allow ordinary identifiers before using schema names in SQL.
        if table.isascii() and table.replace("_", "").isalnum()
    }
    targets = {
        "open": PRIVATE_TABLES,
        "interaction": INTERACTION_TABLES,
        "friend_approval": FRIEND_APPROVAL_TABLES,
    }.get(kind, {})
    personal_entries = set()
    if kind == "open" and "knowledge_entries" in columns:
        if not {"id", "kind"} <= columns["knowledge_entries"]:
            raise ValueError("unrecognized_schema:knowledge_entries")
        personal_entries = {
            row[0]
            for row in db.execute(
                "SELECT id FROM knowledge_entries WHERE kind='personal'"
            )
        }
    triggers = list(
        db.execute("SELECT name,tbl_name,sql FROM sqlite_master WHERE type='trigger'")
    )
    if personal_entries:
        expected = {
            "knowledge_entries": {
                "id",
                "kind",
                "owner",
                "account",
                "group_id",
                "title",
                "body",
                "bytes",
                "created",
                "updated",
            },
            "knowledge_spaces": {
                "kind",
                "owner",
                "account",
                "group_id",
                "version",
                "mentions",
                "idle",
            },
            "knowledge_fts": {"tokens"},
            "knowledge_totals": {"id", "bytes", "entries"},
        }
        if any(
            not required <= columns.get(table, set())
            for table, required in expected.items()
        ):
            raise ValueError("unrecognized_knowledge_schema")
        # This FTS is standalone; external-content or extra-column variants need review.
        if "".join(tables["knowledge_fts"].split()).casefold() != (
            "createvirtualtableknowledge_ftsusingfts5(tokens)"
        ):
            raise ValueError("unrecognized_knowledge_fts")
        entry_id = next(
            row
            for row in db.execute("PRAGMA table_info(knowledge_entries)")
            if row[1] == "id"
        )
        if entry_id[2].upper() != "INTEGER" or entry_id[5] != 1:
            raise ValueError("unrecognized_knowledge_entry_id")
        known = {
            name: "".join(sql.split()).casefold()
            for name, sql in KNOWLEDGE_TRIGGERS.items()
        }
        actual = {
            name: "".join(sql.split()).casefold()
            for name, table, sql in triggers
            if table == "knowledge_entries"
        }
        if actual != known:
            raise ValueError("unrecognized_knowledge_triggers")
        totals = db.execute("SELECT id,bytes,entries FROM knowledge_totals").fetchall()
        aggregate = db.execute(
            "SELECT COALESCE(SUM(bytes),0),COUNT(*) FROM knowledge_entries"
        ).fetchone()
        if len(totals) != 1 or tuple(totals[0]) != (1, *aggregate):
            raise ValueError("knowledge_totals_inconsistent")
    actions = []
    for table in sorted(columns):
        drop = table in targets
        required = targets.get(table)
        field = None
        if kind == "open" and table == "ai_profiles":
            required = {"kind", "account", "group_id", "selection"}
        elif kind == "open" and table == "capabilities":
            required = {"kind", "digest"}
        elif kind == "open" and table in {"knowledge_entries", "knowledge_spaces"}:
            required = {"kind", "owner", "account", "group_id"}
        elif kind == "open" and table == "knowledge_fts" and personal_entries:
            required = {"tokens"}
        elif kind == "messages" and table == "inbox":
            required = {"account", "team", "message", "payload"}
        elif kind == "framework":
            field = {
                "conversations": "user_id",
                "platform_message_history": "user_id",
                "platform_sessions": "session_id",
                "session_project_relations": "session_id",
                "umo_aliases": "umo",
                "preferences": "scope_id",
                "webchat_threads": "parent_session_id",
            }.get(table)
            if field:
                required = {field} | ({"scope"} if table == "preferences" else set())
        if required is None:
            continue
        exact = kind == "friend_approval" or (
            kind == "open"
            and table in {"private_ai_sources", "private_ai_batches", "chat_history"}
        )
        if not required <= columns[table] or (exact and required != columns[table]):
            missing = sorted(required - columns[table])
            unexpected = sorted(columns[table] - required) if exact else []
            raise ValueError(
                f"unrecognized_schema:{table}:missing={missing}:unexpected={unexpected}"
            )
        rows = db.execute(
            f'SELECT rowid AS "_cleanup_rowid",* FROM "{table}" ORDER BY rowid'
        )
        selected = []
        fingerprint = hashlib.sha256(tables[table].encode())
        for row in rows:
            match = drop
            if kind == "open" and table == "ai_profiles":
                match = row["kind"] in {"consultation", "admin_guidance"}
            elif kind == "open" and table == "capabilities":
                match = row["kind"] == "guidance_trial"
            elif kind == "open" and table in {"knowledge_entries", "knowledge_spaces"}:
                match = row["kind"] == "personal"
            elif kind == "open" and table == "knowledge_fts":
                match = row["_cleanup_rowid"] in personal_entries
            elif kind == "messages" and table == "inbox":
                match = (
                    isinstance(row["team"], str)
                    and row["team"].startswith("private/")
                    and bool(row["team"][len("private/") :])
                )
            elif kind == "framework":
                match = private_origin(row[field], platform_ids) and (
                    table != "preferences" or row["scope"] == "umo"
                )
            if match:
                selected.append(row["_cleanup_rowid"])
                fingerprint.update(
                    json.dumps(
                        list(row),
                        ensure_ascii=True,
                        default=repr,
                        separators=(",", ":"),
                    ).encode()
                )
        if drop or selected:
            actions.append(
                {
                    "table": table,
                    "operation": "drop_private_table"
                    if drop
                    else "delete_private_rows",
                    "count": len(selected),
                    "rowids": selected,
                    "fingerprint": fingerprint.hexdigest(),
                }
            )
    affected = {action["table"] for action in actions}
    if affected:
        for name, table, sql in triggers:
            if table in affected:
                if (
                    table != "knowledge_entries"
                    or name not in KNOWLEDGE_TRIGGERS
                    or "".join(sql.split()).casefold()
                    != "".join(KNOWLEDGE_TRIGGERS[name].split()).casefold()
                ):
                    raise ValueError("unsafe_target_trigger")
            if personal_entries and (
                table == "knowledge_totals" or table.startswith("knowledge_fts")
            ):
                raise ValueError("unsafe_knowledge_trigger")
        # Never let DROP/DELETE cascade into shared group or financial records.
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            if any(
                row[2] in affected or personal_entries and row[2] == "knowledge_totals"
                for row in db.execute(f"PRAGMA foreign_key_list({quoted})")
            ):
                raise ValueError("unsafe_target_foreign_key")
    # Match KnowledgeStore.change: remove each private FTS row before its content.
    actions.sort(key=lambda action: action["table"] != "knowledge_fts")
    return actions


def cleanup(targets, *, platform_ids=(), apply=False, expect_digest="", offline=False):
    """Preview exact targets or apply the same reviewed manifest while offline.

    Args:
        targets: Pairs of database role and explicit existing filesystem path.
        platform_ids: Exact WSL platform IDs for framework origins.
        apply: Whether to mutate the reviewed rows/tables.
        expect_digest: Digest returned by the prior preview.
        offline: Operator assertion that all users of these files are stopped.

    Returns:
        JSON-compatible summary without message contents, tokens or identities.

    Raises:
        ValueError: If scope, safety, or preview confirmation is invalid.
        OSError: If any explicit path cannot be opened.
        sqlite3.Error: If locking or schema validation fails.
    """
    if apply and (not offline or not expect_digest):
        raise ValueError("apply_requires_offline_and_preview_digest")
    platform_ids = tuple(sorted(set(platform_ids)))
    if any(not item or ":" in item for item in platform_ids):
        raise ValueError("invalid_wsl_platform_id")
    targets = sorted((kind, Path(path).absolute()) for kind, path in targets)
    if not targets or len({path for _, path in targets}) != len(targets):
        raise ValueError("explicit_unique_database_paths_required")
    names = {
        "open": "open-service.sqlite3",
        "interaction": "private_interaction.sqlite3",
        "messages": "messages.sqlite3",
        "friend_approval": "friend_approval.sqlite3",
    }
    with ExitStack() as stack:
        manifest, connections, identities = [], [], set()
        for kind, path in targets:
            if kind not in {*names, "framework"}:
                raise ValueError("unknown_database_role")
            if (kind in names and path.name != names[kind]) or (
                kind == "framework" and not platform_ids
            ):
                raise ValueError("database_scope_invalid")
            if path.is_symlink() or path.resolve(strict=True) != path:
                raise ValueError("symlink_database_path_rejected")
            stat = path.stat()
            identity = (stat.st_dev, stat.st_ino)
            if not path.is_file() or identity in identities:
                raise ValueError("database_alias_rejected")
            identities.add(identity)
            uri = path.as_uri() + ("?mode=rw" if apply else "?mode=ro")
            db = stack.enter_context(closing(sqlite3.connect(uri, uri=True, timeout=1)))
            stack.enter_context(db)
            db.row_factory = sqlite3.Row
            if not apply:
                db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
            actions = deletion_plan(db, kind, platform_ids)
            connections.append(db)
            manifest.append({"kind": kind, "path": str(path), "actions": actions})
        digest = hashlib.sha256(
            json.dumps(
                {"platform_ids": platform_ids, "databases": manifest}, sort_keys=True
            ).encode()
        ).hexdigest()
        if apply and digest != expect_digest:
            raise ValueError("preview_changed")
        if apply:
            for db, database in zip(connections, manifest):
                for action in database["actions"]:
                    table = action["table"]
                    if action["operation"] == "drop_private_table":
                        db.execute(f'DROP TABLE "{table}"')
                    else:
                        db.executemany(
                            f'DELETE FROM "{table}" WHERE rowid=?',
                            ((rowid,) for rowid in action["rowids"]),
                        )
                if any(
                    action["table"] == "knowledge_entries"
                    for action in database["actions"]
                ):
                    totals = db.execute(
                        "SELECT bytes,entries FROM knowledge_totals WHERE id=1"
                    ).fetchone()
                    aggregate = db.execute(
                        "SELECT COALESCE(SUM(bytes),0),COUNT(*) FROM knowledge_entries"
                    ).fetchone()
                    if tuple(totals) != tuple(aggregate):
                        raise ValueError("knowledge_totals_changed")
        for database in manifest:
            for action in database["actions"]:
                del action["rowids"]
        return {
            "applied": apply,
            "digest": digest,
            "databases": manifest,
            "protected": PROTECTED,
        }


def main(argv=None):
    """Run the opt-in offline maintenance command.

    Args:
        argv: Optional command-line arguments for isolated regression tests.

    Returns:
        Zero on a successful preview or apply.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in (
        "open-db",
        "messages-db",
        "private-interaction-db",
        "friend-approval-db",
        "framework-db",
    ):
        parser.add_argument("--" + flag, action="append", default=[], type=Path)
    parser.add_argument(
        "--wsl-platform-id",
        action="append",
        default=[],
        help="Exact verified WSL platform instance ID, required for --framework-db.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the confirmed preview; default is read-only.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Assert all processes using these explicit database files are stopped.",
    )
    parser.add_argument(
        "--expect-digest",
        default="",
        help="Digest from the reviewed preview.",
    )
    args = parser.parse_args(argv)
    targets = [
        (kind, path)
        for kind, paths in (
            ("open", args.open_db),
            ("messages", args.messages_db),
            ("interaction", args.private_interaction_db),
            ("friend_approval", args.friend_approval_db),
            ("framework", args.framework_db),
        )
        for path in paths
    ]
    try:
        result = cleanup(
            targets,
            platform_ids=args.wsl_platform_id,
            apply=args.apply,
            expect_digest=args.expect_digest,
            offline=args.offline,
        )
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(2, f"Cleanup refused: {exc}\n")
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
