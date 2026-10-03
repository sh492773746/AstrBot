"""Scoped rollout of immutable pool-game versions after offline acceptance."""

import asyncio
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import httpx
import jwt
from telegram import Bot

sys.path.insert(0, "/opt/scripts")
from deploy_slots_pvp import CHAT, DATABASE, ROOT, pending  # noqa: E402


async def main():
    """Check live permissions, drain operations and reload without test wagers."""
    os.umask(0o077)
    backup = Path("/opt/rebo-backups/games-v3-20260927")
    backup.chmod(0o700)
    config = json.loads((ROOT / "data/cmd_config.json").read_text(encoding="utf-8-sig"))
    plugin = json.loads(
        (ROOT / "data/config/astrbot_plugin_superbot_config.json").read_text(
            encoding="utf-8-sig"
        )
    )
    platform = next(p for p in config["platform"] if p["id"] == plugin["platform_id"])
    async with Bot(platform["telegram_token"]) as bot:
        group = await bot.get_chat(CHAT)
        assert group.title == "机器人测试" and group.type == "supergroup", (
            "Wrong test group"
        )
        me = await bot.get_me()
        rights = await bot.get_chat_member(CHAT, me.id)
        assert rights.status == "creator" or (
            rights.status == "administrator" and rights.can_delete_messages
        ), "Missing cleanup rights"
    db = sqlite3.connect(DATABASE, isolation_level=None, timeout=10)
    assert not pending(db), "Network operations are in flight"
    for table, states in (
        ("slots_tables", "'open','locked'"),
        ("mines_tables", "'open','active'"),
        ("k3_rounds", "'open','closing'"),
    ):
        assert not db.execute(
            f"SELECT 1 FROM {table} WHERE status IN ({states})"
        ).fetchone(), "A game is active"
    assert db.execute(
        "SELECT enabled FROM mod_groups WHERE chat=?", (CHAT,)
    ).fetchone() == (1,)
    original, version = db.execute(
        "SELECT value,version FROM settings WHERE key='modules'"
    ).fetchone()
    modules = json.loads(original)
    assert all(modules.get(k) for k in ("game", "slots", "mines")), (
        "Expected switches changed"
    )
    assert not db.execute(
        "SELECT 1 FROM settings WHERE key='games_v3_groups'"
    ).fetchone(), "Rollout already configured; inspect manually"
    for name in ("slots_groups", "mines_groups"):
        assert db.execute(
            f"SELECT enabled FROM {name} WHERE chat=?", (CHAT,)
        ).fetchone() == (1,)
    snapshot = backup / "preload.sqlite3"
    assert not snapshot.exists(), "Never overwrite a deployment snapshot"
    with sqlite3.connect(snapshot) as target:
        db.backup(target)
    shutil.copy2(ROOT / "data/cmd_config.json", backup / "cmd-config.json")
    paused = json.dumps({**modules, "slots": False, "mines": False, "wheel": False})
    assert (
        db.execute(
            "UPDATE settings SET value=?,version=version+1 WHERE key='modules' AND version=?",
            (paused, version),
        ).rowcount
        == 1
    )
    print(
        "Permissions verified; admissions paused while network work drains.", flush=True
    )
    try:
        await asyncio.sleep(10)
        assert not pending(db), "Network work did not drain"
        for table, states in (
            ("slots_tables", "'open','locked'"),
            ("mines_tables", "'open','active'"),
            ("k3_rounds", "'open','closing'"),
        ):
            assert not db.execute(
                f"SELECT 1 FROM {table} WHERE status IN ({states})"
            ).fetchone(), "A game started while draining; stop rollout"
        dashboard = config["dashboard"]
        token = jwt.encode(
            {"username": dashboard["username"], "exp": int(time.time()) + 120},
            dashboard["jwt_secret"],
            algorithm="HS256",
        )
        async with httpx.AsyncClient(timeout=50) as client:
            result = await client.post(
                "http://127.0.0.1:6185/api/plugin/reload",
                json={"name": "astrbot_plugin_superbot"},
                headers={"Authorization": "Bearer " + token},
            )
            result.raise_for_status()
            assert result.json().get("status") == "ok", "Reload was not confirmed"
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        for table in ("mines_events", "slots_v3_rounds"):
            assert db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
        db.execute("BEGIN IMMEDIATE")
        try:
            assert db.execute(
                "SELECT value,version FROM settings WHERE key='modules'"
            ).fetchone() == (paused, version + 1), "Admin changed switches"
            # Old selectors described different economics; require a fresh panel.
            for game in ("slots", "mines"):
                db.execute(
                    f"UPDATE {game}_menus SET status='expired' WHERE status='active' "
                    f"AND NOT EXISTS(SELECT 1 FROM {game}_tables t WHERE t.menu={game}_menus.id)"
                )
            db.execute(
                "INSERT INTO settings VALUES('games_v3_groups',?,1)",
                (json.dumps([CHAT]),),
            )
            db.execute(
                "UPDATE settings SET value=?,version=version+1 WHERE key='modules'",
                (original,),
            )
            db.execute(
                "INSERT INTO audit(at,actor,action,data) VALUES(?,?,?,?)",
                (
                    time.time(),
                    "deployment",
                    "games_v3_release",
                    json.dumps(
                        {"groups": [CHAT], "backup": str(backup), "wagers_sent": 0}
                    ),
                ),
            )
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        print("Reload confirmed; integrity ok; new games limited to", CHAT, flush=True)
        print(
            "Group switches and other modules preserved. No wagers or test messages sent."
        )
    except BaseException:
        db.execute(
            "UPDATE settings SET value=?,version=version+1 WHERE key='modules' AND value=? AND version=?",
            (original, paused, version + 1),
        )
        raise
    finally:
        db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        # Do not expose exception URLs containing bot tokens or credentials.
        print(
            "Deployment stopped:",
            type(exc).__name__,
            str(exc)
            if isinstance(exc, AssertionError)
            else "Inspect locally; no retry.",
        )
        raise SystemExit(1) from None
