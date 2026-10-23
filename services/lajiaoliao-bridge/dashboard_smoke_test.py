"""UI-only smoke test. All dashboard API responses use synthetic fixtures."""

import json
import os
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


def main():
    artifacts = Path("/opt/lajiaoliao-analysis/linux-login-20261008")
    artifacts.mkdir(parents=True, exist_ok=True)
    template = {
        "id": "lajiaoliao",
        "type": "lajiaoliao",
        "enable": False,
        "account_id": "",
        "enabled_groups": [],
        "reply_private": True,
    }
    metadata = {"platform_group": {"metadata": {"platform": {
        "config_template": {"辣椒聊": template}
    }}}}
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        )
        try:
            for name, width, height, saved_account in [
                ("desktop", 1366, 900, ""),
                ("mobile", 390, 844, ""),
                ("mismatch-desktop", 1366, 900, "old-test-account"),
                ("mismatch-mobile", 390, 844, "old-test-account"),
                ("numeric-account", 1366, 900, 12345),
            ]:
                template["account_id"] = saved_account
                context = browser.new_context(viewport={"width": width, "height": height})
                context.add_init_script(
                    "localStorage.setItem('token','mock-ui-test');"
                    "localStorage.setItem('astrbot-locale','zh-CN');"
                )
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))

                def api(route):
                    path = urlparse(route.request.url).path
                    data = {}
                    if path == "/api/v1/system-config/runtime":
                        data = {"config": {"platform": []}, "metadata": metadata}
                    elif path == "/api/v1/config-profiles":
                        data = {"info_list": [{"id": "default", "name": "默认配置"}]}
                    elif path.startswith("/api/v1/config-profiles/"):
                        data = {"config": {}, "metadata": {}}
                    elif path.endswith("/registration"):
                        assert route.request.post_data_json["action"] == "poll"
                        data = {"authenticated": True, "account_id": "12345", "connection_state": "online"}
                    elif path.endswith("/stats"):
                        data = {"platforms": []}
                    elif "version" in path:
                        data = {"version": "test", "dashboard_version": "test"}
                    route.fulfill(json={"status": "ok", "data": data})

                context.route("**/api/**", api)
                page.goto("http://127.0.0.1:17901/#/platforms")
                page.get_by_role("button", name="创建机器人", exact=True).first.click()
                dialog = page.get_by_role("dialog")
                dialog.locator(".v-select").first.wait_for()
                dialog.locator(".v-select").first.click()
                page.wait_for_timeout(500)
                page.screenshot(path=str(artifacts / f"platform-debug-{name}.png"), full_page=True)
                option = page.get_by_role("listbox").get_by_text("辣椒聊", exact=True)
                option.wait_for()
                icon = page.get_by_role("listbox").locator("img").first
                assert icon.evaluate("(img) => img.complete && img.naturalWidth > 0")
                page.screenshot(path=str(artifacts / f"platform-category-{name}.png"), full_page=True)
                option.click()
                if saved_account == "old-test-account":
                    dialog.get_by_role("button", name="使用此账号", exact=True).wait_for()
                    assert dialog.get_by_text("OpenIM 已连接").count() == 0
                    dialog.get_by_role("button", name="使用此账号", exact=True).click()
                dialog.get_by_text("OpenIM 已连接").wait_for()
                assert dialog.get_by_role("button", name="使用此账号", exact=True).count() == 0
                assert dialog.get_by_role("button", name="使用已登录会话").count() == 0
                assert dialog.get_by_label("密码", exact=True).count() == 0
                dialog.get_by_role("button", name="重新登录", exact=True).click()
                dialog.get_by_label("密码", exact=True).wait_for()
                dialog.locator("form").get_by_role("button", name="取消", exact=True).click()
                assert dialog.get_by_label("密码", exact=True).count() == 0
                page.screenshot(path=str(artifacts / f"platform-session-{name}.png"), full_page=True)
                assert not errors, errors
                context.close()
                print(json.dumps({"viewport": name, "category_icon": True, "session_selection": True}))
        finally:
            browser.close()


if __name__ == "__main__":
    main()
