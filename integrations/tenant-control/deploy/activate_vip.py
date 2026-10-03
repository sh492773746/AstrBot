"""Controlled single-platform cutover; never starts a worker or configures prices."""

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import jwt
from filelock import FileLock, Timeout
from telegram import Bot

ROOT = Path("/opt/astrbot-vip-dhbot")
SOURCE = Path("/opt/astrbot-prod/data/cmd_config.json")
PLUGIN_CONFIG = ROOT / "data/config/astrbot_plugin_tenant_control_config.json"
SERVICE = "astrbot-vip-dhbot.service"


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def platform_enabled(client, enabled):
    response = client.patch(
        "/api/v1/bots/enabled",
        json={"bot_id": "VIP_DHBot", "enabled": enabled},
    )
    if response.status_code != 200 or response.json().get("status") != "ok":
        raise RuntimeError("Production platform update was not acknowledged")


async def activate():
    os.umask(0o077)
    production = read_json(SOURCE)
    source_bot = next(p for p in production["platform"] if p["id"] == "VIP_DHBot")
    config_path = ROOT / "data/cmd_config.json"
    config = read_json(config_path)
    target = next(p for p in config["platform"] if p["id"] == "VIP_DHBot")
    plugin = read_json(PLUGIN_CONFIG)
    if target["enable"] or plugin["enabled"]:
        raise RuntimeError("Target is already enabled; inspect instead of rerunning")
    if plugin["admin_ids"] != ["1000000001"]:
        raise RuntimeError("Verified administrator configuration changed")
    if any(
        p.get("enable")
        and p["id"] != "VIP_DHBot"
        and p.get("telegram_token") == source_bot["telegram_token"]
        for p in production["platform"]
    ):
        raise RuntimeError("Another production receiver shares this token")
    async with Bot(source_bot["telegram_token"]) as bot:
        me = await bot.get_me()
        admin = await bot.get_chat(1000000001)
        webhook = await bot.get_webhook_info()
        if (
            me.id != 8941933661
            or me.username.lower() != "vip_dhbot"
            or not me.can_manage_bots
            or admin.username.lower() != "dh114514"
            or webhook.url
        ):
            raise RuntimeError("Telegram identity, BMM, admin or webhook check failed")
    print("Telegram identity, administrator and BMM verified", flush=True)
    backup = ROOT / "private" / f"cutover-{time.time_ns()}"
    backup.mkdir(mode=0o700)
    environment = ROOT / "private/controller.env"
    for path in (config_path, PLUGIN_CONFIG, environment):
        shutil.copy2(path, backup / path.name)
    write_json(backup / "source-platform.json", source_bot)
    dashboard = production["dashboard"]
    token = jwt.encode(
        {"username": dashboard["username"], "exp": int(time.time()) + 300},
        dashboard["jwt_secret"],
        algorithm="HS256",
    )
    with httpx.Client(
        base_url="http://127.0.0.1:6185",
        headers={"Authorization": f"Bearer {token}"},
        timeout=90,
    ) as client:
        try:
            await asyncio.to_thread(platform_enabled, client, False)
            current = read_json(SOURCE)
            assert not next(p for p in current["platform"] if p["id"] == "VIP_DHBot")[
                "enable"
            ]
            print("Old VIP_DHBot receiver disabled through AstrBot API", flush=True)
            await asyncio.to_thread(
                subprocess.run, ["systemctl", "stop", SERVICE], check=True
            )
            target["telegram_token"] = source_bot["telegram_token"]
            target["enable"] = True
            plugin["enabled"] = True
            write_json(config_path, config)
            write_json(PLUGIN_CONFIG, plugin)
            entries = environment.read_text().splitlines()
            if "CONTROL_ROTATED_TOKEN_ACK=NO" not in entries:
                raise RuntimeError("Unexpected rotation acknowledgement state")
            environment.write_text(
                "\n".join(
                    "CONTROL_ROTATED_TOKEN_ACK=YES"
                    if line == "CONTROL_ROTATED_TOKEN_ACK=NO"
                    else line
                    for line in entries
                )
                + "\n"
            )
            await asyncio.to_thread(
                subprocess.run, ["systemctl", "start", SERVICE], check=True
            )
            for _ in range(40):
                lock_path = ROOT / "control/control.db.controller.lock"
                try:
                    if lock_path.exists():
                        with FileLock(lock_path, timeout=0):
                            pass
                except Timeout:
                    break
                await asyncio.sleep(1)
            else:
                raise RuntimeError("Independent controller failed readiness check")
            print(
                "Independent controller started; inspect polling and plugin health",
                flush=True,
            )
        except BaseException:
            await asyncio.to_thread(
                subprocess.run, ["systemctl", "stop", SERVICE], check=True
            )
            for path in (config_path, PLUGIN_CONFIG, environment):
                shutil.copy2(backup / path.name, path)
            if source_bot["enable"]:
                await asyncio.to_thread(platform_enabled, client, True)
            await asyncio.to_thread(
                subprocess.run, ["systemctl", "start", SERVICE], check=True
            )
            raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rotation-confirmed", action="store_true", required=True)
    parser.parse_args()
    try:
        asyncio.run(activate())
    except Exception as error:
        print(f"Cutover failed: {type(error).__name__}; inspect service state.")
        raise SystemExit(1) from None
