"""Repair hot-created AI profiles and credit confirmed pre-model dispatch failures."""

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from deploy import ROOT, client


def main():
    os.umask(0o077)
    backup = Path("/root/Projects/agents/telethon-ai-deployment") / (
        "pipeline-repair-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    )
    backup.mkdir(mode=0o700)
    profiles = []
    for path in (ROOT / "data/config").glob("abconf_*.json"):
        config = json.loads(path.read_text(encoding="utf-8-sig"))
        persona = config.get("agent_runner", {}).get("config", {}).get("persona", {})
        if persona.get("persona_id") in {
            "telethon-ai-collector",
            "telethon-ai-keywords",
        }:
            profiles.append((path, config))
    assert len(profiles) == 2, "Unexpected AI profile mapping"
    with client() as api:
        for path, config in profiles:
            (backup / path.name).write_text(
                json.dumps(config, ensure_ascii=False, indent=2)
            )
            response = api.post(
                "/api/config/astrbot/update",
                json={
                    "conf_id": path.stem.removeprefix("abconf_"),
                    "config": config,
                },
            )
            response.raise_for_status()
            assert response.json().get("status") == "ok", (
                "Pipeline initialization failed"
            )
            print("Native pipeline initialized:", path.stem)
    data = ROOT / "data/plugin_data/astrbot_plugin_telethon_ai"
    for name in ("tenants.db", "gate.db"):
        with sqlite3.connect(f"file:{data / name}?mode=ro", uri=True) as source:
            with sqlite3.connect(backup / name) as dest:
                source.backup(dest)
    with sqlite3.connect(data / "tenants.db") as db:
        db.execute("ATTACH DATABASE ? AS gate", (str(data / "gate.db"),))
        db.execute("BEGIN IMMEDIATE")
        credited = []
        for message in ("1139", "1140"):
            row = db.execute(
                "SELECT id,state FROM reservations WHERE tenant=? AND account=? AND chat=? AND message=?",
                ("73d632ccb950d05862e71cf3", "collector", "-1001000000003", message),
            ).fetchone()
            assert row and row[1] in {"reserved", "released"}, (
                "Unexpected settlement; review manually"
            )
            if row[1] == "released":
                continue
            db.execute("UPDATE reservations SET state='released' WHERE id=?", (row[0],))
            db.execute(
                "DELETE FROM gate.attempts WHERE account=? AND chat=? AND message=?",
                ("collector", "-1001000000003", message),
            )
            db.execute(
                "INSERT INTO audit(at,actor,action,details) VALUES (?,?,?,?)",
                (
                    time.time(),
                    "operator:acceptance-repair",
                    "pre_model_dispatch_credit",
                    json.dumps(
                        {
                            "reservation": row[0],
                            "message": message,
                            "reason": "journal_confirmed_pipeline_missing",
                        }
                    ),
                ),
            )
            credited.append(message)
        db.commit()
        print("Credited confirmed pre-model failures:", credited)
    print("No message replayed. Backup:", backup)


if __name__ == "__main__":
    main()
