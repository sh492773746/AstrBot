"""Recheck deployed group-management pages without business writes."""

import argparse
import asyncio
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
from scripts.check_wsl_tree_menu_live import (
    PREFIX,
    RECEIVER,
    SENDER,
)

READ_ACTIONS = {"status", "finance_status", "tenant_members", "page_schema"}
READ_ENDPOINTS = {
    "settings",
    "audit",
    "activities",
    "schedule",
    "operations",
    "cashback-logs",
    "group-managers",
    "speech-activity",
    "video-relay",
}
PAGE_SECTIONS = {
    "groups": ("open", "rules", "permissions", "speech-activity", "video-relay"),
    "ai": ("group-ai", "plans", "model"),
    "finance": ("activities", "invite-rewards", "cashback"),
    "runtime": ("runtime", "schedule", "audit", "operations", "open-audit"),
    "help": ("commands", "examples", "architecture", "matrix"),
}


async def check_pages(client, output):
    """Inspect current deployed group pages at mobile and desktop widths."""
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
            for width in (320, 390, 1440):
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
                        if tab in {"rules", "commands", "operations"}:
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
            if args.platform:
                evidence["platform"] = await check_platform()
            evidence["pages"] = await check_pages(client, output)
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
