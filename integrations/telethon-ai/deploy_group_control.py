"""Deploy group management without restarting the core or changing entitlements."""

import argparse
import copy
import hashlib
import json
import os
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZipFile

from build_plugin import FILES
from deploy import NAME, ROOT, client
from migrate_portable_transport import TYPE, put, result, stats

VERSION = "v0.5.2"
PACKAGE = ROOT / "integrations/telethon-ai/dist" / f"{NAME}-{VERSION}.zip"
DATA = ROOT / "data/plugin_data" / NAME
BUSINESS_FIELDS = (
    "owner",
    "bot",
    "expires",
    "budget",
    "used",
    "accounts",
    "groups",
    "enabled",
)


def verified_package():
    with ZipFile(PACKAGE) as archive:
        assert set(archive.namelist()) == set(FILES)
        for name in FILES:
            assert (
                archive.read(name) == (ROOT / "data/plugins" / NAME / name).read_bytes()
            )
    return hashlib.sha256(PACKAGE.read_bytes()).hexdigest()


def backup_state(before):
    os.umask(0o077)
    target = Path("/root/Projects/agents/telethon-ai-deployment") / (
        "group-control-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    )
    target.mkdir(parents=True, mode=0o700)
    shutil.copy2(ROOT / "data/cmd_config.json", target / "cmd_config.json")
    shutil.copytree(ROOT / "data/config", target / "config")
    shutil.copytree(DATA / "private", target / "private")
    accounts = json.loads((DATA / "private/accounts.json").read_text())
    (target / "sessions").mkdir(mode=0o700)
    sources = [
        (DATA / "tenants.db", target / "tenants.db"),
        (DATA / "gate.db", target / "gate.db"),
        (ROOT / "data/data_v4.db", target / "data_v4.db"),
    ]
    if (DATA / "group_control.db").exists():
        sources.append((DATA / "group_control.db", target / "group_control.db"))
    for alias, spec in accounts.items():
        session = Path(spec["session"] + ".session")
        assert session.is_absolute() and session.is_file() and not session.is_symlink()
        sources.append((session, target / "sessions" / f"{alias}.session"))
    for source, destination in sources:
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as db:
            with sqlite3.connect(destination) as backup:
                db.backup(backup)
    shutil.copy2(
        PACKAGE.with_name(f"{NAME}-v0.5.1.zip"), target / "rollback-v0.5.1.zip"
    )
    shutil.copy2(PACKAGE, target / f"release-{VERSION}.zip")
    (target / "status-before.json").write_text(json.dumps(before))
    return target


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    digest = verified_package()
    local = json.loads((ROOT / "data/cmd_config.json").read_text(encoding="utf-8-sig"))
    original = {p["id"]: copy.deepcopy(p) for p in local["platform"]}
    selected = {key: p for key, p in original.items() if p["type"] == TYPE}
    assert selected and selected["VIP_DHBot"]["service_role"] == "controller"
    plugin_config = json.loads(
        (ROOT / "data/config" / f"{NAME}_config.json").read_text(encoding="utf-8-sig")
    )
    assert plugin_config["controller_mode"] == "standalone"
    assert plugin_config["control_platform_id"] == "VIP_DHBot"
    assert plugin_config["admin_ids"] == ["1000000001"]
    for config in selected.values():
        assert config["telegram_required_plugin"] == NAME
        assert config["telegram_dedicated_reporting"] is True
        assert config["service_role"] in {"controller", "customer"}
        assert (
            sum(
                p.get("telegram_token") == config["telegram_token"]
                for p in original.values()
            )
            == 1
        )
    with client() as api:
        for key, config in selected.items():
            live = result(api.get("/api/v1/bots/by-id", params={"bot_id": key}))
            assert live["data"]["bot"] == config
        response = api.get(f"/api/plug/{NAME}/status")
        response.raise_for_status()
        before = response.json()
        assert before["control_attached"]
        assert not before.get("group_management", {}).get("bindings", [])
        prior = stats(api)
        active_accounts = {
            a["platform_id"] for a in before["accounts"] if a["state"] == "running"
        }
        for key in active_accounts:
            assert original[key]["type"] == "telethon_ai" and original[key]["enable"]
        print(
            f"Preflight passed: {len(selected)} service entries; package SHA256 {digest}"
        )
        if not args.apply:
            return
        backup = backup_state(before)
        print(f"Private rollback backup: {backup}")
        try:
            for key in selected:
                put(api, {**original[key], "enable": False})
            assert all(
                stats(api).get(key, {}).get("status") != "running" for key in selected
            )
            result(api.post("/api/plugin/reload", json={"name": NAME}))
            for key, config in selected.items():
                updates = ["message", "callback_query", "my_chat_member"]
                if config["service_role"] == "controller":
                    updates += ["pre_checkout_query", "managed_bot"]
                put(api, {**config, "telegram_allowed_updates": updates})
            # Reload closes account connections. Restore only previously active,
            # still-enabled entries; never borrow a session from another service.
            for key in active_accounts:
                current = result(api.get("/api/v1/bots/by-id", params={"bot_id": key}))[
                    "data"
                ]["bot"]
                assert current == original[key], (
                    "Concurrent account configuration change"
                )
                put(api, current)
            expected = {
                key for key, config in selected.items() if config["enable"]
            } | active_accounts
            for _ in range(60):
                current_stats = stats(api)
                current_response = api.get(f"/api/plug/{NAME}/status")
                current_response.raise_for_status()
                after = current_response.json()
                if after["control_attached"] and all(
                    current_stats.get(key, {}).get("status") == "running"
                    for key in expected
                ):
                    break
                time.sleep(1)
            else:
                raise RuntimeError("Service readiness requires manual review")
            assert after["group_management"] == {"bindings": [], "uncertain": 0}
            previous = {t["id"]: t for t in before["tenants"]}
            assert {t["id"] for t in after["tenants"]} == set(previous)
            for tenant in after["tenants"]:
                assert all(
                    tenant[field] == previous[tenant["id"]][field]
                    for field in BUSINESS_FIELDS
                ), "Business data changed"
            for key, row in prior.items():
                if key not in selected and key not in active_accounts:
                    assert current_stats[key]["started_at"] == row["started_at"]
                    assert current_stats[key]["status"] == row["status"]
            persisted = json.loads(
                (ROOT / "data/cmd_config.json").read_text(encoding="utf-8-sig")
            )
            assert len(persisted["platform"]) == len(original)
            for config in persisted["platform"]:
                if config["id"] not in selected:
                    assert config == original[config["id"]]
                else:
                    expected_config = {**original[config["id"]]}
                    expected_config["telegram_allowed_updates"] = config[
                        "telegram_allowed_updates"
                    ]
                    assert config == expected_config
            (backup / "status-after.json").write_text(json.dumps(after))
            print(
                "PASS: group management loaded; no bindings or automation enabled; "
                "tenant expiry/quota/AI authorization and unrelated Bots unchanged."
            )
        except Exception:
            raise RuntimeError(
                f"Deployment needs manual review; private backup: {backup}. "
                "Stop new service polling before restoring the v0.5.1 package. "
                "Retain group_control.db and audit; never overwrite business databases."
            ) from None


if __name__ == "__main__":
    main()
