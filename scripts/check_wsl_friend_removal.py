"""Read-only deployed checks after removing friendship deletion."""

import argparse
import asyncio
import json
import time
from pathlib import Path

import httpx
import jwt
from playwright.async_api import async_playwright, expect

from scripts.check_wsl_account_names import PREFIX, ROOT, snapshot


async def main():
    """Verify deleted endpoints, retained approvals and real responsive pages.

    Raises:
        AssertionError: If deletion remains reachable or protected state changes.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--docs", action="store_true")
    args = parser.parse_args()
    before = snapshot()
    config = json.loads((ROOT / "data/cmd_config.json").read_text("utf-8-sig"))
    token = jwt.encode(
        {"username": config["dashboard"]["username"], "exp": int(time.time()) + 600},
        config["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )
    output = Path("/opt/backups") / time.strftime(
        "wsl-friend-removal-%Y%m%dT%H%M%S", time.gmtime()
    )
    output.mkdir(mode=0o700)
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:6185",
        timeout=30,
        trust_env=False,
        headers={"Authorization": "Bearer " + token},
    ) as client:
        state = (
            await client.post(
                PREFIX + "open-service",
                json={"action": "status", "payload": {}},
            )
        ).json()["data"]
        accounts = state["accounts"]
        assert len(accounts) == 2
        approvals = {}
        for account in accounts:
            response = await client.get(
                PREFIX + "friend-approval",
                params={"bot_id": account["instance_id"]},
            )
            assert response.json()["status"] == "ok"
            approvals[account["account"]] = response.json()["data"]
        if args.reload:
            response = await client.post(
                "/api/v1/plugins/wangshangliao_moderation/reload"
            )
            response.raise_for_status()
            assert response.json()["status"] == "ok"
        for account in accounts:
            response = await client.get(
                PREFIX + "friend-approval",
                params={"bot_id": account["instance_id"]},
            )
            current = response.json()["data"]
            assert current["enabled"] == approvals[account["account"]]["enabled"]
            assert current["version"] == approvals[account["account"]]["version"]
            for method in ("GET", "POST"):
                response = await client.request(
                    method,
                    PREFIX + "friends",
                    **(
                        {"params": {"bot_id": account["instance_id"]}}
                        if method == "GET"
                        else {
                            "json": {
                                "bot_id": account["instance_id"],
                                "action": "execute",
                                "preview_id": "a" * 32,
                                "confirmed": True,
                            }
                        }
                    ),
                )
                assert response.json() == {
                    "status": "error",
                    "message": "未找到该路由",
                    "data": {},
                }
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                executable_path="/root/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome",
                args=["--no-sandbox"],
            )
            try:
                for width in (390, 1440):
                    page = await browser.new_page(
                        viewport={"width": width, "height": 900}
                    )
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    await page.add_init_script(
                        "if (window === window.top) {"
                        "localStorage.setItem('token', " + json.dumps(token) + ");"
                        "localStorage.setItem('astrbot-locale', 'zh-CN');"
                        "localStorage.setItem('astrbot:first_notice_seen:v1', '1');}"
                    )

                    async def read_only(route):
                        """Permit read requests and the fixed status operation only.

                        Args:
                            route: Browser request being intercepted.
                        """
                        request = route.request
                        if request.method in {"GET", "HEAD", "OPTIONS"} or (
                            request.method == "POST"
                            and request.url.endswith(PREFIX + "open-service")
                            and request.post_data_json
                            == {"action": "status", "payload": {}}
                        ):
                            await route.continue_()
                        else:
                            await route.abort()

                    await page.route("**/api/**", read_only)
                    await page.goto(
                        "http://127.0.0.1:6185/#/plugin-page/wangshangliao_moderation/management"
                    )
                    frame = page.frame_locator("iframe")
                    await frame.get_by_role("tab", name="好友申请").click()
                    await expect(
                        frame.get_by_role("tab", name="好友清理")
                    ).to_have_count(0)
                    await expect(
                        frame.get_by_role("button", name="预览删除")
                    ).to_have_count(0)
                    await expect(
                        frame.get_by_role("checkbox", name="自动同意好友申请")
                    ).to_be_visible()
                    await expect(
                        frame.get_by_role("checkbox", name="自动同意好友申请")
                    ).to_be_enabled()
                    assert await frame.locator("body").evaluate(
                        "() => document.documentElement.scrollWidth <= innerWidth"
                    )
                    assert not errors, errors
                    await page.screenshot(
                        path=str(output / f"friend-approval-{width}.png"),
                        full_page=True,
                        animations="disabled",
                    )
                    if args.docs and width == 1440:
                        await frame.locator(".friend-approval").screenshot(
                            path=str(
                                ROOT
                                / "docs/zh/platform/images/wangshangliao-friend-approval.png"
                            ),
                            animations="disabled",
                        )
                    await page.close()
            finally:
                await browser.close()
        after = snapshot()
        assert before == after, "Configuration, tenants or financial state changed"
        result = {
            "deletion_endpoints_removed": True,
            "approval_settings_preserved": True,
            "entries_unchanged": before["entries"],
            "config_and_tenants_unchanged": True,
            "viewports": [390, 1440],
        }
        path = output / "verified.json"
        path.write_text(json.dumps(result, indent=2))
        path.chmod(0o600)
        print(json.dumps(result, indent=2))
        print("Evidence:", output)


if __name__ == "__main__":
    asyncio.run(main())
