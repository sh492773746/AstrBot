"""Verify a real controlled rejoin and restore its original business manager."""

import argparse
import asyncio
import copy
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import httpx
import jwt

ROOT = Path(__file__).resolve().parents[1]
CONTROL = Path("/opt/backups/wsl-test-lab")
GROUP = "1459282"
BOT, USER = "43506527", "31846829"


def evidence(attempts):
    """Read exact successful membership receipts and retained group ownership.

    Args:
        attempts: One remove, invite and optional accept journal identifier.

    Returns:
        Detached membership receipts and the current controlled tenant.
    """
    with closing(
        sqlite3.connect((CONTROL / "control.sqlite3").as_uri() + "?mode=ro", uri=True)
    ) as db:
        db.row_factory = sqlite3.Row
        receipts = []
        for action, identity in attempts.items():
            if not identity:
                continue
            row = db.execute(
                "SELECT id,group_id,action,created,state,provider_accepted,apply_id "
                "FROM attempts WHERE id=?",
                (identity,),
            ).fetchone()
            assert row and row["group_id"] == GROUP and row["action"] == action
            assert row["state"] == "verified" and row["provider_accepted"]
            receipts.append(dict(row))
    path = ROOT / "data/plugin_data/wangshangliao_open/open-service.sqlite3"
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT * FROM tenants WHERE account=? AND group_id=?", (BOT, GROUP)
        ).fetchone()
        assert row and json.loads(row["managers"]) == [USER]
        tenant = dict(row)
    return receipts, tenant


async def main():
    """Exercise real private recovery commands without rewriting historical rows."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--remove-attempt", required=True)
    parser.add_argument("--invite-attempt", required=True)
    parser.add_argument("--accept-attempt")
    args = parser.parse_args()
    if not args.apply:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "group": GROUP,
                    "robot": BOT,
                    "manager": USER,
                    "first_activation": False,
                    "requires": "Verified real removal, invitation and acceptance receipts",
                }
            )
        )
        return
    from scripts.check_wsl_account_names import PREFIX, snapshot
    from scripts.check_wsl_tree_menu_live import RECEIVER, SENDER, MenuCheck

    attempts = {
        "remove": args.remove_attempt,
        "invite": args.invite_attempt,
        "accept": args.accept_attempt,
    }
    before = snapshot()
    output = Path("/opt/backups") / time.strftime(
        "wsl-pair-rejoin-live-%Y%m%dT%H%M%S", time.gmtime()
    )
    output.mkdir(mode=0o700)
    config = json.loads((ROOT / "data/cmd_config.json").read_text("utf-8-sig"))
    token = jwt.encode(
        {"username": config["dashboard"]["username"], "exp": int(time.time()) + 900},
        config["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )
    result = {"group": GROUP, "first_activation": False, "passed": False}
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:6185",
        headers={"Authorization": "Bearer " + token},
        timeout=30,
        trust_env=False,
    ) as client:
        check = MenuCheck(client, output)
        try:
            receipts, tenant = evidence(attempts)
            invite = next(row for row in receipts if row["action"] == "invite")
            for _ in range(60):
                if (
                    tenant["present"]
                    and tenant["inviter"] == USER
                    and tenant["evidence"] == invite["apply_id"]
                    and tenant["admission"] == "verified"
                ):
                    break
                await asyncio.sleep(1)
                _, tenant = evidence(attempts)
            assert tenant["present"] and not tenant["active"], tenant
            assert tenant["inviter"] == USER
            assert tenant["evidence"] == invite["apply_id"]
            assert tenant["cleanup_done"] == tenant["cleanup_epoch"]
            result["membership_receipts"] = receipts
            result["rejoined_tenant"] = tenant
            state = await check.api(
                "POST", PREFIX + "open-service", {"action": "status", "payload": {}}
            )
            assert all(
                row["paused"] or not row["active"]
                for row in state["groups"]
                if row["group_id"] in {"1516387", "1488006"}
            )
            for instance in (SENDER, RECEIVER):
                status = await check.api(
                    "POST",
                    "/api/v1/bot-types/wangshangliao/registration",
                    {"action": "test_status", "instance_id": instance},
                )
                assert not status["window"]["active"]
            current = await check.settings()
            check.original = copy.deepcopy(current["proactive_send"])
            check.proposed = copy.deepcopy(check.original)
            check.proposed["enabled"] = True
            check.proposed["targets"] = list(
                dict.fromkeys(check.proposed.get("targets", []) + [check.target])
            )
            check.changed = True
            await check.api(
                "POST",
                PREFIX + "settings",
                {
                    "bot_id": SENDER,
                    "revision": current["revision"],
                    "proactive_send": check.proposed,
                },
            )
            await check.send(f"认领 {GROUP} invalid-test-code", "开通码不正确")
            assert not evidence(attempts)[1]["active"]
            minted = await check.api(
                "POST",
                PREFIX + "open-service",
                {
                    "action": "activation_code",
                    "payload": {"account": BOT, "group_id": GROUP},
                },
            )
            assert minted["expires_in"] == 600
            await check.send(f"认领 {GROUP} {minted['code']}", "本群已开通")
            recovered = evidence(attempts)[1]
            assert recovered["active"] and recovered["inviter"] == USER
            assert recovered["evidence"] == invite["apply_id"]
            await check.send(f"认领 {GROUP} {minted['code']}", "这个群已开通过")
            assert evidence(attempts)[1]["version"] == recovered["version"]
            await check.send("群列表", "1. 测试群")
            assert "1516387" not in check.results[-1]["reply"]
            assert "1488006" not in check.results[-1]["reply"]
            await check.send("选择群 1", "测试群")
            await check.send("取消", "已取消并退出菜单")
            result["recovered_tenant"] = recovered
            result["passed"] = True
        finally:
            await check.restore()
            after = snapshot()
            result["finance_unchanged"] = all(
                before[key] == after[key] for key in ("finance", "entries", "groups")
            )
            result["formal_tenants_unchanged"] = [
                row for row in before["tenants"] if row[1] in {"1516387", "1488006"}
            ] == [row for row in after["tenants"] if row[1] in {"1516387", "1488006"}]
            result["config_unchanged"] = before["config"] == after["config"]
            result["send_grant_restored"] = not check.changed
            result["window_closed"] = not check.window
            result["cases"] = len(check.results)
            path = output / "verification.json"
            path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
            path.chmod(0o600)
            print(f"Evidence: {output}", flush=True)
            assert all(
                result[key]
                for key in (
                    "finance_unchanged",
                    "formal_tenants_unchanged",
                    "config_unchanged",
                    "send_grant_restored",
                    "window_closed",
                )
            ), result


if __name__ == "__main__":
    asyncio.run(main())
