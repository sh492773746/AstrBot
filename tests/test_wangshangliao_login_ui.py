"""Opt-in login browser regressions with synthetic API and verification traffic."""

import asyncio
import os
import re
from pathlib import Path

import pytest


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("WSL_LOGIN_TEST_URL"), reason="Opt-in Vite fixture browser test"
)
@pytest.mark.parametrize("locale", ["zh-CN", "en-US"])
async def test_generic_login_error_is_not_presented_as_password_error_or_ban(locale):
    """Keep 1001 visible without inventing its cause or submitting twice."""
    from playwright.async_api import async_playwright, expect

    base = os.environ["WSL_LOGIN_TEST_URL"].rstrip("/")
    actions = []

    async def api(route):
        payload = route.request.post_data_json
        action = payload["action"]
        actions.append(action)
        data = {
            "status": "pending",
            "instance_id": payload["instance_id"],
            "registration_code": "fixture-generic",
            "captcha_id": "fixture",
            "expires_in": 600,
            "error": "",
        }
        if action == "login":
            data.update(
                status="failed",
                error="login_rejected_unspecified",
                error_action="login",
                provider_code=1001,
            )
        await route.fulfill(json={"status": "ok", "data": data})

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        )
        try:
            context = await browser.new_context(viewport={"width": 390, "height": 844})
            await context.add_init_script(
                f"localStorage.setItem('astrbot-locale','{locale}');"
                "window.initNECaptcha=(options,ready)=>ready({verify(){},refresh(){},destroy(){}});"
            )
            page = await context.new_page()
            await page.route(re.compile(r"^https?://"), lambda route: route.abort())
            await page.route(base + "/**", lambda route: route.continue_())
            await page.route(base + "/api/**", api)
            await page.goto(base + "/tests/fixtures/wangshangliao.html?existing=1")
            await page.get_by_role(
                "button", name=re.compile("^(重新登录|Log in again)$")
            ).click()
            await page.get_by_role("textbox", name=re.compile("^(账号|Account)")).fill(
                "fixture"
            )
            await page.locator('form input[type="password"]').fill("fixture-password")
            await page.get_by_role(
                "button", name=re.compile("^(验证并登录|Verify and log in)$")
            ).click()
            alert = page.get_by_role("alert").filter(
                has_text=re.compile("通用登录错误|general login error")
            )
            await expect(alert).to_contain_text("1001")
            await expect(alert).to_contain_text(
                "没有说明具体原因" if locale == "zh-CN" else "without a specific reason"
            )
            await expect(alert).to_contain_text(
                "请勿连续重试" if locale == "zh-CN" else "Do not retry repeatedly"
            )
            await expect(
                page.get_by_role(
                    "button", name=re.compile("保存登录并连接|Save login and connect")
                )
            ).to_have_count(0)
            assert actions.count("login") == 1
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("WSL_LOGIN_TEST_URL"), reason="Opt-in Vite fixture browser test"
)
@pytest.mark.parametrize("locale", ["zh-CN", "en-US"])
@pytest.mark.parametrize("outcome", ["success", "locked", "incorrect", "uncertain"])
async def test_current_security_step_masks_secret_and_requires_complete_authentication(
    locale, outcome
):
    """Exercise the real controls using disposable, intercepted credentials."""
    from playwright.async_api import async_playwright, expect

    base = os.environ["WSL_LOGIN_TEST_URL"].rstrip("/")
    actions, verification_payloads = [], []

    async def api(route):
        payload = route.request.post_data_json
        action = payload["action"]
        actions.append(action)
        data = {
            "status": "pending",
            "instance_id": payload["instance_id"],
            "registration_code": "fixture-security",
            "captcha_id": "fixture",
            "expires_in": 600,
            "error": "",
            "account_id": "42",
        }
        if action == "groups":
            data = {"groups": [], "complete": True}
        elif action == "login":
            data.update(status="security_required", nickname="Fixture")
        elif action == "verify_security_code":
            verification_payloads.append(payload)
            assert payload["security_code"] == "123456"
            assert not payload.get("password") and not payload.get("verification_code")
            if outcome == "success":
                data.update(status="authenticated", session_ref="safe-save-ref")
            elif outcome == "uncertain":
                data.update(
                    status="security_uncertain",
                    error="security_rotation_missing",
                    error_action=action,
                )
            else:
                data.update(
                    status="security_locked"
                    if outcome == "locked"
                    else "security_required",
                    error="secondary_security_locked"
                    if outcome == "locked"
                    else "secondary_security_code_invalid",
                    error_action=action,
                    provider_code=1404 if outcome == "locked" else 1403,
                    security_retry_after=30,
                )
        await route.fulfill(json={"status": "ok", "data": data})

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        )
        try:
            context = await browser.new_context(viewport={"width": 390, "height": 844})
            await context.add_init_script(
                f"localStorage.setItem('astrbot-locale','{locale}');"
                "window.initNECaptcha=(options,ready)=>ready({verify(){},refresh(){},destroy(){}});"
            )
            page = await context.new_page()
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            await page.route(re.compile(r"^https?://"), lambda route: route.abort())
            await page.route(base + "/**", lambda route: route.continue_())
            await page.route(base + "/api/**", api)
            await page.goto(base + "/tests/fixtures/wangshangliao.html?existing=1")
            await page.get_by_role(
                "button", name=re.compile("^(重新登录|Log in again)$")
            ).click()
            await page.get_by_role("textbox", name=re.compile("^(账号|Account)")).fill(
                "fixture"
            )
            await page.locator('form input[type="password"]').fill("fixture-password")
            await page.get_by_role(
                "button", name=re.compile("^(验证并登录|Verify and log in)$")
            ).click()
            code = page.locator('form input[type="password"]')
            await expect(code).to_be_visible()
            await expect(code).to_have_attribute("maxlength", "6")
            await expect(
                page.get_by_role(
                    "button", name=re.compile("保存登录并连接|Save login and connect")
                )
            ).to_have_count(0)
            await code.fill("123456")
            if outcome == "success" and locale == "zh-CN":
                output = Path("data/temp/wsl-login-browser-20261017")
                output.mkdir(parents=True, exist_ok=True)
                await page.screenshot(
                    path=str(output / "security-masked-mobile.png"), full_page=True
                )
            await page.get_by_role(
                "button", name=re.compile("^(验证安全码|Verify security code)")
            ).click()
            if outcome == "success":
                await expect(
                    page.get_by_role(
                        "button",
                        name=re.compile("保存登录并连接|Save login and connect"),
                    )
                ).to_be_visible()
                await expect(code).to_have_count(0)
            elif outcome == "locked":
                await expect(
                    page.get_by_role(
                        "button", name=re.compile("^(验证安全码|Verify security code)")
                    )
                ).to_have_count(0)
                await expect(page.locator("form .v-alert").first).to_contain_text(
                    "1404"
                )
            elif outcome == "uncertain":
                await expect(code).to_have_count(0)
                await expect(
                    page.get_by_role(
                        "button", name=re.compile("^(验证安全码|Verify security code)")
                    )
                ).to_have_count(0)
                await expect(
                    page.get_by_role(
                        "button",
                        name=re.compile("保存登录并连接|Save login and connect"),
                    )
                ).to_have_count(0)
                await expect(page.locator("form .v-alert").first).to_contain_text(
                    "这不表示安全码错误"
                    if locale == "zh-CN"
                    else "does not mean the security code was incorrect"
                )
            else:
                await expect(code).to_have_value("")
                await expect(
                    page.get_by_role(
                        "button", name=re.compile("^(验证安全码|Verify security code)")
                    )
                ).to_be_disabled()
                await expect(page.locator("form .v-alert").first).to_contain_text(
                    "1403"
                )
            for width in (320, 555, 1280):
                await page.set_viewport_size({"width": width, "height": 900})
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth"
                )
            assert "123456" not in await page.evaluate("JSON.stringify(localStorage)")
            assert "123456" not in await page.evaluate("JSON.stringify(sessionStorage)")
            assert len(verification_payloads) == 1
            assert actions.count("login") == 1
            assert not errors
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("WSL_LOGIN_TEST_URL"), reason="Opt-in Vite fixture browser test"
)
@pytest.mark.parametrize(
    "locale,expected,warning",
    [
        (
            "zh-CN",
            "旺商聊要求此账号完成二级安全验证",
            "不要把安全码发给机器人",
        ),
        (
            "en-US",
            "Wangshangliao requires secondary security verification for this account",
            "Do not send the code to the bot",
        ),
    ],
)
async def test_secondary_security_code_ui_explains_official_app_setup(
    locale, expected, warning
):
    from playwright.async_api import async_playwright, expect

    base = os.environ["WSL_LOGIN_TEST_URL"].rstrip("/")
    actions = []

    async def api(route):
        payload = route.request.post_data_json
        action = payload["action"]
        actions.append(action)
        assert action in {"groups", "start", "login", "cancel"}
        data = {
            "status": "pending",
            "instance_id": payload["instance_id"],
            "registration_code": "fixture-security-code",
            "captcha_id": "fixture",
            "expires_in": 600,
            "error": "",
        }
        if action == "groups":
            data = {"groups": [], "complete": True}
        elif action == "login":
            data.update(
                error="secondary_security_code_required",
                error_action="login",
                provider_code=1400,
            )
        await route.fulfill(json={"status": "ok", "data": data})

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        )
        try:
            context = await browser.new_context(viewport={"width": 320, "height": 900})
            await context.add_init_script(
                f"localStorage.setItem('astrbot-locale', '{locale}');"
                """
                window.initNECaptcha = (options, ready) => ready({
                  verify() {}, refresh() {}, destroy() {},
                });
                """
            )
            page = await context.new_page()
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            await page.route(re.compile(r"^https?://"), lambda route: route.abort())
            await page.route(base + "/**", lambda route: route.continue_())
            await page.route(base + "/api/**", api)
            await page.goto(base + "/tests/fixtures/wangshangliao.html?existing=1")
            await page.get_by_role(
                "button", name=re.compile("^(重新登录|Log in again)$")
            ).click()
            await page.get_by_role("textbox", name=re.compile("^(账号|Account)")).fill(
                "fixture"
            )
            await page.locator('form input[type="password"]').fill("fixture")
            await page.get_by_role(
                "button", name=re.compile("^(验证并登录|Verify and log in)$")
            ).click()
            alert = page.locator("form .v-alert").first
            await expect(alert).to_contain_text(expected)
            await expect(alert).to_contain_text(warning)
            await expect(alert).to_contain_text("1400")
            await expect(alert).to_contain_text(
                "若已设置仍报错" if locale == "zh-CN" else "If already set"
            )
            await expect(alert).to_contain_text(
                "联系旺商聊客服" if locale == "zh-CN" else "Wangshangliao support"
            )
            await expect(alert).not_to_contain_text("session expired")
            await expect(alert).not_to_contain_text("会话已失效")
            for width in (320, 555):
                await page.set_viewport_size({"width": width, "height": 900})
                assert await page.evaluate(
                    "() => document.documentElement.scrollWidth <= innerWidth"
                )
                assert await alert.evaluate(
                    "element => element.scrollWidth <= element.clientWidth"
                )
            assert actions.count("login") == 1
            assert set(actions) <= {"groups", "start", "login"}
            assert not errors
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("WSL_LOGIN_TEST_URL"), reason="Opt-in Vite fixture browser test"
)
async def test_business_badges_follow_runtime_without_mutating_login_config():
    from playwright.async_api import async_playwright, expect

    base = os.environ["WSL_LOGIN_TEST_URL"].rstrip("/")
    requests = []

    async def transport(route):
        requests.append(route.request)
        await route.fulfill(
            json={
                "status": "ok",
                "data": {
                    "groups": [
                        {
                            "id": "1516387",
                            "name": "真龙国际满点招商（大海传媒江哥团队）",
                            "role": "admin",
                        }
                    ],
                    "complete": True,
                },
            }
        )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        )
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.add_init_script("localStorage.setItem('astrbot-locale','zh-CN')")
            await page.route(base + "/api/**", transport)
            await page.goto(
                base + "/tests/fixtures/wangshangliao.html?existing=1&runtime=1"
            )
            await expect(page.get_by_text("业务运行中", exact=True)).to_be_visible()
            await expect(page.get_by_text("已启用群 1", exact=True)).to_be_visible()
            await expect(page.get_by_text("业务暂停", exact=True)).to_have_count(0)
            await expect(
                page.get_by_text(
                    "真龙国际满点招商（大海传媒江哥团队） · 管理员 (1516387)",
                    exact=True,
                )
            ).to_be_visible()
            await page.evaluate(
                "window.setWslFixtureRuntime({business_active:false,enabled_groups:[]})"
            )
            await expect(page.get_by_text("业务暂停", exact=True)).to_be_visible()
            await expect(page.get_by_text("已启用群 0", exact=True)).to_be_visible()
            await page.evaluate("window.setWslFixtureRuntime(null)")
            await expect(page.get_by_text("业务状态待核实", exact=True)).to_be_visible()
            assert await page.evaluate(
                "document.documentElement.scrollWidth <= innerWidth"
            )
            assert requests and all(
                item.method == "POST" and item.post_data_json.get("action") == "groups"
                for item in requests
            )
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("WSL_LOGIN_TEST_URL"), reason="Opt-in Vite fixture browser test"
)
@pytest.mark.parametrize("locale", ["zh-CN", "en-US"])
async def test_receive_health_does_not_treat_unhandled_notifications_as_bad_messages(
    locale,
):
    from playwright.async_api import async_playwright, expect

    base = os.environ["WSL_LOGIN_TEST_URL"].rstrip("/")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        )
        try:
            context = await browser.new_context(viewport={"width": 390, "height": 900})
            await context.add_init_script(
                f"localStorage.setItem('astrbot-locale','{locale}');"
            )
            page = await context.new_page()
            await page.route(re.compile(r"^https?://"), lambda route: route.abort())
            await page.route(base + "/**", lambda route: route.continue_())
            await page.route(
                base + "/api/**",
                lambda route: route.fulfill(
                    json={"status": "ok", "data": {"groups": [], "complete": True}}
                ),
            )
            await page.goto(
                base + "/tests/fixtures/wangshangliao.html?existing=1&runtime=1"
            )
            for count in (None, 0, -1, "522", "<script>secret</script>", 3):
                await page.evaluate(
                    "value => window.setWslFixtureRuntime(value)",
                    {
                        "connection_state": "online",
                        "business_active": True,
                        "enabled_groups": [],
                        "unsupported_pushes": 522,
                        "receive_health": {
                            "unhandled_packets": 522,
                            "decode_failures": count,
                        },
                    },
                )
                notice = page.get_by_test_id("wsl-receive-health")
                if count != 3:
                    await expect(notice).to_have_count(0)
                else:
                    await expect(notice).to_contain_text(
                        "3 条消息" if locale == "zh-CN" else "3 messages"
                    )
                    await expect(notice).to_contain_text("AstrBot")
                    await expect(
                        page.get_by_test_id("wsl-connection-status")
                    ).to_contain_text(
                        "账号已在线" if locale == "zh-CN" else "Account online"
                    )
                    for width in (320, 1280):
                        await page.set_viewport_size({"width": width, "height": 900})
                        assert await page.evaluate(
                            "document.documentElement.scrollWidth <= innerWidth"
                        )
            assert "secret" not in await page.locator("form").inner_text()
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("WSL_LOGIN_TEST_URL"), reason="Opt-in Vite fixture browser test"
)
@pytest.mark.parametrize("locale", ["zh-CN", "en-US"])
async def test_connection_notices_distinguish_current_state_and_historical_errors(
    locale,
):
    """Use live component updates without sending any login or management request."""
    from playwright.async_api import async_playwright, expect

    base = os.environ["WSL_LOGIN_TEST_URL"].rstrip("/")
    actions = []

    async def api(route):
        actions.append(route.request.post_data_json["action"])
        await route.fulfill(
            json={"status": "ok", "data": {"groups": [], "complete": True}}
        )

    cases = [
        (
            "online",
            "secondary_security_code_required",
            "账号已在线",
            "Account online",
            "历史错误记录不代表",
            "Historical errors do not mean",
        ),
        (
            "reconnecting",
            "reauth_required",
            "正在连接或重连",
            "Connecting or reconnecting",
            "系统会按间隔",
            "retry at intervals",
        ),
        (
            "rate_limited",
            "rate_limited",
            "平台限流，等待恢复",
            "Platform rate limit; waiting",
            "平台等待时间",
            "waiting period",
        ),
        (
            "reauth_required",
            "reauth_required",
            "连接已停止，需要处理",
            "Connection stopped; action required",
            "会话已失效",
            "session expired",
        ),
        (
            "account_conflict",
            "account_conflict",
            "连接已停止，需要处理",
            "Connection stopped; action required",
            "登录冲突",
            "sign-in conflict",
        ),
        (
            "error",
            "secondary_security_unverified",
            "连接已停止，需要处理",
            "Connection stopped; action required",
            "本次登录的安全验证",
            "sign-in needs security verification",
        ),
        (
            "error",
            "<script>secret-token</script>",
            "连接已停止，需要处理",
            "Connection stopped; action required",
            "具体原因尚未确认",
            "cause is not confirmed",
        ),
        (
            "error",
            "__proto__",
            "连接已停止，需要处理",
            "Connection stopped; action required",
            "具体原因尚未确认",
            "cause is not confirmed",
        ),
        (
            "error",
            "constructor",
            "连接已停止，需要处理",
            "Connection stopped; action required",
            "具体原因尚未确认",
            "cause is not confirmed",
        ),
        (
            "stopped",
            "",
            "消息连接已停止",
            "Messaging stopped",
            "保留登录信息",
            "retains its login",
        ),
        (
            "future-unknown-state",
            "",
            "连接状态待核实",
            "Connection status unavailable",
            "不能据此判断",
            "does not establish",
        ),
    ]
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        )
        try:
            context = await browser.new_context(viewport={"width": 390, "height": 844})
            await context.add_init_script(
                f"localStorage.setItem('astrbot-locale','{locale}');"
            )
            page = await context.new_page()
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            await page.route(re.compile(r"^https?://"), lambda route: route.abort())
            await page.route(base + "/**", lambda route: route.continue_())
            await page.route(base + "/api/**", api)
            await page.goto(
                base + "/tests/fixtures/wangshangliao.html?existing=1&runtime=1"
            )
            notice = page.get_by_test_id("wsl-connection-status")
            for state, error, title_zh, title_en, hint_zh, hint_en in cases:
                await page.evaluate(
                    "value => window.setWslFixtureRuntime(value)",
                    {
                        "connection_state": state,
                        "current_error": error,
                        "error_count": 3,
                        "business_active": False,
                        "enabled_groups": [],
                    },
                )
                await expect(notice).to_contain_text(
                    title_zh if locale == "zh-CN" else title_en
                )
                await expect(notice).to_contain_text(
                    hint_zh if locale == "zh-CN" else hint_en
                )
                assert "secret-token" not in await page.locator("form").inner_text()
                for width in (320, 555, 1280):
                    await page.set_viewport_size({"width": width, "height": 900})
                    assert await page.evaluate(
                        "document.documentElement.scrollWidth <= innerWidth"
                    )
                    assert await notice.evaluate(
                        "element => element.scrollWidth <= element.clientWidth"
                    )
            await page.evaluate("window.setWslFixtureRuntime(null)")
            await expect(notice).to_contain_text(
                "连接状态待核实"
                if locale == "zh-CN"
                else "Connection status unavailable"
            )
            await expect(
                page.get_by_text(
                    "启用群数待核实"
                    if locale == "zh-CN"
                    else "Enabled-group count unavailable",
                    exact=True,
                )
            ).to_be_visible()
            await expect(
                page.get_by_text(
                    "已启用群 0" if locale == "zh-CN" else "Enabled groups 0",
                    exact=True,
                )
            ).to_have_count(0)
            assert actions == ["groups"]
            assert not errors
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("WSL_LOGIN_TEST_URL"), reason="Opt-in Vite fixture browser test"
)
@pytest.mark.parametrize("locale", ["zh-CN", "en-US"])
async def test_business_notices_do_not_treat_paused_or_zero_groups_as_failed_login(
    locale,
):
    """Keep connection health independent of business configuration and group reads."""
    from playwright.async_api import async_playwright, expect

    base = os.environ["WSL_LOGIN_TEST_URL"].rstrip("/")
    actions = []

    async def api(route):
        actions.append(route.request.post_data_json["action"])
        await route.fulfill(status=503, json={"status": "error"})

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        )
        try:
            context = await browser.new_context(viewport={"width": 390, "height": 844})
            await context.add_init_script(
                f"localStorage.setItem('astrbot-locale','{locale}');"
            )
            page = await context.new_page()
            await page.route(re.compile(r"^https?://"), lambda route: route.abort())
            await page.route(base + "/**", lambda route: route.continue_())
            await page.route(base + "/api/**", api)
            await page.goto(
                base + "/tests/fixtures/wangshangliao.html?existing=1&runtime=1"
            )
            notice = page.get_by_test_id("wsl-business-status")
            for active, groups, hint_zh, hint_en in [
                (False, [], "业务已暂停", "Business is paused"),
                (True, [], "当前未启用任何业务群", "No business groups are enabled"),
                (
                    True,
                    ["1516387"],
                    "核实机器人具有所需群权限",
                    "necessary group permissions",
                ),
            ]:
                await page.evaluate(
                    "value => window.setWslFixtureRuntime(value)",
                    {
                        "connection_state": "online",
                        "business_active": active,
                        "enabled_groups": groups,
                    },
                )
                await expect(notice).to_contain_text(
                    hint_zh if locale == "zh-CN" else hint_en
                )
                await expect(
                    page.get_by_test_id("wsl-connection-status")
                ).to_contain_text(
                    "账号已在线" if locale == "zh-CN" else "Account online"
                )
            await expect(
                page.locator("form .v-alert").filter(
                    has_text=re.compile(
                        "群聊列表暂未读取成功|group list could not be loaded"
                    )
                )
            ).to_contain_text(
                "不代表账号已退出"
                if locale == "zh-CN"
                else "does not establish that you were signed out"
            )
            if locale == "zh-CN":
                output = Path("data/temp/wsl-state-browser-20261017")
                output.mkdir(parents=True, exist_ok=True)
                for width, name in [(390, "mobile"), (1280, "desktop")]:
                    await page.set_viewport_size({"width": width, "height": 900})
                    await page.screenshot(
                        path=str(output / f"connection-states-{name}.png"),
                        full_page=True,
                    )
                    assert await page.evaluate(
                        "document.documentElement.scrollWidth <= innerWidth"
                    )
            assert actions == ["groups"]
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("WSL_LOGIN_TEST_URL"), reason="Opt-in Vite fixture browser test"
)
@pytest.mark.parametrize(
    "connection,active,groups,hint",
    [
        ("online", True, [], "当前未启用任何业务群"),
        ("reauth_required", True, ["34"], "已启用群仍需保持连接在线"),
        ("online", False, ["34"], "业务已暂停"),
    ],
)
async def test_workbench_preserves_runtime_business_gate_when_merging_plugin_status(
    connection, active, groups, hint
):
    from playwright.async_api import async_playwright, expect

    base = os.environ["WSL_LOGIN_TEST_URL"].rstrip("/")
    bot = {
        "id": "fixture-existing",
        "type": "wangshangliao",
        "enable": True,
        "account_id": "42",
    }
    actions, errors = [], []

    async def api(route):
        request = route.request
        path = request.url.split("?", 1)[0]
        data = {}
        if path.endswith("/system-config/runtime"):
            data = {"config": {"platform": [bot]}, "metadata": {}}
        elif path.endswith("/bots/stats"):
            data = {
                "platforms": [
                    {
                        **bot,
                        "connection_state": connection,
                        "business_active": active,
                        "enabled_groups": groups,
                    }
                ]
            }
        elif path.endswith("/open-service"):
            assert request.post_data_json == {"action": "status", "payload": {}}
            data = {
                "mode": "integrated",
                "settings": {"paused": False},
                "accounts": [
                    {
                        "instance_id": bot["id"],
                        "account": "42",
                        "nickname": "Fixture",
                        "state": connection,
                    }
                ],
                "groups": [
                    {
                        "account": "42",
                        "group_id": group,
                        "active": True,
                        "present": True,
                        "paused": False,
                    }
                    for group in groups
                ],
            }
        elif path.endswith("/registration"):
            assert request.post_data_json["action"] == "groups"
            data = {"groups": [], "complete": True}
        else:
            assert request.method == "GET", path
        actions.append(path)
        await route.fulfill(json={"status": "ok", "data": data})

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        )
        try:
            context = await browser.new_context(viewport={"width": 390, "height": 844})
            await context.add_init_script(
                "localStorage.setItem('astrbot-locale','zh-CN');"
            )
            page = await context.new_page()
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            await page.route(re.compile(r"^https?://"), lambda route: route.abort())
            await page.route(base + "/**", lambda route: route.continue_())
            await page.route(base + "/api/**", api)
            await page.goto(base + "/tests/fixtures/wangshangliao.html?workbench=1")
            await expect(page.get_by_test_id("wsl-business-status")).to_contain_text(
                hint
            )
            await expect(page.locator(".bot-list-item__title")).to_have_text(
                "Fixture", timeout=15000
            )
            assert any(path.endswith("/open-service") for path in actions)
            await expect(page.get_by_test_id("wsl-business-status")).to_contain_text(
                hint
            )
            assert not errors
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("WSL_LOGIN_TEST_URL"), reason="Opt-in Vite fixture browser test"
)
@pytest.mark.parametrize("locale", ["zh-CN", "en-US"])
async def test_online_transport_with_expired_business_auth_requires_explicit_login(
    locale,
):
    from playwright.async_api import async_playwright, expect

    base = os.environ["WSL_LOGIN_TEST_URL"].rstrip("/")
    actions = []

    async def api(route):
        actions.append(route.request.post_data_json["action"])
        await route.fulfill(
            status=400, json={"status": "error", "message": "reauth_required"}
        )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        )
        try:
            context = await browser.new_context(viewport={"width": 390, "height": 844})
            await context.add_init_script(
                f"localStorage.setItem('astrbot-locale','{locale}');"
            )
            page = await context.new_page()
            await page.route(re.compile(r"^https?://"), lambda route: route.abort())
            await page.route(base + "/**", lambda route: route.continue_())
            await page.route(base + "/api/**", api)
            await page.goto(
                base + "/tests/fixtures/wangshangliao.html?existing=1&runtime=1"
            )
            await expect(page.get_by_test_id("wsl-business-auth")).to_contain_text(
                "登录已失效" if locale == "zh-CN" else "has expired"
            )
            connection = page.get_by_test_id("wsl-connection-status")
            await expect(connection).to_contain_text(
                "业务查询需要重新登录"
                if locale == "zh-CN"
                else "business queries require a new sign-in"
            )
            assert "无需重新登录" not in await connection.inner_text()
            assert "no new login is needed" not in await connection.inner_text()
            assert actions == ["groups"]
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("WSL_LOGIN_TEST_URL"), reason="Opt-in Vite fixture browser test"
)
@pytest.mark.parametrize("locale", ["zh-CN", "en-US"])
@pytest.mark.parametrize("width", [390, 1280])
async def test_runtime_business_expiry_and_android_profile_do_not_require_failed_refresh(
    locale, width
):
    """Show independent authentication state even when an earlier directory is cached."""
    from playwright.async_api import async_playwright, expect

    base = os.environ["WSL_LOGIN_TEST_URL"].rstrip("/")
    actions = []

    async def api(route):
        actions.append(route.request.post_data_json["action"])
        await route.fulfill(
            json={"status": "ok", "data": {"groups": [], "complete": True}}
        )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        )
        try:
            context = await browser.new_context(
                viewport={"width": width, "height": 844}
            )
            await context.add_init_script(
                f"localStorage.setItem('astrbot-locale','{locale}');"
            )
            page = await context.new_page()
            await page.route(re.compile(r"^https?://"), lambda route: route.abort())
            await page.route(base + "/**", lambda route: route.continue_())
            await page.route(base + "/api/**", api)
            await page.goto(
                base + "/tests/fixtures/wangshangliao.html?existing=1&runtime=1"
            )
            await page.evaluate(
                """() => window.setWslFixtureRuntime({
                    connection_state: 'online', business_active: false,
                    enabled_groups: [], business_auth: {
                        state: 'expired', device_type: 3, protocol_version: '3.7.3',
                    },
                })"""
            )
            notice = page.get_by_test_id("wsl-connection-status")
            await expect(notice).to_contain_text(
                "消息在线，业务登录已失效"
                if locale == "zh-CN"
                else "Messaging online; business sign-in expired"
            )
            await expect(page.get_by_test_id("wsl-business-auth")).to_contain_text(
                "手机登录" if locale == "zh-CN" else "phone sign-in"
            )
            await expect(page.get_by_test_id("wsl-device-profile")).to_contain_text(
                "安卓端登录" if locale == "zh-CN" else "Android device class"
            )
            assert "无需重新登录" not in await notice.inner_text()
            assert await page.evaluate(
                "document.documentElement.scrollWidth <= window.innerWidth"
            )
            await page.evaluate(
                """() => window.setWslFixtureRuntime({
                    connection_state: 'online', business_active: false,
                    enabled_groups: [], business_auth: {
                        state: 'credential_present', device_type: 1,
                        protocol_version: '2.8.3',
                    },
                })"""
            )
            await expect(page.get_by_test_id("wsl-business-auth")).to_have_count(0)
            await expect(page.get_by_test_id("wsl-device-profile")).to_have_count(0)
            assert actions == ["groups"]
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("WSL_LOGIN_TEST_URL"), reason="Opt-in Vite fixture browser test"
)
async def test_sms_errors_cooldown_cancellation_and_manual_verification():
    """Check real login controls without contacting the messaging provider."""
    from playwright.async_api import async_playwright, expect

    base_url = os.environ["WSL_LOGIN_TEST_URL"].rstrip("/")
    requests = []
    starts = 0
    phase = "rejected"
    entered, release = asyncio.Event(), asyncio.Event()
    output = Path("data/temp/wsl-login-browser-20261011")
    output.mkdir(parents=True, exist_ok=True)

    async def api(route):
        nonlocal starts
        request = route.request
        payload = request.post_data_json if request.method == "POST" else {}
        requests.append(payload)
        data = {}
        if "/registration" in request.url:
            action = payload["action"]
            if action == "start":
                starts += 1
            data = {
                "instance_id": payload["instance_id"],
                "registration_code": payload.get(
                    "registration_code", f"fixture-{starts}"
                ),
                "captcha_id": "fixture",
                "expires_in": 600,
                "status": "pending",
                "error": "",
                "error_action": "",
                "provider_code": None,
            }
            if action == "request_sms":
                if phase == "delayed":
                    entered.set()
                    await release.wait()
                    data["status"] = "sms_required"
                elif phase == "rejected":
                    data.update(
                        status="failed",
                        error="business_rejected",
                        error_action="request_sms",
                        provider_code=1201,
                        resend_after=60,
                    )
                elif phase == "captcha" and not payload.get("validate_str"):
                    data.update(error="captcha_required", error_action="request_sms")
                elif phase == "envelope":
                    await route.fulfill(
                        json={"status": "error", "message": "registration_rate_limit"}
                    )
                    return
                else:
                    data.update(status="sms_required", resend_after=60)
            elif action == "verify_sms":
                if payload.get("verification_code") == "103900":
                    data.update(
                        status="sms_required",
                        error="phone_not_found",
                        error_action="verify_sms",
                        provider_code=1039,
                        resend_after=45,
                    )
                elif payload.get("verification_code") == "123456":
                    data.update(
                        status="authenticated",
                        session_ref="fixture-save",
                        account_id="12",
                        nickname="Fixture",
                    )
                else:
                    data.update(
                        status="sms_required",
                        error="invalid_sms",
                        error_action="verify_sms",
                        provider_code=1203,
                        resend_after=45,
                    )
            elif action == "groups":
                data = {"groups": [], "complete": True}
            elif action == "cancel":
                data = {"status": "cancelled"}
        elif "/config-profiles" in request.url:
            data = {
                "profiles": [{"id": "default", "name": "Default"}],
                "config": {},
                "metadata": {},
            }
        elif "/config-routes" in request.url:
            data = {"routing": {}}
        await route.fulfill(json={"status": "ok", "data": data})

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        )
        context = await browser.new_context(
            viewport={"width": 555, "height": 900}, reduced_motion="reduce"
        )
        try:
            await context.add_init_script(
                """
                localStorage.setItem('astrbot-locale', 'zh-CN');
                window.initNECaptcha = (options, ready) => ready({
                  verify: () => options.onVerify(null, {validate: 'fixture-proof'}),
                  refresh() {}, destroy() {},
                });
                """
            )
            page = await context.new_page()
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            await page.route(f"{base_url}/api/**", api)
            await page.route(
                re.compile(r"^https?://(?!127\.0\.0\.1)"), lambda route: route.abort()
            )
            await page.goto(f"{base_url}/tests/fixtures/wangshangliao.html")
            await page.get_by_role("combobox").click()
            await page.get_by_role("listbox").get_by_text("旺商聊", exact=True).click()
            await page.get_by_role("button", name="开始登录", exact=True).click()
            await page.get_by_role("button", name="短信登录", exact=True).click()
            phone = page.get_by_role("textbox", name=re.compile("手机号"))
            send = page.get_by_role("button", name=re.compile("^验证并获取验证码"))
            alerts = page.locator("form .v-alert")
            await phone.fill("not-a-phone")
            before = len(requests)
            await send.click()
            await expect(alerts).to_contain_text("11位手机号")
            assert len(requests) == before
            await phone.fill("13800000000")
            await send.click()
            await expect(alerts).to_contain_text("未接受短信请求")
            await expect(alerts).to_contain_text("平台返回码：1201")
            await expect(send).to_be_disabled()
            await expect(
                page.get_by_role("textbox", name=re.compile("短信验证码"))
            ).to_have_count(0)
            assert sum(r.get("action") == "request_sms" for r in requests) == 1
            await page.screenshot(path=str(output / "sms-rejected-555.png"))
            for width in (320, 390, 555, 1440):
                await page.set_viewport_size({"width": width, "height": 900})
                await page.screenshot(path=str(output / f"sms-rejected-{width}.png"))
                assert await page.evaluate(
                    "() => document.documentElement.scrollWidth <= innerWidth"
                )
                assert await page.locator("form").evaluate(
                    """element => [...element.querySelectorAll('button')].every(
                      button => button.scrollWidth <= button.clientWidth)"""
                )
            await page.get_by_role("button", name="密码登录", exact=True).click()
            await expect(alerts).to_have_count(0)
            await page.get_by_role("button", name="取消", exact=True).first.click()
            await page.get_by_role("button", name="开始登录", exact=True).click()
            await page.get_by_role("button", name="短信登录", exact=True).click()
            await phone.fill("13800000000")
            phase = "delayed"
            await send.click()
            await entered.wait()
            await expect(page.get_by_role("status")).to_contain_text(
                "正在验证并请求短信"
            )
            await page.get_by_role("button", name="取消", exact=True).first.click()
            await page.get_by_role("button", name="开始登录", exact=True).click()
            await expect(phone).to_be_visible()
            release.set()
            await page.wait_for_timeout(200)
            await expect(phone).to_be_visible()
            await expect(
                page.get_by_role("textbox", name=re.compile("短信验证码"))
            ).to_have_count(0)
            phase = "envelope"
            await send.click()
            await expect(alerts).to_contain_text("尝试过于频繁")
            await page.get_by_role("button", name="取消", exact=True).first.click()
            await page.get_by_role("button", name="开始登录", exact=True).click()
            await phone.fill("13800000000")
            phase = "captcha"
            await send.click()
            code = page.get_by_role("textbox", name=re.compile("短信验证码"))
            await expect(code).to_be_visible()
            await expect(
                page.get_by_role("button", name=re.compile("^重发短信"))
            ).to_be_disabled()
            login = page.get_by_role("button", name="验证并登录", exact=True)
            before = len(requests)
            await code.fill("123")
            await login.click()
            await expect(alerts).to_contain_text("6位短信验证码")
            assert len(requests) == before
            await code.fill("000000")
            await login.click()
            await expect(alerts).to_contain_text("短信验证码错误")
            await expect(alerts).to_contain_text("平台返回码：1203")
            await code.fill("103900")
            await login.click()
            await expect(alerts).to_contain_text("未找到该手机号对应的账号")
            await expect(alerts).to_contain_text("平台返回码：1039")
            await expect(alerts).not_to_contain_text("未接受登录请求")
            for width in (320, 555, 1440):
                await page.set_viewport_size({"width": width, "height": 900})
                assert await page.evaluate(
                    "() => document.documentElement.scrollWidth <= innerWidth"
                )
            await page.screenshot(path=str(output / "phone-not-found-1440.png"))
            await code.fill("123456")
            await login.click()
            await expect(alerts).to_contain_text("认证成功但尚未连接")
            assert not errors
            assert not any("config" in request for request in requests)
        finally:
            release.set()
            await context.close()
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("WSL_LOGIN_TEST_URL"), reason="Opt-in Vite fixture browser test"
)
@pytest.mark.parametrize(
    ("http_status", "message", "expected", "blocked"),
    [
        (
            400,
            "account_change_requires_new_instance",
            "新账号不能覆盖旧机器人实例",
            True,
        ),
        (
            200,
            "account_change_requires_new_instance",
            "新账号不能覆盖旧机器人实例",
            True,
        ),
        (400, "session_reference_invalid", "登录保存凭据已失效", True),
        (400, "account_already_configured", "此账号已有机器人实例", True),
        (400, "platform_config_fields", "保存内容与服务器不兼容", False),
        (400, "ai_routes_invalid", "现有模型与群配置不匹配", False),
        (400, "developer_test_invalid", "测试配置与当前群设置不匹配", False),
        (
            400,
            "请分别保存连接设置与插件业务设置",
            "群设置发生变化",
            False,
        ),
        (
            400,
            "请在旺商聊插件管理页修改业务设置",
            "群设置发生变化",
            False,
        ),
        (500, "private-provider-payload<script>", "保存登录未完成", False),
    ],
)
async def test_existing_bot_save_errors_preserve_specific_safe_reason(
    http_status, message, expected, blocked
):
    """Keep authentication separate from rejected saves without exposing raw failures."""
    from playwright.async_api import async_playwright, expect

    base_url = os.environ["WSL_LOGIN_TEST_URL"].rstrip("/")
    updates = []

    async def api(route):
        request = route.request
        payload = request.post_data_json if request.method in {"POST", "PUT"} else {}
        if request.method == "GET" and "/api/v1/bots/by-id" in request.url:
            await route.fulfill(
                json={
                    "status": "ok",
                    "data": {
                        "bot": {
                            "id": "fixture-existing",
                            "type": "wangshangliao",
                            "account_id": "42",
                            "_business_revision": "fixture-policy-revision",
                        }
                    },
                }
            )
            return
        if request.method == "PUT":
            updates.append(payload)
            await route.fulfill(
                status=http_status, json={"status": "error", "message": message}
            )
            return
        action = payload.get("action")
        data = {
            "instance_id": payload.get("instance_id", "fixture-existing"),
            "registration_code": "fixture-existing-registration",
            "captcha_id": "fixture",
            "expires_in": 600,
            "status": "pending",
        }
        if action == "login":
            data.update(
                status="authenticated",
                session_ref="fixture-save",
                account_id="12",
                nickname="New fixture account",
            )
        elif action == "groups":
            data = {"groups": [], "complete": True}
        await route.fulfill(json={"status": "ok", "data": data})

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        )
        context = await browser.new_context(
            viewport={"width": 555, "height": 900}, reduced_motion="reduce"
        )
        try:
            await context.add_init_script(
                """
                localStorage.setItem('astrbot-locale', 'zh-CN');
                window.initNECaptcha = (options, ready) => ready({
                  verify: () => options.onVerify(null, {validate: 'fixture-proof'}),
                  refresh() {}, destroy() {},
                });
                """
            )
            page = await context.new_page()
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            await page.route(f"{base_url}/api/**", api)
            await page.route(
                re.compile(r"^https?://(?!127\.0\.0\.1)"), lambda route: route.abort()
            )
            await page.goto(f"{base_url}/tests/fixtures/wangshangliao.html?existing=1")
            await page.get_by_role("button", name="重新登录", exact=True).click()
            await page.get_by_role("textbox", name=re.compile("^账号")).fill("fixture")
            await page.locator('form input[type="password"]').fill("fixture")
            await page.get_by_role("button", name="验证并登录", exact=True).click()
            save = page.get_by_role("button", name="保存登录并连接", exact=True)
            await expect(save).to_be_visible()
            await save.click()
            await expect(page.locator("form .v-alert").first).to_contain_text(expected)
            await expect(page.locator("form .v-alert").first).not_to_contain_text(
                "登录失败"
            )
            assert len(updates) == 1
            assert updates[0]["bot_id"] == "fixture-existing"
            assert updates[0]["config"]["session_ref"] == "fixture-save"
            assert updates[0]["config"]["account_id"] == "12"
            if blocked:
                await expect(save).to_be_disabled()
            else:
                await expect(save).to_be_enabled()
                if message.startswith("private-"):
                    assert message not in await page.locator("form").inner_text()
            for width in (320, 555, 1440):
                await page.set_viewport_size({"width": width, "height": 900})
                assert await page.evaluate(
                    "() => document.documentElement.scrollWidth <= innerWidth"
                )
            output = Path("data/temp/wsl-login-browser-20261012")
            output.mkdir(parents=True, exist_ok=True)
            name = message if blocked else "unknown"
            await page.screenshot(path=str(output / f"save-{http_status}-{name}.png"))
            assert not errors
        finally:
            await context.close()
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("WSL_LOGIN_TEST_URL"), reason="Opt-in Vite fixture browser test"
)
@pytest.mark.parametrize("locale", ["zh-CN", "en-US"])
@pytest.mark.parametrize(
    "integrated",
    ["legacy", "managed", "transport_only", "lookup_failed", "wrong_instance"],
)
async def test_reauthentication_save_excludes_runtime_and_managed_policy(
    locale, integrated
):
    """Save a verified login without sending effective rules back as configuration."""
    from playwright.async_api import async_playwright, expect

    base = os.environ["WSL_LOGIN_TEST_URL"].rstrip("/")
    updates, actions = [], []

    async def api(route):
        req = route.request
        payload = req.post_data_json if req.method in {"POST", "PUT"} else {}
        if req.method == "GET" and "/api/v1/bots/by-id" in req.url:
            bot = {
                "id": "fixture-existing",
                "type": "wangshangliao",
                "account_id": "42",
                **(
                    {"_business_revision": "fixture-policy-revision"}
                    if integrated != "legacy"
                    else {}
                ),
            }
            if integrated == "wrong_instance":
                bot["id"] = "another-instance"
            await route.fulfill(
                json={
                    "status": "ok",
                    "data": {} if integrated == "lookup_failed" else {"bot": bot},
                }
            )
            return
        if req.method == "PUT":
            updates.append(payload)
            await route.fulfill(json={"status": "ok"})
            return
        action = payload.get("action")
        actions.append(action)
        data = {
            "instance_id": "fixture-existing",
            "registration_code": "fixture-reconnect",
            "captcha_id": "fixture",
            "expires_in": 600,
            "status": "pending",
        }
        if action == "login":
            data.update(status="security_required", account_id="42")
        elif action == "verify_security_code":
            data.update(
                status="authenticated",
                session_ref="fixture-authenticated-session",
                account_id="42",
                nickname="Fixture account",
            )
        elif action == "groups":
            data = {"groups": [], "complete": True}
        await route.fulfill(json={"status": "ok", "data": data})

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        )
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.add_init_script(
                f"localStorage.setItem('astrbot-locale','{locale}');"
                "window.initNECaptcha=(options,ready)=>ready({"
                "verify:()=>options.onVerify(null,{validate:'fixture-proof'}),"
                "refresh(){},destroy(){}});"
            )
            await page.route(re.compile(r"^https?://"), lambda route: route.abort())
            await page.route(base + "/**", lambda route: route.continue_())
            await page.route(base + "/api/**", api)
            url = base + "/tests/fixtures/wangshangliao.html?existing=1"
            await page.goto(url + ("&integrated=1" if integrated == "managed" else ""))
            await page.get_by_role(
                "button", name=re.compile("^(重新登录|Log in again)$")
            ).click()
            await page.get_by_role("textbox", name=re.compile("^(账号|Account)")).fill(
                "fixture"
            )
            await page.locator('form input[type="password"]').fill("fixture-password")
            await page.get_by_role(
                "button", name=re.compile("^(验证并登录|Verify and log in)$")
            ).click()
            await page.locator('form input[type="password"]').fill("123456")
            await page.get_by_role(
                "button", name=re.compile("^(验证安全码|Verify security code)")
            ).click()
            save = page.get_by_role(
                "button", name=re.compile("^(保存登录并连接|Save login and connect)$")
            )
            if integrated in {"lookup_failed", "wrong_instance"}:
                await save.click()
                await expect(page.locator("form .v-alert").first).to_contain_text(
                    "暂时无法核对" if locale == "zh-CN" else "could not be verified"
                )
                assert updates == []
                assert actions.count("verify_security_code") == 1
                return
            async with page.expect_navigation(wait_until="domcontentloaded"):
                await save.click()
            await expect(
                page.get_by_role("button", name=re.compile("^(重新登录|Log in again)$"))
            ).to_be_visible(timeout=15000)
            assert len(updates) == 1
            saved = updates[0]["config"]
            assert saved["session_ref"] == "fixture-authenticated-session"
            assert saved["account_id"] == "42"
            assert saved["enable"] is False
            assert not any(key.startswith("_") for key in saved)
            business = {
                "enabled_groups",
                "reply_private",
                "reply_groups",
                "moderation",
                "ai_routes",
                "proactive_send",
                "developer_test",
            }
            if integrated != "legacy":
                assert not business.intersection(saved)
            else:
                assert saved["enabled_groups"] == []
            assert actions.count("verify_security_code") == 1
        finally:
            await browser.close()
