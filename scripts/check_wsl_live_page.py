"""Read-only desktop/mobile screenshots of the deployed plugin, without fixtures."""

import asyncio
import json
import time
from pathlib import Path

import jwt
from playwright.async_api import async_playwright


async def main():
    """Load the live iframe and inspect tabs without changing settings."""
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "data/cmd_config.json").read_text(encoding="utf-8-sig"))
    token = jwt.encode(
        {"username": config["dashboard"]["username"], "exp": int(time.time()) + 600},
        config["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )
    output = Path("/opt/backups") / time.strftime(
        "wsl-deployed-page-%Y%m%dT%H%M%S", time.gmtime()
    )
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/root/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome",
            headless=True,
            args=["--no-sandbox"],
        )
        for name, width in (("desktop", 1440), ("mobile", 390)):
            page = await browser.new_page(viewport={"width": width, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.add_init_script(
                "if (window === window.top) {"
                f"localStorage.setItem('token',{json.dumps(token)});"
                "localStorage.setItem('astrbot-locale','zh-CN');"
                "localStorage.setItem('astrbot:first_notice_seen:v1','1');"
                "}"
            )

            async def read_only(route):
                """Allow only authenticated reads and the fixed status operation.

                Args:
                    route: Browser request intercepted before network dispatch.
                """
                request = route.request
                if request.method in {"GET", "HEAD", "OPTIONS"} or (
                    request.method == "POST"
                    and request.url.endswith(
                        "/api/v1/plugins/extensions/"
                        "wangshangliao_moderation/open-service"
                    )
                    and request.post_data_json == {"action": "status", "payload": {}}
                ):
                    await route.continue_()
                else:
                    await route.abort()

            await page.route("**/api/**", read_only)
            await page.goto(
                "http://127.0.0.1:6185/#/plugin-page/wangshangliao_moderation/management"
            )
            frame = page.frame_locator("iframe.plugin-page-frame")
            await frame.get_by_role("heading", name="旺商聊群管", exact=True).wait_for(
                timeout=30000
            )
            await frame.get_by_text("开放服务运行中", exact=True).wait_for()
            tabs = frame.get_by_role("tab")
            count = await tabs.count()
            for label in ("架构表", "权限表", "群管理"):
                await frame.get_by_role("tab", name=label, exact=True).click()
                await page.wait_for_timeout(350)
            shot = output / f"{name}.png"
            await page.screenshot(path=str(shot), full_page=True)
            shot.chmod(0o600)
            assert not errors, errors
            assert await page.evaluate(
                "document.documentElement.scrollWidth <= innerWidth"
            )
            inner = next(
                f for f in page.frames if f != page.main_frame and "plugin" in f.url
            )
            assert await inner.evaluate(
                "document.documentElement.scrollWidth <= innerWidth"
            )
            print("PASS live page:", name, count, "tabs;", shot, flush=True)
            await page.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
