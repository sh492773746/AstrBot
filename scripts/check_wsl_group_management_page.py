"""Verify the grant UI with mock calls only, never production role changes."""

import asyncio
from pathlib import Path

from PIL import Image, ImageStat
from playwright.async_api import async_playwright

from scripts.check_wsl_policy_page import FIXTURE, ROOT

FIXTURE = FIXTURE.replace(
    'if (endpoint === "settings")',
    'if (endpoint === "group-managers") return {version:0,revision:"10001:1",users:[],members:[{id:"20001",name:"授权管理A"},{id:"20002",name:"很长很长很长很长的管理账号名称"}]};\nif (endpoint === "settings")',
).replace(
    'if (endpoint === "settings") {',
    'if (endpoint === "group-managers") return {version:1};\nif (endpoint === "settings") {',
)


async def main():
    """Inspect desktop/mobile layout, scoped save payload and nonblank images."""
    asset = (
        ROOT
        / "astrbot/builtin_stars/wangshangliao_moderation/pages/management/index.html"
    )
    output = Path("/opt/backups/wsl-rule-management-20261013")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path="/root/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome",
            headless=True,
            args=["--no-sandbox"],
        )
        for name, viewport in [
            ("desktop", {"width": 1440, "height": 1000}),
            ("mobile", {"width": 390, "height": 844}),
        ]:
            page = await browser.new_page(viewport=viewport)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.add_init_script(FIXTURE)
            await page.goto(asset.as_uri())
            await page.get_by_role("tab", name="群授权", exact=True).click()
            member = page.get_by_role("checkbox", name="授权管理A", exact=False)
            await member.wait_for()
            await member.check()
            await page.get_by_role("button", name="保存群管理员", exact=True).click()
            await page.get_by_text("群管理员已保存", exact=True).wait_for()
            requests = await page.evaluate("window.policyRequests")
            assert requests == [
                {
                    "endpoint": "group-managers",
                    "body": {
                        "bot_id": "fixture",
                        "group_id": "100001",
                        "users": ["20001"],
                        "version": 0,
                        "revision": "10001:1",
                    },
                }
            ]
            assert await page.evaluate(
                "document.documentElement.scrollWidth <= innerWidth"
            )
            for box in await page.locator(".group-managers button").all():
                assert await box.evaluate("e => e.scrollWidth <= e.clientWidth + 1")
            screenshot = output / f"group-managers-{name}.png"
            await page.screenshot(path=str(screenshot), full_page=True)
            with Image.open(screenshot) as image:
                assert max(ImageStat.Stat(image.convert("RGB")).stddev) > 10
            assert not errors, errors
            await page.close()
        await browser.close()
    print("PASS desktop/mobile group grant UI, fixed scoped payload; mocked API only.")


if __name__ == "__main__":
    asyncio.run(main())
