"""Check card controls using live reads and intercepted configuration writes."""

import argparse
import asyncio
import json
import time
from pathlib import Path

import jwt
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]


async def main():
    """Verify permissions, suffix payloads and mobile layout without renaming."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:6185")
    parser.add_argument("--browser", required=True)
    args = parser.parse_args()
    dashboard = json.loads(
        (ROOT / "data/cmd_config.json").read_text(encoding="utf-8-sig")
    )["dashboard"]
    token = jwt.encode(
        {"username": dashboard["username"], "exp": int(time.time()) + 600},
        dashboard["jwt_secret"],
        algorithm="HS256",
    )
    writes, errors = [], []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=args.browser, headless=True
        )
        page = await browser.new_page(viewport={"width": 1280, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.add_init_script(
            f"localStorage.setItem('token', {json.dumps(token)});"
            "localStorage.setItem('astrbot-locale', 'zh-CN');"
            "localStorage.setItem('astrbot:first_notice_seen:v1', '1');"
        )

        async def guard(route):
            req = route.request
            if req.method == "GET":
                await route.continue_()
            elif req.url.endswith(
                "/bot-types/wangshangliao/registration"
            ) and req.post_data_json.get("action") in {"groups", "test_status"}:
                await route.continue_()
            elif (
                req.url.endswith("/bot-types/wangshangliao/registration")
                and req.post_data_json.get("action") == "card_preview"
            ):
                await route.fulfill(
                    status=400,
                    json={"status": "error", "message": "moderation_permission"},
                )
            else:
                writes.append(req.post_data_json)
                await route.fulfill(
                    json={"status": "ok", "message": "测试保存，未写入生产", "data": {}}
                )

        await page.route("**/api/**", guard)
        await page.goto(f"{args.base_url}/#/platforms", wait_until="domcontentloaded")
        batch = (
            page.locator("section")
            .filter(has=page.get_by_role("heading", name="成员批量操作", exact=True))
            .last
        )
        await batch.wait_for()
        await batch.get_by_role("combobox").click()
        await page.get_by_role("option").filter(has_text="大海兼职群").click()
        await batch.get_by_role("button", name="生成预览", exact=True).click()
        await batch.get_by_role("alert").filter(has_text="请启用机器人群管").wait_for()
        assert "Request failed" not in await batch.inner_text()
        permissions = (
            page.locator("section")
            .filter(has=page.get_by_role("heading", name="机器人群管授权", exact=True))
            .last
        )
        automatic = permissions.get_by_label("自动规范本群普通成员名片", exact=True)
        assert await automatic.is_disabled()
        await permissions.get_by_label("修改普通成员群名片", exact=True).check()
        suffix = permissions.get_by_label("本群名片后缀（可留空）", exact=True)
        await suffix.fill("·同事")
        await automatic.check()
        assert await permissions.get_by_label(
            "自动踢出已停用（仅允许手动命令）", exact=True
        ).is_disabled()
        await page.get_by_role("button", name="保存更改", exact=True).click()
        await page.get_by_text("测试保存，未写入生产", exact=True).wait_for()
        configs = [
            body["config"]
            for body in writes
            if isinstance(body, dict) and "config" in body
        ]
        assert len(configs) == 1
        config = configs[0]
        group = str(config["enabled_groups"][0])
        assert config["moderation"]["card_suffix"][group] == "·同事"
        assert config["moderation"]["card_auto"][group] is True
        assert config["moderation"]["manual_kick_only"] is True
        for width, height in ((1280, 1000), (390, 844)):
            await page.set_viewport_size({"width": width, "height": height})
            await suffix.scroll_into_view_if_needed()
            await page.wait_for_timeout(250)
            bounds = await suffix.bounding_box()
            assert (
                bounds
                and 0 <= bounds["x"]
                and bounds["x"] + bounds["width"] <= width + 1
            ), (width, bounds)
            await page.screenshot(path=f"/tmp/wsl-platform-card-suffix-{width}.png")
        assert not errors, errors
        print(
            "Card error message, authorization gate, suffix payload and desktop/mobile layout passed; all writes were intercepted."
        )
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
