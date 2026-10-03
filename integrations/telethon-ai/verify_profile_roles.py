"""Read-only desktop/mobile acceptance of native role-aware configuration."""

import json
from pathlib import Path

from deploy import client
from playwright.sync_api import sync_playwright

OUT = Path("/root/Projects/agents/telethon-ai-deployment")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with client() as api, sync_playwright() as playwright:
        profiles = api.get("/api/v1/config-profiles").json()["data"]["info_list"]
        samples = {}
        for profile in profiles:
            data = api.get(f"/api/v1/config-profiles/{profile['id']}").json()["data"]
            role = data.get("profile_role", {}).get("role")
            if role:
                samples.setdefault(role, profile)
        samples["other"] = next(p for p in profiles if p["name"].startswith("大海传媒"))
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox"],
            executable_path="/root/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome",
        )
        context = browser.new_context(viewport={"width": 1400, "height": 960})
        context.add_init_script(
            "if(window.top === window){localStorage.setItem('token',"
            + json.dumps(api.headers["Authorization"].removeprefix("Bearer "))
            + ");localStorage.setItem('astrbot:first_notice_seen:v1','1');}"
        )
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)[:250]))
        page.goto(str(api.base_url) + "/#/config")
        page.locator(".config-profile-trigger").wait_for(timeout=45000)
        for width, height, viewport in [(1400, 960, "desktop"), (390, 844, "mobile")]:
            page.set_viewport_size({"width": width, "height": height})
            page.wait_for_timeout(500)
            scrim = page.locator(".v-navigation-drawer__scrim")
            if scrim.is_visible():
                scrim.click(position={"x": 380, "y": 20})
            for role, profile in samples.items():
                page.locator(".config-profile-trigger").click()
                page.locator(".config-profile-menu__item").filter(
                    has_text=profile["name"]
                ).click()
                page.wait_for_function(
                    "(name) => document.querySelector('.config-profile-trigger')?.textContent.includes(name)",
                    arg=profile["name"],
                )
                page.wait_for_timeout(600)
                if role == "other":
                    assert page.locator(".config-role-summary").count() == 0
                    assert page.locator(".config-workspace__nav-item").count() == 4
                else:
                    page.locator(".config-role-summary").wait_for()
                    if role in ("controller", "customer"):
                        assert page.locator(".config-workspace__nav-item").count() == 0
                        assert page.locator("button .mdi-content-save").count() == 0
                        assert page.locator("button .mdi-code-json").count() == 0
                    else:
                        assert page.locator(".config-workspace__nav-item").count() == 2
                        assert page.locator(
                            ".ai-config-tabs__item"
                        ).all_text_contents() == [
                            "模型",
                            "人格",
                            "高级",
                        ]
                        assert (
                            page.locator(
                                ".ai-config-panel__actions .mdi-dots-horizontal"
                            ).count()
                            == 0
                        )
                        page.locator(".ai-config-tabs__item").filter(
                            has_text="人格"
                        ).click()
                        assert (
                            "知识库"
                            not in page.locator(".ai-config-panel").inner_text()
                        )
                        assert (
                            "工具" not in page.locator(".ai-config-panel").inner_text()
                        )
                        page.locator(".ai-config-tabs__item").filter(
                            has_text="模型"
                        ).click()
                assert not page.locator("body").evaluate(
                    "(el) => el.scrollWidth > window.innerWidth"
                )
                summary = page.locator(".config-role-summary")
                if summary.count():
                    for element in summary.locator("h2, dt, dd, code").all():
                        assert not element.evaluate(
                            "(el) => el.scrollWidth > el.clientWidth + 1"
                        )
                page.screenshot(
                    path=str(OUT / f"config-roles-{viewport}-{role}.png"),
                    full_page=True,
                )
        assert not errors, errors
        browser.close()
        print(
            "PASS: native config roles, unchanged unrelated profile, desktop/mobile, no model calls"
        )


if __name__ == "__main__":
    main()
