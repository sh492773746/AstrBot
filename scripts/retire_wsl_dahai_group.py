"""Remove only the explicitly retired Dahai group from active local stores."""

import argparse
import json
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import jwt

from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenStore

ROOT = Path(__file__).resolve().parents[1]
GROUP = "1143980"
TEAM = "32467997072"
NAME = "\u5927\u6d77\u517c\u804c\u7fa4"
PLUGIN = ROOT / "data/plugin_data"
PREFIX = "/api/v1/plugins/extensions/wangshangliao_moderation/"


def group_origin(value):
    """Match a native group origin with exact group boundaries.

    Args:
        value: Stored session identifier.

    Returns:
        Whether the identifier belongs exclusively to the retired group.
    """
    return isinstance(value, str) and bool(
        re.match(
            rf"^wangshangliao_[A-Za-z0-9_]+:GroupMessage:\d+/{GROUP}(?:/|$)",
            value,
        )
    )


def group_payload(value):
    """Resolve explicit JSON routing fields, never substrings in message text.

    Args:
        value: Parsed structured payload.

    Returns:
        Whether the payload contains an explicit retired-group route.
    """
    if isinstance(value, dict):
        return any(
            str(item) == GROUP
            if key in {"group", "group_id", "groupId", "business_group"}
            else group_origin(item)
            if key in {"session", "session_id", "umo", "unified_msg_origin"}
            else group_payload(item)
            if isinstance(item, (dict, list))
            else False
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(group_payload(item) for item in value)
    return False


def scrub_policy(value, field=""):
    """Remove only group mappings and group-selection list entries.

    Args:
        value: Parsed native policy or policy-history payload.
        field: Parent field identifying group-selection lists.

    Returns:
        A detached payload preserving all other policy values.
    """
    if isinstance(value, dict):
        return {
            key: scrub_policy(item, key) for key, item in value.items() if key != GROUP
        }
    if isinstance(value, list):
        return [
            scrub_policy(item, field)
            for item in value
            if not (field in {"enabled_groups", "groups", "targets"} and item == GROUP)
        ]
    return value


def deletion_plan(db, framework=False):
    """Resolve direct scope and linked child rows before deleting any parent.

    Args:
        db: Readable SQLite connection with dictionary rows.
        framework: Require native platform origins in shared session tables.

    Returns:
        Row IDs by table, all justified by exact group identity or a parent link.
    """
    tables = [
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    selected, contents = {}, {}
    json_fields = {
        "operations": ("result",),
        "approvals": ("operation",),
        "card_jobs": ("data",),
        "requests": ("result",),
        "cron_jobs": ("payload",),
        "settings_log": ("detail",),
    }
    for table in tables:
        if table in {
            "policies",
            "policy_history",
            "retired_groups",
            "sqlite_sequence",
        }:
            continue
        quoted = '"' + table.replace('"', '""') + '"'
        records = [
            dict(row)
            for row in db.execute(f"SELECT rowid AS _purge_rowid,* FROM {quoted}")
        ]
        contents[table] = records
        selected[table] = set()
        for row in records:
            match = any(
                str(row.get(key, "")) == GROUP
                for key in ("group_id", "group", "business_group")
            )
            if table == "groups":
                match |= str(row.get("id")) == GROUP
            if table == "inbox":
                match |= row.get("team") in {
                    GROUP,
                    TEAM,
                    "unsupported/1/" + TEAM,
                }
            if framework:
                match |= any(
                    group_origin(row.get(key))
                    for key in (
                        "user_id",
                        "umo",
                        "session_id",
                        "scope_id",
                        "parent_session_id",
                    )
                )
            if table in {"outbox", "card_names", "card_seen", "card_rules"}:
                identifier = str(row.get("key", row.get("identity", "")))
                parts = identifier.split("/")
                match |= group_origin(identifier) or (
                    len(parts) >= 2 and parts[0].isdigit() and parts[1] == GROUP
                )
                if table != "outbox":
                    try:
                        identity = json.loads(identifier)
                        match |= (
                            isinstance(identity, list)
                            and len(identity) == 4
                            and str(identity[0]).isdigit()
                            and str(identity[1]) == GROUP
                        )
                    except (ValueError, TypeError):
                        pass
                if table == "outbox":
                    try:
                        value = json.loads(row["fingerprint"])
                        match |= (
                            isinstance(value, list)
                            and bool(value)
                            and str(value[0]) == GROUP
                        ) or group_payload(value)
                    except (ValueError, TypeError):
                        pass
            for key in json_fields.get(table, ()):
                try:
                    match |= group_payload(json.loads(row[key]))
                except (ValueError, TypeError, KeyError):
                    pass
            if match:
                selected[table].add(row["_purge_rowid"])
    for child, column, parent, parent_column in (
        ("lottery_entries", "lottery", "lotteries", "id"),
        ("automatic_kick_events", "operation", "automatic_kicks", "operation"),
        ("reply_recalls", "key", "outbox", "key"),
        ("session_project_relations", "session_id", "platform_sessions", "session_id"),
    ):
        identifiers = {
            row[parent_column]
            for row in contents.get(parent, [])
            if row["_purge_rowid"] in selected[parent]
        }
        if child in contents:
            selected[child].update(
                row["_purge_rowid"]
                for row in contents[child]
                if row.get(column) in identifiers
            )
    return {table: sorted(ids) for table, ids in selected.items() if ids}


def main():
    """Back up and purge the one authorized group while preserving other finance."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    cash_path = PLUGIN / "wangshangliao_cashback/cashback.sqlite3"
    settings_path = PLUGIN / "wangshangliao_moderation/settings.sqlite3"
    open_path = PLUGIN / "wangshangliao_open/open-service.sqlite3"
    paths = sorted((ROOT / "data/platform_data/wangshangliao").glob("*/*.sqlite3"))
    paths += [
        cash_path,
        settings_path,
        open_path,
        PLUGIN / "wangshangliao_moderation/group_management.sqlite3",
        ROOT / "data/data_v4.db",
    ]
    with closing(sqlite3.connect(cash_path)) as db:
        row = db.execute("SELECT name FROM groups WHERE id=?", (GROUP,)).fetchone()
        assert row and row[0] == NAME, "Exact group identity was not confirmed."
        fields = "id,group_id,member,inviter,amount,paid_at,paid_by,member_class"
        financial_before = set(
            db.execute(f"SELECT {fields} FROM entries WHERE group_id!=?", (GROUP,))
        )
    plans = {}
    for path in paths:
        if not path.exists():
            continue
        with closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        ) as db:
            db.row_factory = sqlite3.Row
            plan = deletion_plan(db, framework=path.name == "data_v4.db")
        if plan or path in {settings_path, open_path}:
            plans[path] = plan
    counts = {
        str(path.relative_to(ROOT)): {table: len(ids) for table, ids in plan.items()}
        for path, plan in plans.items()
    }
    print(json.dumps({"group": GROUP, "dry_run": not args.apply, "rows": counts}))
    if not args.apply:
        return
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%dT%H%M%S")
    backup = Path("/opt/backups") / ("wsl-retire-dahai-" + stamp)
    backup.mkdir(mode=0o700)
    backup_files = {}
    for index, path in enumerate(plans):
        destination = backup / f"{index:02d}-{path.name}"
        with closing(sqlite3.connect(path)) as live:
            with closing(sqlite3.connect(destination)) as snapshot:
                live.backup(snapshot)
        destination.chmod(0o600)
        backup_files[str(path.relative_to(ROOT))] = destination.name
    config = json.loads((ROOT / "data/cmd_config.json").read_text("utf-8-sig"))
    token = jwt.encode(
        {"username": config["dashboard"]["username"], "exp": int(time.time()) + 600},
        config["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )
    with httpx.Client(
        base_url="http://127.0.0.1:6185",
        headers={"Authorization": "Bearer " + token},
        trust_env=False,
        timeout=60,
    ) as api:
        store = OpenStore(open_path.parent)
        for tenant in store.tenants():
            if tenant["group_id"] != GROUP:
                continue
            payload = {
                "account": tenant["account"],
                "group_id": GROUP,
                "version": tenant["version"],
                "managers": tenant["managers"],
                "rules": tenant["rules"],
                "paused": True,
            }
            response = api.post(
                PREFIX + "open-service",
                json={"action": "tenant_preview", "payload": payload},
            )
            preview = response.json()
            assert response.status_code == 200 and preview["status"] == "ok"
            response = api.post(
                PREFIX + "open-service",
                json={
                    "action": "tenant",
                    "payload": {
                        **payload,
                        "confirmation": preview["data"]["confirmation"],
                    },
                },
            )
            assert response.status_code == 200 and response.json()["status"] == "ok"
        store.retire_group(GROUP)
        with closing(sqlite3.connect(cash_path)) as db:
            db.execute(
                "UPDATE groups SET enabled=0,version=version+1 WHERE id=?", (GROUP,)
            )
            db.commit()
        with closing(sqlite3.connect(settings_path)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute("SELECT * FROM policies").fetchall():
                original = json.loads(row["payload"])
                cleaned = scrub_policy(original)
                if cleaned != original:
                    db.execute(
                        "UPDATE policies SET payload=?,version=version+1,effective_at=? "
                        "WHERE instance=? AND account=? AND version=?",
                        (
                            json.dumps(cleaned, ensure_ascii=True, sort_keys=True),
                            time.time(),
                            row["instance"],
                            row["account"],
                            row["version"],
                        ),
                    )
            for row in db.execute("SELECT * FROM policy_history").fetchall():
                original = {
                    key: json.loads(row[key]) for key in ("before_json", "after_json")
                }
                cleaned = {key: scrub_policy(value) for key, value in original.items()}
                if cleaned != original:
                    db.execute(
                        "UPDATE policy_history SET before_json=?,after_json=? WHERE id=?",
                        (
                            json.dumps(cleaned["before_json"])
                            if cleaned["before_json"] != original["before_json"]
                            else row["before_json"],
                            json.dumps(cleaned["after_json"])
                            if cleaned["after_json"] != original["after_json"]
                            else row["after_json"],
                            row["id"],
                        ),
                    )
            db.commit()
        response = api.post("/api/v1/plugins/wangshangliao_moderation/reload")
        assert response.status_code == 200 and response.json()["status"] == "ok"
        for _ in range(30):
            response = api.post(
                PREFIX + "open-service", json={"action": "status", "payload": {}}
            )
            state = response.json()["data"]
            if not any(t["group_id"] == GROUP for t in state["groups"]) and all(
                a["state"] == "online" for a in state["accounts"]
            ):
                break
            time.sleep(1)
        assert not any(t["group_id"] == GROUP for t in state["groups"])
        assert all(a["state"] == "online" for a in state["accounts"])
        deleted = {}
        for path in plans:
            with closing(sqlite3.connect(path)) as db:
                db.row_factory = sqlite3.Row
                db.execute("BEGIN IMMEDIATE")
                plan = deletion_plan(db, framework=path.name == "data_v4.db")
                deleted[str(path.relative_to(ROOT))] = {}
                ordered = sorted(plan, key=lambda table: table == "groups")
                for table in ordered:
                    quoted = '"' + table.replace('"', '""') + '"'
                    changed = db.executemany(
                        f"DELETE FROM {quoted} WHERE rowid=?",
                        [(row_id,) for row_id in plan[table]],
                    ).rowcount
                    assert changed == len(plan[table])
                    deleted[str(path.relative_to(ROOT))][table] = changed
                assert not deletion_plan(db, framework=path.name == "data_v4.db")
                assert db.execute("PRAGMA foreign_key_check").fetchall() == []
                db.commit()
        with closing(sqlite3.connect(cash_path)) as db:
            assert not db.execute(
                "SELECT 1 FROM groups WHERE id=?", (GROUP,)
            ).fetchone()
            after = set(db.execute(f"SELECT {fields} FROM entries"))
            assert financial_before <= after, "Other-group financial fields changed."
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        result = {
            "group": GROUP,
            "deleted": deleted,
            "other_financial_fields_preserved": True,
            "rediscovery_blocked": GROUP in store.retired_groups(),
            "backup": str(backup),
            "backup_files": backup_files,
            "open_service_rows_removed": counts[str(open_path.relative_to(ROOT))],
        }
        evidence = backup / "deletion.json"
        evidence.write_text(json.dumps(result, ensure_ascii=True, indent=2))
        evidence.chmod(0o600)
        print(json.dumps(result))


if __name__ == "__main__":
    main()
