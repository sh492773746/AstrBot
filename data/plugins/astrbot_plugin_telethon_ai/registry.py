"""Private, portable account registry. Sessions are referenced, never bundled."""

import json
import re
from pathlib import Path

from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

NAME = "astrbot_plugin_telethon_ai"


def private_root():
    root = Path(get_astrbot_plugin_data_path()) / NAME / "private"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    return root


def load():
    path = private_root() / "accounts.json"
    if not path.exists():
        return {}
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("Account registry must be a private regular file")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Invalid account registry")
    seen_ids, seen_sessions = set(), set()
    for name, spec in data.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", name):
            raise ValueError("Invalid account alias")
        uid = str(spec.get("user_id", ""))
        if not uid.isdigit() or int(uid) <= 0 or uid in seen_ids:
            raise ValueError("Invalid or duplicate account ID")
        session = Path(spec["session"])
        if not session.is_absolute() or str(session.resolve()) in seen_sessions:
            raise ValueError("Session path must be absolute and unique")
        if (
            type(spec.get("api_id")) is not int
            or spec["api_id"] <= 0
            or not spec.get("api_hash")
        ):
            raise ValueError("Missing Telegram API credentials")
        seen_ids.add(uid)
        seen_sessions.add(str(session.resolve()))
    return data


def credentials(account):
    spec = load().get(account)
    if spec is None:
        raise ValueError("Account has not been registered")
    session = Path(spec["session"])
    if not Path(str(session) + ".session").is_file():
        raise ValueError("Existing authorized session not found")
    guard = spec.get("collector_lock_dir")
    # Legacy collector guard is optional for imported accounts only.
    return spec, spec.get("proxy"), Path(guard) if guard else None
