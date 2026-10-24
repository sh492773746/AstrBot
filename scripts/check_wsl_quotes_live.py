"""Opt-in nine-message quote matrix on the three already logged-in test accounts."""

import argparse
import asyncio
import json
import time
import uuid
from pathlib import Path

import httpx
import jwt

from scripts.check_wsl_pair_live import (
    GROUP,
    ORDINARY,
    OWNER,
    PEER,
    PREFIX,
    REGISTRATION,
    ROOT,
    require,
)
from scripts.wsl_test_lab import protected_state


async def main():
    """Inspect prerequisites or submit each bounded quote step exactly once."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    if not args.apply and not args.preflight:
        print(
            json.dumps(
                {"dry_run": True, "group": GROUP, "messages": 9, "extra_logins": 0}
            )
        )
        return
    config = json.loads((ROOT / "data/cmd_config.json").read_text("utf-8-sig"))
    token = jwt.encode(
        {"username": config["dashboard"]["username"], "exp": int(time.time()) + 600},
        config["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )
    operation = "WSL_TEST_QUOTES_" + uuid.uuid4().hex
    platforms = [
        p
        for p in config["platform"]
        if p.get("type") == "wangshangliao" and p.get("enable", True)
    ]
    expected = {str(p.get("account_id", "")): p["id"] for p in platforms}
    require(
        len(platforms) == 3
        and set(expected) == {OWNER, PEER, ORDINARY}
        and len(set(expected.values())) == 3,
        "Exact three configured account/instance bindings required",
    )
    receiver, fixture = expected[PEER], expected[ORDINARY]
    evidence = {
        "operation_id": operation,
        "group": GROUP,
        "steps": [],
        "extra_logins": 0,
        "financial_writes": False,
        "phone_rendering_verified": False,
    }
    before = protected_state(ROOT, GROUP)
    output = None

    def save():
        if output is not None:
            target = output / "evidence.json"
            target.touch(mode=0o600)
            target.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:6185",
        trust_env=False,
        headers={"Authorization": "Bearer " + token},
        timeout=35,
    ) as client:

        async def post(route, payload):
            result = await client.post(route, json=payload)
            if result.status_code != 200:
                raise RuntimeError(
                    "Controlled quote request rejected; check AstrBot logs."
                )
            body = result.json()
            if body.get("status") != "ok":
                raise RuntimeError(
                    "Controlled quote request failed; do not repeat an uncertain send."
                )
            return body["data"]

        state = await post(PREFIX + "open-service", {"action": "status", "payload": {}})
        accounts = {a["account"]: a for a in state["accounts"]}
        evidence["accounts_online"] = set(accounts) == set(expected) and all(
            accounts[uid]["instance_id"] == instance
            and accounts[uid]["state"] == "online"
            for uid, instance in expected.items()
        )
        require(
            evidence["accounts_online"], "Exact three existing online accounts required"
        )
        # A random close only probes the allowlisted handler; it cannot close another run.
        capability = await client.post(
            PREFIX + "open-service",
            json={
                "action": "test_quotes",
                "payload": {"operation_id": operation, "step": "close"},
            },
        )
        evidence["running_handler_loaded"] = (
            capability.status_code == 200 and capability.json().get("status") == "ok"
        )
        if not args.apply:
            evidence["protected_unchanged"] = protected_state(ROOT, GROUP) == before
            print(json.dumps(evidence, ensure_ascii=False))
            return
        require(
            evidence["running_handler_loaded"],
            "Restart into the tested code during an approved maintenance window first",
        )
        for instance in expected.values():
            status = await post(
                REGISTRATION, {"action": "test_status", "instance_id": instance}
            )
            require(
                not (status.get("window") or {}).get("active"),
                "Another test window is active",
            )
        output = Path("/opt/backups") / time.strftime(
            "wsl-quotes-live-%Y%m%dT%H%M%SZ", time.gmtime()
        )
        output.mkdir(mode=0o700)
        save()
        opened = False
        try:
            await post(
                REGISTRATION,
                {
                    "action": "test_open",
                    "instance_id": receiver,
                    "sender_instance": fixture,
                    "groups": [GROUP],
                    "scopes": ["group_commands"],
                    "keywords": [],
                    "seconds": 300,
                    "budget": 10,
                },
            )
            opened = True
            for step in range(9):
                attempt = {"step": step, "status": "unknown"}
                evidence["steps"].append(attempt)
                save()
                result = await post(
                    PREFIX + "open-service",
                    {
                        "action": "test_quotes",
                        "payload": {"operation_id": operation, "step": step},
                    },
                )
                attempt.update(result)
                save()
                require(
                    result["status"] == "accepted" and len(result["witnesses"]) == 2,
                    "Quote send or independent receipt unverified; do not replay",
                )
            evidence["native_quote_matrix_verified"] = True
        finally:
            if opened:
                try:
                    evidence["cleanup"] = await post(
                        PREFIX + "open-service",
                        {
                            "action": "test_quotes",
                            "payload": {"operation_id": operation, "step": "close"},
                        },
                    )
                except Exception:
                    evidence["cleanup"] = (
                        "unknown; original window expires within five minutes"
                    )
            evidence["protected_unchanged"] = protected_state(ROOT, GROUP) == before
            save()
        require(
            evidence["protected_unchanged"],
            "Protected state changed; do not automatically restore live data",
        )
    print(
        json.dumps(
            {"report": str(output / "evidence.json"), **evidence}, ensure_ascii=False
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
