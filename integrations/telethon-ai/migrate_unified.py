"""Reversible, one-Bot cutover to the single AI service plugin."""

import copy
import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from deploy import NAME, ROOT, client

OLD = "astrbot_plugin_tenant_control"
PROFILE = "de35c453-37b1-430a-9df8-10d2429b5228"


def main():
    os.umask(0o077)
    with sqlite3.connect(
        "file:/var/lib/astrbot-tenant-state/control.db?mode=ro", uri=True
    ) as db:
        if db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]:
            raise RuntimeError(
                "Existing orders require payment migration; cutover refused"
            )
    root = json.loads((ROOT / "data/cmd_config.json").read_text(encoding="utf-8-sig"))
    old_bot = next(p for p in root["platform"] if p["id"] == "VIP_DHBot")
    profile_path = ROOT / f"data/config/abconf_{PROFILE}.json"
    old_profile = json.loads(profile_path.read_text(encoding="utf-8-sig"))
    plugin_path = ROOT / f"data/config/{NAME}_config.json"
    old_plugin = json.loads(plugin_path.read_text(encoding="utf-8-sig"))
    if old_bot.get("telegram_required_plugin") != OLD:
        raise RuntimeError("Not in expected legacy state; refusing repeat migration")
    backup = Path("/root/Projects/agents/telethon-ai-deployment") / (
        "unified-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    )
    backup.mkdir(mode=0o700)
    shutil.copy2(ROOT / "data/cmd_config.json", backup / "cmd_config.json")
    shutil.copytree(ROOT / "data/config", backup / "config")
    for source in [
        ROOT / "data/data_v4.db",
        ROOT / "data/plugin_data" / NAME / "tenants.db",
        Path("/var/lib/astrbot-tenant-state/control.db"),
    ]:
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as db:
            with sqlite3.connect(backup / source.name) as dest:
                db.backup(dest)
    shutil.copytree(ROOT / "data/plugin_data" / NAME / "private", backup / "private")
    with client() as api:

        def post(path, body):
            result = api.post(path, json=body)
            result.raise_for_status()
            if result.json().get("status") != "ok":
                raise RuntimeError("Management API rejected operation: " + path)

        def bot_update(config):
            post("/api/config/platform/update", {"id": "VIP_DHBot", "config": config})

        def profile_update(config):
            post("/api/config/astrbot/update", {"conf_id": PROFILE, "config": config})

        def plugin_update(config):
            post("/api/config/plugin/update?plugin_name=" + NAME, config)

        stopped = False
        try:
            stopped_bot = copy.deepcopy(old_bot)
            stopped_bot["enable"] = False
            bot_update(stopped_bot)
            stopped = True
            post("/api/plugin/off", {"name": OLD})
            profile = copy.deepcopy(old_profile)
            profile["plugin_set"] = [NAME]
            profile["provider_settings"]["enable"] = False
            profile_update(profile)
            config = dict(
                old_plugin,
                controller_mode="standalone",
                support_text="@example_owner",
                admin_ids=["1000000001"],
            )
            plugin_update(config)
            post("/api/plugin/on", {"name": NAME})
            new_bot = copy.deepcopy(old_bot)
            new_bot["telegram_required_plugin"] = NAME
            new_bot["enable"] = True
            bot_update(new_bot)
            # Plugin hook binds on platform loaded, before required-plugin polling starts.
            status = api.get(f"/api/plug/{NAME}/status").json()
            if not status.get("control_attached"):
                raise RuntimeError("New controller did not attach")
            # Refresh the public menu once the newly rebuilt Bot client is initialized.
            post("/api/plugin/reload", {"name": NAME})
            print(
                json.dumps(
                    {
                        "migrated": True,
                        "backup": str(backup),
                        "accounts_enabled": False,
                    },
                    ensure_ascii=False,
                )
            )
        except BaseException:
            if stopped:
                try:
                    bot_update(stopped_bot)
                    post("/api/plugin/off", {"name": NAME})
                    profile_update(old_profile)
                    # Restore old plugin config as a file while it is disabled.
                    # The managed API persists the rollback config.
                    plugin_update(dict(old_plugin, controller_mode="companion"))
                    post("/api/plugin/on", {"name": OLD})
                    bot_update(old_bot)
                except Exception:
                    print("Rollback needs operator review; private backup:", backup)
            raise


if __name__ == "__main__":
    main()
