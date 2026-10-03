"""Read-only native Page acceptance; no reload, account mutations or model calls."""

import json
import sys
from pathlib import Path

from deploy import NAME, client
from playwright.sync_api import sync_playwright

OUT = Path("/root/Projects/agents/telethon-ai-deployment")


def main():
    with client() as api, sync_playwright() as playwright:
        response = api.get(f"/api/plug/{NAME}/status")
        response.raise_for_status()
        state = response.json()
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox"],
            executable_path="/root/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome",
        )
        context = browser.new_context(viewport={"width": 1400, "height": 960})
        context.add_init_script(
            "if(window.top === window){localStorage.setItem('token', "
            + json.dumps(api.headers["Authorization"].removeprefix("Bearer "))
            + ");localStorage.setItem('astrbot:first_notice_seen:v1','1');}"
        )
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)[:300]))
        marker = (
            "?__cf_access_message=unauthorized" if "--access-marker" in sys.argv else ""
        )
        page.goto(str(api.base_url) + f"/{marker}#/plugin-page/{NAME}/accounts")
        frame = page.frame_locator("iframe")
        frame.locator("#accounts tr").first.wait_for(timeout=45000)
        assert frame.locator("#accounts tr").count() == len(state["accounts"])
        for account in state["accounts"]:
            row = frame.locator("#accounts tr").filter(
                has=frame.locator(".account-identity small").filter(
                    has_text=f"平台别名：{account['account']}"
                )
            )
            assert (
                row.locator(".ai-profile .ai-value").inner_text()
                == account["profile_name"]
            )
            assert row.locator(".ai-persona .ai-value").inner_text() == (
                account["persona"] or "未指定人格"
            )
            assert account["daily_limit"] is None
            assert account["quota_scope"] == "tenant_authorization"
            assert (
                row.locator(".daily-usage")
                .inner_text()
                .startswith(f"{account['daily_used']} 次")
            )
            assert "账号不设日上限" in row.locator(".daily-usage").inner_text()
        assert frame.locator("#accounts .group-list").count() == 0
        frame.locator("#tab-groups").click()
        management = state.get("group_management", {"bindings": [], "uncertain": 0})
        assert frame.locator("#management-count").inner_text() == (
            f"{len(management['bindings'])} 项绑定 · "
            f"{management['uncertain']} 项操作待复核"
        )
        assert frame.locator("#management-groups").is_visible()
        for account in state["accounts"]:
            owner = next(
                (t for t in state["tenants"] if t["id"] == account["tenant"]), None
            )
            target = "#recycled" if owner and owner["expired"] else "#groups"
            frame.locator(
                "#tab-recycle" if target == "#recycled" else "#tab-groups"
            ).click()
            for group in account.get("groups", []):
                assert (
                    frame.locator(target + " .group-list code")
                    .filter(has_text=f"群号：{group['id']}")
                    .count()
                    >= 1
                )
                if group["name"]:
                    assert (
                        frame.locator(target + " .group-name")
                        .filter(has_text=group["name"])
                        .count()
                        >= 1
                    )
        for width, height, name in [(1400, 960, "desktop"), (390, 844, "mobile")]:
            page.set_viewport_size({"width": width, "height": height})
            page.wait_for_timeout(400)
            scrim = page.locator(".v-navigation-drawer__scrim")
            if scrim.is_visible():
                scrim.click(position={"x": 380, "y": 20})
                page.wait_for_timeout(400)
            for tab in (
                "accounts",
                "authorization",
                "groups",
                "recycle",
                "diagnostics",
                "preview",
            ):
                frame.locator(f"#tab-{tab}").click()
                assert frame.locator(f"#panel-{tab}").is_visible()
                assert not frame.locator("body").evaluate(
                    "(el) => el.scrollWidth > window.innerWidth"
                )
                page.screenshot(path=str(OUT / f"operations-{name}-{tab}.png"))
            frame.locator("#tab-authorization").click()
            for tenant in state["tenants"]:
                target = "#recycled" if tenant["expired"] else "#authorizations"
                frame.locator(
                    "#tab-recycle" if tenant["expired"] else "#tab-authorization"
                ).click()
                assert tenant["id"] not in frame.locator("#accounts").inner_text()
                assert tenant["owner"] in frame.locator(target).inner_text()
                if tenant.get("owner_username"):
                    assert (
                        "@" + tenant["owner_username"]
                        in frame.locator(target).inner_text()
                    )
                if tenant.get("bot_username"):
                    assert (
                        "@" + tenant["bot_username"]
                        in frame.locator(target).inner_text()
                    )
            frame.locator("#tab-diagnostics").click()
            assert frame.locator("#checks dd").count() == 3
            frame.locator("#tab-accounts").click()
            frame.locator("#add-account").click()
            assert frame.locator("#add-dialog").is_visible()
            assert frame.locator("#login-phone").is_visible()
            assert frame.locator("#login-api-hash").get_attribute("type") == "password"
            assert not frame.locator("#login-code-field").is_visible()
            assert not frame.locator("#add-form").evaluate("(el) => el.checkValidity()")
            assert not frame.locator("#add-dialog").evaluate(
                "(el) => el.scrollWidth > el.clientWidth"
            )
            page.screenshot(path=str(OUT / f"operations-{name}-add-account.png"))
            frame.locator("#login-cancel").click()
            assert not frame.locator("#add-dialog").is_visible()
        assert not errors, errors
        browser.close()
        after = api.get(f"/api/plug/{NAME}/status").json()
        assert [
            {k: v for k, v in t.items() if k != "expiry_notice"}
            for t in after["tenants"]
        ] == [
            {k: v for k, v in t.items() if k != "expiry_notice"}
            for t in state["tenants"]
        ]
        assert after["accounts"] == state["accounts"]
        print(
            "PASS: separate accounts/authorization/groups/recycle, six tabs and add-account dialog, desktop/mobile, no business mutation"
        )


if __name__ == "__main__":
    main()
