"""Exercise the actual verification component with a fake official SDK."""

import json
import subprocess
from urllib.parse import urlsplit

import pytest
from playwright.async_api import async_playwright, expect


@pytest.fixture(scope="module")
def verification_bundle(tmp_path_factory):
    output = tmp_path_factory.mktemp("rebo-verification-bundle")
    subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            """
            import {build} from 'vite';
            import vue from '@vitejs/plugin-vue';
            import vuetify from 'vite-plugin-vuetify';
            await build({
              configFile:false,
              plugins:[vue(),vuetify({autoImport:true})],
              define:{'process.env.NODE_ENV':'"production"'},
              logLevel:'error',
              build:{
                outDir:process.argv[1],
                lib:{entry:'tests/fixtures/rebo-human-verification.js',
                  formats:['es'],fileName:()=>'verification.js'},
              },
            });
            """,
            str(output),
        ],
        cwd="dashboard",
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "paused_presentation"),
    [
        pytest.param("verified", False, id="verified"),
        pytest.param("cancel", False, id="cancel"),
        pytest.param("sdk_blocked", False, id="sdk_blocked"),
        pytest.param("service_error", False, id="service_error"),
        pytest.param("init_timeout", False, id="init_timeout"),
        pytest.param("verified", True, id="verified-paused-presentation"),
        pytest.param("cancel", True, id="cancel-paused-presentation"),
    ],
)
async def test_trusted_verification_frame(
    verification_bundle, outcome, paused_presentation
):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox"],
        )
        page = await browser.new_page(viewport={"width": 390, "height": 800})
        errors = []
        external_requests = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.add_init_script("""
            if (window === window.top) {
              window.verificationDiagnostics = {messages: [], clicks: []};
              window.addEventListener('message', event => {
                const message = event.data;
                if (message?.channel !== 'astrbot-human-verification') return;
                const frame = document.querySelector('iframe');
                const key = frame?.srcdoc.match(/const requestKey = "([^"]+)"/)?.[1];
                window.verificationDiagnostics.messages.push({
                  kind: message.kind,
                  ...('validate' in message ? {validate: message.validate} : {}),
                  ...('reason' in message ? {reason: message.reason} : {}),
                  origin: event.origin,
                  fromFrame: event.source === frame?.contentWindow,
                  matchingKey: message.requestKey === key,
                });
              });
            }
        """)
        if outcome == "init_timeout":
            await page.add_init_script("""
                const original=window.setTimeout;
                window.setTimeout=(fn,ms,...args)=>original(fn,ms===30000?1500:ms,...args);
            """)

        async def route(request):
            url = urlsplit(request.request.url)
            if url.netloc == "cstaticdun.126.net":
                external_requests.append(url.path)
                if outcome == "sdk_blocked":
                    await request.abort()
                    return
                await request.fulfill(
                    content_type="application/javascript",
                    body=f"""
                    window.initNECaptcha=(options,ready,failed)=>{{
                      document.cookie='verification_probe=1;SameSite=None;Secure';
                      localStorage.setItem('verification_probe','1');
                      if(Function('return 1')()!==1)throw new Error('sdk_eval_unavailable');
                      if(options.mode!=='embed')throw new Error('unexpected_mode');
                      if({json.dumps(outcome)}==='service_error'){{failed({{}});return;}}
                      if({json.dumps(outcome)}==='init_timeout')return;
                      const button=document.createElement('button');
                      button.textContent='Mock human completion';
                      button.onclick=event=>{{
                        const click={{trusted:event.isTrusted,callbackReturned:false}};
                        parent.verificationDiagnostics.clicks.push(click);
                        options.onVerify(null,{{validate:'test-human-proof'}});
                        click.callbackReturned=true;
                      }};
                      document.querySelector(options.element).append(button);
                      ready({{destroy:()=>{{}}}});
                    }};
                    """,
                )
                return
            assert url.netloc == "verification.test"
            path = url.path.lstrip("/")
            if not path:
                css = "".join(
                    f'<link rel="stylesheet" href="/{file.name}">'
                    for file in verification_bundle.glob("*.css")
                )
                await request.fulfill(
                    content_type="text/html",
                    body=f'{css}<div id="app"></div><script type="module" src="/verification.js"></script>',
                )
            else:
                await request.fulfill(
                    body=(verification_bundle / path).read_bytes(),
                    content_type="text/css"
                    if path.endswith(".css")
                    else "application/javascript",
                )

        await page.route("**/*", route)
        try:
            await page.goto("https://verification.test/")
            if outcome in {"verified", "cancel"}:
                frame = page.frame_locator('iframe[title="官方人机验证"]')
                button = frame.get_by_role("button", name="Mock human completion")
                await expect(button).to_be_visible()
                # Seeing the mock button does not mean Vue consumed frame readiness.
                await expect(page.get_by_role("status")).to_have_text(
                    "请手动完成下方验证，完成后才会请求短信。"
                )
                if paused_presentation:
                    state = await page.locator(".v-overlay__content").evaluate("""
                        node => {
                          const animation = node.animate(
                            [{opacity: 1}, {opacity: 1}],
                            {duration: 1000, fill: 'both'},
                          );
                          animation.pause();
                          return animation.playState;
                        }
                    """)
                    assert state == "paused"
                # Paused presentation animations need not finish for a real click.
                await page.wait_for_function("""
                    () => {
                      const node = document.querySelector('.v-overlay__content');
                      return document.fonts.status === 'loaded' &&
                        getComputedStyle(node).pointerEvents !== 'none' &&
                        ![...node.classList].some(name =>
                          /-(enter|appear)-(from|active|to)$/.test(name));
                    }
                """)
                messages = await page.evaluate(
                    "window.verificationDiagnostics.messages"
                )
                assert messages == [
                    {
                        "kind": "ready",
                        "origin": "https://verification.test",
                        "fromFrame": True,
                        "matchingKey": True,
                    }
                ]
                assert await page.evaluate("window.verificationResults") == []
                # Wait for delivery, not a fixed sleep, before asserting rejection.
                await page.evaluate("""
                    const source=document.querySelector('iframe').srcdoc;
                    const requestKey=source.match(/const requestKey = "([^"]+)"/)[1];
                    window.postMessage({channel:'astrbot-human-verification',requestKey,
                      kind:'verified',validate:'forged-proof'},location.origin);
                """)
                await page.wait_for_function("""
                    window.verificationDiagnostics.messages.some(message =>
                      message.validate === 'forged-proof')
                """)
                messages = await page.evaluate(
                    "window.verificationDiagnostics.messages"
                )
                assert messages[-1] == {
                    "kind": "verified",
                    "validate": "forged-proof",
                    "origin": "https://verification.test",
                    "fromFrame": False,
                    "matchingKey": True,
                }
                assert await page.evaluate("window.verificationResults") == []
                if outcome == "verified":
                    await button.click()
                else:
                    await page.get_by_role("button", name="取消", exact=True).click()
            await page.wait_for_function("window.verificationResults.length===1")
            results = await page.evaluate("window.verificationResults")
            diagnostics = await page.evaluate("window.verificationDiagnostics")
            if outcome == "verified":
                assert results == [{"kind": "verified", "validate": "test-human-proof"}]
                assert diagnostics["clicks"] == [
                    {"trusted": True, "callbackReturned": True}
                ]
                assert diagnostics["messages"][-1] == {
                    "kind": "verified",
                    "validate": "test-human-proof",
                    "origin": "https://verification.test",
                    "fromFrame": True,
                    "matchingKey": True,
                }
            elif outcome == "cancel":
                assert results == [{"kind": "cancel"}]
                assert diagnostics["clicks"] == []
            else:
                reason = {
                    "sdk_blocked": "verification_sdk_load_failed",
                    "service_error": "verification_service_unavailable",
                    "init_timeout": "verification_init_timeout",
                }[outcome]
                assert results == [{"kind": "failed", "reason": reason}]
            assert await page.locator("iframe").count() == 0
            assert external_requests == ["/load.min.js"]
            assert not errors
        except Exception as error:
            diagnostics = await page.evaluate("""
                () => {
                  const frame = document.querySelector('iframe');
                  const button = frame?.contentDocument?.querySelector('button');
                  const overlay = document.querySelector('.v-overlay__content');
                  return {
                    ...window.verificationDiagnostics,
                    results: window.verificationResults,
                    fonts: document.fonts.status,
                    status: document.querySelector('[role="status"]')?.textContent,
                    button: button && {
                      text: button.textContent,
                      rect: button.getBoundingClientRect().toJSON(),
                    },
                    overlay: overlay && {
                      classes: overlay.className,
                      rect: overlay.getBoundingClientRect().toJSON(),
                      pointerEvents: getComputedStyle(overlay).pointerEvents,
                      animations: overlay.getAnimations().map(animation => ({
                        state: animation.playState,
                        type: animation.constructor.name,
                        property: animation.transitionProperty,
                      })),
                    },
                  };
                }
            """)
            raise AssertionError(
                f"Verification {outcome} failed: "
                f"{json.dumps(diagnostics, ensure_ascii=True)}; "
                f"page_errors={errors}; external_requests={external_requests}"
            ) from error
        finally:
            await browser.close()
