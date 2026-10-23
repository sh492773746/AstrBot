"""Migration and runtime acceptance; writes require explicit --hot-save."""

import argparse
import asyncio
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import httpx
import jwt

from astrbot.core.platform.sources.wangshangliao.runtime_policy import BUSINESS_FIELDS

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "/api/v1/plugins/extensions/wangshangliao_moderation"


def verify_migration(config, backup):
    """Compare migration evidence against the unchanged predeployment baseline.

    Args:
        config: Current transport configuration.
        backup: Original stopped-backup directory, never a generated live snapshot.

    Raises:
        AssertionError: If bindings, policies or historical records differ.
    """
    before = json.loads((backup / "cmd_config.json").read_text(encoding="utf-8-sig"))
    current = {b["id"]: b for b in config["platform"] if b["type"] == "wangshangliao"}
    policy_snapshot = (
        backup / "plugin_data" / "wangshangliao_moderation" / "settings.sqlite3"
    )
    prior_policies = {}
    if policy_snapshot.is_file():
        with closing(
            sqlite3.connect(policy_snapshot.as_uri() + "?mode=ro", uri=True)
        ) as old:
            assert old.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            prior_policies = {
                instance: (account, json.loads(payload))
                for instance, account, payload in old.execute(
                    "SELECT instance,account,payload FROM policies"
                )
            }
    path = ROOT / "data/plugin_data/wangshangliao_moderation/settings.sqlite3"
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        for bot in before["platform"]:
            if bot["type"] != "wangshangliao":
                continue
            row = db.execute(
                "SELECT account,payload FROM policies WHERE instance=?", (bot["id"],)
            ).fetchone()
            assert row and row[0] == str(bot["account_id"])
            values = json.loads(row[1])
            assert all(
                k in values and values[k] == bot[k] for k in BUSINESS_FIELDS if k in bot
            ), "migrated policy differs from the original baseline"
            if policy_snapshot.is_file():
                assert prior_policies.get(bot["id"]) == (
                    row[0],
                    values,
                ), "plugin policy differs from the stopped snapshot"
                print("PASS persisted policy snapshot:", bot["id"])
            assert not BUSINESS_FIELDS.intersection(current[bot["id"]])
            assert all(
                current[bot["id"]].get(k) == v
                for k, v in bot.items()
                if k not in BUSINESS_FIELDS
            )
            print("PASS policy migration:", bot["id"])
    checked = 0
    for old_path in backup.rglob("*.sqlite3"):
        live_path = ROOT / "data" / old_path.relative_to(backup)
        if not live_path.exists():
            raise AssertionError(
                "Missing migrated ledger: " + str(old_path.relative_to(backup))
            )
        with closing(sqlite3.connect(old_path.as_uri() + "?mode=ro", uri=True)) as old:
            with closing(
                sqlite3.connect(live_path.as_uri() + "?mode=ro", uri=True)
            ) as live:
                assert old.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                assert live.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                old_tables = {
                    r[0]
                    for r in old.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                for table, columns, where in (
                    ("invite_credits", "account,group_id,member,inviter,amount", ""),
                    ("invite_seen", "account,group_id,member,first_seen,rate", ""),
                    (
                        "entries",
                        "id,group_id,member,amount,paid_at,paid_by",
                        "WHERE state='paid'",
                    ),
                ):
                    if table not in old_tables:
                        continue
                    prior = set(old.execute(f"SELECT {columns} FROM {table} {where}"))
                    after = set(live.execute(f"SELECT {columns} FROM {table} {where}"))
                    assert prior <= after, (table, "historical ledger changed")
                    checked += len(prior)
    print("PASS historical baseline/credit/payment rows preserved:", checked)


async def main():
    """Verify live reads without treating later policy changes as migration loss."""
    parser = argparse.ArgumentParser()
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--backup", type=Path)
    scope.add_argument("--runtime-only", action="store_true")
    parser.add_argument("--hot-save", action="store_true")
    args = parser.parse_args()
    if args.runtime_only and args.hot_save:
        parser.error("--runtime-only is read-only and cannot use --hot-save")
    config = json.loads((ROOT / "data/cmd_config.json").read_text(encoding="utf-8-sig"))
    if args.backup:
        verify_migration(config, args.backup)
    else:
        print("Runtime-only checks: migration and historical comparisons not performed")
    token = jwt.encode(
        {"username": config["dashboard"]["username"], "exp": int(time.time()) + 600},
        config["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:6185",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
        trust_env=False,
    ) as client:
        response = await client.get(PREFIX + "/settings")
        response.raise_for_status()
        bots = response.json()["data"]["bots"]
        for bot in bots:
            assert bot["state"] == "online", bot["id"]
            assert bot["business_active"], bot["id"]
            print("PASS online:", bot["id"], "groups:", bot["group_names"])
            if args.hot_save:
                payload = {
                    "bot_id": bot["id"],
                    "revision": bot["revision"],
                    "reply_private": bot["reply_private"],
                }
                saved = await client.post(PREFIX + "/settings", json=payload)
                assert saved.status_code == 200, saved.status_code
                stale = await client.post(PREFIX + "/settings", json=payload)
                assert stale.status_code == 409, stale.status_code
                updated = (await client.get(PREFIX + "/settings")).json()["data"][
                    "bots"
                ]
                updated = next(b for b in updated if b["id"] == bot["id"])
                assert updated["last_connected_at"] == bot["last_connected_at"]
                assert updated["state"] == "online" and updated["business_active"]
                print(
                    "PASS hot save without reconnect and stale version rejection:",
                    bot["id"],
                )
            for group in bot["enabled_groups"]:
                for endpoint in (
                    "audit",
                    "activities",
                    "schedule",
                    "invitations",
                    "cashback-logs",
                    "operations",
                    "test-window",
                ):
                    reply = await client.get(
                        PREFIX + "/" + endpoint,
                        params={"bot_id": bot["id"]}
                        if endpoint == "test-window"
                        else {"bot_id": bot["id"], "group_id": group},
                    )
                    if (
                        endpoint == "cashback-logs"
                        and bot["id"] != "wangshangliao_3cf3b038"
                    ):
                        assert reply.status_code == 403
                        print("PASS non-collector audit rejection:", bot["id"])
                        continue
                    assert reply.status_code == 200, (endpoint, reply.status_code)
                    assert reply.json()["status"] == "ok", endpoint
                    print("PASS read:", bot["id"], group, endpoint)
    async with httpx.AsyncClient(trust_env=False, timeout=15) as client:
        for method, address, body in (
            ("GET", "http://127.0.0.1:6185" + PREFIX + "/settings", None),
            (
                "POST",
                "http://127.0.0.1:6185" + PREFIX + "/cashier",
                {"action": "me", "payload": {}},
            ),
        ):
            response = await client.request(method, address, json=body)
            assert response.status_code in (401, 403)
            print("PASS unauthenticated rejection:", response.status_code)


if __name__ == "__main__":
    asyncio.run(main())
