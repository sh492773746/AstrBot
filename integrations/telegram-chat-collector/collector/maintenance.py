"""Local consistent backups and bounded retention for collector operations."""

import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path


def backup_bundle(config_path: Path, astrbot: Path | None = None) -> Path:
    """Create a consistent backup archive without copying live SQLite WAL files.

    Args:
        config_path: Collector configuration path.
        astrbot: Optional AstrBot root for plugin configuration and delivery state.

    Returns:
        Completed archive path. The archive contains credentials and is private.
    """
    config_path = config_path.resolve()
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    data = Path(config.get("data_dir", "var")).expanduser()
    if not data.is_absolute():
        data = config_path.parent / data
    directory = data / "backups"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = directory / f"scheduled-{stamp}.zip"
    temporary = destination.with_suffix(".partial")
    try:
        with tempfile.TemporaryDirectory(dir=directory) as staging:
            snapshot = Path(staging) / "activity.sqlite3"
            with sqlite3.connect(
                (data / "activity.sqlite3").as_uri() + "?mode=ro", uri=True
            ) as source:
                with sqlite3.connect(snapshot) as target:
                    source.backup(target)
                    if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError("Backup integrity check failed")
            with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as bundle:
                bundle.write(snapshot, "activity.sqlite3")
                bundle.write(config_path, "collector-config.json")
                if astrbot:
                    plugin_config = (
                        astrbot / "data/config/astrbot_plugin_tgwatch_config.json"
                    )
                    if plugin_config.exists():
                        bundle.write(plugin_config, "plugin-config.json")
                    selected = (
                        json.loads(plugin_config.read_text(encoding="utf-8-sig"))
                        if plugin_config.exists()
                        else {}
                    )
                    core_path = astrbot / "data/cmd_config.json"
                    if core_path.exists():
                        core = json.loads(core_path.read_text(encoding="utf-8-sig"))
                        platform = next(
                            (
                                p
                                for p in core.get("platform", [])
                                if p.get("id") == selected.get("platform_id")
                            ),
                            None,
                        )
                        if platform:
                            bundle.writestr(
                                "telegram-platform.json",
                                json.dumps(platform, ensure_ascii=False),
                            )
                    database = astrbot / "data/data_v4.db"
                    if database.exists():
                        with sqlite3.connect(
                            database.resolve().as_uri() + "?mode=ro", uri=True
                        ) as source:
                            rows = source.execute(
                                "SELECT scope,scope_id,key,value FROM preferences WHERE scope='plugin' AND scope_id='local/astrbot_plugin_tgwatch'"
                            ).fetchall()
                            routing_row = source.execute(
                                "SELECT value FROM preferences WHERE key='umop_config_routing'"
                            ).fetchone()
                            routing = (
                                json.loads(routing_row[0]).get("val", {})
                                if routing_row
                                else {}
                            )
                            routing = {
                                k: v
                                for k, v in routing.items()
                                if k.split(":", 1)[0] == selected.get("platform_id")
                            }
                            bundle.writestr(
                                "config-routes.json",
                                json.dumps(routing, ensure_ascii=False),
                            )
                            for profile_id in set(routing.values()):
                                if profile_id == "default":
                                    continue
                                profile = (
                                    astrbot
                                    / "data/config"
                                    / f"abconf_{profile_id}.json"
                                )
                                if profile.exists():
                                    bundle.write(profile, "profiles/" + profile.name)
                        bundle.writestr(
                            "plugin-preferences.json",
                            json.dumps(rows, ensure_ascii=False),
                        )
            os.chmod(temporary, 0o600)
            temporary.replace(destination)
        for log_name in ("service.log", "service-error.log"):
            log = data / log_name
            if log.exists() and log.stat().st_size > 2 * 1024 * 1024:
                for index in (2, 1):
                    previous = data / f"{log_name}.{index}"
                    if previous.exists():
                        previous.replace(data / f"{log_name}.{index + 1}")
                shutil.copyfile(log, data / f"{log_name}.1")
                with log.open("r+") as stream:
                    stream.truncate(0)
        for old in sorted(directory.glob("scheduled-*.zip"))[:-14]:
            old.unlink()
    finally:
        temporary.unlink(missing_ok=True)
    return destination
