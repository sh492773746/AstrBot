"""Exercise actual shipped assets offline, without credentials or API mutations."""

import json
from pathlib import Path

from playwright.sync_api import sync_playwright

PAGE = (
    Path(__file__).resolve().parents[2]
    / "data/plugins/astrbot_plugin_telethon_ai/pages/accounts"
)
DATA = {
    "accounts": [],
    "tenants": [],
    "recent": [],
    "checks": {},
    "control_attached": True,
    "tenant_count": 0,
    "customer_entries": 0,
}


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox"],
            executable_path="/root/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome",
        )
        for mode in (
            "normal",
            "missing_bridge",
            "bridge_timeout",
            "api_timeout",
            "denied",
            "missing_script",
        ):
            context = browser.new_context()
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.clock.install()
            page.add_init_script(
                "window.testMode="
                + json.dumps(mode)
                + ";window.testData="
                + json.dumps(DATA)
                + ";"
                "if(window.testMode!=='missing_bridge'){window.AstrBotPluginPage={"
                "ready:()=>window.testMode==='bridge_timeout'?new Promise(()=>{}):Promise.resolve(),"
                "apiGet:()=>window.testMode==='api_timeout'?new Promise(()=>{}):"
                "window.testMode==='denied'?Promise.reject(new Error('未授权')):Promise.resolve(window.testData),"
                "apiPost:()=>{throw new Error('Unexpected write')}};}"
            )

            def serve(route):
                name = route.request.url.rsplit("/", 1)[-1]
                if name == "app.js" and mode == "missing_script":
                    route.abort()
                    return
                if name not in {"index.html", "app.js", "style.css"}:
                    route.abort()
                    return
                content_type = {
                    "index.html": "text/html",
                    "app.js": "application/javascript",
                    "style.css": "text/css",
                }[name]
                route.fulfill(body=(PAGE / name).read_text(), content_type=content_type)

            page.route("**/*", serve)
            page.goto("https://page.test/index.html", wait_until="load")
            page.wait_for_timeout(20)
            page.clock.fast_forward(14000)
            text = page.locator("#status").inner_text()
            expected = {
                "normal": "已更新",
                "missing_bridge": "未检测到",
                "bridge_timeout": "插件桥接未建立",
                "api_timeout": "状态接口无响应",
                "denied": "未授权",
                "missing_script": "页面脚本未加载",
            }[mode]
            assert expected in text, (mode, text)
            assert page.locator("#refresh").is_enabled()
            if mode != "missing_script":
                page.evaluate(
                    "window.testMode='normal';window.AstrBotPluginPage={ready:()=>Promise.resolve(),apiGet:()=>Promise.resolve(window.testData)}"
                )
                page.locator("#refresh").click()
                page.wait_for_function(
                    "document.getElementById('status').textContent.includes('已更新')"
                )
                # A late resolution/timeout must not overwrite a recovered page.
                page.clock.fast_forward(20000)
                assert "已更新" in page.locator("#status").inner_text()
            assert not errors, errors
            print("PASS:", mode)
            context.close()
        browser.close()


if __name__ == "__main__":
    main()
