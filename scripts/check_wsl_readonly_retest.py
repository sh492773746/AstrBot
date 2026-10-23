"""Recheck deployed WSL pages and fixed private commands without business writes."""

import argparse
import asyncio
import copy
import json
import os
import time
from pathlib import Path

import aiohttp
import httpx
import jwt
from playwright.async_api import async_playwright

from astrbot.core.platform.sources.wangshangliao.business import (
    BusinessClient,
    BusinessError,
    Deployment,
)
from astrbot.core.platform.sources.wangshangliao.storage import Vault
from scripts.check_wsl_account_names import ROOT, snapshot
from scripts.check_wsl_authorization_live import rows
from scripts.check_wsl_tree_menu_live import (
    BOT,
    PREFIX,
    RECEIVER,
    REGISTRATION,
    SENDER,
    USER,
    MenuCheck,
)

READ_ACTIONS = {"status", "finance_status", "finance_members", "tenant_members"}
READ_ENDPOINTS = {
    "settings",
    "audit",
    "activities",
    "schedule",
    "friend-approval",
    "operations",
    "cashback-logs",
    "group-managers",
}
PAGE_SECTIONS = {
    "groups": ("open", "permissions", "friend-approval"),
    "ai": ("consultation", "group-ai", "plans", "model"),
    "finance": ("activities", "cashback", "finance"),
    "runtime": ("runtime", "schedule", "audit", "operations", "open-audit"),
    "help": ("commands", "examples", "architecture", "matrix"),
}
PRIVATE_CHECKS = (
    ("取消", "已取消"),
    ("菜单", "9. 群聊与热群AI"),
    ("999", "请按本页的数字选择"),
    ("8", "私聊AI咨询"),
    ("3", "当前套餐："),
    ("4", "次数为上限，同时受成本预算保护"),
    ("0", "9. 群聊与热群AI"),
    ("9", "群聊与热群AI"),
    ("0", "9. 群聊与热群AI"),
    ("取消", "已取消"),
    (
        "结账",
        ("暂无获授权", "暂时无法核实你的账号身份", "旺商聊暂不允许机器人核实成员"),
    ),
    (
        "结账结果",
        ("暂无获授权", "暂时无法核实你的账号身份", "旺商聊暂不允许机器人核实成员"),
    ),
    ("/exit", "已结束AI咨询"),
)


async def check_pages(client, output):
    """Inspect actual deployed UI, including denied member reads, at two sizes."""
    evidence = {"screens": [], "requests": [], "blocked_mutations": []}

    async def api(_source, method, endpoint, payload):
        permitted = (method == "GET" and endpoint in READ_ENDPOINTS) or (
            method == "POST"
            and endpoint == "open-service"
            and payload.get("action") in READ_ACTIONS
        )
        if not permitted:
            evidence["blocked_mutations"].append([method, endpoint])
            raise RuntimeError("Read-only acceptance forbids this operation")
        response = await client.request(
            method,
            PREFIX + endpoint,
            params=payload if method == "GET" else None,
            json=payload if method == "POST" else None,
        )
        result = response.json()
        evidence["requests"].append(
            {
                "method": method,
                "endpoint": endpoint,
                "action": payload.get("action", ""),
                "status": response.status_code,
            }
        )
        if response.is_error or result.get("status") != "ok":
            raise RuntimeError(result.get("message", "Read request failed"))
        return result.get("data")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"),
            args=["--no-sandbox"],
        )
        try:
            for width in (390, 1440):
                page = await browser.new_page(viewport={"width": width, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.expose_binding("deployedAPI", api)
                await page.add_init_script("""
                    Object.defineProperty(window, "AstrBotPluginPage", {value: {
                        ready: async () => {}, getLocale: () => "zh-CN",
                        onContext: () => () => {},
                        apiGet: (endpoint, payload = {}) =>
                            window.deployedAPI("GET", endpoint, payload),
                        apiPost: (endpoint, payload) =>
                            window.deployedAPI("POST", endpoint, payload)
                    }});
                """)
                await page.goto(
                    (
                        ROOT
                        / "astrbot/builtin_stars/wangshangliao_moderation/pages/management/index.html"
                    ).as_uri()
                )
                await page.locator("#section-finance").click()
                await page.locator("#tab-finance").click()
                await page.get_by_label("结算授权范围", exact=True).wait_for()
                finance = page.get_by_role("region", name="结算授权", exact=True)
                scope = finance.get_by_label("结算授权范围", exact=True)
                await scope.select_option(
                    json.dumps([BOT, "1459282"], separators=(",", ":"))
                )
                await page.wait_for_function(
                    "!document.querySelector('.finance-grants select')?.disabled"
                    " && !document.querySelector('.finance-grants')"
                    "?.textContent.includes('读取成员中')"
                )
                alerts = await finance.get_by_role("alert").all_text_contents()
                if alerts:
                    assert await finance.get_by_label(
                        "结算授权成员", exact=True
                    ).is_disabled()
                    assert await finance.get_by_role(
                        "button", name="保存结算授权", exact=True
                    ).is_disabled()
                else:
                    assert (
                        await finance.get_by_label("结算授权成员", exact=True)
                        .locator("option")
                        .count()
                        > 1
                    )
                assert not await page.get_by_role(
                    "button", name="开始登录", exact=True
                ).count()
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth"
                )
                assert not errors, errors
                evidence["screens"].append(
                    {
                        "width": width,
                        "member_read_alerts": alerts,
                        "page_errors": errors,
                    }
                )
                await page.screenshot(
                    path=str(output / f"readonly-page-{width}.png"), full_page=True
                )
                visited = []
                for section, tabs in PAGE_SECTIONS.items():
                    await page.locator("#section-" + section).click()
                    for tab in tabs:
                        await page.locator("#tab-" + tab).click()
                        await page.wait_for_timeout(400)
                        assert await page.evaluate(
                            "document.documentElement.scrollWidth <= innerWidth"
                        ), f"Overflow on {tab} at {width}px"
                        assert not errors, errors
                        visited.append(tab)
                        if tab in {"friend-approval", "commands", "operations"}:
                            await page.screenshot(
                                path=str(output / f"readonly-{tab}-{width}.png"),
                                full_page=True,
                            )
                evidence["screens"][-1]["visited_views"] = visited
                await page.close()
        finally:
            await browser.close()
    assert not evidence["blocked_mutations"], evidence
    return evidence


async def check_private(client, output):
    """Use the two fixed accounts for read-only menus; restore temporary send scope."""
    test = MenuCheck(client, output)
    evidence = {}
    operations = {
        instance: rows(instance, "moderation.sqlite3", "SELECT * FROM operations")
        for instance in (SENDER, RECEIVER)
    }
    try:
        for instance in (SENDER, RECEIVER):
            status = await test.api(
                "POST", REGISTRATION, {"action": "test_status", "instance_id": instance}
            )
            assert not status["window"]["active"], "Another test window is active"
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
        for text, expected in PRIVATE_CHECKS:
            await test.send(text, expected)
    finally:
        await test.restore()
        evidence.update(
            checks=len(test.results),
            passed=sum(result["passed"] for result in test.results),
            actions_unchanged=all(
                rows(instance, "moderation.sqlite3", "SELECT * FROM operations")
                == values
                for instance, values in operations.items()
            ),
            test_closed=not test.window,
            send_scope_restored=not test.changed,
            finance_denial_only=True,
        )
        output.joinpath("private-summary.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2)
        )
    assert evidence["actions_unchanged"], evidence
    return evidence


async def check_platform():
    """Read each fixed account's test-group directory once without authentication edits."""
    evidence = []
    deployment = Deployment.load()
    for instance in (RECEIVER, SENDER):
        async with aiohttp.ClientSession() as session:
            client = BusinessClient(deployment, session)
            client.restore(Vault(instance).load()["business"])
            try:
                result = await client.request(
                    "/v1/group/get-group-members", {"groupId": 1459282, "v": "0"}
                )
                evidence.append(
                    {
                        "instance": instance,
                        "accepted": True,
                        "shape": type(result).__name__,
                    }
                )
            except BusinessError as error:
                evidence.append(
                    {
                        "instance": instance,
                        "accepted": False,
                        "code": error.number,
                        "reason": str(error),
                    }
                )
        await asyncio.sleep(3.1)
    return evidence


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private", action="store_true")
    parser.add_argument("--platform", action="store_true")
    args = parser.parse_args()
    output = Path("/opt/backups") / time.strftime(
        "wsl-readonly-retest-%Y%m%dT%H%M%SZ", time.gmtime()
    )
    output.mkdir(mode=0o700)
    before = snapshot()
    config = json.loads((ROOT / "data/cmd_config.json").read_text("utf-8-sig"))
    token = jwt.encode(
        {"username": config["dashboard"]["username"], "exp": int(time.time()) + 900},
        config["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )
    evidence = {"started": time.time(), "production_writes": False}
    try:
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:6185",
            headers={"Authorization": "Bearer " + token},
            timeout=30,
            trust_env=False,
        ) as client:
            response = await client.post(
                PREFIX + "open-service", json={"action": "status", "payload": {}}
            )
            response.raise_for_status()
            state = response.json()["data"]
            expected = {
                item["id"]: str(item["account_id"])
                for item in config["platform"]
                if item["type"] == "wangshangliao" and item.get("enable", True)
            }
            assert {
                item["instance_id"]: item["account"] for item in state["accounts"]
            } == expected
            assert all(item["state"] == "online" for item in state["accounts"])
            assert all(
                group["paused"] or not group["active"]
                for group in state["groups"]
                if group["group_id"] in {"1516387", "1488006"}
            )
            evidence["runtime"] = {
                key: state.get(key)
                for key in (
                    "mode",
                    "allocated_count",
                    "operating_count",
                    "queue_health",
                    "health",
                )
            }
            evidence["runtime"]["ai_hard_budget_confirmed"] = state["ai_budget"][
                "confirmed"
            ]
            response = await client.post(
                PREFIX + "open-service",
                json={"action": "finance_status", "payload": {}},
            )
            response.raise_for_status()
            assert not any(
                row["active"] and row["uid"] == USER and row["account"] == BOT
                for row in response.json()["data"]["grants"]
            ), "Read-only denial test cannot run with active finance permission"
            evidence["friends"] = {}
            for instance in (SENDER, RECEIVER):
                response = await client.get(
                    PREFIX + "friend-approval", params={"bot_id": instance}
                )
                response.raise_for_status()
                evidence["friends"][instance] = {
                    key: response.json()["data"].get(key)
                    for key in (
                        "enabled",
                        "active",
                        "paused_reason",
                        "pending",
                        "accepted",
                        "read_failures",
                    )
                }
            if args.platform:
                evidence["platform"] = await check_platform()
            evidence["pages"] = await check_pages(client, output)
            if args.private:
                evidence["private"] = await check_private(client, output)
        async with httpx.AsyncClient(trust_env=False, timeout=15) as anonymous:
            evidence["anonymous"] = {}
            for action in ("me", "groups", "audit"):
                response = await anonymous.post(
                    "http://127.0.0.1:6185" + PREFIX + "cashier",
                    json={"action": action, "payload": {}},
                )
                evidence["anonymous"]["cashier_" + action] = response.status_code
                assert response.status_code in {401, 403}
            response = await anonymous.post(
                "http://127.0.0.1:6185" + PREFIX + "open-service",
                json={"action": "finance_status", "payload": {}},
            )
            evidence["anonymous"]["finance_status"] = response.status_code
            assert response.status_code in {401, 403}
    finally:
        evidence.update(finished=time.time(), protected_unchanged=before == snapshot())
        output.joinpath("evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2)
        )
        print("Read-only retest evidence:", output, flush=True)
    assert evidence["protected_unchanged"], evidence


if __name__ == "__main__":
    asyncio.run(main())
