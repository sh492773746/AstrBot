"""Install the reviewed headless controller bundle with a local rollback snapshot."""

import json
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from zipfile import ZipFile

ROOT = Path("/opt/astrbot-vip-dhbot")
SERVICE = "astrbot-vip-dhbot.service"


def main():
    os.umask(0o077)
    archive = (
        Path(__file__).resolve().parents[1]
        / "dist/astrbot_plugin_tenant_control-v0.3.0.zip"
    )
    plugin = ROOT / "data/plugins/astrbot_plugin_tenant_control"
    config = ROOT / "data/cmd_config.json"
    backup = ROOT / "private" / f"headless-{time.time_ns()}"
    backup.mkdir(mode=0o700)
    subprocess.run(["systemctl", "stop", SERVICE], check=True)
    try:
        shutil.copy2(config, backup / "cmd_config.json")
        shutil.copytree(plugin, backup / "plugin")
        db_path = ROOT / "control/control.db"
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as src:
            with sqlite3.connect(backup / "control.db") as dst:
                src.backup(dst)
        with ZipFile(archive) as bundle:
            for member in bundle.infolist():
                path = Path(member.filename)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError("Unsafe bundle path")
            bundle.extractall(plugin)
        data = json.loads(config.read_text(encoding="utf-8-sig"))
        data["dashboard"]["enable"] = False
        config.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        for path in plugin.rglob("*"):
            shutil.chown(path, user="astrvip", group="astrvip")
    except BaseException:
        if (backup / "cmd_config.json").exists():
            shutil.copy2(backup / "cmd_config.json", config)
        if (backup / "plugin").exists():
            shutil.copytree(backup / "plugin", plugin, dirs_exist_ok=True)
        raise
    finally:
        subprocess.run(["systemctl", "start", SERVICE], check=True)
    print("Headless controller installed; inspect Telegram polling and port closure.")


if __name__ == "__main__":
    main()
