"""Opt-in responsive image-rule controls against the real isolated RPC."""

import os
from pathlib import Path

import httpx
import pytest

from astrbot.builtin_stars.wangshangliao_moderation.open_rpc import OpenCall, OpenRPC
from tests.test_wangshangliao_dashboard import PREFIX
from tests.test_wangshangliao_dashboard import headers as headers
from tests.test_wangshangliao_dashboard import page_app as page_app
from tests.test_wangshangliao_open_rpc_guards import rpc_scope as rpc_scope


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("WSL_ACTIVITY_PAGE"), reason="Opt-in compiled UI"
)
async def test_media_flags_save_without_changing_other_groups(
    page_app, headers, rpc_scope, tmp_path
):
    from playwright.async_api import async_playwright, expect

    service, adapter, _ = rpc_scope
    adapter.groups = {"5": "1005", "6": "1006"}
    rpc = OpenRPC(service)
    output = Path(os.environ.get("WSL_ACTIVITY_OUTPUT", str(tmp_path)))
    output.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=page_app[0]),
        base_url="http://fixture",
        headers=headers,
    ) as client:

        async def api(_source, method, endpoint, body=None):
            if endpoint == "open-service":
                return await rpc.dispatch(OpenCall(actor="dashboard/offline", **body))
            if method == "GET" and endpoint == "settings":
                result = (await client.get(PREFIX + "/settings")).json()["data"]
                result["open_mode"] = "integrated"
                result["bots"][0]["account_id"] = "99"
                return result
            response = await client.request(
                method,
                PREFIX + "/" + endpoint,
                **({"params": body or {}} if method == "GET" else {"json": body}),
            )
            assert response.status_code == 200
            return response.json()["data"]

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(args=["--no-sandbox"])
            try:
                for width in (320, 390, 1440):
                    page = await browser.new_page(
                        viewport={"width": width, "height": 1000}
                    )
                    errors = []
                    page.on("pageerror", lambda exc: errors.append(str(exc)))
                    await page.expose_binding("fixtureAPI", api)
                    await page.add_init_script("""
                      Object.defineProperty(window, "AstrBotPluginPage", {value:{
                        ready:async()=>({}),getLocale:()=> "zh-CN",onContext:()=>()=>{},
                        apiGet:(endpoint,body)=>window.fixtureAPI("GET",endpoint,body),
                        apiPost:(endpoint,body)=>window.fixtureAPI("POST",endpoint,body),
                      }});
                    """)
                    await page.goto(
                        Path(os.environ["WSL_ACTIVITY_PAGE"]).resolve().as_uri()
                    )
                    await page.get_by_role("tab", name="开通", exact=True).click()
                    scope = page.locator(".open-service")
                    await scope.get_by_label("群聊", exact=True).select_option("99/5")
                    await page.get_by_role("tab", name="规则", exact=True).click()
                    for label in (
                        "撤回合并聊天记录（白名单除外）",
                        "撤回二维码图片（本地识别）",
                        "撤回含数字图片（也包括截图、时间、金额）",
                    ):
                        await scope.get_by_role(
                            "checkbox", name=label, exact=True
                        ).check()
                    await scope.get_by_role(
                        "button", name="保存规则", exact=True
                    ).click()
                    await expect(
                        scope.get_by_text("本群配置已保存", exact=True)
                    ).to_be_visible()
                    saved = service.store.tenant("99", "5")["rules"]
                    assert all(
                        saved[field] is True
                        for field in (
                            "recall_forward",
                            "image_qr_recall",
                            "image_digits_recall",
                        )
                    )
                    assert (
                        "image_qr_recall"
                        not in service.store.tenant("99", "6")["rules"]
                    )
                    assert await page.evaluate(
                        "document.documentElement.scrollWidth <= innerWidth"
                    )
                    await scope.locator(".fields:visible").first.screenshot(
                        path=str(output / f"media-rules-{width}.png")
                    )
                    assert not errors
                    await page.close()
            finally:
                await browser.close()
