"""Connect one approved transport; leave tenant activation to Telegram admin."""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from deploy import ROOT, client


def main():
    os.umask(0o077)
    tenant = "73d632ccb950d05862e71cf3"
    db = ROOT / "data/plugin_data/astrbot_plugin_telethon_ai/tenants.db"
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as connection:
        assert connection.execute(
            "SELECT 1 FROM tenants WHERE id=? AND enabled=0 AND expires>?",
            (tenant, datetime.now(timezone.utc).timestamp()),
        ).fetchone(), "Expected unexpired paused trial"
        assert connection.execute(
            "SELECT 1 FROM leases WHERE tenant=? AND account='collector'", (tenant,)
        ).fetchone()
        assert connection.execute(
            "SELECT 1 FROM groups WHERE tenant=? AND chat=? AND authorized=1",
            (tenant, "-1001000000003"),
        ).fetchone(), "Group not approved"
    config = json.loads((ROOT / "data/cmd_config.json").read_text(encoding="utf-8-sig"))
    platform = next(p for p in config["platform"] if p["id"] == "TelethonAI_collector")
    backup = Path("/root/Projects/agents/telethon-ai-deployment") / (
        "collector-before-live-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        + ".json"
    )
    with backup.open("x") as file:
        json.dump(platform, file, indent=2)
    platform.update(
        allowed_chats=["-1001000000003"],
        allowed_senders=["1000000001"],
        disclosure_confirmed=True,
        daily_limit=3,
        cooldown_seconds=30,
        enable=True,
    )
    with client() as api:
        response = api.post(
            "/api/config/platform/update",
            json={"id": platform["id"], "config": platform},
        )
        response.raise_for_status()
        assert response.json().get("status") == "ok", "Platform update rejected"
        print("Approved collector transport configured; tenant remains paused.")
        print(
            json.dumps(
                api.get("/api/plug/astrbot_plugin_telethon_ai/status").json(),
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
