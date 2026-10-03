"""Migrate VIP to the production AstrBot with one receiver and reversible state."""

import argparse
import asyncio
import copy
import json
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import httpx
import jwt
from cryptography.fernet import Fernet
from filelock import FileLock
from telegram import Bot

PROD = Path("/opt/astrbot-prod")
OLD = Path("/opt/astrbot-vip-dhbot")
PLUGIN = "astrbot_plugin_tenant_control"
PROFILE = "de35c453-37b1-430a-9df8-10d2429b5228"
BASE = PROD / "data/plugin_data" / PLUGIN
OLD_SERVICE = "astrbot-vip-dhbot.service"
REQUIRED = ["message", "callback_query", "pre_checkout_query", "managed_bot"]
MARKER = OLD / "MIGRATED_TO_PRODUCTION"


def load(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(".migration-tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temp.chmod(0o600)
    temp.replace(path)


def service(action, name):
    subprocess.run(["systemctl", action, name], check=True, timeout=120)


def api_client():
    dashboard = load(PROD / "data/cmd_config.json")["dashboard"]
    token = jwt.encode(
        {"username": dashboard["username"], "exp": int(time.time()) + 1800},
        dashboard["jwt_secret"],
        algorithm="HS256",
    )
    return httpx.Client(
        base_url="http://127.0.0.1:6185",
        headers={"Authorization": "Bearer " + token},
        timeout=120,
    )


def api(client, method, path, **kwargs):
    response = client.request(method, path, **kwargs)
    if response.status_code != 200:
        raise RuntimeError(f"Management API {path}: HTTP {response.status_code}")
    result = response.json()
    if result.get("status") != "ok":
        raise RuntimeError(f"Management API {path} refused the operation")
    return result.get("data")


def backup_db(source, target):
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src:
        with sqlite3.connect(target) as dst:
            src.backup(dst)
    target.chmod(0o600)


def env_file():
    result = {}
    for line in (OLD / "private/controller.env").read_text().splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            result[key] = value
    return result


async def verify_telegram(platform):
    async with Bot(platform["telegram_token"]) as bot:
        me = await bot.get_me()
        webhook = await bot.get_webhook_info()
        admin = await bot.get_chat(1000000001)
        if (
            me.id != 8941933661
            or me.username.lower() != "vip_dhbot"
            or not me.can_manage_bots
            or webhook.url
            or admin.username.lower() != "dh114514"
        ):
            raise RuntimeError("Telegram identity/BMM/admin/webhook preflight failed")


def wait_ready():
    for _ in range(60):
        try:
            with api_client() as client:
                data = api(client, "GET", "/api/plugin/get")
                plugins = data if isinstance(data, list) else data["plugins"]
                if any(
                    p.get("name") == PLUGIN and p.get("version") == "v0.4.0"
                    for p in plugins
                ):
                    status = BASE / "runtime-status.json"
                    if status.exists():
                        info = load(status)
                        if info.get("ready") and time.time() - info["at"] < 45:
                            return
        except (httpx.HTTPError, ValueError, RuntimeError, KeyError):
            pass
        time.sleep(2)
    raise RuntimeError("Production controller failed readiness verification")


def migrate():
    os.umask(0o077)
    if MARKER.exists() or (BASE / "private/runtime.json").exists():
        raise RuntimeError(
            "Migration already exists; inspect or roll back, do not rerun"
        )
    source_config = load(OLD / "data/cmd_config.json")
    source_platform = next(
        p for p in source_config["platform"] if p["id"] == "VIP_DHBot"
    )
    runtime = env_file()
    if runtime.get("CONTROL_ROTATED_TOKEN_ACK") != "YES":
        raise RuntimeError("Rotation acknowledgement missing")
    Fernet(runtime["CONTROL_FERNET_KEY"].encode())
    asyncio.run(verify_telegram(source_platform))
    production = load(PROD / "data/cmd_config.json")
    target = next(p for p in production["platform"] if p["id"] == "VIP_DHBot")
    if target["enable"]:
        raise RuntimeError("Production receiver must be disabled before cutover")
    archive = (
        Path(__file__).resolve().parents[1]
        / "dist/astrbot_plugin_tenant_control-v0.4.0.zip"
    )
    if not archive.exists():
        raise RuntimeError("Build reviewed v0.4.0 bundle first")
    baseline = BASE / "private" / f"migration-{time.time_ns()}"
    baseline.mkdir(parents=True, mode=0o700)
    # Store a coherent rollback set without touching unrelated profiles or secrets.
    save(baseline / "production.json", production)
    save(baseline / "old-config.json", source_config)
    save(baseline / "old-plugin.json", load(OLD / f"data/config/{PLUGIN}_config.json"))
    save(baseline / "plugin.json", load(PROD / f"data/config/{PLUGIN}_config.json"))
    save(baseline / "profile.json", load(PROD / f"data/config/abconf_{PROFILE}.json"))
    shutil.copy2(OLD / "private/controller.env", baseline / "controller.env")
    with api_client() as client:
        routes = api(client, "GET", "/api/config/umo_abconf_routes")["routing"]
        save(baseline / "routes.json", routes)
    save(
        BASE / "private/migration.json", {"backup": str(baseline), "state": "preparing"}
    )
    print(f"Migration snapshot: {baseline}", flush=True)
    try:
        service("stop", OLD_SERVICE)
        source_db = Path(runtime["CONTROL_DB_PATH"])
        with FileLock(str(source_db) + ".controller.lock", timeout=0):
            backup_db(source_db, baseline / "control.db")
            target_db = BASE / "control/control.db"
            target_db.parent.mkdir(parents=True, mode=0o700)
            backup_db(source_db, target_db)
        runtime["CONTROL_DB_PATH"] = str(target_db)
        save(BASE / "private/runtime.json", runtime)
        with api_client() as client:
            profile = load(baseline / "profile.json")
            profile["admins_id"] = ["1000000001"]
            profile["plugin_set"] = [PLUGIN]
            profile["wake_prefix"] = ["/"]
            profile["disable_builtin_commands"] = True
            profile.setdefault("provider_settings", {})["enable"] = False
            for key in ("provider_stt_settings", "provider_tts_settings"):
                profile.setdefault(key, {})["enable"] = False
            api(
                client,
                "POST",
                "/api/config/astrbot/update",
                json={"conf_id": PROFILE, "config": profile},
            )
            routing = {"VIP_DHBot::": PROFILE}
            routing.update({k: v for k, v in routes.items() if k != "VIP_DHBot::"})
            api(
                client,
                "POST",
                "/api/config/umo_abconf_route/update_all",
                json={"routing": routing},
            )
            default = copy.deepcopy(production)
            if PLUGIN not in default["plugin_set"]:
                default["plugin_set"].append(PLUGIN)
            api(
                client,
                "POST",
                "/api/config/astrbot/update",
                json={"conf_id": "default", "config": default},
            )
            with archive.open("rb") as stream:
                api(
                    client,
                    "POST",
                    "/api/plugin/install-upload",
                    files={"file": (archive.name, stream, "application/zip")},
                )
            settings = {
                "enabled": True,
                "platform_id": "VIP_DHBot",
                "admin_ids": ["1000000001"],
                "support_text": "客服：@example_owner。当前暂未开放购买。",
                "profile_id": PROFILE,
                "tenant_template_version": "community-v1",
                "allow_test_purchase": False,
                "default_features": ["community"],
                "default_model_quota": 1000,
            }
            api(
                client,
                "POST",
                "/api/config/plugin/update",
                params={"plugin_name": PLUGIN},
                json=settings,
            )
            new_platform = copy.deepcopy(target)
            new_platform.update(
                telegram_token=source_platform["telegram_token"],
                enable=False,
                telegram_allowed_updates=REQUIRED,
                telegram_required_plugin=PLUGIN,
                telegram_command_register=False,
                telegram_command_auto_refresh=False,
            )
            api(
                client,
                "POST",
                "/api/config/platform/update",
                json={"id": "VIP_DHBot", "config": new_platform},
            )
        # Reload core code once; all unrelated platform settings remain unchanged.
        service("restart", "astrbot.service")
        for _ in range(45):
            try:
                with api_client() as client:
                    api(
                        client,
                        "PATCH",
                        "/api/v1/bots/enabled",
                        json={"bot_id": "VIP_DHBot", "enabled": True},
                    )
                break
            except (httpx.HTTPError, RuntimeError):
                time.sleep(2)
        else:
            raise RuntimeError("Production API did not recover")
        wait_ready()
        current = load(PROD / "data/cmd_config.json")
        assert [p for p in current["platform"] if p["id"] != "VIP_DHBot"] == [
            p for p in production["platform"] if p["id"] != "VIP_DHBot"
        ], "Unrelated platform configuration changed"
        with sqlite3.connect(target_db) as db:
            db.execute(
                "INSERT INTO audit(at,actor,action,ref) VALUES(?,?,?,?)",
                (int(time.time()), "operator", "controller_migrated", baseline.name),
            )
        old_config = copy.deepcopy(source_config)
        old_platform = next(p for p in old_config["platform"] if p["id"] == "VIP_DHBot")
        old_platform["enable"] = False
        old_platform["telegram_token"] = ""
        save(OLD / "data/cmd_config.json", old_config)
        old_plugin = load(baseline / "old-plugin.json")
        old_plugin["enabled"] = False
        save(OLD / f"data/config/{PLUGIN}_config.json", old_plugin)
        MARKER.write_text(
            "Controller migrated to /opt/astrbot-prod; use documented rollback.\n"
        )
        service("disable", OLD_SERVICE)
        save(
            BASE / "private/migration.json",
            {"backup": str(baseline), "state": "complete"},
        )
        print("Production owns VIP_DHBot; independent receiver stopped and disabled.")
    except BaseException:
        print("Cutover failed; rolling back the VIP configuration only.", flush=True)
        rollback(baseline)
        raise


def rollback(baseline):
    gateway = subprocess.run(
        ["systemctl", "is-active", "--quiet", "astrbot-tenant-gateway.service"],
        check=False,
        capture_output=True,
    )
    if gateway.returncode == 0:
        raise RuntimeError("Stop the model gateway before controller rollback")
    service("stop", OLD_SERVICE)
    # Prevent both receivers before restoring the latest business state.
    with api_client() as client:
        api(
            client,
            "PATCH",
            "/api/v1/bots/enabled",
            json={"bot_id": "VIP_DHBot", "enabled": False},
        )
        disabled = load(baseline / "plugin.json")
        disabled["enabled"] = False
        api(
            client,
            "POST",
            "/api/config/plugin/update",
            params={"plugin_name": PLUGIN},
            json=disabled,
        )
        api(
            client,
            "POST",
            "/api/config/astrbot/update",
            json={"conf_id": PROFILE, "config": load(baseline / "profile.json")},
        )
        api(
            client,
            "POST",
            "/api/config/umo_abconf_route/update_all",
            json={"routing": load(baseline / "routes.json")},
        )
        # Restore only the changed default plugin list; keep later unrelated settings.
        current = load(PROD / "data/cmd_config.json")
        current["plugin_set"] = load(baseline / "production.json")["plugin_set"]
        api(
            client,
            "POST",
            "/api/config/astrbot/update",
            json={"conf_id": "default", "config": current},
        )
    runtime_path = BASE / "private/runtime.json"
    target_db = (
        Path(load(runtime_path)["CONTROL_DB_PATH"])
        if runtime_path.exists()
        else BASE / "control/control.db"
    )
    if target_db.exists():
        with FileLock(str(target_db) + ".controller.lock", timeout=5):
            backup_db(target_db, OLD / "control/control.db")
        for path in (OLD / "control").iterdir():
            if path.is_file():
                shutil.chown(path, user="astrvip", group="astrvip")
    save(OLD / "data/cmd_config.json", load(baseline / "old-config.json"))
    save(OLD / f"data/config/{PLUGIN}_config.json", load(baseline / "old-plugin.json"))
    shutil.chown(OLD / "data/cmd_config.json", user="astrvip", group="astrvip")
    shutil.chown(
        OLD / f"data/config/{PLUGIN}_config.json", user="astrvip", group="astrvip"
    )
    MARKER.unlink(missing_ok=True)
    service("enable", OLD_SERVICE)
    service("start", OLD_SERVICE)
    save(
        BASE / "private/migration.json",
        {"backup": str(baseline), "state": "rolled_back"},
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rollback", type=Path)
    args = parser.parse_args()
    try:
        if args.rollback:
            rollback(args.rollback)
        else:
            migrate()
    except Exception as error:
        print(
            f"Migration stopped: {type(error).__name__}; inspect protected snapshot and service logs."
        )
        raise SystemExit(1) from None
