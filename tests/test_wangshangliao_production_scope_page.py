"""Opt-in production selectors with stopped account histories and stale selection."""

import os
from pathlib import Path

import pytest

from scripts.check_wsl_open_page import PUBLIC
from scripts.check_wsl_policy_page import FIXTURE


@pytest.mark.asyncio
@pytest.mark.skipif(not os.environ.get("WSL_SCOPE_PAGE"), reason="Opt-in compiled UI")
async def test_stopped_accounts_hidden_and_removed_scope_cleared(tmp_path):
    """Inspect compiled selectors without production requests or configuration writes.

    Args:
        tmp_path: Isolated screenshots and browser output directory.
    """
    from playwright.async_api import async_playwright, expect

    asset = Path(os.environ["WSL_SCOPE_PAGE"]).resolve().as_uri()
    stopped = """
      const settingsGet = window.AstrBotPluginPage.apiGet;
      window.AstrBotPluginPage.apiGet = async (endpoint, ...args) => {
        const result = await settingsGet(endpoint, ...args);
        return endpoint === "settings" ? {...result,bots:[...result.bots,
          {id:"stopped-a",account_id:"77",nickname:"停用甲",enable:false,state:"stopped"},
          {id:"stopped-b",account_id:"88",nickname:"停用乙",enable:false,state:"stopped"}
        ]} : result;
      };
    """
    history = """
      publicState.groups.push(
        {...publicState.groups[0],account:"77",name:"停用甲的历史群"},
        {...publicState.groups[0],account:"88",name:"停用乙的历史群"});
      window.fixtureState = publicState;
    """
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"),
            args=["--no-sandbox"],
        )
        try:
            for width in (320, 390, 1440):
                page = await browser.new_page(viewport={"width": width, "height": 1000})
                errors = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                await page.add_init_script(FIXTURE + PUBLIC + stopped + history)
                await page.goto(asset)
                scope = page.locator(".open-service")
                selector = scope.get_by_label("群聊", exact=True)
                await expect(selector.locator("option")).to_have_count(3)
                assert not await selector.locator(
                    'option[value^="77/"], option[value^="88/"]'
                ).count()
                await selector.select_option("99/5")
                await expect(
                    scope.get_by_text("原生群管理权限：已核验", exact=True)
                ).to_be_visible()
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth"
                )
                await page.screenshot(
                    path=str(tmp_path / f"production-scope-{width}.png")
                )
                before = await page.evaluate(
                    "window.policyRequests.filter(r=>r.body?.action==='tenant_members').length"
                )
                await page.evaluate("window.fixtureState.accounts = []")
                await scope.get_by_role("button", name="刷新开放服务").click()
                await expect(selector).to_have_value("")
                await expect(selector.locator("option")).to_have_count(1)
                assert (
                    await page.evaluate(
                        "window.policyRequests.filter(r=>r.body?.action==='tenant_members').length"
                    )
                    == before
                )
                assert not [
                    request
                    for request in await page.evaluate("window.policyRequests")
                    if request["body"].get("action") not in {"status", "tenant_members"}
                ]
                assert not errors
                await page.close()

                page = await browser.new_page(viewport={"width": width, "height": 1000})
                await page.add_init_script(FIXTURE + stopped)
                await page.goto(asset)
                selector = page.locator(".scope-bar select").first
                await expect(selector.locator("option")).to_have_count(1)
                await expect(selector).to_have_value("fixture")
                assert all(
                    request["body"].get("action") == "status"
                    for request in await page.evaluate("window.policyRequests")
                )
                await page.close()
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.skipif(not os.environ.get("WSL_SCOPE_PAGE"), reason="Opt-in compiled UI")
async def test_gateway_error_is_actionable_and_writes_are_not_retried():
    """Check explicit recovery without repeating a potentially committed write."""
    from playwright.async_api import async_playwright, expect

    asset = Path(os.environ["WSL_SCOPE_PAGE"]).resolve().as_uri()
    failure = """
      const gatewayPost = window.AstrBotPluginPage.apiPost;
      window.gatewayFailures = {status:1,tenant:1};
      window.AstrBotPluginPage.apiPost = async (endpoint, body) => {
        if (endpoint === "open-service" && window.gatewayFailures[body.action] > 0) {
          window.gatewayFailures[body.action]--;
          window.policyRequests.push({endpoint,body:structuredClone(body)});
          throw new Error("Request failed with status code 502");
        }
        const result = await gatewayPost(endpoint, body);
        if (endpoint === "open-service" && body.action === "tenant_members") {
          result.members = result.members.map(m=>({...m,
            role:m.id === "1" ? "GROUP_ROLE_ADMIN" : "GROUP_ROLE_MEMBER"}));
        }
        return result;
      };
    """
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"),
            args=["--no-sandbox"],
        )
        try:
            page = await browser.new_page()
            await page.add_init_script(FIXTURE + PUBLIC + failure)
            await page.goto(asset)
            scope = page.locator(".open-service")
            await expect(scope.get_by_role("alert")).to_contain_text(
                "管理接口暂时不可用（502）"
            )
            await page.wait_for_timeout(500)
            assert (
                await page.evaluate(
                    "window.policyRequests.filter(r=>r.body?.action==='status').length"
                )
                == 1
            )
            await scope.get_by_role("button", name="刷新开放服务").click()
            await expect(scope.get_by_role("alert")).to_have_count(0)
            await scope.get_by_label("群聊", exact=True).select_option("99/5")
            await scope.get_by_role("button", name="保存本群", exact=True).click()
            await expect(scope.get_by_role("alert")).to_contain_text("勿重复提交")
            await page.wait_for_timeout(500)
            assert (
                await page.evaluate(
                    "window.policyRequests.filter(r=>r.body?.action==='tenant').length"
                )
                == 1
            )
            assert (
                await page.evaluate(
                    "window.policyRequests.filter(r=>r.body?.action==='status').length"
                )
                == 2
            )
            await expect(
                scope.get_by_role("status").filter(has_text="本群配置已保存")
            ).to_have_count(0)
        finally:
            await browser.close()
