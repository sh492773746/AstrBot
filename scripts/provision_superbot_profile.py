"""Provision the dedicated chat profile without granting a business owner."""

import copy
import hashlib
import json
import shutil
import sqlite3
import time
from pathlib import Path
from urllib.parse import quote

import httpx
import jwt
from filelock import FileLock

from astrbot.core.config.default import DEFAULT_CONFIG


def main():
    """Create isolated resources through the local authenticated dashboard API."""
    root = Path(__file__).resolve().parents[1]
    data = root / "data"
    config = json.loads((data / "cmd_config.json").read_text(encoding="utf-8-sig"))
    platform = "大海传媒超级机器人"
    plugin = "astrbot_plugin_superbot"
    platform_config = next(p for p in config["platform"] if p["id"] == platform)
    assert platform_config["type"] == "telegram" and platform_config["enable"]
    plugin_path = data / "config" / f"{plugin}_config.json"
    plugin_config = json.loads(plugin_path.read_text(encoding="utf-8-sig"))
    assert not plugin_config["enabled"], "Stop business runtime before provisioning"
    digest = hashlib.sha256(platform.encode()).hexdigest()
    name = platform + "-" + digest[:12]
    persona = "superbot-" + digest[:12]
    kb_name = name + "-使用知识"
    route = platform + ":*:*"
    docs = data / "plugins" / plugin / "docs"
    instance = data / "plugin_data" / plugin / digest
    instance.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = Path("/opt/rebo-backups") / (
        "superbot-profile-" + time.strftime("%Y%m%d-%H%M%S")
    )
    backup.mkdir(mode=0o700)
    shutil.copy2(data / "cmd_config.json", backup / "cmd_config.json")
    shutil.copytree(data / "config", backup / "config")
    for source in [data / "data_v4.db", instance / "superbot.sqlite3"]:
        if source.exists():
            with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src:
                with sqlite3.connect(backup / source.name) as dst:
                    src.backup(dst)
    for path in backup.rglob("*"):
        path.chmod(0o700 if path.is_dir() else 0o600)
    dashboard = config["dashboard"]
    token = jwt.encode(
        {"username": dashboard["username"], "exp": int(time.time()) + 900},
        dashboard["jwt_secret"],
        algorithm="HS256",
    )
    with (
        FileLock(instance / "runtime.lock", timeout=0),
        httpx.Client(
            base_url="http://127.0.0.1:6185/api/v1",
            headers={"Authorization": "Bearer " + token},
            trust_env=False,
            timeout=90,
        ) as client,
    ):

        def api(method, path, **kwargs):
            """Call the local API without exposing authentication or credentials.

            Args:
                method: HTTP method.
                path: API-relative path.
                **kwargs: HTTP request arguments.

            Returns:
                The successful response data.
            """
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            result = response.json()
            if result.get("status") != "ok":
                raise RuntimeError(f"API operation failed: {method} {path}")
            return result["data"]

        routes = api("GET", "/config-routes")["routing"]
        profiles = api("GET", "/config-profiles")["info_list"]
        matches = [p for p in profiles if p["name"] == name]
        assert len(matches) <= 1
        profile_id = matches[0]["id"] if matches else None
        assert route not in routes or routes[route] == profile_id
        personas = api("GET", "/personas")
        if not any(p["persona_id"] == persona for p in personas):
            api(
                "POST",
                "/personas",
                json={
                    "persona_id": persona,
                    "system_prompt": (docs / "persona.md").read_text(),
                    "tools": [],
                    "skills": [],
                },
            )
        bases = api("GET", "/knowledge-bases")["items"]
        kb = next((b for b in bases if b["kb_name"] == kb_name), None)
        if kb is None:
            kb = api(
                "POST",
                "/knowledge-bases",
                json={
                    "name": kb_name,
                    "description": "本机器人专属功能、玩法与操作帮助；动态业务数据以菜单为准。",
                    "embedding_provider_id": "硅基流动",
                },
            )
        kb_id = kb["kb_id"]
        documents = api("GET", f"/knowledge-bases/{kb_id}/documents")["items"]
        names = {d["doc_name"] for d in documents}
        tasks = []
        for filename in ("knowledge.md", "game-rules.md"):
            content = (docs / filename).read_bytes()
            file_name = (
                "superbot-" + hashlib.sha256(content).hexdigest()[:12] + "-" + filename
            )
            if file_name not in names:
                upload = api(
                    "POST",
                    f"/knowledge-bases/{kb_id}/documents",
                    files={
                        "file": (file_name, content, "text/markdown"),
                    },
                )
                tasks.append(upload["task_id"])
        if profile_id is None:
            profile = copy.deepcopy(DEFAULT_CONFIG)
            profile["admins_id"] = []
            profile["plugin_set"] = [plugin]
            profile["wake_prefix"] = []
            profile["platform_settings"].update(
                {
                    "friend_message_needs_wake_prefix": False,
                    "ignore_bot_self_message": True,
                    "ignore_at_all": True,
                    "unique_session": True,
                }
            )
            profile["provider_settings"]["persona_pool"] = [persona]
            profile["provider_settings"]["provider_pool"] = ["savnx/grok-4.6"]
            profile["agent_runner"]["config"]["model"]["provider_id"] = "savnx/grok-4.6"
            profile["agent_runner"]["config"]["persona"]["persona_id"] = persona
            profile["kb_names"] = [kb_name]
            profile["kb_agentic_mode"] = False
            profile_id = api(
                "POST", "/config-profiles", json={"name": name, "config": profile}
            )["conf_id"]
        # Seed only compatible provisioning tables, without creating an owner,
        # balances or business settings. Store performs its normal migration later.
        db_path = instance / "superbot.sqlite3"
        with sqlite3.connect(db_path) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL,version INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,at REAL NOT NULL,actor TEXT NOT NULL,action TEXT NOT NULL,data TEXT NOT NULL);
            """)
            old = db.execute(
                "SELECT value FROM settings WHERE key='profile_id'"
            ).fetchone()
            assert old is None or json.loads(old[0]) == profile_id
            db.execute(
                "INSERT OR IGNORE INTO settings(key,value,version) VALUES(?,?,1)",
                ("profile_id", json.dumps(profile_id)),
            )
            db.execute(
                "INSERT INTO audit(at,actor,action,data) VALUES(?,?,?,?)",
                (
                    time.time(),
                    "system",
                    "chat_profile_provisioned",
                    json.dumps({"id": profile_id, "kb": kb_name, "persona": persona}),
                ),
            )
        db_path.chmod(0o600)
        api(
            "PUT",
            "/config-routes/" + quote(route, safe=""),
            json={"config_id": profile_id},
        )
        plugin_config.update(
            {
                "platform_id": platform,
                "chat_provider_id": "savnx/grok-4.6",
                "embedding_provider_id": "硅基流动",
            }
        )
        api(
            "PUT",
            "/plugins/config",
            json={"plugin_id": plugin, "config": plugin_config},
        )
        print(
            json.dumps(
                {
                    "profile_id": profile_id,
                    "persona": persona,
                    "kb_id": kb_id,
                    "tasks": tasks,
                    "backup": str(backup),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
