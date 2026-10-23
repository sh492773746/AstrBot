"""Opt-in chat finance acceptance; real transfers and production writes are forbidden."""

import argparse
import asyncio
import copy
import json
import re
import time
import uuid
from pathlib import Path

import httpx
import jwt

from scripts.check_wsl_account_names import ROOT, snapshot
from scripts.check_wsl_tree_menu_live import (
    BOT,
    PREFIX,
    REGISTRATION,
    SENDER,
    MenuCheck,
)


async def main():
    """Inspect live guards, optionally reload the plugin and observe stability."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--native", action="store_true")
    parser.add_argument("--extended", action="store_true")
    parser.add_argument("--observe-seconds", type=int, default=0)
    args = parser.parse_args()
    if args.extended and not args.native:
        parser.error("--extended requires the isolated native rehearsal")
    if not 0 <= args.observe_seconds <= 3600:
        parser.error("Observation must be between zero and one hour")
    output = Path("/opt/backups") / time.strftime(
        "wsl-chat-finance-live-%Y%m%dT%H%M%SZ", time.gmtime()
    )
    output.mkdir(mode=0o700)
    evidence = {
        "started": time.time(),
        "native_passed": False,
        "production_writes": False,
        "samples": [],
    }
    before = snapshot()
    config = json.loads((ROOT / "data/cmd_config.json").read_text("utf-8-sig"))
    token = jwt.encode(
        {"username": config["dashboard"]["username"], "exp": int(time.time()) + 7200},
        config["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:6185",
        trust_env=False,
        headers={"Authorization": "Bearer " + token},
        timeout=60,
    ) as client:
        test = MenuCheck(client, output)
        try:
            if args.reload:
                response = await client.post(
                    "/api/v1/plugins/wangshangliao_moderation/reload"
                )
                response.raise_for_status()
                assert response.json()["status"] == "ok"
                await asyncio.sleep(5)
            status = await test.api(
                "POST",
                PREFIX + "open-service",
                {"action": "finance_status", "payload": {}},
            )
            evidence["initial_grant_count"] = len(status["grants"])
            evidence["grants_before"] = status["grants"]
            response = await client.post(
                PREFIX + "open-service",
                json={
                    "action": "finance_members",
                    "payload": {"account": BOT, "group_id": "1459282"},
                },
            )
            evidence["directory"] = {
                "http_status": response.status_code,
                "message": response.json().get("message", ""),
                "verified": response.status_code == 200
                and response.json().get("status") == "ok",
            }
            if args.native and evidence["directory"]["verified"]:
                # The ordinary helper grants no financial permission. All writes
                # below are routed to the explicitly marked, separate rehearsal.
                original_send = test.api

                async def api(method, path, body=None):
                    if (
                        path == REGISTRATION
                        and body
                        and body.get("action") == "test_open"
                    ):
                        body = {
                            **body,
                            "groups": ["1459282"],
                            "scopes": ["private_commands", "private_ai"],
                        }
                    return await original_send(method, path, body)

                test.api = api
                current = await test.settings()
                test.original = copy.deepcopy(current["proactive_send"])
                test.proposed = copy.deepcopy(test.original)
                test.proposed["enabled"] = True
                test.proposed["targets"] = list(
                    dict.fromkeys(test.proposed.get("targets", []) + [test.target])
                )
                test.changed = True
                await test.api(
                    "POST",
                    PREFIX + "settings",
                    {
                        "bot_id": SENDER,
                        "revision": current["revision"],
                        "proactive_send": test.proposed,
                    },
                )
                await test.send("结账", "暂无获授权")
                opened = await test.api(
                    "POST",
                    PREFIX + "open-service",
                    {
                        "action": "finance_test",
                        "payload": {
                            "active": True,
                            "version": status["test_version"],
                            "request_id": uuid.uuid4().hex,
                        },
                    },
                )
                evidence["test_window"] = opened
                for text, expected in (
                    ("结账", "测试账本"),
                    ("1", "待结账"),
                    ("1", "测试账本"),
                    ("1", "成员明细"),
                ):
                    await test.send(text, expected)
                member = re.search(
                    r"(?m)^([1-9][0-9]*)\. 测试成员1（账号99000001） ¥5\.00 未发言$",
                    test.results[-1]["reply"],
                )
                assert member, "Exact synthetic member missing from numbered page"
                await test.send(member[1], "登记已付款")
                await test.send("1", "请核对")
                # Only a labeled, synthetic-ledger preview is confirmable here.
                preview = test.results[-1]["reply"]
                assert all(
                    expected in preview
                    for expected in (
                        "【测试账本，不代表真实付款】",
                        "群：测试账本（模拟邀请及时间）",
                        "账号31846829",
                        "本次1笔，合计¥5.00",
                        "成员：测试成员1",
                        "仅登记，不会转账",
                    )
                ), "Refusing confirmation outside the exact synthetic fixture"
                code = re.search(r"确认已付款 ([0-9]{6})", preview)[1]
                await test.send("确认已付款 " + code, "已完成")
                await test.send("结账结果", "已完成")
                evidence["native_passed"] = True
                if args.extended:
                    await test.send("确认已付款 " + code, "已完成")
                    for section, action, reason, amount, target in (
                        ("3", "2", "测试撤回登记，不涉及真实付款", "5.00", None),
                        ("1", "4", "测试代理价格，不涉及正式账本", "5.00", "10.00"),
                        ("1", "3", "测试恢复普通价格，不涉及正式账本", "10.00", "5.00"),
                    ):
                        for text, expected in (
                            ("结账", "测试账本"),
                            ("1", "待结账"),
                            (section, "账号31846829"),
                            ("1", "成员明细"),
                        ):
                            await test.send(text, expected)
                        member = re.search(
                            r"(?m)^([1-9][0-9]*)\. 测试成员1（账号99000001） ¥(?:5|10)\.00 未发言$",
                            test.results[-1]["reply"],
                        )
                        assert member, "Exact synthetic member missing from page"
                        await test.send(member[1], "登记已付款")
                        await test.send(action, "填写修改原因")
                        await test.send(reason, "请核对")
                        preview = test.results[-1]["reply"]
                        assert all(
                            expected in preview
                            for expected in (
                                "【测试账本，不代表真实付款】",
                                "群：测试账本（模拟邀请及时间）",
                                "账号31846829",
                                f"本次1笔，合计¥{amount}",
                                "成员：测试成员1",
                            )
                        ), "Refusing modification outside the exact synthetic fixture"
                        if target is not None:
                            assert f"\n切换后合计¥{target}\n" in preview
                        code = re.search(r"确认修改 ([0-9]{6})", preview)[1]
                        await test.send("确认修改 " + code, "已完成")
                    for text, expected in (
                        ("结账", "测试账本"),
                        ("1", "待结账"),
                        ("7", "姓名"),
                        ("时间 本月", "待结账"),
                        ("1", "共3笔 ¥15.00"),
                        ("0", "待结账"),
                        ("9", "普通 ¥5.00；代理 ¥10.00"),
                        ("0", "待结账"),
                        ("10", "风控复核"),
                        ("取消", "已退出结账"),
                    ):
                        await test.send(text, expected)
                    evidence["extended_passed"] = True
                else:
                    await test.send("取消", "已退出结账")
            elif args.native:
                evidence["native_blocked"] = (
                    "Fresh platform member identity unavailable; no grants or rehearsal created."
                )
            deadline = time.monotonic() + args.observe_seconds
            while True:
                live = await test.api(
                    "POST", PREFIX + "open-service", {"action": "status", "payload": {}}
                )
                formal = [
                    t for t in live["groups"] if t["group_id"] in {"1516387", "1488006"}
                ]
                assert all(t["paused"] or not t["active"] for t in formal)
                evidence["samples"].append(
                    {
                        "at": time.time(),
                        "accounts": [
                            {k: a[k] for k in ("account", "state")}
                            for a in live["accounts"]
                        ],
                        "queue_health": live["queue_health"],
                        "formals_paused": True,
                    }
                )
                output.joinpath("evidence.json").write_text(
                    json.dumps(evidence, ensure_ascii=False, indent=2)
                )
                if time.monotonic() >= deadline:
                    break
                await asyncio.sleep(min(30, deadline - time.monotonic()))
        finally:
            cleanup_errors = []
            try:
                if evidence.get("test_window"):
                    await test.api(
                        "POST",
                        PREFIX + "open-service",
                        {
                            "action": "finance_test",
                            "payload": {
                                "active": False,
                                "version": evidence["test_window"]["version"],
                                "request_id": uuid.uuid4().hex,
                            },
                        },
                    )
            except Exception as exc:
                cleanup_errors.append(type(exc).__name__)
            finally:
                try:
                    await test.restore()
                except Exception as exc:
                    cleanup_errors.append(type(exc).__name__)
            final_status = await test.api(
                "POST",
                PREFIX + "open-service",
                {"action": "finance_status", "payload": {}},
            )
            evidence["grants_after"] = final_status["grants"]
            before_grants = evidence.get("grants_before", [])
            evidence["grants_unchanged"] = before_grants == final_status["grants"]
            after = snapshot()
            evidence.update(
                finished=time.time(),
                protected_before=before,
                protected_after=after,
                protected_unchanged=before == after,
                test_window_closed=not test.window,
                financial_grants_created=sum(
                    grant not in before_grants for grant in final_status["grants"]
                ),
                cleanup_errors=cleanup_errors,
            )
            output.joinpath("evidence.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2)
            )
            print(
                json.dumps(
                    {
                        "output": str(output),
                        "native_passed": evidence["native_passed"],
                        "directory": evidence.get("directory"),
                        "protected_unchanged": evidence["protected_unchanged"],
                        "observation_samples": len(evidence["samples"]),
                    },
                    ensure_ascii=False,
                )
            )
            assert evidence["protected_unchanged"], "Protected production state changed"
            assert evidence["grants_unchanged"], "Native financial grants changed"
            assert not cleanup_errors, "Rehearsal cleanup incomplete"


if __name__ == "__main__":
    asyncio.run(main())
