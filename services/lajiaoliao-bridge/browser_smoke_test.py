"""Test the loopback login UI with fake credentials and a mocked captcha SDK.

No real account endpoint or real security challenge is exercised by this test.
Run with the repository virtualenv while the bridge listens on 127.0.0.1:17890.
"""

import json
import os
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


def main():
    artifacts = Path("/opt/lajiaoliao-analysis/linux-login-20261008")
    artifacts.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True, executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        )
        try:
            for name, viewport in [
                ("desktop", {"width": 1280, "height": 800}),
                ("mobile", {"width": 390, "height": 844}),
            ]:
                context = browser.new_context(viewport=viewport)
                context.route(
                    "**/*",
                    lambda route: route.continue_()
                    if urlparse(route.request.url).hostname in ("localhost", "127.0.0.1")
                    else route.abort(),
                )
                page = context.new_page()
                errors = []
                sends = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.route(
                    "**/v1/session/challenge",
                    lambda route: route.fulfill(
                        json={
                            "required": True,
                            "region": "cn",
                            "prefix": "mock-prefix",
                            "scene_id": "mock-scene",
                        }
                    ),
                )
                page.route(
                    "https://o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js",
                    lambda route: route.fulfill(
                        content_type="application/javascript",
                        body="window.initAliyunCaptcha = options => {"
                        "window.completeMock = () => options.success('mock-human-proof');"
                        "window.failMock = () => options.fail();"
                        "options.getInstance({show() {}}); };",
                    ),
                )

                def login_response(route):
                    sends.append(route.request.post_data_json)
                    route.fulfill(json={"status": "logged_in", "account_id": "mock-user"})

                page.route("**/v1/session/login", login_response)
                host = "localhost" if name == "desktop" else "127.0.0.1"
                page.goto(f"http://{host}:17890/login")
                assert page.title() == "辣椒聊 · 本地授权"
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth"
                )
                page.screenshot(path=str(artifacts / f"{name}.png"), full_page=True)

                page.locator("#account").fill("13800000000")
                page.locator("#password").fill("fake-test-password")
                page.locator("#submit").click()
                page.locator("iframe").wait_for()
                frame = next(frame for frame in page.frames if frame.parent_frame)
                frame.wait_for_function("typeof window.completeMock === 'function'")
                assert frame.evaluate(
                    "(() => { try { return parent.document === document; }"
                    "catch (_) { return true; } })()"
                ), "Verification provider can access password form"
                assert not sends, "Password submitted before human verification"
                page.evaluate(
                    "window.postMessage({source:'lajiao-aliyun-captcha',"
                    "type:'success',value:'forged',requestID:'wrong'},'*')"
                )
                page.wait_for_timeout(100)
                assert not sends, "Untrusted message triggered login"
                frame.evaluate("window.failMock()")
                page.wait_for_function("document.querySelector('#status').textContent.includes('验证未通过')")
                assert page.locator("iframe").is_visible()
                assert not sends, "Unsuccessful verification submitted credentials"
                frame.evaluate("window.completeMock()")
                page.locator("#status.success").wait_for()
                assert len(sends) == 1
                assert sends[0]["captcha_verify_param"] == "mock-human-proof"
                assert sends[0]["password"] == "fake-test-password"
                assert page.locator("#password").input_value() == ""
                assert page.evaluate("localStorage.length + sessionStorage.length") == 0

                page.locator("#password").fill("fake-test-password")
                page.locator("#submit").click()
                page.locator("#cancel").click()
                page.locator("#status.error").wait_for()
                assert len(sends) == 1, "Cancelling verification sent credentials"
                assert page.locator("#password").input_value() == ""
                page.route(
                    "https://o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js",
                    lambda route: route.abort(),
                )
                page.locator("#password").fill("fake-test-password")
                page.locator("#submit").click()
                page.wait_for_function("document.querySelector('#status').textContent.includes('无法加载阿里云验证码脚本')")
                assert len(sends) == 1, "SDK failure submitted credentials"
                assert page.locator("#password").input_value() == ""
                assert not errors, errors
                context.close()
                print(json.dumps({"viewport": name, "result": "passed"}))
        finally:
            browser.close()


if __name__ == "__main__":
    main()
