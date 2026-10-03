"""One-time private import of the two operator-authorized legacy session references."""

import json
import os
from pathlib import Path


def main():
    os.umask(0o077)
    source = Path("/opt/telegram-chat-collector-prod/config.json")
    config = json.loads(source.read_text(encoding="utf-8-sig"))
    data = Path(config.get("data_dir", "var"))
    if not data.is_absolute():
        data = source.parent / data
    accounts = {
        "collector": {
            "user_id": 1000000002,
            "session": str(data / "account"),
            "api_id": config["api_id"],
            "api_hash": config["api_hash"],
            "proxy": config.get("proxy"),
            "collector_lock_dir": str(data),
        }
    }
    extra = next(x for x in config["unified"]["accounts"] if x["id"] == "keywords")
    accounts["keywords"] = {
        "user_id": 1000000003,
        "session": extra["session"],
        "api_id": extra["api_id"],
        "api_hash": extra["api_hash"],
        "proxy": config.get("proxy"),
        "collector_lock_dir": str(data),
    }
    root = Path("/opt/astrbot-prod/data/plugin_data/astrbot_plugin_telethon_ai/private")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    target = root / "accounts.json"
    with target.open("x") as file:
        target.chmod(0o600)
        json.dump(accounts, file, indent=2)
    print("Imported two private account references; no sessions copied or connected.")


if __name__ == "__main__":
    main()
