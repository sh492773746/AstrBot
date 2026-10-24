"""Opt-in three-online-account checks; never create an extra NIM connection."""

import argparse
import asyncio
import json
import re
import sqlite3
import sys
import time
import uuid
from pathlib import Path

import aiohttp
import httpx
import jwt

from astrbot.core.platform.sources.wangshangliao.business import (
    BusinessClient,
    Deployment,
)
from astrbot.core.platform.sources.wangshangliao.directory import member_directory
from astrbot.core.platform.sources.wangshangliao.storage import Vault
from scripts.check_wsl_pair_live import (
    GROUP,
    ORDINARY,
    OWNER,
    PEER,
    PREFIX,
    RECEIVER,
    REGISTRATION,
    ROOT,
    SENDER,
    PairCheck,
    protected,
    require,
)
from scripts.check_wsl_tree_menu_live import MenuCheck
from scripts.wsl_test_lab import CONTROL, TestLab, protected_state

FIXTURE = "wangshangliao_6541e09d"
BACKUPS = Path("/opt/backups")


async def main():
    """Exercise reversible menus and role-scoped operations with private evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--native-write-limit", type=int, choices=(10, 20), default=10)
    parser.add_argument(
        "--manage-test-role",
        action="store_true",
        help="Explicitly grant/restore the test bot role, subject to the existing daily cap",
    )
    parser.add_argument(
        "--phase",
        action="append",
        choices=(
            "knowledge",
            "commands",
            "ordinary-security",
            "manual",
            "escalation",
            "flood",
            "native-extra",
            "schedule",
            "lottery",
            "lottery-member",
            "administrators",
            "finance",
        ),
    )
    args = parser.parse_args()
    phases = args.phase or ["knowledge", "commands", "ordinary-security", "schedule"]
    if not args.apply:
        print(json.dumps({"dry_run": True, "group": GROUP, "phases": phases}))
        return
    output = BACKUPS / (
        "wsl-three-live-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    )
    output.mkdir(mode=0o700)
    config = json.loads((ROOT / "data/cmd_config.json").read_text("utf-8-sig"))
    token = jwt.encode(
        {"username": config["dashboard"]["username"], "exp": int(time.time()) + 7200},
        config["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )
    baseline = protected()
    evidence = {
        "phases": [],
        "extra_logins": 0,
        "financial_writes": False,
        "native_daily_write_limit": args.native_write_limit,
    }
    async with (
        httpx.AsyncClient(
            base_url="http://127.0.0.1:6185",
            trust_env=False,
            headers={"Authorization": "Bearer " + token},
            timeout=60,
        ) as client,
        aiohttp.ClientSession() as http,
    ):
        helper = MenuCheck(client, output)
        state = await helper.api(
            "POST", PREFIX + "open-service", {"action": "status", "payload": {}}
        )
        accounts = {item["account"]: item for item in state["accounts"]}
        require(
            set(accounts) == {OWNER, PEER, ORDINARY}, "Exact three online bots required"
        )
        require(all(a["state"] == "online" for a in accounts.values()), "Offline bot")
        clients, pins = {}, {}
        for uid, instance in ((OWNER, SENDER), (PEER, RECEIVER), (ORDINARY, FIXTURE)):
            require(
                accounts[uid]["instance_id"] == instance, "Unexpected account binding"
            )
            saved = Vault(instance).load()
            require(str(saved["business"]["uid"]) == uid, "Vault identity conflict")
            pins[uid] = str(saved["nim_id"])
            if uid != ORDINARY:
                business = BusinessClient(Deployment.load(), http)
                business.restore(saved["business"])
                clients[uid] = business
        directory = await member_directory(clients[PEER], GROUP)
        members = {str(m["userId"]): m for m in directory["groupMemberInfo"]}
        require(
            directory["complete"]
            and set(members) == {OWNER, PEER, ORDINARY}
            and all(
                str(members[uid]["nimId"]) == pins[uid]
                and members[uid].get("accountState") == "ACCOUNT_STATE_GOOD"
                for uid in pins
            )
            and members[OWNER]["groupRole"] == "GROUP_ROLE_OWNER"
            and members[ORDINARY]["groupRole"] == "GROUP_ROLE_MEMBER",
            "Exact current owner and ordinary-member identities required",
        )
        original_role = members[PEER]["groupRole"]
        evidence["native_roles"] = {
            uid: row["groupRole"] for uid, row in members.items()
        }
        evidence["automatic_role_changes"] = args.manage_test_role
        summary = output / "summary.json"
        summary.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
        summary.chmod(0o600)
        require(
            original_role == "GROUP_ROLE_ADMIN"
            or args.manage_test_role
            and original_role == "GROUP_ROLE_MEMBER",
            "Set Dahu as administrator in the official App before testing; "
            "automatic role changes are disabled by default",
        )
        lab = (
            TestLab(
                clients,
                pins,
                GROUP,
                CONTROL,
                protection=lambda group, first=False: protected_state(
                    ROOT, group, first=first
                ),
                daily_write_limit=args.native_write_limit,
            )
            if args.manage_test_role
            else None
        )
        granted = False
        try:
            if original_role == "GROUP_ROLE_MEMBER" and lab is not None:
                preview = await lab.preview("grant")
                result = await lab.execute(preview["preview"], preview["confirmation"])
                evidence["native_grant"] = result
                require(result["state"] == "verified", "Native role grant not verified")
                granted = True
                await asyncio.sleep(4)
            for phase in phases:
                folder = output / phase
                folder.mkdir(mode=0o700)
                with sqlite3.connect(folder / "attempts.sqlite3") as journal:
                    journal.execute(
                        "CREATE TABLE attempts(id TEXT PRIMARY KEY,run TEXT,kind TEXT,state TEXT,created REAL)"
                    )
                    check = PairCheck(
                        client, http, folder, journal, [phase], existing_fixture=FIXTURE
                    )
                    try:
                        await check.preflight()
                        if phase not in {"knowledge", "commands", "finance"}:
                            await check.ordinary_start()
                        if phase == "knowledge":
                            check.ai_scope = True
                            check.remaining = 0
                            marker = "验收资料" + uuid.uuid4().hex[:8]
                            for personal in (True, False):
                                root, category = ("8", "5") if personal else ("9", "7")
                                label = "我的知识" if personal else "本群知识"
                                await check.command("取消", "取消")
                                listing = await check.command(label, "知识库")
                                require(marker not in listing, "Canary collision")
                                for text, expected in (
                                    ("菜单", "命令菜单"),
                                    (root, "AI"),
                                    (category, "知识库"),
                                    ("2", "标题"),
                                    (
                                        marker + " | 本轮专用资料，校验词是青竹七号。",
                                        "即将执行",
                                    ),
                                    ("1", "已保存"),
                                ):
                                    await check.command(text, expected)
                                listing = await check.command(label, marker)
                                match = re.search(
                                    r"(?m)^(\d+)\. " + re.escape(marker) + "$", listing
                                )
                                require(
                                    match is not None, "Canary not listed on first page"
                                )
                                number = match[1]
                                await check.command(
                                    ("查看我的知识 " if personal else "查看群知识 ")
                                    + number,
                                    "青竹七号",
                                )
                                for text, expected in (
                                    ("菜单", "命令菜单"),
                                    (root, "AI"),
                                    (category, "知识库"),
                                    ("3", "序号"),
                                    (
                                        number
                                        + " "
                                        + marker
                                        + " | 校验词已改为青竹八号。",
                                        "即将执行",
                                    ),
                                    ("1", "已保存"),
                                ):
                                    await check.command(text, expected)
                                await check.command(
                                    ("查看我的知识 " if personal else "查看群知识 ")
                                    + number,
                                    "青竹八号",
                                )
                                for text, expected in (
                                    ("菜单", "命令菜单"),
                                    (root, "AI"),
                                    (category, "知识库"),
                                    ("4", "序号"),
                                    (number, "即将执行"),
                                    ("1", "已删除"),
                                ):
                                    await check.command(text, expected)
                                listing = await check.command(label, "知识库")
                                require(marker not in listing, "Deleted canary remains")
                                check.record(
                                    "knowledge_crud",
                                    personal=personal,
                                    native_ingress=True,
                                )
                        elif phase == "ordinary-security":
                            await check.security()
                        elif phase == "native-extra":
                            await check.native_extra()
                        elif phase == "escalation":
                            await check.advertisements(escalate=True)
                        elif phase == "finance":
                            await check.api(
                                "POST",
                                REGISTRATION,
                                {"action": "test_close", "instance_id": RECEIVER},
                            )
                            check.window, check.remaining = False, 0
                            process = await asyncio.create_subprocess_exec(
                                sys.executable,
                                "-m",
                                "scripts.check_wsl_finance_chat_live",
                                "--native",
                                "--extended",
                                cwd=ROOT,
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.STDOUT,
                            )
                            transcript, _ = await process.communicate()
                            (folder / "isolated-finance.log").write_bytes(transcript)
                            require(
                                process.returncode == 0,
                                "Isolated finance workflow failed",
                            )
                            check.record("isolated_finance_passed", real_payments=False)
                        elif phase == "lottery-member":
                            await check.lottery(controlled=True)
                        else:
                            await getattr(check, phase)()
                        evidence["phases"].append({"phase": phase, "passed": True})
                        if phase == "lottery":
                            evidence["phases"][-1].update(
                                scope="managed-account rejection and empty draw",
                                ordinary_enrollment_verified=False,
                                ordinary_winner_verified=False,
                            )
                        elif phase == "lottery-member":
                            evidence["phases"][-1].update(
                                scope="controlled native-member enrollment and winner; dashboard owner controls",
                                controlled_native_member=True,
                                owner_chat_verified=False,
                            )
                    except Exception as exc:
                        evidence["phases"].append(
                            {"phase": phase, "passed": False, "error": str(exc)}
                        )
                        print(
                            "PHASE FAILED",
                            phase,
                            type(exc).__name__,
                            str(exc),
                            flush=True,
                        )
                    finally:
                        await check.finish()
                        (output / "summary.json").write_text(
                            json.dumps(evidence, ensure_ascii=False, indent=2)
                        )
        finally:
            if granted:
                preview = await lab.preview("revoke")
                evidence["native_restore"] = await lab.execute(
                    preview["preview"], preview["confirmation"]
                )
                require(
                    evidence["native_restore"]["state"] == "verified",
                    "Role restore incomplete",
                )
            evidence["protected_unchanged"] = protected() == baseline
            require(
                evidence["protected_unchanged"],
                "Financial or formal group state changed",
            )
            (output / "summary.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2)
            )
            for path in output.rglob("*"):
                if path.is_file():
                    path.chmod(0o600)
            print("Private evidence:", output, flush=True)
    require(
        all(phase["passed"] for phase in evidence["phases"]),
        "One or more real phases failed; inspect the private summary",
    )


if __name__ == "__main__":
    asyncio.run(main())
