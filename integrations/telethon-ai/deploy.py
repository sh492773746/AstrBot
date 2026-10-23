"""Install only this plugin through the loopback AstrBot administration API."""

import json
import os
import shutil
import sqlite3
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import jwt

ROOT = Path("/opt/astrbot-prod")
NAME = "astrbot_plugin_telethon_ai"


def client():
    config = json.loads((ROOT / "data/cmd_config.json").read_text(encoding="utf-8-sig"))
    dashboard = config["dashboard"]
    token = jwt.encode(
        {
            "username": dashboard["username"],
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        },
        dashboard["jwt_secret"],
        algorithm="HS256",
    )
    return httpx.Client(
        base_url=f"http://127.0.0.1:{dashboard.get('port', 6185)}",
        headers={"Authorization": "Bearer " + token},
        timeout=180,
        follow_redirects=False,
    )


def main():
    os.umask(0o077)
    backup = Path("/root/Projects/agents/telethon-ai-deployment") / datetime.now(
        timezone.utc
    ).strftime("%Y%m%dT%H%M%S")
    backup.mkdir(parents=True, mode=0o700)
    shutil.copy2(ROOT / "data/cmd_config.json", backup / "cmd_config.json")
    shutil.copytree(ROOT / "data/config", backup / "config")
    with sqlite3.connect(f"file:{ROOT}/data/data_v4.db?mode=ro", uri=True) as source:
        with sqlite3.connect(backup / "data_v4.db") as dest:
            source.backup(dest)
    plugin = ROOT / "data/plugins" / NAME
    archive = backup / (NAME + ".zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in plugin.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                bundle.write(path, path.relative_to(plugin))
    with client() as api, archive.open("rb") as file:
        response = api.post(
            "/api/plugin/install-upload",
            files={"file": (archive.name, file, "application/zip")},
        )
        response.raise_for_status()
        result = response.json()
        print(
            json.dumps(
                {"status": result.get("status"), "message": result.get("message")},
                ensure_ascii=False,
            )
        )
        if result.get("status") != "ok":
            raise SystemExit("Plugin install did not complete")
        status = api.get(f"/api/plug/{NAME}/status")
        status.raise_for_status()
        report = status.json()
        print(json.dumps(report, ensure_ascii=False))
        (backup / "status.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2)
        )
    print("Backup:", backup)


if __name__ == "__main__":
    main()
