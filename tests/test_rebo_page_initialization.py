"""Browser checks using fake bridge responses; no real account actions."""

from pathlib import Path

import pytest
from playwright.async_api import async_playwright


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    ["ready", "missing", "unauthorized", "blocked", "timeout", "mutation_timeout"],
)
async def test_page_feedback(mode):
    root = Path("data/plugins/astrbot_plugin_rebo_live/pages/settings")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path="/root/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome",
            headless=True,
            args=["--no-sandbox"],
        )
        page = await browser.new_page()
        await page.add_init_script("""
            const original = window.setTimeout;
            window.setTimeout = (fn,ms,...args) => original(fn,ms>=12000?200:ms,...args);
        """)
        if mode not in {"missing", "blocked"}:
            ready = "new Promise(()=>{})" if mode == "timeout" else "Promise.resolve()"
            status = (
                "Promise.reject(new Error('401 Unauthorized'))"
                if mode == "unauthorized"
                else "Promise.resolve({account:'已登录',rooms:{},visible:{},grants:{}})"
            )
            await page.add_init_script(
                f"window.AstrBotPluginPage={{ready:()=>{ready},apiGet:()=>{status},apiPost:()=>new Promise(()=>{{}})}};"
            )

        async def route(request):
            name = request.request.url.rsplit("/", 1)[-1] or "index.html"
            if mode == "blocked" and name == "app.js":
                await request.abort()
                return
            await request.fulfill(
                body=(root / name).read_text(),
                content_type="text/html"
                if name.endswith(".html")
                else "application/javascript",
            )

        await page.route("http://rebo.test/**", route)
        await page.goto("http://rebo.test/index.html")
        await page.wait_for_timeout(450)
        if mode == "mutation_timeout":
            await page.locator("#login").click()
            await page.wait_for_timeout(450)
            assert "可能已经执行" in await page.locator("#notice").inner_text()
        if mode == "ready":
            assert await page.locator("#login").is_enabled()
            assert "已就绪" in await page.locator("#page-state").inner_text()
        else:
            assert await page.locator("#login").is_disabled()
            assert await page.locator("#reload-page").is_visible()
            assert "⚠️" in await page.locator("#page-state").inner_text()
        await browser.close()
