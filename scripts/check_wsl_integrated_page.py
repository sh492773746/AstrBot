"""Inspect the deployed merged UI through authenticated read-only API calls."""

import asyncio
import json
import time
from pathlib import Path

import httpx
import jwt
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "/api/v1/plugins/extensions/wangshangliao_moderation/"


async def main():
    """Verify real deployed account and group data without changing grants or rules.

    Raises:
        AssertionError: If the merged view or API authorization is inconsistent.
    """
    config = json.loads((ROOT / "data/cmd_config.json").read_text(encoding="utf-8-sig"))
    output = Path("/opt/backups") / time.strftime(
        "wsl-integrated-page-%Y%m%dT%H%M%S", time.gmtime()
    )
    output.mkdir(mode=0o700)
    token = jwt.encode(
        {"username": config["dashboard"]["username"], "exp": int(time.time()) + 300},
        config["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:6185",
        timeout=30,
        trust_env=False,
        headers={"Authorization": "Bearer " + token},
    ) as client:

        async def api(_source, method, endpoint, payload):
            if method == "POST":
                assert endpoint == "open-service" and payload["action"] in {
                    "status",
                    "tenant_members",
                }
                response = await client.post(PREFIX + endpoint, json=payload)
            else:
                assert endpoint in {"settings", "audit", "activities", "schedule"}
                response = await client.get(PREFIX + endpoint, params=payload)
            response.raise_for_status()
            return response.json()["data"]

        response = await client.post(
            PREFIX + "open-service", json={"action": "status", "payload": {}}
        )
        response.raise_for_status()
        state = response.json()["data"]
        assert state["mode"] == "integrated"
        assert all(a["state"] == "online" for a in state["accounts"])
        group = next(
            t for t in state["groups"] if t["active"] and t["account"] == "43506527"
        )
        async with httpx.AsyncClient(trust_env=False) as anonymous:
            assert (
                await anonymous.get("http://127.0.0.1:6185" + PREFIX + "open-service")
            ).status_code in {401, 403}
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                executable_path="/root/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome",
                headless=True,
                args=["--no-sandbox"],
            )
            for width in (390, 1440):
                page = await browser.new_page(viewport={"width": width, "height": 900})
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                await page.expose_binding("deployedAPI", api)
                await page.add_init_script("""
                    Object.defineProperty(window,"AstrBotPluginPage",{value:{
                        ready:async()=>{},getLocale:()=> "zh-CN",onContext:()=>()=>{},
                        apiGet:(endpoint,payload={})=>window.deployedAPI("GET",endpoint,payload),
                        apiPost:(endpoint,payload)=>window.deployedAPI("POST",endpoint,payload)
                    }});
                """)
                await page.goto(
                    (
                        ROOT
                        / "astrbot/builtin_stars/wangshangliao_moderation/pages/management/index.html"
                    ).as_uri()
                )
                try:
                    await page.get_by_text("开放服务运行中", exact=True).wait_for()
                except Exception:
                    print("Page errors:", errors)
                    print(
                        "Page alerts:",
                        await page.locator("[role=alert]").all_text_contents(),
                    )
                    await page.screenshot(
                        path=str(output / f"merged-live-error-{width}.png"),
                        full_page=True,
                    )
                    raise
                section = page.locator(".open-service")
                await section.get_by_label("群聊", exact=True).select_option(
                    group["account"] + "/" + group["group_id"]
                )
                await section.get_by_label("查找成员", exact=True).wait_for(
                    state="visible"
                )
                await page.wait_for_function(
                    "!document.querySelector('.member-grants input[type=search]').disabled"
                )
                assert (
                    await section.locator(".member-list input[type=checkbox]").count()
                    > 0
                )
                assert (
                    await section.get_by_label("AI咨询模型", exact=True).input_value()
                    == state["model"]["provider_id"]
                )
                assert (
                    await page.get_by_role(
                        "button", name="开始登录", exact=True
                    ).count()
                    == 0
                )
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth"
                )
                assert not errors, errors
                destination = output / f"merged-live-{width}.png"
                await page.screenshot(path=str(destination), full_page=True)
                await page.close()
            await browser.close()
        print(
            "PASS deployed merged UI at 390/1440px; live account/member data, no mutations."
        )
        print("PASS anonymous total-control access denied; no duplicate login UI.")


if __name__ == "__main__":
    asyncio.run(main())
