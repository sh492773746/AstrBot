"""Check staged UI with read-only production data and isolated runtime fixtures."""

import argparse
import asyncio
import hashlib
import json
import subprocess
import time
from pathlib import Path

import httpx
import jwt
from playwright.async_api import async_playwright

from astrbot.builtin_stars.wangshangliao_moderation.component_governance import (
    ACCOUNTS,
    GROUP,
)
from astrbot.builtin_stars.wangshangliao_moderation.rule_replies import (
    RuleConfiguration,
)
from astrbot.builtin_stars.wangshangliao_moderation.service_settings import (
    SupportConfiguration,
)

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "/api/v1/plugins/extensions/wangshangliao_moderation/"


async def check(output):
    """Inspect runtime, rules and support without sending native or model requests."""
    config_path = ROOT / "data/cmd_config.json"
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    digest = hashlib.sha256(config_path.read_bytes()).hexdigest()
    pid_before = (
        await asyncio.to_thread(
            subprocess.check_output,
            ["systemctl", "show", "astrbot.service", "--property=MainPID", "--value"],
            text=True,
        )
    ).strip()
    token = jwt.encode(
        {"username": config["dashboard"]["username"], "exp": int(time.time()) + 600},
        config["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )
    headers = {"Authorization": "Bearer " + token}
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:6185",
        headers=headers,
        timeout=30,
        trust_env=False,
    ) as client:

        async def post(action, payload):
            response = await client.post(
                PREFIX + "open-service", json={"action": action, "payload": payload}
            )
            response.raise_for_status()
            body = response.json()
            assert body["status"] == "ok", body.get("message")
            return body["data"]

        live = await post("status", {})
        runtime_response = await client.get("/api/v1/plugins/runtime/status")
        report = {
            "accounts": live["accounts"],
            "test_groups": [
                {
                    key: row.get(key)
                    for key in (
                        "account",
                        "group_id",
                        "active",
                        "present",
                        "native_role",
                    )
                }
                for row in live["groups"]
                if row["group_id"] == GROUP
            ],
            "budget_confirmed": bool(live.get("ai_budget", {}).get("confirmed")),
            "config_sha256": digest,
            "pid_before": pid_before,
            "native_sends": 0,
            "model_calls": 0,
            "runtime_data": "isolated fixtures; not a live pilot",
            "production_runtime_api_status": runtime_response.status_code,
            "screens": [],
        }

        async def api(_source, method, endpoint, payload):
            if method == "GET":
                assert endpoint in {"settings", "audit", "activities", "schedule"}, (
                    endpoint
                )
                response = await client.get(PREFIX + endpoint, params=payload)
                response.raise_for_status()
                return response.json()["data"]
            assert endpoint == "open-service", endpoint
            action, payload = payload["action"], payload.get("payload", {})
            if action == "component_policy":
                assert not payload
                return {
                    "enabled": False,
                    "version": 1,
                    "accounts": sorted(ACCOUNTS),
                    "group": GROUP,
                }
            if action == "component_status":
                return {
                    "entries": [
                        {
                            "capability": "wsl.private_ai",
                            "scope": ["provider", "offline-fixture"],
                            "reference": "offline",
                            "state": "open",
                            "version": 3,
                            "failures": 3,
                            "in_flight": 0,
                            "retry_after": 30,
                            "plugin": "wangshangliao_moderation",
                            "error": "TimeoutError",
                        }
                    ]
                }
            if action == "page_schema":
                model = {
                    "business_rules": RuleConfiguration,
                    "support_settings": SupportConfiguration,
                }[payload["name"]]
                return {
                    "schema_version": 1,
                    "id": "fixture",
                    "plugin": "wangshangliao_moderation",
                    "capability": "fixture",
                    "fields": model.model_json_schema(),
                }
            if action == "support_settings":
                assert payload["operation"] == "read"
                return {"version": 0, "config": SupportConfiguration().model_dump()}
            if action == "support_editor":
                assert payload["operation"] == "read"
                return {"editors": []}
            if action == "business_rules":
                assert payload["operation"] == "read"
                return {"version": 0, "config": RuleConfiguration().model_dump()}
            assert action in {"status", "tenant_members"}, action
            return await post(action, payload)

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                executable_path="/root/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome",
                headless=True,
                args=["--no-sandbox"],
            )
            for width in (1440, 390):
                page = await browser.new_page(viewport={"width": width, "height": 960})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.expose_binding("stagedAPI", api)
                await page.add_init_script("""
                    Object.defineProperty(window,"AstrBotPluginPage",{value:{
                        ready:async()=>{},getLocale:()=> "zh-CN",onContext:()=>()=>{},
                        apiGet:(endpoint,payload={})=>window.stagedAPI("GET",endpoint,payload),
                        apiPost:(endpoint,payload)=>window.stagedAPI("POST",endpoint,payload)
                    }});
                """)
                await page.goto((output / "page/index.html").as_uri())
                await page.get_by_role("tab", name="运行与日志", exact=True).click()
                await page.get_by_role("tab", name="服务总控", exact=True).click()
                await page.get_by_label("受控试点", exact=True).wait_for()
                assert not await page.get_by_label("受控试点", exact=True).is_checked()
                await page.get_by_role("button", name="核对后恢复", exact=True).click()
                await page.get_by_label("核对记录", exact=True).fill(
                    "离线页面验收，不提交恢复"
                )
                assert await page.get_by_role(
                    "button", name="确认恢复", exact=True
                ).is_enabled()
                await page.screenshot(
                    path=str(output / f"runtime-{width}.png"), full_page=True
                )
                report["screens"].append(f"runtime-{width}.png")
                await page.get_by_role("button", name="取消", exact=True).click()
                await page.get_by_role("tab", name="AI服务", exact=True).click()
                await page.get_by_role("tab", name="客服咨询", exact=True).click()
                await page.get_by_label("业务话术", exact=True).wait_for()
                assert (
                    await page.get_by_label("回复长度（字）", exact=True).get_attribute(
                        "max"
                    )
                    == "2000"
                )
                assert (
                    await page.get_by_label("业务话术", exact=True).get_attribute(
                        "maxlength"
                    )
                    == "4000"
                )
                await page.screenshot(
                    path=str(output / f"support-{width}.png"), full_page=True
                )
                report["screens"].append(f"support-{width}.png")
                await page.get_by_role("tab", name="群管理", exact=True).click()
                await page.get_by_role("tab", name="开通与规则", exact=True).click()
                await page.get_by_label("群聊", exact=True).select_option(
                    "43506527/" + GROUP
                )
                rules = page.get_by_role("region", name="群关键词回复与定时广告")
                if not await rules.count():
                    rules = page.locator(".automation")
                await rules.get_by_role(
                    "button", name="添加关键词回复", exact=True
                ).click()
                await rules.get_by_label("名称", exact=True).first.fill("离线规则草稿")
                assert (
                    await rules.get_by_label("名称", exact=True).first.get_attribute(
                        "maxlength"
                    )
                    == "40"
                )
                assert (
                    await rules.get_by_label("冷却（秒）", exact=True).get_attribute(
                        "max"
                    )
                    == "86400"
                )
                await rules.get_by_label("回复正文", exact=True).fill(
                    "离线验收，不发送"
                )
                assert (
                    await rules.get_by_label("回复正文", exact=True).get_attribute(
                        "maxlength"
                    )
                    == "2000"
                )
                await rules.get_by_role(
                    "button", name="添加广告计划", exact=True
                ).click()
                await rules.get_by_label("名称", exact=True).nth(1).fill("离线广告草稿")
                await rules.get_by_label("发送计划").select_option("interval")
                assert (
                    await rules.get_by_label("间隔（分钟）", exact=True).get_attribute(
                        "min"
                    )
                    == "5"
                )
                assert (
                    await rules.get_by_label("间隔（分钟）", exact=True).get_attribute(
                        "max"
                    )
                    == "1440"
                )
                await rules.get_by_label("文字与链接", exact=True).fill(
                    "离线验收，不投放"
                )
                for toggle in await rules.get_by_label("启用", exact=True).all():
                    assert not await toggle.is_checked()
                await page.screenshot(
                    path=str(output / f"rules-{width}.png"), full_page=True
                )
                report["screens"].append(f"rules-{width}.png")
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth"
                )
                assert not errors, errors
                await page.close()
            await browser.close()
        report["config_unchanged"] = (
            digest == hashlib.sha256(config_path.read_bytes()).hexdigest()
        )
        assert report["config_unchanged"]
        report["pid_after"] = (
            await asyncio.to_thread(
                subprocess.check_output,
                [
                    "systemctl",
                    "show",
                    "astrbot.service",
                    "--property=MainPID",
                    "--value",
                ],
                text=True,
            )
        ).strip()
        assert report["pid_after"] == pid_before
        sources = [
            "astrbot/core/star/components.py",
            "astrbot/core/star/component_runtime.py",
            "astrbot/core/star/star_manager.py",
            "astrbot/api/star/__init__.py",
            "astrbot/dashboard/api/plugins.py",
            "astrbot/core/pipeline/process_stage/method/star_request.py",
            "astrbot/core/platform/sources/wangshangliao/database.py",
            *[
                "astrbot/builtin_stars/wangshangliao_moderation/" + name + ".py"
                for name in (
                    "component_governance",
                    "private_batches",
                    "command_capabilities",
                    "syntax",
                    "open_service",
                    "open_rpc",
                    "group_ai",
                    "pc28",
                    "rule_replies",
                    "service_settings",
                    "schedules",
                    "activities",
                    "dashboard",
                    "main",
                )
            ],
        ]
        report["source_sha256"] = {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in sources
        }
        (output / "browser-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2)
        )
        print(
            "PASS staged desktop/mobile runtime and support; no native sends or model calls."
        )
        print("Native test-group authority:", report["test_groups"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    asyncio.run(check(parser.parse_args().output))
