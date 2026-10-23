"""Exercise the installed native Plugin Page on loopback."""

import json
import sys
from pathlib import Path

from deploy import NAME, client
from playwright.sync_api import sync_playwright

OUT = Path("/root/Projects/agents/telethon-ai-deployment")


def main():
    with client() as api:
        # Reload only the already installed gateway, never all plugins.
        result = api.post("/api/plugin/reload", json={"name": NAME}).json()
        assert result["status"] == "ok", result.get("message")
        token = api.headers["Authorization"].removeprefix("Bearer ")
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox"],
                executable_path="/root/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome",
            )
            context = browser.new_context(viewport={"width": 1400, "height": 900})
            context.add_init_script(
                "localStorage.setItem('token', " + json.dumps(token) + ");"
            )
            context.add_init_script(
                "localStorage.setItem('astrbot:first_notice_seen:v1', '1');"
            )
            page = context.new_page()
            page.goto(
                str(api.base_url) + f"/#/plugin-page/{NAME}/accounts",
                wait_until="domcontentloaded",
            )
            frame = page.frame_locator("iframe")
            frame.locator("#accounts tr").first.wait_for(timeout=45000)
            assert frame.locator("#accounts tr").count() == 2
            if "--skip-preview" not in sys.argv:
                frame.locator("#prompt").fill("你叫什么？请简短介绍一下自己。")
                frame.locator("#preview-submit").click()
                frame.locator("#reply").filter(has_text="小聊").wait_for(timeout=60000)
                print("Native Page preview:", frame.locator("#reply").inner_text())
            page.screenshot(path=str(OUT / "gateway-desktop.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_timeout(1000)
            scrim = page.locator(".v-navigation-drawer__scrim")
            if scrim.is_visible():
                scrim.click(position={"x": 380, "y": 20})
                page.wait_for_timeout(400)
            page.screenshot(path=str(OUT / "gateway-mobile.png"), full_page=True)
            overflow = frame.locator("body").evaluate(
                "(el) => el.scrollWidth > window.innerWidth"
            )
            assert not overflow, "Plugin page body overflows mobile viewport"
            # Pausing a disabled account is an isolated state test with no Telegram call.
            first = frame.locator("#accounts tr").first
            first.get_by_role("button", name="紧急停用").click()
            first.get_by_role("button", name="解除暂停").wait_for()
            first.get_by_role("button", name="解除暂停").click()
            frame.get_by_role("button", name="确认解除", exact=True).click()
            first.get_by_role("button", name="紧急停用").wait_for()
            browser.close()
        state = api.get(f"/api/plug/{NAME}/status").json()
        assert all(not a["enabled"] and not a["paused"] for a in state["accounts"])
        print(
            "PASS: installed Page, preview, mobile layout, pause/resume; accounts remain disabled"
        )


if __name__ == "__main__":
    main()
