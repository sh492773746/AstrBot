"""Back up configuration/business state, restart the patched core, verify roles."""

import json
import os
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from deploy import NAME, ROOT, client


def snapshot():
    files = [ROOT / "data/cmd_config.json", *(ROOT / "data/config").glob("*.json")]
    return {
        str(p.relative_to(ROOT)): json.loads(p.read_text(encoding="utf-8-sig"))
        for p in files
    }


def business(status):
    fields = (
        "id",
        "owner",
        "bot",
        "platform",
        "expires",
        "budget",
        "used",
        "accounts",
        "groups",
        "enabled",
    )
    return [{key: row.get(key) for key in fields} for row in status["tenants"]]


def main():
    os.umask(0o077)
    backup = Path("/root/Projects/agents/telethon-ai-deployment") / (
        "profile-roles-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    )
    backup.mkdir(parents=True, mode=0o700)
    shutil.copy2(ROOT / "data/cmd_config.json", backup / "cmd_config.json")
    shutil.copytree(ROOT / "data/config", backup / "config")
    for name in ("tenants.db", "gate.db"):
        source = ROOT / "data/plugin_data" / NAME / name
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src:
            with sqlite3.connect(backup / name) as dest:
                src.backup(dest)
    original = subprocess.check_output(
        [
            "git",
            "-c",
            f"safe.directory={ROOT}",
            "show",
            "HEAD:astrbot/dashboard/services/config_service.py",
        ],
        cwd=ROOT,
    )
    (backup / "config_service.before.py").write_bytes(original)
    shutil.copy2(
        ROOT / "integrations/telethon-ai/dist/astrbot_plugin_telethon_ai-v0.3.10.zip",
        backup / "plugin.before.zip",
    )
    before_config = snapshot()
    with client() as api:
        before = api.get(f"/api/plug/{NAME}/status").json()
    subprocess.run(["systemctl", "restart", "astrbot.service"], check=True, timeout=90)
    deadline = time.monotonic() + 120
    with client() as api:
        while True:
            try:
                response = api.get(f"/api/plug/{NAME}/status", timeout=8)
                response.raise_for_status()
                after = response.json()
                if (
                    after["control_attached"]
                    and after["customer_entries"] == before["customer_entries"]
                    and [(a["account"], a["state"]) for a in after["accounts"]]
                    == [(a["account"], a["state"]) for a in before["accounts"]]
                ):
                    break
            except (httpx.HTTPError, KeyError, ValueError):
                pass
            if time.monotonic() > deadline:
                raise RuntimeError(
                    "Readiness timeout; inspect service before any rollback"
                )
            time.sleep(2)
        profiles = api.get("/api/v1/config-profiles").json()["data"]["info_list"]
        roles = []
        for item in profiles:
            result = api.get(f"/api/v1/config-profiles/{item['id']}").json()["data"]
            descriptor = result.get("profile_role")
            if descriptor:
                roles.append((item["name"], descriptor["role"]))
        assert {"controller", "customer", "telethon"} <= {role for _, role in roles}
        assert snapshot() == before_config, (
            "Configuration changed during startup; review private backup"
        )
        assert business(after) == business(before), (
            "Business state changed; review before further actions"
        )
        assert [
            (a["account"], a["state"], a["enabled"], a["paused"])
            for a in after["accounts"]
        ] == [
            (a["account"], a["state"], a["enabled"], a["paused"])
            for a in before["accounts"]
        ], "Account connection state changed"
        print(
            "PASS: role schemas active; all configuration and tenant entitlement state unchanged"
        )
        print("Roles:", roles)
        print("Private backup:", backup)


if __name__ == "__main__":
    main()
