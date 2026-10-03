"""Migrate one existing test customer Bot through AstrBot's local admin API.

Run from the server only. Never print or export the Telegram token.
"""

import argparse
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from deploy import NAME, ROOT, client

TEST_PLATFORM = "AIClient_8910402926"
ISOLATION = {
    "telegram_allowed_updates": ["message"],
    "telegram_required_plugin": NAME,
    "telegram_command_register": False,
    "telegram_command_auto_refresh": False,
    "telegram_dedicated_reporting": True,
}


def check(current):
    assert current["id"] == TEST_PLATFORM and current["type"] == "telegram"
    assert current["enable"] is True
    assert isinstance(current.get("telegram_token"), str) and current["telegram_token"]
    for key, expected in ISOLATION.items():
        legacy_updates = key == "telegram_allowed_updates" and current.get(key) == [
            "message",
            "callback_query",
        ]
        assert key not in current or current[key] == expected or legacy_updates, (
            f"Platform has unexpected {key}; manual review required"
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    local = json.loads((ROOT / "data/cmd_config.json").read_text(encoding="utf-8-sig"))
    matches = [p for p in local["platform"] if p.get("id") == TEST_PLATFORM]
    assert len(matches) == 1, "Expected exactly one test customer Bot"
    check(matches[0])
    with client() as api:
        response = api.get("/api/v1/bots/by-id", params={"bot_id": TEST_PLATFORM})
        response.raise_for_status()
        body = response.json()
        assert body["status"] == "ok", "Cannot read live Bot config"
        live = body["data"]["bot"]
        assert live == matches[0], "Live Bot config differs from on-disk config"
        updated = {**live, **ISOLATION}
        if updated == live:
            print("Test customer Bot already migrated.")
            return
        if not args.apply:
            print("Preflight passed for one test customer Bot; use --apply to migrate.")
            return
        os.umask(0o077)
        backup = Path("/root/Projects/agents/telethon-ai-deployment") / (
            "boundary-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        )
        backup.mkdir(parents=True, mode=0o700)
        shutil.copy2(ROOT / "data/cmd_config.json", backup / "cmd_config.json")
        print(f"Private configuration backup: {backup}")
        response = api.put(
            "/api/v1/bots/by-id", json={"bot_id": TEST_PLATFORM, "config": updated}
        )
        response.raise_for_status()
        assert response.json()["status"] == "ok", "Bot update failed; review live state"
        response = api.get("/api/v1/bots/by-id", params={"bot_id": TEST_PLATFORM})
        response.raise_for_status()
        assert response.json()["data"]["bot"] == updated, (
            "Bot update needs manual review"
        )
        print("Test customer Bot reloaded with plugin-only reception.")


if __name__ == "__main__":
    main()
