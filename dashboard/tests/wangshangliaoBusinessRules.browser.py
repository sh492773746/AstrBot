"""Exercise compiled business-rule/support forms using local-only fake APIs."""

import argparse
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[1]


async def main():
    """Verify responsive preview/confirmation workflows without live mutations."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--built-page", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    html = args.built_page.read_text("utf-8")
    log = (args.output / "browser-vite.log").open("w")
    server = await asyncio.create_subprocess_exec(
        "node",
        "node_modules/vite/bin/vite.js",
        "--host",
        "127.0.0.1",
        "--port",
        "3049",
        "--strictPort",
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
                for _ in range(100):
                    request = await playwright.request.new_context()
                    try:
                        response = await request.get("http://127.0.0.1:3049")
                        if response.ok:
                            break
                    except Exception:
                        pass
                    finally:
                        await request.dispose()
                    if server.returncode is not None:
                        raise RuntimeError("Fixture server exited")
                    await asyncio.sleep(0.1)
                for width in (320, 390, 1440):
                    page = await browser.new_page(
                        viewport={"width": width, "height": 1000}
                    )
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    await page.route("**/api/**", lambda route: route.abort())
                    await page.goto(
                        "http://127.0.0.1:3049/tests/fixtures/wangshangliao-management.html?rules"
                    )
                    await page.locator("#section-ai").wait_for()
                    assets = await page.evaluate(
                        """html => {
                      const doc = new DOMParser().parseFromString(html, "text/html");
                      return {
                        css: [...doc.querySelectorAll("style")].map(s => s.textContent),
                        js: [...doc.querySelectorAll("script:not([data-astrbot-bridge])")]
                          .filter(s => !s.src).map(s => s.textContent)
                      };
                    }""",
                        html,
                    )
                    await page.evaluate("""() => {
                      document.querySelector("#app").__vue_app__.unmount();
                      document.querySelectorAll('style,link[rel=stylesheet]').forEach(s => s.remove());
                      window.compiledFixtureCalls = [];
                      const bridge = window.AstrBotPluginPage;
                      const original = bridge.apiPost;
                      bridge.apiPost = (endpoint, request) => {
                        window.compiledFixtureCalls.push({
                          endpoint, action: request.action,
                          payload: JSON.parse(JSON.stringify(request.payload))
                        });
                        return original(endpoint, request);
                      };
                    }""")
                    for css in assets["css"]:
                        await page.add_style_tag(content=css)
                    await page.add_script_tag(content=assets["js"][0])
                    await page.locator("#section-ai").click()
                    await page.locator("#tab-consultation").click()
                    panel = page.get_by_role("region", name="专属与账号业务客服设置")
                    await expect(
                        panel.get_by_label("业务话术", exact=True)
                    ).to_be_visible()
                    await panel.get_by_label("业务话术", exact=True).fill(
                        "已确认的客服业务说明"
                    )
                    await panel.get_by_label("公开链接（每行一个）", exact=True).fill(
                        "https://example.test/confirmed"
                    )
                    await panel.get_by_role(
                        "button", name="添加业务知识", exact=True
                    ).click()
                    await panel.get_by_label("业务知识 1", exact=True).fill(
                        "服务时间为每天九点至十八点"
                    )
                    await panel.get_by_role(
                        "button", name="预览客服修改", exact=True
                    ).click()
                    confirmation = panel.get_by_role("alertdialog")
                    await expect(confirmation).to_be_visible()
                    await expect(
                        panel.get_by_label("业务话术", exact=True)
                    ).to_be_disabled()
                    await confirmation.get_by_role(
                        "button", name="确认保存", exact=True
                    ).click()
                    await expect(confirmation).to_have_count(0)
                    await expect(
                        panel.get_by_label("业务话术", exact=True)
                    ).to_have_value("已确认的客服业务说明")
                    await page.screenshot(
                        path=str(args.output / f"business-support-{width}.png"),
                        full_page=True,
                    )
                    assert await page.evaluate(
                        "document.documentElement.scrollWidth <= innerWidth"
                    )
                    await panel.get_by_label("业务归属", exact=True).select_option(
                        "template"
                    )
                    await expect(
                        panel.get_by_label("业务话术", exact=True)
                    ).to_have_value("仅按已确认资料解答")
                    await panel.get_by_label("业务话术", exact=True).fill(
                        "新开通者默认业务说明"
                    )
                    await panel.get_by_role(
                        "button", name="预览客服修改", exact=True
                    ).click()
                    confirmation = panel.get_by_role("alertdialog")
                    await expect(confirmation).to_contain_text("默认模板")
                    await confirmation.get_by_role(
                        "button", name="确认保存", exact=True
                    ).click()
                    await expect(confirmation).to_have_count(0)
                    await panel.get_by_label("业务归属", exact=True).select_option("2")
                    await expect(
                        panel.get_by_label("业务话术", exact=True)
                    ).to_have_value("仅按已确认资料解答")
                    await expect(
                        panel.get_by_label("真实 UID", exact=True)
                    ).to_have_count(0)
                    await panel.scroll_into_view_if_needed()
                    await page.screenshot(
                        path=str(args.output / f"owner-support-{width}.png"),
                        full_page=True,
                    )
                    assert await page.evaluate(
                        "document.documentElement.scrollWidth <= innerWidth"
                    )
                    await page.locator("#section-groups").click()
                    await page.get_by_label("群聊", exact=True).select_option("1/5")
                    await page.get_by_role("tab", name="规则", exact=True).click()
                    video = page.get_by_role(
                        "checkbox", name="仅白名单成员可发视频", exact=True
                    )
                    await expect(video).not_to_be_checked()
                    await video.check()
                    await expect(video).to_be_checked()
                    await video.uncheck()
                    await video.scroll_into_view_if_needed()
                    await page.screenshot(
                        path=str(args.output / f"video-whitelist-{width}.png"),
                        full_page=True,
                    )
                    rules = page.get_by_role("region", name="群关键词回复与定时发言")
                    await rules.get_by_role(
                        "button", name="添加关键词回复", exact=True
                    ).click()
                    item = rules.locator("article").first
                    await item.get_by_label("名称", exact=True).fill("业务地址")
                    await item.get_by_label("同义词（每行一个）", exact=True).fill(
                        "链接\n地址"
                    )
                    await item.get_by_label("回复正文", exact=True).fill(
                        "https://example.test/confirmed"
                    )
                    await expect(
                        item.get_by_label("启用", exact=True)
                    ).not_to_be_checked()
                    await rules.get_by_role(
                        "button", name="添加发言计划", exact=True
                    ).click()
                    ad = rules.locator("article").last
                    await ad.get_by_label("名称", exact=True).fill("测试广告")
                    await ad.get_by_label("时刻（逗号分隔）", exact=True).fill(
                        "09:00,18:00"
                    )
                    await ad.get_by_label("文字与链接", exact=True).fill(
                        "已确认的业务通知"
                    )
                    await expect(
                        ad.get_by_label("启用", exact=True)
                    ).not_to_be_checked()
                    await rules.get_by_role(
                        "button", name="预览修改", exact=True
                    ).click()
                    confirmation = rules.get_by_role("alertdialog")
                    await expect(confirmation).to_be_visible()
                    await expect(confirmation).to_contain_text("关闭")
                    await page.screenshot(
                        path=str(args.output / f"business-rules-{width}.png"),
                        full_page=True,
                    )
                    assert await page.evaluate(
                        "document.documentElement.scrollWidth <= innerWidth"
                    )
                    await confirmation.get_by_role(
                        "button", name="确认保存", exact=True
                    ).click()
                    await expect(confirmation).to_have_count(0)
                    await page.get_by_label("群聊", exact=True).select_option("1/6")
                    await expect(rules.locator("article")).to_have_count(0)
                    await page.locator("#section-help").click()
                    help_panel = page.get_by_role("region", name="命令能力目录")
                    await help_panel.get_by_role(
                        "checkbox", name="全部兼容命令", exact=True
                    ).check()
                    await help_panel.get_by_role("tab", name="私信规则命令").click()
                    await expect(help_panel).to_contain_text("专属客服所有者")
                    await help_panel.get_by_role("tab", name="服务端专属设置").click()
                    await expect(help_panel).to_contain_text("开启自主接话")
                    await page.screenshot(
                        path=str(args.output / f"command-help-{width}.png"),
                        full_page=True,
                    )
                    calls = await page.evaluate("window.compiledFixtureCalls")
                    previews = [
                        c
                        for c in calls
                        if c.get("payload", {}).get("operation") == "preview"
                    ]
                    commits = [
                        c
                        for c in calls
                        if c.get("payload", {}).get("operation") == "commit"
                    ]
                    assert len(previews) == len(commits) == 3
                    assert previews[1]["action"] == "owner_support"
                    assert previews[1]["payload"]["uid"] == ""
                    assert previews[-1]["payload"]["group_id"] == "5"
                    assert not any(
                        r["enabled"]
                        for kind in ("keywords", "ads")
                        for r in previews[-1]["payload"]["config"][kind]
                    )
                    assert not errors, errors
                    await page.close()
                print(
                    "PASS compiled 320/390/1440px video checkbox, support and business-rule previews, confirmations, default-off and cross-group isolation; no live APIs."
                )
            finally:
                await browser.close()
    finally:
        server.terminate()
        await server.wait()
        log.close()


if __name__ == "__main__":
    asyncio.run(main())
