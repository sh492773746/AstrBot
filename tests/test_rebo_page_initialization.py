"""Browser checks using fake bridge responses; no real account actions."""

from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.async_api import async_playwright

from astrbot.dashboard.services.plugin_page_service import PluginPageService


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [390, 1100])
async def test_sandboxed_page_preserves_access_cookie_and_real_bridge(width):
    root = Path("data/plugins/astrbot_plugin_rebo_live/pages/settings")
    service = PluginPageService(plugin_manager=None)
    content_path = service.build_plugin_page_content_path(
        "astrbot_plugin_rebo_live", "settings"
    )
    html = service.rewrite_plugin_page_html(
        (root / "index.html").read_text(),
        "astrbot_plugin_rebo_live",
        "settings",
        "index.html",
        theme="light",
        extra_query_params={"asset_token": "test-scoped-asset"},
    )
    loaded_scripts = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path="/root/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome",
            headless=True,
            args=["--no-sandbox"],
        )
        context = await browser.new_context(viewport={"width": width, "height": 900})
        await context.add_cookies(
            [
                {
                    "name": "test-access",
                    "value": "authorized",
                    "url": "https://rebo.test",
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "None",
                }
            ]
        )
        page = await context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        async def route(request):
            path = urlsplit(request.request.url).path
            headers = service.build_security_headers()
            # Access gateways also require their cookie for script requests.
            if "test-access=authorized" not in request.request.headers.get(
                "cookie", ""
            ):
                await request.fulfill(
                    status=403, body="Access authentication required", headers=headers
                )
                return
            if path == "/":
                await request.fulfill(
                    content_type="text/html",
                    body=f"""
                    <script>
                    window.receivedActions=[];
                    window.addEventListener('message',event=>{{
                      const frame=document.querySelector('iframe');
                      if(event.source!==frame.contentWindow)return;
                      const message=event.data;
                      if(message.channel!=='astrbot-plugin-page')return;
                      if(message.kind==='ready'){{
                        event.source.postMessage({{
                          channel:message.channel,kind:'context',
                          context:{{locale:'zh-CN',isDark:false}}
                        }},'*');
                      }}
                      if(message.kind==='request'){{
                        if(message.action!=='api:get')window.receivedActions.push(message);
                        event.source.postMessage({{
                          channel:message.channel,kind:'response',
                          requestId:message.requestId,ok:true,
                          data:{{account:'待登录',rooms:{{}},visible:{{}},next_send:{{}},grants:{{}}}}
                        }},'*');
                      }}
                    }});
                    </script>
                    <iframe src="{content_path}?asset_token=test-scoped-asset"
                      sandbox="allow-scripts allow-forms allow-downloads"
                      style="border:0;width:100%;height:850px"></iframe>
                    """,
                )
                return
            if path == content_path:
                await request.fulfill(
                    body=html, content_type="text/html", headers=headers
                )
                return
            assert request.request.url.endswith("asset_token=test-scoped-asset")
            loaded_scripts.append(path.rsplit("/", 1)[-1])
            body = (
                service.bridge_file.read_text()
                if path == "/api/plugin/page/bridge-sdk.js"
                else (root / path.rsplit("/", 1)[-1]).read_text()
            )
            await request.fulfill(
                body=body, content_type="application/javascript", headers=headers
            )

        await page.route("https://rebo.test/**", route)
        await page.goto("https://rebo.test/")
        frame = page.frame_locator("iframe")
        await frame.locator("#phone").fill("13900000000", timeout=5000)
        await frame.locator('input[value="sms"]').check()
        await frame.locator("#sms-code").fill("123456")
        assert await frame.locator("#sms-login").is_enabled()
        assert "已就绪" in await frame.locator("#page-state").inner_text()
        assert set(loaded_scripts) == {"bootstrap.js", "bridge-sdk.js", "app.js"}
        assert await page.evaluate("window.receivedActions.length") == 0
        assert not errors
        await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "ready",
        "disabled_text",
        "missing",
        "unauthorized",
        "blocked",
        "timeout",
        "mutation_timeout",
    ],
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
            if mode == "disabled_text":
                status = """Promise.resolve({
                    account:'已登录',grants:{},next_send:{},
                    rooms:{r:{enabled:true,revision:1,texts:['safe','rejected'],
                        disabled_items:{'1':{reason:'消息审核拒绝（406）'}},
                        history:[{time:1,part:2,result:'rejected',reason:'消息审核拒绝（406）'}]}},
                    visible:{r:{title:'Test room'}}
                })"""
            await page.add_init_script(
                f"window.AstrBotPluginPage={{ready:()=>{ready},apiGet:()=>{status},apiPost:()=>new Promise(()=>{{}})}};"
            )

        async def route(request):
            name = request.request.url.rsplit("/", 1)[-1] or "index.html"
            if name == "bridge-sdk.js":
                await request.fulfill(body="", content_type="application/javascript")
                return
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
        if mode in {"ready", "disabled_text"}:
            assert await page.locator("#login").is_enabled()
            assert "已就绪" in await page.locator("#page-state").inner_text()
            if mode == "disabled_text":
                await page.locator("#rooms summary").click()
                assert (
                    "第2条 · 已停用：消息审核拒绝（406）"
                    in await page.locator("#rooms").inner_text()
                )
        else:
            assert await page.locator("#login").is_disabled()
            assert await page.locator("#reload-page").is_visible()
            assert "⚠️" in await page.locator("#page-state").inner_text()
        await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [390, 1100])
async def test_sms_login_human_verification_and_errors(width):
    root = Path("data/plugins/astrbot_plugin_rebo_live/pages/settings")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path="/root/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome",
            headless=True,
            args=["--no-sandbox"],
        )
        page = await browser.new_page(viewport={"width": width, "height": 900})
        await page.add_init_script("""
            window.sentActions=[];
            window.AstrBotPluginPage={
              ready:()=>Promise.resolve(),
              apiGet:()=>Promise.resolve({account:'待登录',rooms:{},visible:{},next_send:{},grants:{}}),
              verifyHuman:()=>new Promise((resolve,reject)=>{
                window.completeHumanVerification=resolve;
                window.cancelHumanVerification=()=>reject(new Error('verification_cancelled'));
              }),
              apiPost:(_,payload)=>{
                window.sentActions.push(payload);
                if(payload.action==='sms_login')return Promise.resolve({status:'error',message:'验证码错误，请重新输入'});
                return Promise.resolve({ok:true,message:'验证码已发送，请查收短信'});
              }
            };
        """)

        async def route(request):
            name = request.request.url.rsplit("/", 1)[-1] or "index.html"
            if name == "bridge-sdk.js":
                await request.fulfill(body="", content_type="application/javascript")
                return
            await request.fulfill(
                body=(root / name).read_text(),
                content_type="text/html"
                if name.endswith(".html")
                else "application/javascript",
            )

        await page.route("http://rebo.test/**", route)
        await page.goto("http://rebo.test/index.html")
        await page.locator('input[value="sms"]').check()
        assert await page.locator("#sms-login").is_visible()
        assert not await page.locator("#password").is_visible()
        await page.locator("#phone").fill("13900000000")
        await page.locator("#sms-send").click()
        assert await page.evaluate("window.sentActions.length") == 0
        await page.evaluate(
            "window.completeHumanVerification({validate:'human-proof'})"
        )
        await page.wait_for_function("window.sentActions.length===1")
        assert "验证码已发送" in await page.locator("#notice").inner_text()
        assert await page.locator("#sms-send").is_disabled()
        assert await page.evaluate("window.sentActions[0].action") == "sms_send"
        await page.locator("#sms-code").fill("123456")
        await page.locator("#sms-login").click()
        assert await page.locator("#sms-code").input_value() == ""
        assert "验证码错误" in await page.locator("#notice").inner_text()
        assert await page.locator("#sms-login").is_enabled()
        assert await page.evaluate("document.documentElement.scrollWidth<=innerWidth")
        await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", ["cancel", "phone_changed", "bridge_missing", "host_outdated"]
)
async def test_sms_verification_failures_do_not_submit(outcome):
    root = Path("data/plugins/astrbot_plugin_rebo_live/pages/settings")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path="/root/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome",
            headless=True,
            args=["--no-sandbox"],
        )
        page = await browser.new_page()
        await page.add_init_script("""
            window.sentActions=[];
            window.AstrBotPluginPage={
              ready:()=>Promise.resolve(),
              apiGet:()=>Promise.resolve({account:'待登录',rooms:{},visible:{},next_send:{},grants:{}}),
              verifyHuman:()=>new Promise((resolve,reject)=>{
                window.completeHumanVerification=resolve;
                window.cancelHumanVerification=()=>reject(new Error('verification_cancelled'));
              }),
              apiPost:(_,payload)=>{window.sentActions.push(payload);return Promise.resolve({ok:true});},
            };
        """)

        async def route(request):
            name = urlsplit(request.request.url).path.rsplit("/", 1)[-1]
            await request.fulfill(
                body="" if name == "bridge-sdk.js" else (root / name).read_text(),
                content_type="text/html"
                if name.endswith(".html")
                else "application/javascript",
            )

        await page.route("https://rebo.test/**", route)
        await page.goto("https://rebo.test/index.html")
        await page.locator('input[value="sms"]').check()
        await page.locator("#phone").fill("13900000000")
        if outcome == "bridge_missing":
            await page.evaluate("delete window.AstrBotPluginPage.verifyHuman")
        elif outcome == "host_outdated":
            await page.evaluate("""()=>{
                window.AstrBotPluginPage.verifyHuman=()=>Promise.reject(
                  new Error('Unsupported plugin bridge action: verification:yidun'));
            }
            """)
        await page.locator("#sms-send").click()
        if outcome == "cancel":
            await page.evaluate("window.cancelHumanVerification()")
        elif outcome == "phone_changed":
            await page.locator("#phone").fill("13800000000")
            await page.evaluate(
                "window.completeHumanVerification({validate:'human-proof'})"
            )
        await page.wait_for_function(
            "document.getElementById('notice').textContent!=='请手动完成人机验证'"
        )
        assert await page.evaluate("window.sentActions.length") == 0
        assert await page.locator("#sms-send").is_enabled()
        expected = {
            "cancel": "已取消",
            "phone_changed": "手机号已变更",
            "bridge_missing": "刷新整个 WebUI",
            "host_outdated": "刷新整个 WebUI",
        }
        assert expected[outcome] in await page.locator("#notice").inner_text()
        await browser.close()
