"""Migrate only the existing controller and test customer via the loopback API."""

import argparse
import copy
import json
import os
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from deploy import NAME, ROOT, client

SELECTED = {"VIP_DHBot": "controller", "AIClient_8910402926": "customer"}
TYPE = "telethon_ai_service"
ISOLATION = {
    "telegram_required_plugin": NAME,
    "telegram_dedicated_reporting": True,
    "telegram_command_register": False,
    "telegram_command_auto_refresh": False,
}


def result(response):
    response.raise_for_status()
    body = response.json()
    if body.get("status") != "ok":
        raise RuntimeError("Administration action needs manual review")
    return body


def put(api, config):
    result(
        api.put("/api/v1/bots/by-id", json={"bot_id": config["id"], "config": config})
    )


def stats(api):
    return {
        row["id"]: row
        for row in result(api.get("/api/v1/bots/stats"))["data"]["platforms"]
    }


def backup_state(before):
    os.umask(0o077)
    backup = Path("/root/Projects/agents/telethon-ai-deployment") / (
        "portable-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    )
    backup.mkdir(parents=True, mode=0o700)
    shutil.copy2(ROOT / "data/cmd_config.json", backup / "cmd_config.json")
    shutil.copytree(ROOT / "data/config", backup / "config")
    data = ROOT / "data/plugin_data" / NAME
    shutil.copytree(data / "private", backup / "private")
    accounts = json.loads((data / "private/accounts.json").read_text())
    sessions = backup / "sessions"
    sessions.mkdir(mode=0o700)
    for alias, spec in accounts.items():
        session = Path(spec["session"] + ".session")
        assert session.is_absolute() and session.is_file() and not session.is_symlink()
        with sqlite3.connect(f"file:{session}?mode=ro", uri=True) as source_db:
            with sqlite3.connect(sessions / f"{alias}.session") as target:
                source_db.backup(target)
    for source, name in (
        (data / "tenants.db", "tenants.db"),
        (data / "gate.db", "gate.db"),
        (ROOT / "data/data_v4.db", "data_v4.db"),
    ):
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as source_db:
            with sqlite3.connect(backup / name) as target:
                source_db.backup(target)
    shutil.copy2(
        ROOT / "integrations/telethon-ai/dist/astrbot_plugin_telethon_ai-v0.3.11.zip",
        backup / "rollback-v0.3.11.zip",
    )
    (backup / "status-before.json").write_text(json.dumps(before))
    return backup


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    local = json.loads((ROOT / "data/cmd_config.json").read_text(encoding="utf-8-sig"))
    original = {p["id"]: copy.deepcopy(p) for p in local["platform"]}
    plugin_config = json.loads(
        (ROOT / "data/config" / f"{NAME}_config.json").read_text(encoding="utf-8-sig")
    )
    assert plugin_config["control_platform_id"] == "VIP_DHBot"
    assert plugin_config["controller_mode"] == "standalone"
    assert plugin_config["admin_ids"] == ["1000000001"]
    assert all(original[key]["type"] == "telegram" for key in SELECTED)
    assert all(original[key]["enable"] is True for key in SELECTED)
    for key, role in SELECTED.items():
        config = original[key]
        assert all(
            config.get(k) == v
            or (
                role == "controller"
                and k == "telegram_dedicated_reporting"
                and k not in config
            )
            for k, v in ISOLATION.items()
        )
        prefix = config["telegram_token"].split(":", 1)[0]
        assert prefix == ("8941933661" if role == "controller" else "8910402926")
        assert (
            sum(
                p.get("telegram_token") == config["telegram_token"]
                for p in original.values()
            )
            == 1
        )

    with client() as api:
        for key in SELECTED:
            live = result(api.get("/api/v1/bots/by-id", params={"bot_id": key}))[
                "data"
            ]["bot"]
            assert live == original[key], "Live and persisted configuration differ"
        before = api.get(f"/api/plug/{NAME}/status").json()
        assert before["control_attached"]
        active = [
            a["platform_id"] for a in before["accounts"] if a["state"] == "running"
        ]
        prior_stats = stats(api)
        if not args.apply:
            print(
                "Preflight passed: only controller and existing test clone will migrate."
            )
            return
        backup = backup_state(before)
        print(f"Private rollback backup: {backup}")
        try:
            for key in SELECTED:
                put(api, {**original[key], "enable": False})
            assert not any(key in stats(api) for key in SELECTED)
            result(api.post("/api/plugin/reload", json={"name": NAME}))
            for key, role in SELECTED.items():
                updated = {
                    **original[key],
                    **ISOLATION,
                    "type": TYPE,
                    "service_role": role,
                }
                updated["telegram_allowed_updates"] = (
                    ["message", "callback_query", "pre_checkout_query", "managed_bot"]
                    if role == "controller"
                    else ["message"]
                )
                put(api, updated)
            for key in active:
                assert (
                    original[key]["type"] == "telethon_ai" and original[key]["enable"]
                )
                put(api, original[key])
            for _ in range(45):
                current = stats(api)
                if all(
                    current.get(key, {}).get("status") == "running"
                    for key in [*SELECTED, *active]
                ):
                    break
                time.sleep(1)
            else:
                raise RuntimeError(
                    "Runtime did not recover; inspect private rollback backup"
                )
            after_response = api.get(f"/api/plug/{NAME}/status")
            after_response.raise_for_status()
            after = after_response.json()
            assert after["control_attached"]
            assert after["compatibility"]["controller_transport"] == TYPE
            previous = {t["id"]: t for t in before["tenants"]}
            assert {t["id"] for t in after["tenants"]} == set(previous)
            for tenant in after["tenants"]:
                for field in (
                    "owner",
                    "bot",
                    "expires",
                    "budget",
                    "used",
                    "accounts",
                    "groups",
                    "enabled",
                ):
                    assert tenant[field] == previous[tenant["id"]][field], (
                        f"Business field changed: {field}"
                    )
            for key, row in prior_stats.items():
                if key not in SELECTED and key not in active:
                    assert current[key]["started_at"] == row["started_at"]
                    assert current[key]["status"] == row["status"]
            persisted = json.loads(
                (ROOT / "data/cmd_config.json").read_text(encoding="utf-8-sig")
            )
            assert len(persisted["platform"]) == len(original)
            for entry in persisted["platform"]:
                if entry["id"] not in SELECTED:
                    assert entry == original[entry["id"]], (
                        "Unrelated platform configuration changed"
                    )
            print(
                "Controller and test clone migrated; account states and business data preserved."
            )
            print(
                "No full-core restart, account invitation, trial renewal or AI message was performed."
            )
        except Exception:
            # Never restore a stale database or replay a partially completed migration.
            raise RuntimeError(
                f"Migration requires operator review. Private backup: {backup}; "
                "inspect actual polling/configuration before rollback, and stop new polling first."
            ) from None


if __name__ == "__main__":
    main()
