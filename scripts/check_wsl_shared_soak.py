"""Read-only stability observation of the integrated public service."""

import argparse
import asyncio
import json
import shutil
import sqlite3
import subprocess
import time
from contextlib import closing
from pathlib import Path

import httpx
import jwt

from scripts.check_wsl_account_names import PREFIX, ROOT, snapshot


async def main():
    """Observe protected finance, formal groups, connections and process bounds.

    Raises:
        AssertionError: If a protected field changes or a service restarts.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=1800)
    args = parser.parse_args()
    assert 60 <= args.seconds <= 3600
    config = json.loads((ROOT / "data/cmd_config.json").read_text("utf-8-sig"))
    token = jwt.encode(
        {"username": config["dashboard"]["username"], "exp": int(time.time()) + 7200},
        config["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )
    output = Path("/opt/backups") / time.strftime(
        "wsl-shared-soak-%Y%m%dT%H%M%S", time.gmtime()
    )
    output.mkdir(mode=0o700)
    before = snapshot()
    friend_states = {}
    for database in (ROOT / "data/platform_data/wangshangliao").glob(
        "*/friend_approval.sqlite3"
    ):
        with closing(
            sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        ) as db:
            friend_states[str(database)] = db.execute(
                "SELECT account,enabled,paused,version FROM approval_settings "
                "ORDER BY account"
            ).fetchall()
    formal = [r for r in before["tenants"] if r[1] in {"1516387", "1488006"}]
    evidence = {"started": time.time(), "required_seconds": args.seconds, "samples": []}
    baseline_pids = {}
    baseline_restarts = {}
    expected_accounts = {
        bot["id"]
        for bot in config["platform"]
        if bot.get("enable", True) and bot["type"] == "wangshangliao"
    }
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:6185",
        trust_env=False,
        timeout=20,
        headers={"Authorization": "Bearer " + token},
    ) as client:
        end = time.monotonic() + args.seconds
        while True:
            response = await client.post(
                PREFIX + "open-service", json={"action": "status", "payload": {}}
            )
            response.raise_for_status()
            state = response.json()["data"]
            assert state["mode"] == "integrated" and not state["ai_budget"]["confirmed"]
            assert len(state["accounts"]) == len(expected_accounts)
            assert {
                account.get("instance_id") for account in state["accounts"]
            } == expected_accounts
            assert all(a["state"] == "online" for a in state["accounts"])
            stats = await client.get("/api/v1/bots/stats")
            stats.raise_for_status()
            enabled = {p["id"] for p in config["platform"] if p.get("enable", True)}
            platforms = {
                row["id"]: row
                for row in stats.json()["data"]["platforms"]
                if row["id"] in enabled
            }
            assert platforms.keys() == enabled
            assert all(row["status"] == "running" for row in platforms.values())
            assert all(
                row["connection_state"] == "online"
                for row in platforms.values()
                if row["type"] in {"wangshangliao", "lajiaoliao"}
            )
            cashier_request = client.build_request(
                "POST", PREFIX + "cashier", json={"action": "me", "payload": {}}
            )
            cashier_request.headers.pop("Authorization", None)
            cashier = await client.send(cashier_request)
            assert cashier.status_code == 401
            assert cashier.json().get("status") == "error"
            after = snapshot()
            assert (
                after["finance"] == before["finance"]
                and after["groups"] == before["groups"]
                and after["config"] == before["config"]
            )
            assert [
                r for r in after["tenants"] if r[1] in {"1516387", "1488006"}
            ] == formal
            processes = {}
            for service, memory_limit, task_limit in (
                ("astrbot.service", 2 * 1024**3, 256),
            ):
                fields = await asyncio.to_thread(
                    subprocess.check_output,
                    [
                        "systemctl",
                        "show",
                        service,
                        "-p",
                        "MainPID",
                        "-p",
                        "MemoryCurrent",
                        "-p",
                        "TasksCurrent",
                        "-p",
                        "NRestarts",
                        "-p",
                        "ActiveState",
                    ],
                    text=True,
                )
                process = dict(line.split("=", 1) for line in fields.splitlines())
                assert process["ActiveState"] == "active"
                assert int(process["MainPID"]) > 0
                baseline_pids.setdefault(service, process["MainPID"])
                baseline_restarts.setdefault(service, process["NRestarts"])
                assert process["MainPID"] == baseline_pids[service]
                assert process["NRestarts"] == baseline_restarts[service]
                assert int(process["MemoryCurrent"]) < memory_limit
                assert int(process["TasksCurrent"]) <= task_limit
                processes[service] = process
            for database, expected in friend_states.items():
                with closing(
                    sqlite3.connect(Path(database).as_uri() + "?mode=ro", uri=True)
                ) as db:
                    assert (
                        db.execute(
                            "SELECT account,enabled,paused,version FROM approval_settings "
                            "ORDER BY account"
                        ).fetchall()
                        == expected
                    )
            evidence["samples"].append(
                {
                    "at": time.time(),
                    "finance": after["finance"],
                    "process": processes["astrbot.service"],
                    "services": processes,
                    "cashier_anonymous_status": cashier.status_code,
                    "platform_stats": stats.json()["data"],
                    "queue_health": state["queue_health"],
                    "disk_free_bytes": shutil.disk_usage(ROOT).free,
                }
            )
            evidence["elapsed_seconds"] = round(time.time() - evidence["started"], 2)
            evidence["baseline_restarts"] = baseline_restarts
            evidence["complete"] = time.monotonic() >= end
            path = output / "evidence.json"
            path.write_text(json.dumps(evidence, ensure_ascii=False))
            path.chmod(0o600)
            print(
                "Stable observation", evidence["elapsed_seconds"], "seconds", flush=True
            )
            if evidence["complete"]:
                break
            await asyncio.sleep(min(30, end - time.monotonic()))
    print("Stability evidence:", output / "evidence.json", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
