"""Responsive AI profile controls against a write-denying local fixture."""

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[1]


async def main():
    """Start a local fixture, inspect both viewports, then stop the server."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--built-page", type=Path)
    args = parser.parse_args()
    built = args.built_page.read_text("utf-8") if args.built_page else None
    with tempfile.TemporaryDirectory(prefix="wsl-ai-ui-") as directory:
        out = Path(directory)
        with (out / "vite.log").open("w") as log:
            server = await asyncio.create_subprocess_exec(
                *[
                    "node",
                    "node_modules/vite/bin/vite.js",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "3027",
                    "--strictPort",
                ],
                cwd=ROOT,
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch(
                        executable_path="/root/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome",
                        headless=True,
                        args=["--no-sandbox"],
                    )
                    try:
                        for attempt in range(100):
                            try:
                                request = await playwright.request.new_context()
                                try:
                                    response = await request.get(
                                        "http://127.0.0.1:3027"
                                    )
                                    if response.ok:
                                        break
                                finally:
                                    await request.dispose()
                            except Exception:
                                pass
                            if server.returncode is not None:
                                raise RuntimeError("Fixture Vite exited")
                            await asyncio.sleep(0.1)
                        else:
                            raise RuntimeError("Fixture Vite did not start")
                        for width in (390, 1440):
                            page = await browser.new_page(
                                viewport={"width": width, "height": 1000},
                            )
                            page.set_default_timeout(8000)
                            errors = []
                            page.on(
                                "pageerror", lambda error: errors.append(str(error))
                            )
                            await page.route("**/api/**", lambda route: route.abort())
                            await page.goto(
                                "http://127.0.0.1:3027/tests/fixtures/wangshangliao-management.html",
                                wait_until="domcontentloaded",
                                timeout=30000,
                            )
                            if built is not None:
                                await page.locator("#section-ai").wait_for(
                                    timeout=30000
                                )
                                assets = await page.evaluate(
                                    """html => {
                                      const doc = new DOMParser().parseFromString(html, "text/html");
                                      return {
                                        styles: [...doc.querySelectorAll("style")].map(x => x.textContent),
                                        scripts: [...doc.querySelectorAll("script:not([data-astrbot-bridge])")]
                                          .filter(x => !x.src).map(x => x.textContent),
                                      };
                                    }""",
                                    built,
                                )
                                assert len(assets["scripts"]) == 1
                                await page.evaluate(
                                    """() => {
                                      document.querySelector("#app").__vue_app__.unmount();
                                      document.querySelectorAll('style,link[rel="stylesheet"]')
                                        .forEach(x => x.remove());
                                      window.compiledFixtureCalls = [];
                                      const bridge = window.AstrBotPluginPage;
                                      for (const key of ["apiGet", "apiPost"]) {
                                        const original = bridge[key];
                                        bridge[key] = (...args) => {
                                          window.compiledFixtureCalls.push({
                                            endpoint: args[0], action: args[1]?.action,
                                          });
                                          return original(...args);
                                        };
                                      }
                                    }"""
                                )
                                for style in assets["styles"]:
                                    await page.add_style_tag(content=style)
                                await page.add_script_tag(content=assets["scripts"][0])
                            await page.locator("#section-ai").click()
                            for retired in (
                                "consultation",
                                "admin-guidance",
                                "friend-approval",
                            ):
                                await expect(
                                    page.locator(f"#tab-{retired}")
                                ).to_have_count(0)
                            await page.locator("#tab-group-ai").click()
                            await page.get_by_label("群聊", exact=True).select_option(
                                "1/5"
                            )
                            await expect(
                                page.get_by_text("设置已保存，当前不能发话", exact=True)
                            ).to_be_visible()
                            await expect(
                                page.get_by_text(
                                    "模型调用尚未开放：供应商硬预算未核验。",
                                    exact=True,
                                )
                            ).to_be_visible()
                            await expect(
                                page.get_by_label("普通聊天自主接话", exact=True)
                            ).not_to_be_checked()
                            await expect(
                                page.get_by_label("@机器人热群对话", exact=True)
                            ).to_be_checked()
                            all_day = page.get_by_label("全天可接话", exact=True)
                            await expect(all_day).not_to_be_checked()
                            await all_day.check()
                            await expect(
                                page.get_by_label(
                                    "开始时间（北京时间，小时）", exact=True
                                )
                            ).to_have_count(0)
                            await expect(
                                page.get_by_label(
                                    "结束时间（北京时间，小时）", exact=True
                                )
                            ).to_have_count(0)
                            await all_day.uncheck()
                            await expect(
                                page.get_by_label(
                                    "开始时间（北京时间，小时）", exact=True
                                )
                            ).to_have_value("9")
                            await expect(
                                page.get_by_label(
                                    "结束时间（北京时间，小时）", exact=True
                                )
                            ).to_have_value("23")
                            await expect(
                                page.get_by_label("群聊默认 · 聊天上下文", exact=True)
                            ).to_have_value("1")
                            await expect(
                                page.locator(
                                    'select[aria-label$=" · AstrBot配置"], select[aria-label$=" · 人格"]'
                                )
                            ).to_have_count(0)
                            await page.screenshot(
                                path=f"/tmp/wsl-ai-{'built-' if built is not None else ''}group-{width}.png",
                                full_page=True,
                            )
                            assert await page.evaluate(
                                "document.documentElement.scrollWidth <= innerWidth"
                            )
                            for name in (
                                "群聊默认 · 回复随机性",
                                "群聊默认 · 表达范围",
                                "群聊默认 · 回复长度",
                                "群聊默认 · 聊天上下文",
                            ):
                                bounds = await page.get_by_label(
                                    name, exact=True
                                ).bounding_box()
                                assert bounds and bounds["x"] >= 0
                                assert bounds["x"] + bounds["width"] <= width
                            await page.locator("#section-groups").click()
                            await page.locator("#tab-open").click()
                            await page.get_by_label("群聊", exact=True).select_option(
                                "1/5"
                            )
                            candidates = page.locator(".member-grants .member-list")
                            await expect(candidates).to_contain_text("管理甲")
                            await expect(candidates).not_to_contain_text("成员乙")
                            await page.screenshot(
                                path=f"/tmp/wsl-ai-{'built-' if built is not None else ''}managers-{width}.png",
                                full_page=True,
                            )
                            assert await page.evaluate(
                                "document.documentElement.scrollWidth <= innerWidth"
                            )
                            calls = (
                                await page.evaluate("window.compiledFixtureCalls")
                                if built is not None
                                else json.loads(
                                    await page.locator("#fixture-calls").inner_text()
                                )
                            )
                            assert all(
                                call.get("action")
                                in {
                                    None,
                                    "status",
                                    "tenant_members",
                                    "business_rules",
                                    "page_schema",
                                }
                                for call in calls
                            )
                            assert not errors, errors
                            await page.close()
                    finally:
                        await browser.close()
            finally:
                if server.returncode is None:
                    server.terminate()
                await asyncio.wait_for(server.wait(), timeout=10)
    print(
        f"PASS:{'compiled page' if built is not None else 'source fixture'};"
        "390/1440px; preset controls, legacy values, separate modes, no writes or overflow."
    )


if __name__ == "__main__":
    asyncio.run(main())
