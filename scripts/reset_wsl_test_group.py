"""Preview an offline reset of one existing test tenant; never call platform APIs."""

import argparse
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time
from contextlib import ExitStack, closing
from pathlib import Path

from scripts.audit_wsl_finance import FINANCIAL_FIELDS

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "43506527"
MANAGER = "31846829"
GROUP = "1459282"
FORMAL_GROUPS = {"1516387", "1488006"}
OPEN = "data/plugin_data/wangshangliao_open/open-service.sqlite3"
FINANCE = "data/plugin_data/wangshangliao_cashback/cashback.sqlite3"
SETTINGS = "data/plugin_data/wangshangliao_moderation/settings.sqlite3"
GRANTS = "data/plugin_data/wangshangliao_moderation/group_management.sqlite3"
FRAMEWORK = "data/data_v4.db"
UNITS = ("astrbot.service",)
FORMAT_VERSION = 1


class ResetError(ValueError):
    """Reject an unsafe or stale local test reset."""


def digest(value):
    """Fingerprint structured evidence without disclosing its contents.

    Args:
        value: JSON-compatible evidence, optionally containing SQLite blobs.

    Returns:
        Stable SHA-256 hexadecimal digest.
    """
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            default=lambda item: {"sqlite_blob": item.hex()},
        ).encode()
    ).hexdigest()


def offline_guard(root, paths):
    """Require explicitly stopped writers and no surviving database holders.

    Args:
        root: Exact installation being maintained.
        paths: Existing SQLite paths held by this reset.

    Raises:
        ResetError: If a service is running, a writer survives or inspection fails.
    """
    for unit in UNITS:
        result = subprocess.run(
            [
                "systemctl",
                "show",
                unit,
                "--property=ActiveState,MainPID,ControlPID,Job",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        state = dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
        )
        if (
            result.returncode
            or state.get("ActiveState") != "inactive"
            or state.get("MainPID") != "0"
            or state.get("ControlPID") != "0"
            or state.get("Job") not in {"", "0"}
        ):
            raise ResetError("stop_service_and_keep_stopped_until_apply_finishes")
    names = {str(path) + suffix for path in paths for suffix in ("", "-wal", "-shm")}
    for process in Path("/proc").iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            for fd in (process / "fd").iterdir():
                try:
                    name = os.readlink(fd).removesuffix(" (deleted)")
                except FileNotFoundError:
                    continue
                if name in names:
                    raise ResetError("database_still_open_by_another_process")
            command = (process / "cmdline").read_bytes().split(b"\0")
            cwd = (process / "cwd").resolve()
            python = bool(command) and re.fullmatch(
                r"python(?:\d+(?:\.\d+)*)?",
                Path(os.fsdecode(command[0])).name,
            )
            runners = {
                "astrbot.builtin_stars.wangshangliao_moderation.cashback_standalone",
                "scripts.wsl_test_lab",
                "scripts.wsl_test_membership",
            }
            if (
                cwd == root
                and python
                and any(
                    os.fsdecode(item) in runners
                    or Path(os.fsdecode(item)).name
                    in {"main.py", "wsl_test_lab.py", "wsl_test_membership.py"}
                    for item in command[1:]
                    if item and b"\n" not in item
                )
            ):
                raise ResetError("application_or_test_runner_still_running")
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            raise ResetError("cannot_verify_offline_processes") from None


def database_state(db):
    """Read deterministic rows while excluding the independent append-only receipt.

    Args:
        db: Existing SQLite connection with a consistent transaction.

    Returns:
        Full ordered table contents, including row identifiers.
    """
    state = []
    for (table,) in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' AND name<>'test_reset_audit' ORDER BY name"
    ):
        quoted = '"' + table.replace('"', '""') + '"'
        state.append(
            [
                table,
                [
                    tuple(row)
                    for row in db.execute(
                        f"SELECT rowid AS _reset_rowid,* FROM {quoted} ORDER BY rowid"
                    )
                ],
            ]
        )
    return state


class TestGroupReset:
    """Build exact-row mutations, requiring an unchanged offline preview to commit."""

    __test__ = False

    def __init__(self, root=ROOT):
        """Resolve trusted local identity and stable account-owned storage.

        Args:
            root: Local application installation, never an API URL.

        Raises:
            ResetError: If local identities, namespaces or protected state conflict.
        """
        self.root = Path(root).resolve()
        self.config = self.root / "data/cmd_config.json"
        self.bindings = (
            self.root / "data/platform_data/wangshangliao/account-roots.json"
        )
        for path in (self.config, self.bindings):
            self.safe_path(path)
        config = json.loads(self.config.read_text("utf-8-sig"))
        if config.get("wangshangliao_open", {}).get("mode") != "integrated":
            raise ResetError("integrated_mode_required")
        bots = [
            row
            for row in config.get("platform", [])
            if row.get("type") == "wangshangliao"
            and str(row.get("account_id")) in {ACCOUNT, MANAGER}
        ]
        if len(bots) != 2 or {str(b["account_id"]) for b in bots} != {
            ACCOUNT,
            MANAGER,
        }:
            raise ResetError("controlled_accounts_not_unique")
        instances = {str(b["account_id"]): b.get("id") for b in bots}
        if (
            any(
                not isinstance(instance, str)
                or not re.fullmatch(r"wangshangliao_[A-Za-z0-9_]+", instance)
                for instance in instances.values()
            )
            or len(set(instances.values())) != 2
        ):
            raise ResetError("instance_identity_invalid")
        self.instance = instances[ACCOUNT]
        roots = json.loads(self.bindings.read_text())
        namespace = roots.get(ACCOUNT)
        if (
            not isinstance(namespace, str)
            or not re.fullmatch(r"[a-f0-9]{64}", namespace)
            or sum(value == namespace for value in roots.values()) != 1
        ):
            raise ResetError("account_namespace_not_unique")
        self.native = self.bindings.parent / namespace
        self.paths = {
            name: self.safe_path(self.root / name)
            for name in (OPEN, FINANCE, SETTINGS, GRANTS, FRAMEWORK)
        }
        for filename in (
            "messages.sqlite3",
            "moderation.sqlite3",
            "schedules.sqlite3",
            "cards.sqlite3",
            "activities.sqlite3",
        ):
            path = self.safe_path(self.native / filename)
            self.paths[str(path.relative_to(self.root))] = path
        self.team = ""

    def safe_path(self, path):
        """Reject absent files and symlinked components under application data.

        Args:
            path: Expected existing regular file.

        Returns:
            Absolute validated path.

        Raises:
            ResetError: If a path escapes the installation or follows a symlink.
        """
        path = Path(path)
        if not path.is_relative_to(self.root):
            raise ResetError("path_outside_installation")
        for component in (path, *path.parents):
            if component == self.root:
                break
            if component.is_symlink():
                raise ResetError("symlinked_data_path")
        if not path.is_file() or path.stat().st_nlink != 1:
            raise ResetError("required_data_file_missing")
        return path

    def inspect(self, connections):
        """Build a read-only plan using explicit business namespaces and row versions.

        Args:
            connections: Existing connections keyed by installation-relative path.

        Returns:
            Public preview and private exact-row mutations.

        Raises:
            ResetError: If finance exists, identity conflicts or routing is malformed.
        """
        resolved = TestGroupReset(self.root)
        if (
            resolved.instance != self.instance
            or resolved.native != self.native
            or resolved.paths != self.paths
        ):
            raise ResetError("instance_or_storage_binding_changed")
        db = connections[OPEN]
        audit_head = ""
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='test_reset_audit'"
        ).fetchone():
            for row in db.execute(
                "SELECT previous,receipt,sha256 FROM test_reset_audit ORDER BY id"
            ):
                if row["previous"] != audit_head or row["sha256"] != digest(
                    [audit_head, row["receipt"]]
                ):
                    raise ResetError("reset_audit_chain_invalid")
                audit_head = row["sha256"]
        tenants = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM tenants WHERE group_id=? ORDER BY account", (GROUP,)
            )
        ]
        target = next((r for r in tenants if r["account"] == ACCOUNT), None)
        owner = next((r for r in tenants if r["account"] == MANAGER), None)
        if (
            not target
            or not owner
            or target["team"] != owner["team"]
            or not re.fullmatch(r"[0-9]{1,20}", target["team"])
            or target["cleanup_epoch"] > target["cleanup_done"]
            or json.loads(target["managers"]) not in ([], [MANAGER])
            or db.execute(
                "SELECT 1 FROM tenants WHERE account=? AND team=? AND group_id<>?",
                (ACCOUNT, target["team"], GROUP),
            ).fetchone()
            or db.execute(
                "SELECT 1 FROM retired_groups WHERE group_id=?", (GROUP,)
            ).fetchone()
        ):
            raise ResetError("test_tenant_identity_conflict")
        self.team = target["team"]
        for uid in (ACCOUNT, MANAGER):
            row = db.execute(
                "SELECT peer,conflicted FROM actor_bindings WHERE account=? AND uid=?",
                (ACCOUNT, uid),
            ).fetchone()
            if (
                not row
                or row["conflicted"]
                or not re.fullmatch(r"[0-9]{1,20}", row["peer"])
                or db.execute(
                    "SELECT 1 FROM actor_bindings WHERE account=? AND peer=? AND uid<>?",
                    (ACCOUNT, row["peer"], uid),
                ).fetchone()
            ):
                raise ResetError("controlled_identity_unverified")
        finance = connections[FINANCE]
        for (table,) in finance.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ):
            columns = {r[1] for r in finance.execute(f'PRAGMA table_info("{table}")')}
            column = "id" if table == "groups" else "group_id"
            if (
                column in columns
                and finance.execute(
                    f'SELECT 1 FROM "{table}" WHERE "{column}"=? LIMIT 1', (GROUP,)
                ).fetchone()
            ):
                raise ResetError("test_group_has_financial_data")
        if db.execute(
            "SELECT 1 FROM ai_charges WHERE account=? AND group_id=? LIMIT 1",
            (ACCOUNT, GROUP),
        ).fetchone():
            raise ResetError("test_group_has_model_cost_data")

        plans, fingerprints, expected, schemas = {}, {}, {}, {}
        origin = re.compile(
            rf"{re.escape(self.instance)}:GroupMessage:{ACCOUNT}/{GROUP}(?:/[^\n]*)?"
        )
        allowed = {
            OPEN: {
                "tenants",
                "capabilities",
                "manager_quotas",
                "group_ai",
                "ai_profiles",
                "ai_payers",
            },
            GRANTS: {"grants"},
            "moderation.sqlite3": {
                "content_violations",
                "semantic_reviews",
                "progressive_warnings",
            },
            "schedules.sqlite3": {"schedules", "confirmations"},
            "activities.sqlite3": {
                "lottery_settings",
                "invite_rules",
                "invite_seen",
                "invite_identities",
                "invite_attributions",
            },
        }
        for name, connection in connections.items():
            schema = connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
            ).fetchall()
            schemas[name] = digest([tuple(row) for row in schema])
            state, operations = [], []
            for (table,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name<>'test_reset_audit' ORDER BY name"
            ):
                quoted = '"' + table.replace('"', '""') + '"'
                rows = connection.execute(
                    f"SELECT rowid AS _reset_rowid,* FROM {quoted} ORDER BY rowid"
                ).fetchall()
                state.append([table, [tuple(row) for row in rows]])
                tables = allowed.get(name, allowed.get(Path(name).name, set()))
                for record in rows:
                    row = dict(record)
                    scoped = (
                        row.get("account") == ACCOUNT and row.get("group_id") == GROUP
                    )
                    update, delete = {}, scoped and table in tables
                    if name == OPEN and table == "membership_notices":
                        delete = (
                            row.get("account") == ACCOUNT
                            and row.get("team") == self.team
                            and row.get("state") != "departure_verified"
                        )
                    elif Path(name).name == "activities.sqlite3":
                        if table == "invite_credits" and scoped:
                            raise ResetError("test_group_has_activity_reward_data")
                        if table == "lotteries" and scoped:
                            update = {
                                "status": "cancelled",
                                "prize": "",
                                "contact": "",
                                "owner_name": "",
                                "results": "[]",
                                "error": "test_reset_existing_group",
                            }
                        elif table == "lottery_entries":
                            parent = connection.execute(
                                "SELECT account,group_id FROM lotteries WHERE id=?",
                                (row["lottery"],),
                            ).fetchone()
                            delete = bool(parent and tuple(parent) == (ACCOUNT, GROUP))
                    elif Path(name).name == "messages.sqlite3":
                        if table in {"inbox", "review_holds"}:
                            delete = row.get("account") == ACCOUNT and row.get(
                                "team"
                            ) in {GROUP, "unsupported/1/" + self.team}
                        elif table == "reply_recalls" and scoped:
                            if row["state"] == "pending":
                                update = {"state": "cancelled"}
                    elif Path(name).name == "moderation.sqlite3" and scoped:
                        if table == "automatic_mutes":
                            update = {"closed": 1}
                        elif table == "flood_recalls" and row["status"] == "pending":
                            update = {"status": "cancelled"}
                    elif Path(name).name == "cards.sqlite3":
                        if table in {"card_rules", "card_names", "card_seen"}:
                            identity = json.loads(row["identity"])
                            delete = (
                                isinstance(identity, list)
                                and len(identity) >= 2
                                and identity[:2] == [ACCOUNT, GROUP]
                            )
                        elif table == "card_jobs":
                            job = json.loads(row["data"])
                            if not isinstance(job, dict):
                                raise ResetError("card_job_shape_invalid")
                            if (
                                str(job.get("account")) == ACCOUNT
                                and str(job.get("group")) == GROUP
                            ):
                                job.update(
                                    state="stopped",
                                    items=[
                                        item
                                        for item in job.get("items", [])
                                        if item.get("state")
                                        in {"accepted", "unknown", "verified"}
                                    ],
                                )
                                update = {"data": json.dumps(job)}
                    elif name == SETTINGS and table == "policies":
                        if row["account"] == ACCOUNT:
                            policy = json.loads(row["payload"])
                            before = digest(policy)
                            policy["enabled_groups"] = [
                                g
                                for g in policy.get("enabled_groups", [])
                                if g != GROUP
                            ]
                            for mapping in (
                                policy.get("reply_groups", {}),
                                policy.get("ai_routes", {}).get("groups", {}),
                            ):
                                mapping.pop(GROUP, None)
                            for field in (
                                "permissions",
                                "auto_kick",
                                "card_auto",
                                "card_suffix",
                            ):
                                policy.get("moderation", {}).get(field, {}).pop(
                                    GROUP, None
                                )
                            for key, field in (
                                ("proactive_send", "targets"),
                                ("developer_test", "groups"),
                            ):
                                if field in policy.get(key, {}):
                                    policy[key][field] = [
                                        g for g in policy[key][field] if g != GROUP
                                    ]
                            if digest(policy) != before:
                                update = {
                                    "payload": json.dumps(policy),
                                    "version": row["version"] + 1,
                                }
                    elif name == FRAMEWORK:
                        fields = {
                            "platform_message_history": "user_id",
                            "conversations": "user_id",
                            "platform_sessions": "session_id",
                            "session_project_relations": "session_id",
                            "umo_aliases": "umo",
                            "webchat_threads": "parent_session_id",
                        }
                        if table in fields:
                            value = row.get(fields[table])
                            delete = isinstance(value, str) and bool(
                                origin.fullmatch(value)
                            )
                        elif table == "cron_jobs":
                            value = json.loads(row["payload"])
                            if isinstance(value, dict) and any(
                                isinstance(value.get(k), str)
                                and origin.fullmatch(value[k])
                                for k in (
                                    "session_id",
                                    "session",
                                    "umo",
                                    "unified_msg_origin",
                                )
                            ):
                                update = {
                                    "enabled": 0,
                                    "status": "paused",
                                    "last_error": "test_reset_existing_group",
                                }
                    if update:
                        update = {
                            key: value
                            for key, value in update.items()
                            if row[key] != value
                        }
                    if delete or update:
                        if name == FINANCE:
                            raise ResetError("financial_mutation_forbidden")
                        operations.append(
                            {
                                "table": table,
                                "row": row,
                                "update": update,
                                "delete": bool(delete),
                            }
                        )
            fingerprints[name] = digest(state)
            plans[name] = operations
            by_row = {(op["table"], op["row"]["_reset_rowid"]): op for op in operations}
            changed_state = []
            for table, rows in state:
                changed_rows = []
                for row in rows:
                    operation = by_row.get((table, row[0]))
                    if operation and operation["delete"]:
                        continue
                    if operation:
                        replacement = {
                            **operation["row"],
                            **operation["update"],
                        }
                        changed_rows.append(tuple(replacement.values()))
                    else:
                        changed_rows.append(row)
                changed_state.append([table, changed_rows])
            expected[name] = digest(changed_state)
        public = {
            "format_version": FORMAT_VERSION,
            "previous_reset_audit_sha256": audit_head,
            "account": ACCOUNT,
            "manager": MANAGER,
            "group": GROUP,
            "instance": self.instance,
            "namespace": self.native.name,
            "team": self.team,
            "tenant_version": target["version"],
            "epoch": target["epoch"],
            "bot_present": bool(target["present"]),
            "bot_active": bool(target["active"]),
            "departure_confirmed": (
                not target["present"]
                and not target["active"]
                and target["admission"] == "departed"
                and bool(
                    db.execute(
                        "SELECT 1 FROM audit WHERE account=? AND group_id=? "
                        "AND action='departure_confirmed' AND "
                        "json_valid(detail) AND json_extract(detail,'$.epoch')=?",
                        (ACCOUNT, GROUP, target["epoch"]),
                    ).fetchone()
                )
            ),
            "reset_existing_group": True,
            "genuinely_new_group": False,
            "requires_fresh_invitation_after_reset": True,
            "preserved_departure_fences": db.execute(
                "SELECT COUNT(*) FROM membership_notices WHERE account=? AND team=? "
                "AND state='departure_verified'",
                (ACCOUNT, self.team),
            ).fetchone()[0],
            "financial_sha256": digest(
                [
                    tuple(row)
                    for row in finance.execute(
                        f"SELECT {FINANCIAL_FIELDS} FROM entries ORDER BY id"
                    )
                ]
            ),
            "rows": {
                name: {
                    table: sum(op["table"] == table for op in operations)
                    for table in sorted({op["table"] for op in operations})
                }
                for name, operations in plans.items()
                if operations
            },
            "fingerprints": fingerprints,
            "expected_after": expected,
            "schemas": schemas,
            "config_sha256": hashlib.sha256(self.config.read_bytes()).hexdigest(),
            "bindings_sha256": hashlib.sha256(self.bindings.read_bytes()).hexdigest(),
        }
        public["confirmation_sha256"] = digest(public)
        return public, plans

    def preview(self):
        """Read SQLite snapshots without creating backup files or modifying stores.

        Returns:
            Versioned, content-free confirmation preview.
        """
        with ExitStack() as stack:
            connections = {}
            for name, path in self.paths.items():
                db = stack.enter_context(
                    closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True))
                )
                db.row_factory = sqlite3.Row
                db.execute("BEGIN")
                connections[name] = db
            return self.inspect(connections)[0]

    def apply(self, confirmation, output):
        """Back up and reset exact rows only after an unchanged, offline preview.

        Args:
            confirmation: Exact SHA-256 from a reviewed preview.
            output: New private backup directory outside application data.

        Returns:
            Minimal completion receipt; this is not a new-group acceptance result.

        Raises:
            ResetError: If scope, offline state, preview or database checks fail.
        """
        if os.geteuid() != 0:
            raise ResetError("apply_requires_root_maintenance_operator")
        if not re.fullmatch(r"[0-9a-f]{64}", confirmation or ""):
            raise ResetError("reviewed_preview_required")
        output = Path(output)
        if (
            not output.is_absolute()
            or output.exists()
            or output.is_symlink()
            or output.resolve().is_relative_to(self.root)
            or any(p.is_symlink() for p in output.parents)
            or not output.parent.is_dir()
            or output.parent.stat().st_uid != 0
            or output.parent.stat().st_mode & 0o022
        ):
            raise ResetError("new_private_external_backup_directory_required")
        offline_guard(self.root, self.paths.values())
        output.mkdir(mode=0o700, parents=False, exist_ok=False)
        committed = []
        receipt = {
            "format_version": FORMAT_VERSION,
            "account": ACCOUNT,
            "manager": MANAGER,
            "group": GROUP,
            "reset_existing_group": True,
            "genuinely_new_group": False,
            "status": "preparing",
            "backup": str(output),
            "confirmation_sha256": confirmation,
            "snapshots": {},
            "committed": committed,
        }
        with ExitStack() as stack:
            lock = stack.enter_context((output / "reset.lock").open("xb"))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            connections = {}
            for name, path in self.paths.items():
                self.safe_path(path)
                db = stack.enter_context(closing(sqlite3.connect(path, timeout=0)))
                db.row_factory = sqlite3.Row
                db.execute("BEGIN IMMEDIATE")
                connections[name] = db
                stack.callback(db.rollback)
            current, plans = self.inspect(connections)
            if current["confirmation_sha256"] != confirmation:
                raise ResetError("preview_changed_refresh_and_review")
            if not current["departure_confirmed"]:
                raise ResetError("remove_test_bot_and_confirm_departure_before_reset")
            offline_guard(self.root, self.paths.values())
            for index, (name, path) in enumerate(self.paths.items()):
                destination = output / f"{index:02d}-{path.name}"
                with (
                    closing(
                        sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
                    ) as source,
                    closing(sqlite3.connect(destination)) as backup,
                ):
                    source.backup(backup)
                    if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ResetError("backup_integrity_check_failed")
                destination.chmod(0o400)
                receipt["snapshots"][name] = {
                    "file": destination.name,
                    "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                }
                with closing(sqlite3.connect(destination)) as snapshot:
                    if (
                        digest(database_state(snapshot))
                        != current["fingerprints"][name]
                    ):
                        raise ResetError("backup_does_not_match_locked_preview")
            receipt["expected_after"] = current["expected_after"]
            # The application owner cannot rewrite this root-owned archive or receipt.
            report = output / "receipt.json"
            with report.open("x") as stream:
                json.dump(receipt, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            report.chmod(0o400)
            directory = os.open(output, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            audit = connections[OPEN]
            audit.execute(
                "CREATE TABLE IF NOT EXISTS test_reset_audit("
                "id INTEGER PRIMARY KEY,created REAL NOT NULL,"
                "account TEXT NOT NULL,group_id TEXT NOT NULL,"
                "confirmation TEXT NOT NULL,previous TEXT NOT NULL,"
                "receipt TEXT NOT NULL,sha256 TEXT NOT NULL)"
            )
            for operation in ("UPDATE", "DELETE"):
                audit.execute(
                    f"CREATE TRIGGER IF NOT EXISTS test_reset_no_{operation.lower()} "
                    f"BEFORE {operation} ON test_reset_audit BEGIN "
                    "SELECT RAISE(ABORT,'test reset audit is append-only'); END"
                )
            previous = audit.execute(
                "SELECT sha256 FROM test_reset_audit ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = previous[0] if previous else ""
            payload = {
                **receipt,
                "status": "reset_planned",
                "tenant_version": current["tenant_version"],
                "epoch": current["epoch"],
                "team": self.team,
                "instance": self.instance,
            }
            payload_json = json.dumps(payload, sort_keys=True)
            audit.execute(
                "INSERT INTO test_reset_audit(created,account,group_id,confirmation,"
                "previous,receipt,sha256) VALUES(?,?,?,?,?,?,?)",
                (
                    time.time(),
                    ACCOUNT,
                    GROUP,
                    confirmation,
                    previous,
                    payload_json,
                    digest([previous, payload_json]),
                ),
            )
            # Keep all action receipts/audits. Commit authorization removal last.
            try:
                order = [name for name in connections if name != OPEN] + [OPEN]
                for name in order:
                    if not plans[name] and name != OPEN:
                        continue
                    offline_guard(self.root, self.paths.values())
                    db = connections[name]
                    for op in plans[name]:
                        table = '"' + op["table"].replace('"', '""') + '"'
                        rowid = op["row"]["_reset_rowid"]
                        if op["delete"]:
                            changed = db.execute(
                                f"DELETE FROM {table} WHERE rowid=?", (rowid,)
                            ).rowcount
                        else:
                            fields = ",".join(f'"{key}"=?' for key in op["update"])
                            changed = db.execute(
                                f"UPDATE {table} SET {fields} WHERE rowid=?",
                                (*op["update"].values(), rowid),
                            ).rowcount
                        if changed != 1:
                            raise ResetError("target_row_changed")
                    if (
                        db.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
                        or db.execute("PRAGMA foreign_key_check").fetchall()
                        or digest(database_state(db)) != current["expected_after"][name]
                    ):
                        raise ResetError("reset_integrity_check_failed")
                offline_guard(self.root, self.paths.values())
                for name in order:
                    if plans[name] or name == OPEN:
                        connections[name].commit()
                        committed.append(name)
                receipt["status"] = "reset_complete"
            except BaseException:
                receipt["status"] = "incomplete_keep_services_stopped"
                raise
            finally:
                # Never automatically restore old snapshots over newer financial data.
                final = output / "completion.json"
                with final.open("x") as stream:
                    json.dump(receipt, stream, sort_keys=True)
                    stream.flush()
                    os.fsync(stream.fileno())
                final.chmod(0o400)
        return receipt


def main():
    """Expose preview by default and a separately confirmed offline apply.

    Raises:
        SystemExit: If CLI arguments or reset safety checks fail.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--format-version", type=int, default=FORMAT_VERSION)
    parser.add_argument("--confirmation-sha256")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.format_version != FORMAT_VERSION:
        parser.error("Unsupported reset format version.")
    if args.apply != bool(args.confirmation_sha256 and args.output) or (
        not args.apply and (args.confirmation_sha256 or args.output)
    ):
        parser.error("--apply requires --confirmation-sha256 and --output.")
    try:
        reset = TestGroupReset(args.root)
        result = (
            reset.apply(args.confirmation_sha256, args.output)
            if args.apply
            else reset.preview()
        )
    except (ResetError, sqlite3.Error, OSError, ValueError) as error:
        parser.exit(1, f"Test reset refused: {type(error).__name__}: {error}\n")
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
