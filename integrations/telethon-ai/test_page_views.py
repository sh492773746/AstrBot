import json
from pathlib import Path

from playwright.sync_api import sync_playwright


def test_accounts_user_authorization_and_group_usage_are_separate():
    source = (
        Path(__file__).resolve().parents[2]
        / "data/plugins/astrbot_plugin_telethon_ai/pages/accounts/index.html"
    )
    account = {
        "account": "one",
        "user_id": "10",
        "username": "ai_one",
        "tenant": "internal-tenant-A",
        "state": "running",
        "enabled": True,
        "paused": False,
        "model": "model",
        "persona": "persona",
        "allowed_chats": ["-123", "-789"],
        "profile_name": "Renamed account profile",
        "profile_id": "account-profile",
        "allowed_senders": ["100"],
        "disclosure_confirmed": True,
        "groups": [
            {"id": "-123", "name": "Group A"},
            {"id": "-789", "name": "Config only"},
        ],
        "daily_used": 105,
        "daily_limit": 3,
    }
    tenant = {
        "id": "internal-tenant-A",
        "owner": "100",
        "owner_username": "customer_one",
        "bot": "200",
        "bot_username": "customer_one_bot",
        "accounts": ["one"],
        "groups": ["-123"],
        "group_details": [{"id": "-123", "name": "Group A"}],
        "enabled": True,
        "expired": False,
        "remaining": 29,
        "budget": 30,
        "used": 1,
        "expires": 1800000000,
    }
    status = {
        "accounts": [
            account,
            {
                **account,
                "account": "two",
                "user_id": "20",
                "tenant": None,
                "allowed_chats": ["-456"],
                "groups": [{"id": "-456", "name": "<img src=x>"}],
            },
        ],
        "tenants": [tenant],
        "tenant_count": 1,
        "customer_entries": 1,
        "control_attached": True,
        "checks": {},
        "recent": [],
    }
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox"],
        )
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.add_init_script(
            "window.snapshot="
            + json.dumps(status)
            + ";window.AstrBotPluginPage={ready:async()=>{},apiGet:async()=>window.snapshot};"
        )
        page.goto(source.as_uri())
        page.locator("#accounts tr").first.wait_for()
        assert page.get_by_role("tab").all_text_contents() == [
            "账号",
            "授权",
            "群聊",
            "回收桶",
            "诊断",
            "试聊",
        ]
        assert "internal-tenant-A" not in page.locator("#accounts").inner_text()
        assert "Group A" not in page.locator("#accounts").inner_text()
        assert "账号 ID：10" in page.locator("#accounts").inner_text()
        assert "@ai_one" in page.locator("#accounts").inner_text()
        assert "平台别名：one" in page.locator("#accounts").inner_text()
        assert page.locator("#accounts .daily-usage").first.inner_text() == (
            "105 次\n按用户授权限额 · 账号不设日上限"
        )
        assert "105 / 3" not in page.locator("#accounts").inner_text()
        assert page.locator("#accounts .ai-config").first.locator(
            ".ai-label"
        ).all_text_contents() == [
            "配置档",
            "对话模型",
            "人格 ID",
        ]
        assert (
            page.locator("#accounts .ai-profile .ai-value").first.inner_text()
            == "Renamed account profile"
        )
        assert (
            page.locator("#accounts .ai-model .ai-value").first.inner_text() == "model"
        )
        assert (
            page.locator("#accounts .ai-persona .ai-value").first.inner_text()
            == "persona"
        )
        page.locator("#tab-authorization").click()
        assert "Telegram ID：100" in page.locator("#authorizations").inner_text()
        assert "Bot ID：200" in page.locator("#authorizations").inner_text()
        assert "one" in page.locator("#authorizations").inner_text()
        assert "@customer_one" in page.locator("#authorizations").inner_text()
        assert "@customer_one_bot" in page.locator("#authorizations").inner_text()
        assert "@ai_one" in page.locator("#authorizations").inner_text()
        assert "Group A" not in page.locator("#authorizations").inner_text()
        page.locator("#tab-groups").click()
        assert page.locator("#groups tr").count() == 3
        authorized = page.locator("#groups tr").filter(has_text="群号：-123")
        assert "配置就绪" in authorized.inner_text()
        assert "Telegram ID：100" in authorized.inner_text()
        assert "@customer_one" in authorized.inner_text()
        assert "@ai_one" in authorized.inner_text()
        assert (
            "未获用户群授权"
            in page.locator("#groups tr").filter(has_text="群号：-789").inner_text()
        )
        assert (
            "未分配用户"
            in page.locator("#groups tr").filter(has_text="群号：-456").inner_text()
        )
        assert page.locator("#groups img").count() == 0
        assert not page.locator("body").evaluate(
            "(el)=>el.scrollWidth>window.innerWidth"
        )
        page.evaluate("window.snapshot.tenants[0].expired=true")
        page.locator("#refresh").click()
        page.wait_for_function(
            "document.querySelector('#recycled').textContent.includes('@customer_one')"
        )
        assert page.locator("#groups tr").filter(has_text="群号：-123").count() == 0
        assert page.locator("#groups tr").filter(has_text="群号：-789").count() == 0
        page.locator("#tab-authorization").click()
        assert "@customer_one" not in page.locator("#authorizations").inner_text()
        page.locator("#tab-recycle").click()
        assert "Group A" in page.locator("#recycled").inner_text()
        assert "@customer_one_bot" in page.locator("#recycled").inner_text()
        assert not page.locator("body").evaluate(
            "(el)=>el.scrollWidth>window.innerWidth"
        )
        page.evaluate("window.snapshot.tenants[0].expired=false")
        page.locator("#refresh").click()
        page.wait_for_function(
            "document.querySelector('#authorizations').textContent.includes('@customer_one')"
        )
        assert "@customer_one" not in page.locator("#recycled").inner_text()
        browser.close()
