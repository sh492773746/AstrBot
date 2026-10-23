"""Verify real own-message recall with a locally injected classification only.

This probes the existing persistent scheduler, not a human command roundtrip.
No authentication, consumer, protection, or public action interface is changed.
"""

import argparse
import copy
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from acceptance_client import GROUP, PREFIX, ROOT, AcceptanceClient

from data.plugins.astrbot_plugin_lajiaoliao.maintenance import MaintenanceLedger
from data.plugins.astrbot_plugin_lajiaoliao.storage import atomic_json


class ExistingReplyLedger(MaintenanceLedger):
    """Use existing tables without running startup recovery or schema creation."""

    def __init__(self, path):
        if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o077:
            raise RuntimeError("existing_private_ledger_required")
        self.path = path

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()


def main():
    """Send four owned probes, schedule once each, and verify durable recalls."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    if args.output.exists():
        raise RuntimeError("existing_report_requires_review_not_resend")
    args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    client = AcceptanceClient()
    uids = {row["account_id"] for row in client.sessions}
    scopes = [
        {"session_ref": bot["session_ref"], "group_id": GROUP} for bot in client.bots
    ]
    before = [client.api(PREFIX + "business", scope) for scope in scopes]
    for scope in scopes:
        roster = client.api(PREFIX + "members", {**scope, "page": 1})
        if (
            roster["total"] != 2
            or {row["userID"] for row in roster["members"]} != uids
            or client.api(PREFIX + "group-state", scope)["group_status"] != 0
        ):
            raise RuntimeError("unsafe_recall_test_scope")
    report = {
        "started_at": time.time(),
        "scope": "real_scheduler_and_native_recall_not_human_command_pipeline",
        "synthetic_classification": True,
        "executed": False,
        "passed": False,
        "steps": [],
    }
    atomic_json(args.output, report)
    if not args.execute:
        print("Read-only recall preflight passed.")
        return
    changed, windows = {}, {}

    def save(index, value):
        current = client.api(PREFIX + "business", scopes[index])
        result = client.api(
            PREFIX + "business",
            {
                **scopes[index],
                "action": "save",
                "confirm": True,
                "version": current["version"],
                "instance_revision": current["instance_revision"],
                "settings": value,
            },
        )
        if result["settings"] != value:
            raise RuntimeError("recall_configuration_not_saved")

    try:
        for index, bot in enumerate(client.bots):
            settings = copy.deepcopy(before[index]["settings"])
            settings["maintenance"]["reply_recall_enabled"] = True
            settings["native_actions"] = sorted(
                set(settings["native_actions"]) | {"reply_recall"}
            )
            save(index, settings)
            changed[index] = settings
            peer_uid = client.sessions[1 - index]["account_id"]
            windows[index] = client.api(
                PREFIX + "test-window",
                {
                    "session_ref": bot["session_ref"],
                    "action": "open",
                    "confirm": True,
                    "account_id": client.sessions[index]["account_id"],
                    "duration": 180,
                    "budget": 2,
                    "targets": [
                        {"kind": "group", "target_id": GROUP},
                        {"kind": "private", "target_id": peer_uid},
                    ],
                },
            )
            ledger = ExistingReplyLedger(
                ROOT
                / "data/plugin_data/astrbot_plugin_lajiaoliao"
                / bot["session_ref"]
                / "business.sqlite3"
            )
            for kind, target in (("group", GROUP), ("private", peer_uid)):
                entry = {
                    "instance": bot["id"],
                    "kind": kind,
                    "status": "unknown",
                    "operation_id": "reply-source-" + secrets.token_hex(8),
                    "peer_index": 1 - index,
                    "peer_target": GROUP
                    if kind == "group"
                    else client.sessions[index]["account_id"],
                }
                report["steps"].append(entry)
                report["executed"] = True
                atomic_json(args.output, report)
                receipt = client.api(
                    PREFIX + "test-send",
                    {
                        "session_ref": bot["session_ref"],
                        "window_ref": windows[index]["window_ref"],
                        "operation_id": entry["operation_id"],
                        "kind": kind,
                        "target_id": target,
                        "text": "自动撤回验收：本条将在20秒后撤回。",
                    },
                )
                entry["source_receipt"] = receipt
                atomic_json(args.output, report)
                if receipt.get("status") != "sent":
                    raise RuntimeError("recall_source_not_confirmed")
                message = receipt["message_id"]
                deadline = time.monotonic() + 20
                while not any(
                    row.get("message_id") == message
                    for row in client.inspect(
                        entry["peer_index"], kind, entry["peer_target"]
                    )
                ):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("recall_source_not_visible")
                    time.sleep(0.5)
                for _ in range(2):
                    ledger.schedule_reply(
                        GROUP, message, kind, target, "dashboard:" + client.owner
                    )
                with ledger.connect() as db:
                    jobs = db.execute(
                        "SELECT * FROM scheduled_jobs WHERE feature='reply_recall' "
                        "AND json_extract(payload,'$.message_id')=?",
                        (message,),
                    ).fetchall()
                if len(jobs) != 1:
                    raise RuntimeError("recall_job_not_idempotent")
                entry.update(job_id=jobs[0]["id"], due=jobs[0]["due"])
                atomic_json(args.output, report)

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            for entry in report["steps"]:
                if entry["status"] == "succeeded":
                    continue
                index = 1 - entry["peer_index"]
                current = client.api(PREFIX + "business", scopes[index])
                job = next(
                    (
                        row
                        for row in current["scheduled"]
                        if row["id"] == entry["job_id"]
                    ),
                    None,
                )
                if not job or job["state"] == "pending":
                    continue
                if job["state"] not in {"running", "succeeded"}:
                    raise RuntimeError("recall_job_" + job["state"])
                if job["state"] == "running":
                    continue
                receipt = client.api(
                    PREFIX + "platform-action",
                    {
                        "session_ref": client.bots[index]["session_ref"],
                        "instance_id": client.bots[index]["id"],
                        "operation_id": "task-" + entry["job_id"],
                        "query": True,
                    },
                )
                rows = client.inspect(
                    entry["peer_index"], entry["kind"], entry["peer_target"]
                )
                source = next(
                    (
                        row
                        for row in rows
                        if row.get("message_id")
                        == entry["source_receipt"]["message_id"]
                    ),
                    None,
                )
                if receipt.get("status") != "succeeded":
                    raise RuntimeError("native_recall_not_confirmed")
                if source and source.get("content_type") != 2101:
                    continue
                entry.update(
                    status="succeeded",
                    job=job,
                    native_receipt=receipt,
                    peer_recalled=True,
                    duplicate_job_count=1,
                )
                atomic_json(args.output, report)
            if all(row["status"] == "succeeded" for row in report["steps"]):
                report["passed"] = True
                break
            time.sleep(0.5)
        if not report["passed"]:
            raise RuntimeError("recall_acceptance_timeout_do_not_resend")
    except Exception as error:
        report["error"] = (
            str(error) if isinstance(error, RuntimeError) else type(error).__name__
        )
    finally:
        for index in windows:
            try:
                client.api(
                    PREFIX + "test-window",
                    {
                        "session_ref": client.bots[index]["session_ref"],
                        "action": "close",
                    },
                )
            except Exception:
                report.setdefault("cleanup_errors", []).append("window_close_failed")
        for index, value in changed.items():
            try:
                current = client.api(PREFIX + "business", scopes[index])
                if current["settings"] != value:
                    raise RuntimeError("settings_changed_externally")
                save(index, before[index]["settings"])
            except Exception as error:
                report.setdefault("cleanup_errors", []).append(str(error))
        report["configuration_unchanged"] = (
            client.original_config == (ROOT / "data/cmd_config.json").read_bytes()
        )
        report["finished_at"] = time.time()
        if report.get("cleanup_errors") or not report["configuration_unchanged"]:
            report["passed"] = False
        atomic_json(args.output, report)
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "steps"},
            ensure_ascii=False,
        )
    )
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
