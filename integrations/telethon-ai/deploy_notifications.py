"""Back up private state and deploy owner notices, restoring only running accounts."""

import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from deploy import NAME, ROOT, client


def main():
    os.umask(0o077)
    backup = Path("/root/Projects/agents/telethon-ai-deployment") / (
        "notifications-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    )
    backup.mkdir(parents=True, mode=0o700)
    shutil.copy2(ROOT / "data/cmd_config.json", backup / "cmd_config.json")
    shutil.copytree(ROOT / "data/config", backup / "config")
    data = ROOT / "data/plugin_data" / NAME
    for name in ("tenants.db", "gate.db"):
        with sqlite3.connect(f"file:{data / name}?mode=ro", uri=True) as source:
            with sqlite3.connect(backup / name) as target:
                source.backup(target)
    print(f"Private backup: {backup}")
    with client() as api:
        response = api.get(f"/api/plug/{NAME}/status")
        response.raise_for_status()
        before = response.json()
        active = [
            a["platform_id"] for a in before["accounts"] if a["state"] == "running"
        ]
        response = api.post("/api/plugin/reload", json={"name": NAME})
        response.raise_for_status()
        assert response.json().get("status") == "ok", (
            "Plugin reload needs manual review"
        )
        for platform in active:
            response = api.get("/api/v1/bots/by-id", params={"bot_id": platform})
            response.raise_for_status()
            config = response.json()["data"]["bot"]
            assert config["enable"] is True and config["type"] == "telethon_ai"
            response = api.put(
                "/api/v1/bots/by-id", json={"bot_id": platform, "config": config}
            )
            response.raise_for_status()
            assert response.json().get("status") == "ok"
            print(f"Restored existing connection: {platform}")
        response = api.get(f"/api/plug/{NAME}/status")
        response.raise_for_status()
        after = response.json()
        prior = {t["id"]: t for t in before["tenants"]}
        for item in after["tenants"]:
            if item["id"] not in prior:
                continue
            for key in (
                "owner",
                "bot",
                "expires",
                "budget",
                "used",
                "accounts",
                "groups",
            ):
                assert item[key] == prior[item["id"]][key], (
                    f"Unexpected change to {key}"
                )
        print(
            {
                "control": after["control_attached"],
                "service_counts": after["tenant_counts"],
                "notices": [(n["kind"], n["state"]) for n in after["notifications"]],
            }
        )


if __name__ == "__main__":
    main()
