"""Opt-in native cooperation without role grants or business-policy changes."""

import argparse
import asyncio
import hashlib
import json
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import jwt

from astrbot.core.platform.sources.wangshangliao.storage import Vault, instance_dir
from scripts import check_wsl_tree_menu_live as menus
from scripts.check_wsl_pair_live import (
    GROUP,
    ORDINARY,
    OWNER,
    PEER,
    PREFIX,
    REGISTRATION,
    ROOT,
    protected,
    read_rows,
    require,
)


async def main():
    """Run fixed private commands and group refusals using existing send authority.

    Raises:
        RuntimeError: If identities, authority, delivery or protected state differ.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--phase", choices=("all", "permissions"), default="all")
    args = parser.parse_args()
    private_commands = [
        (
            "客服设置",
            (
                "只有服务端",
                "只有已核验的专属客服所有者或本机器人指定客服负责人可修改",
                "暂时无法确认你的账号",
            ),
        ),
        ("取消", "取消"),
    ]
    if args.phase == "all":
        private_commands.insert(0, ("帮助", "【私信规则命令】"))
    if not args.apply:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "group": GROUP,
                    "private_sender": PEER,
                    "private_receiver": OWNER,
                    "private_commands": [text for text, _ in private_commands],
                    "group_commands": ["菜单", "客服设置"],
                    "new_roles_or_send_grants": False,
                    "model_calls": 0,
                },
                ensure_ascii=False,
            )
        )
        return
    config = json.loads((ROOT / "data/cmd_config.json").read_text("utf-8-sig"))
    platforms = [
        p
        for p in config["platform"]
        if p.get("type") == "wangshangliao" and p.get("enable", True)
    ]
    instances = {str(p.get("account_id", "")): p["id"] for p in platforms}
    require(
        len(platforms) == 3
        and set(instances) == {OWNER, PEER, ORDINARY}
        and len(set(instances.values())) == 3,
        "Exact three configured account bindings required",
    )
    output = Path("/opt/backups") / time.strftime(
        "wsl-cooperative-live-%Y%m%dT%H%M%SZ", time.gmtime()
    )
    output.mkdir(mode=0o700)
    report = {
        "group": GROUP,
        "phase": args.phase,
        "private": [],
        "group_commands": [],
        "extra_logins": 0,
    }
    target = output / "evidence.json"

    def save():
        """Persist private evidence before and after each non-replayable send."""
        target.touch(mode=0o600)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    before = protected()
    process = await asyncio.to_thread(
        subprocess.check_output,
        ["systemctl", "show", "astrbot.service", "--property=MainPID,NRestarts"],
        text=True,
    )
    store = ROOT / "data/plugin_data/wangshangliao_open/open-service.sqlite3"
    calls_before = {r["request"] for r in read_rows(store, "SELECT request FROM calls")}
    charges_before = {
        r["request"] for r in read_rows(store, "SELECT request FROM ai_charges")
    }
    token = jwt.encode(
        {"username": config["dashboard"]["username"], "exp": int(time.time()) + 600},
        config["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:6185",
        headers={"Authorization": "Bearer " + token},
        trust_env=False,
        timeout=35,
    ) as client:
        # MenuCheck's legacy bindings are CLI-local; no running adapter is replaced.
        menus.SENDER, menus.RECEIVER = instances[PEER], instances[OWNER]
        menus.USER, menus.BOT = PEER, OWNER
        helper = menus.MenuCheck(client, output)
        state = await helper.api(
            "POST", PREFIX + "open-service", {"action": "status", "payload": {}}
        )
        accounts = {a["account"]: a for a in state["accounts"]}
        require(
            set(accounts) == set(instances)
            and all(
                accounts[uid]["instance_id"] == instance
                and accounts[uid]["state"] == "online"
                for uid, instance in instances.items()
            ),
            "Exact three current online connections required",
        )
        settings = await helper.api("GET", PREFIX + "settings")
        policy_fields = (
            "id",
            "account_id",
            "enable",
            "enabled_groups",
            "reply_private",
            "reply_groups",
            "moderation",
            "ai_routes",
            "proactive_send",
            "developer_test",
            "revision",
        )
        policies = [
            {field: bot.get(field) for field in policy_fields}
            for bot in settings["bots"]
        ]
        report["policy_sha256_before"] = hashlib.sha256(
            json.dumps(policies, sort_keys=True).encode()
        ).hexdigest()
        sender = next(b for b in settings["bots"] if b["id"] == instances[PEER])
        require(
            sender.get("proactive_send", {}).get("enabled") is True
            and helper.target in sender["proactive_send"].get("targets", [])
            and read_rows(
                instance_dir(instances[PEER]) / "messages.sqlite3",
                "SELECT 1 FROM inbox WHERE account=? AND team=? LIMIT 1",
                (PEER, helper.target),
            ),
            "Existing exact private send grant and native peer required",
        )
        for instance in instances.values():
            status = await helper.api(
                "POST", REGISTRATION, {"action": "test_status", "instance_id": instance}
            )
            require(not status["window"]["active"], "Another test window is active")
        report["production_runtime_status"] = (
            await client.get("/api/v1/plugins/runtime/status")
        ).status_code
        report["private_scope"] = {"sender": PEER, "receiver": OWNER}
        group_open = False
        save()
        try:
            for text, expected in private_commands:
                report["private"].append({"input": text, "status": "unknown"})
                save()
                try:
                    await helper.send(text, expected)
                finally:
                    if helper.results and helper.results[-1]["input"] == text:
                        report["private"][-1].update(helper.results[-1])
                        report["private"][-1]["status"] = helper.results[-1]["receipt"][
                            "status"
                        ]
                    save()
            await helper.api(
                "POST",
                REGISTRATION,
                {"action": "test_close", "instance_id": instances[OWNER]},
            )
            helper.window = False
            await helper.api(
                "POST",
                REGISTRATION,
                {
                    "action": "test_open",
                    "instance_id": instances[PEER],
                    "sender_instance": instances[ORDINARY],
                    "groups": [GROUP],
                    "scopes": ["group_commands"],
                    "keywords": [],
                    "seconds": 120,
                    "budget": 2,
                },
            )
            group_open = True
            for text in ("菜单", "客服设置"):
                attempt = {"input": text, "status": "unknown"}
                report["group_commands"].append(attempt)
                save()
                receipt = await helper.api(
                    "POST",
                    PREFIX + "open-service",
                    {
                        "action": "test_probe",
                        "payload": {
                            "operation_id": "WSL_TEST_COOPERATE_" + uuid.uuid4().hex,
                            "text": text,
                        },
                    },
                )
                attempt.update(receipt)
                save()
                require(receipt["status"] == "accepted", "Do not replay unknown sends")
                mid = receipt["message_id"]
                for _ in range(100):
                    incoming = read_rows(
                        instance_dir(instances[PEER]) / "messages.sqlite3",
                        "SELECT state,payload FROM inbox WHERE account=? "
                        "AND team=? AND message=?",
                        (PEER, GROUP, mid),
                    )
                    outgoing = read_rows(
                        instance_dir(instances[PEER]) / "messages.sqlite3",
                        "SELECT key,state,server_id FROM outbox WHERE key LIKE ?",
                        (f"{PEER}/{GROUP}/{mid}/%/part/%",),
                    )
                    witnessed = [
                        read_rows(
                            instance_dir(instances[ORDINARY]) / "messages.sqlite3",
                            "SELECT payload FROM inbox WHERE account=? AND message=?",
                            (ORDINARY, part["server_id"]),
                        )
                        for part in outgoing
                        if part["state"] == "accepted"
                    ]
                    if (
                        incoming
                        and incoming[0]["state"] == "processed"
                        and outgoing
                        and len(witnessed) == len(outgoing)
                        and all(witnessed)
                    ):
                        break
                    await asyncio.sleep(0.25)
                reply = "\n".join(
                    json.loads(rows[0]["payload"])["text"] for rows in witnessed if rows
                )
                attempt.update(
                    {
                        "incoming_state": incoming[0]["state"] if incoming else "",
                        "outgoing": outgoing,
                        "reply": reply,
                        "independent_native_witness": bool(witnessed)
                        and all(witnessed),
                        "passed": bool(incoming)
                        and incoming[0]["state"] == "processed"
                        and len(outgoing) == 1
                        and bool(witnessed)
                        and all(witnessed)
                        and "当前机器人不是本群群管理员" in reply,
                    }
                )
                save()
                require(attempt["passed"], "Native permission refusal unverified")
                await asyncio.sleep(3.2)
        finally:
            for instance, owned in (
                (instances[OWNER], helper.window),
                (instances[PEER], group_open),
            ):
                if owned:
                    await helper.api(
                        "POST",
                        REGISTRATION,
                        {"action": "test_close", "instance_id": instance},
                    )
            report["private_receipts"] = helper.results
            report["protected_unchanged"] = protected() == before
            after_settings = await helper.api("GET", PREFIX + "settings")
            after_policies = [
                {field: bot.get(field) for field in policy_fields}
                for bot in after_settings["bots"]
            ]
            report["settings_unchanged"] = policies == after_policies
            report["policy_sha256_after"] = hashlib.sha256(
                json.dumps(after_policies, sort_keys=True).encode()
            ).hexdigest()
            report["process_unchanged"] = process == await asyncio.to_thread(
                subprocess.check_output,
                [
                    "systemctl",
                    "show",
                    "astrbot.service",
                    "--property=MainPID,NRestarts",
                ],
                text=True,
            )
            calls_after = {
                r["request"] for r in read_rows(store, "SELECT request FROM calls")
            }
            charges_after = {
                r["request"] for r in read_rows(store, "SELECT request FROM ai_charges")
            }
            report["new_model_call_records"] = len(calls_after - calls_before)
            report["new_charge_records"] = len(charges_after - charges_before)
            peer = str(Vault(instances[PEER]).load()["nim_id"])
            report["private_scoped_model_calls"] = sum(
                hashlib.sha256(
                    f"{OWNER}/nim/{peer}/{r['incoming']}".encode()
                ).hexdigest()
                in calls_after
                for r in helper.results
                if r.get("incoming")
            )
            report["windows_closed"] = True
            for instance in instances.values():
                status = await helper.api(
                    "POST",
                    REGISTRATION,
                    {"action": "test_status", "instance_id": instance},
                )
                report["windows_closed"] &= not status["window"]["active"]
            save()
        require(
            all(
                report[name]
                for name in (
                    "protected_unchanged",
                    "settings_unchanged",
                    "process_unchanged",
                    "windows_closed",
                )
            )
            and report["private_scoped_model_calls"] == 0,
            "Protected state or no-AI command invariant failed",
        )
    print(json.dumps({"report": str(target), **report}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
