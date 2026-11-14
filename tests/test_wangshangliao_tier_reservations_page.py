"""Opt-in compiled reservation editor checks against isolated activity APIs."""

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from tests.test_wangshangliao_dashboard import PREFIX
from tests.test_wangshangliao_dashboard import headers as headers
from tests.test_wangshangliao_dashboard import page_app as page_app


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [320, 390, 1440])
@pytest.mark.skipif(
    not os.environ.get("WSL_ACTIVITY_PAGE"), reason="Opt-in compiled UI"
)
async def test_reservation_editor_handles_amount_changes_and_stale_searches(
    page_app, headers, tmp_path, width
):
    from playwright.async_api import async_playwright, expect

    app, dashboard, _, _, _ = page_app
    dashboard.plugin = SimpleNamespace(
        open_service=SimpleNamespace(
            active=True,
            integrated=False,
            roster=AsyncMock(
                return_value={
                    "2": {
                        "nimId": "102",
                        "groupMemberNick": "Member Alpha",
                        "accountState": "ACCOUNT_STATE_GOOD",
                    },
                    "3": {
                        "nimId": "103",
                        "groupMemberNick": "Member Beta",
                        "accountState": "ACCOUNT_STATE_GOOD",
                    },
                }
            ),
        )
    )
    release = asyncio.Event()
    started = asyncio.Event()
    finished = asyncio.Event()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fixture",
        headers=headers,
    ) as client:

        async def api(_source, method, endpoint, body=None):
            if endpoint == "lottery-members" and body.get("query") == "Beta":
                started.set()
                await release.wait()
            response = await client.request(
                method,
                PREFIX + "/" + endpoint,
                **({"params": body or {}} if method == "GET" else {"json": body}),
            )
            assert response.status_code == 200, response.text
            data = response.json()["data"]
            if endpoint == "lottery-members" and body.get("query") == "Beta":
                finished.set()
            return data

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(args=["--no-sandbox"])
            page = await browser.new_page(viewport={"width": width, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.expose_binding("fixtureAPI", api)
            await page.add_init_script("""
                const originalTimeout = window.setTimeout.bind(window);
                window.setTimeout = (callback, delay, ...args) =>
                    originalTimeout(callback,
                        window.shortSearchTimeout && delay === 18000 ? 25 : delay,
                        ...args);
                Object.defineProperty(window, "AstrBotPluginPage", {value: {
                    ready: async () => ({}), getLocale: () => "zh-CN",
                    onContext: () => () => {},
                    apiGet: (endpoint, body) => window.fixtureAPI("GET", endpoint, body),
                    apiPost: (endpoint, body) => window.fixtureAPI("POST", endpoint, body),
                }});
            """)
            try:
                await page.goto(
                    Path(os.environ["WSL_ACTIVITY_PAGE"]).resolve().as_uri()
                )
                await page.get_by_role("tab", name="活动与财务", exact=True).click()
                await page.get_by_role("tab", name="玩家福利抽奖", exact=True).click()
                await page.get_by_label("红包类型", exact=True).select_option("tiered")
                await page.get_by_label("活动名称", exact=True).fill("Reservation test")
                await page.get_by_label("领奖联系人", exact=True).fill(
                    "Test administrator"
                )
                await page.get_by_label("总金额（元）", exact=True).fill("100")
                await page.get_by_label("参加人数", exact=True).fill("3")
                await page.get_by_role("button", name="添加档位", exact=True).click()
                tier = page.get_by_label("每份金额（元）", exact=True)
                await tier.fill("50")
                query = page.get_by_label("当前群成员", exact=True)
                search = page.locator(".lottery-member-query").get_by_role(
                    "button", name="搜索", exact=True
                )
                results = page.get_by_role("radiogroup", name="指定成员搜索结果")
                await query.fill("Beta")
                await search.click()
                await asyncio.wait_for(started.wait(), 5)
                await query.fill("Alpha")
                await expect(search).to_be_enabled()
                await search.click()
                await expect(results).to_contain_text("Member Alpha")
                release.set()
                await asyncio.wait_for(finished.wait(), 5)
                await expect(results).not_to_contain_text("Member Beta")
                await results.get_by_role("radio").check()
                await results.get_by_role("button", name="加入指定方案").click()
                reservations = page.locator(".lottery-reserved-members")
                await expect(reservations).to_contain_text("Member Alpha")
                await tier.fill("60")
                await expect(reservations).to_contain_text("60 元")
                await expect(
                    page.get_by_label("指定成员对应固定档位", exact=True)
                ).to_have_value("60")
                await page.get_by_role(
                    "button", name="保存抽奖参数", exact=True
                ).click()
                await expect(
                    page.get_by_text("活动参数已保存", exact=True)
                ).to_be_visible()
                await expect(reservations).to_contain_text("60.00 元")
                await page.get_by_role("button", name="添加档位", exact=True).click()
                await expect(tier).to_have_count(2)
                await tier.nth(1).fill("60")
                await expect(tier.nth(1)).not_to_have_value("60")
                await (
                    page.get_by_role("button", name="删除档位", exact=True)
                    .nth(1)
                    .click()
                )
                assert await page.locator("form form").count() == 0
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth"
                )
                output = Path(os.environ.get("WSL_ACTIVITY_OUTPUT", str(tmp_path)))
                output.mkdir(parents=True, exist_ok=True)
                await page.screenshot(
                    path=str(output / f"tier-reservations-{width}.png"), full_page=True
                )
                # Reset invalidates a pending search, without waiting for that request.
                release.clear()
                started.clear()
                finished.clear()
                await query.fill("Beta")
                await search.click()
                await asyncio.wait_for(started.wait(), 5)
                await tier.fill("65")
                await page.get_by_role("button", name="还原", exact=True).click()
                release.set()
                await asyncio.wait_for(finished.wait(), 5)
                await expect(results).to_have_count(0)
                await expect(query).to_have_value("")
                await expect(reservations).to_contain_text("60.00 元")
                # A timed-out bridge call must not leave the editor permanently busy.
                release.clear()
                started.clear()
                finished.clear()
                await page.evaluate("window.shortSearchTimeout = true")
                await query.fill("Beta")
                await search.click()
                await expect(
                    page.get_by_text("群成员查询超时，请稍后重试", exact=True)
                ).to_be_visible()
                await expect(search).to_be_enabled()
                release.set()
                await asyncio.wait_for(finished.wait(), 5)
                await expect(results).to_have_count(0)
                await page.evaluate("window.shortSearchTimeout = false")
                # Switching groups invalidates responses even when member IDs overlap.
                release.clear()
                started.clear()
                finished.clear()
                await search.click()
                await asyncio.wait_for(started.wait(), 5)
                await page.get_by_label("当前群", exact=True).select_option("6")
                release.set()
                await asyncio.wait_for(finished.wait(), 5)
                await expect(query).to_have_count(0)
                await page.get_by_label("红包类型", exact=True).select_option("tiered")
                await expect(results).to_have_count(0)
                await expect(query).to_have_value("")
                assert not errors
            finally:
                release.set()
                await browser.close()
