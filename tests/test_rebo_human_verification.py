"""Exercise the actual verification component with a fake official SDK."""

import json
import subprocess
from urllib.parse import urlsplit

import pytest
from playwright.async_api import async_playwright


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
    "outcome", ["verified", "cancel", "sdk_blocked", "service_error", "init_timeout"]
)
async def test_trusted_verification_frame(verification_bundle, outcome):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path="/root/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome",
            headless=True,
            args=["--no-sandbox"],
        )
        page = await browser.new_page(viewport={"width": 390, "height": 800})
        errors = []
        external_requests = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.add_init_script("""
            const original=window.setTimeout;
            window.setTimeout=(fn,ms,...args)=>original(fn,ms>=150000?10000:ms>=25000?1500:ms,...args);
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
                      button.onclick=()=>options.onVerify(null,{{validate:'test-human-proof'}});
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
        await page.goto("https://verification.test/")
        if outcome in {"verified", "cancel"}:
            frame = page.frame_locator('iframe[title="官方人机验证"]')
            await frame.locator("button").wait_for(timeout=5000)
            await page.locator(".v-overlay__content").evaluate(
                "(node)=>Promise.all(node.getAnimations().map(animation=>animation.finished))"
            )
            assert await page.evaluate("window.verificationResults.length") == 0
            # A forged top-level response cannot impersonate the trusted frame.
            await page.evaluate("""
                const source=document.querySelector('iframe').srcdoc;
                const requestKey=source.match(/const requestKey = "([^"]+)"/)[1];
                window.postMessage({channel:'astrbot-human-verification',requestKey,
                  kind:'verified',validate:'forged-proof'},location.origin);
            """)
            await page.wait_for_timeout(30)
            assert await page.evaluate("window.verificationResults.length") == 0
            if outcome == "verified":
                await frame.locator("button").click()
            else:
                await page.get_by_role("button", name="取消", exact=True).click()
        await page.wait_for_function("window.verificationResults.length===1")
        results = await page.evaluate("window.verificationResults")
        if outcome == "verified":
            assert results == [{"kind": "verified", "validate": "test-human-proof"}]
        elif outcome == "cancel":
            assert results == [{"kind": "cancel"}]
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
        await browser.close()
