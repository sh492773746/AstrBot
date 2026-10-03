"""Exercise the named administrator control without granting real privileges."""

import asyncio
import json
import os

from playwright.async_api import async_playwright


async def main():
    """Verify identity, explicit confirmation, failure recovery and mobile layout."""
    failed = False
    requests = []
    infos = [
        {
            "umo": f"wangshangliao_bot:FriendMessage:20000002/private/{uid}/MTIz",
            "creator_sender_id": uid,
            "auto_name": name,
        }
        for uid, name in [
            ("20000001", "已授权用户"),
            ("23691273", "testfork"),
            ("123", "同名用户"),
            ("124", "同名用户"),
            ("125", "很长的昵称" * 30),
            ("126", "<script>window.selectorInjected=true</script>"),
        ]
    ]
    infos.append(
        {
            "umo": "wangshangliao_bot:GroupMessage:20000002/1143980",
            "creator_sender_id": "12345",
            "auto_name": "A Group Must Not Be An Administrator",
        }
    )
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=os.environ.get("WSL_GUIDE_CHROMIUM"),
            args=["--no-sandbox"],
        )
        page = await browser.new_page(viewport={"width": 1280, "height": 960})
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))

        async def route_api(route):
            requests.append(route.request.method)
            await route.fulfill(
                status=500 if failed else 200,
                json={"status": "ok", "data": {"umo_infos": infos}},
            )

        await page.route("**/api/v1/**", route_api)
        await page.add_init_script("localStorage.setItem('astrbot-locale','zh-CN')")
        await page.goto(
            os.environ.get("ADMIN_SELECTOR_TEST_URL", "http://127.0.0.1:3018")
            + "/tests/fixtures/administrator-selector.html"
        )
        await page.get_by_text("已授权用户", exact=True).wait_for()
        output = page.get_by_test_id("administrators")
        initial = json.loads(await output.text_content())
        choose = page.get_by_role("button", name="从对话选择", exact=True)
        await choose.click()
        dialog = page.get_by_role("dialog")
        await dialog.get_by_role("checkbox", name="testfork", exact=True).check()
        assert json.loads(await output.text_content()) == initial
        await dialog.get_by_role("button", name="取消", exact=True).click()
        assert json.loads(await output.text_content()) == initial
        await choose.click()
        await dialog.get_by_role("checkbox", name="testfork", exact=True).check()
        await dialog.get_by_role("button", name="确认", exact=True).click()
        expected = [*initial, "23691273"]
        assert json.loads(await output.text_content()) == expected
        await choose.click()
        assert await dialog.get_by_role(
            "checkbox", name="testfork", exact=True
        ).is_checked()
        assert (
            await dialog.get_by_role("checkbox", name="同名用户", exact=True).count()
            == 2
        )
        assert (
            await dialog.get_by_text(
                "A Group Must Not Be An Administrator", exact=True
            ).count()
            == 0
        )
        assert not await page.evaluate("Boolean(window.selectorInjected)")
        await dialog.get_by_role("textbox", name="搜索昵称或账号").fill("testfork")
        assert await dialog.locator(".administrator-conversation").count() == 1
        await dialog.get_by_role("textbox", name="搜索昵称或账号").fill("")
        failed = True
        await dialog.get_by_role("button", name="刷新对话", exact=True).click()
        await dialog.get_by_role("alert").filter(has_text="对话读取失败").wait_for()
        await dialog.get_by_role("button", name="取消", exact=True).click()
        assert json.loads(await output.text_content()) == expected
        failed = False
        await page.set_viewport_size({"width": 390, "height": 844})
        await choose.click()
        await dialog.get_by_role("checkbox", name="testfork", exact=True).wait_for()
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert await dialog.evaluate(
            "element => element.scrollWidth <= element.clientWidth"
        )
        await dialog.get_by_role("button", name="手动填写账号", exact=True).click()
        await dialog.get_by_role("textbox", name="账号", exact=True).fill(
            "manual-account"
        )
        await dialog.get_by_role("button", name="添加", exact=True).click()
        await dialog.get_by_role("button", name="确认", exact=True).click()
        assert json.loads(await output.text_content()) == [
            *expected,
            "manual-account",
        ]
        await page.get_by_role("checkbox", name="Readonly", exact=True).check()
        assert await choose.is_disabled()
        assert all(method == "GET" for method in requests)
        assert not errors, errors
        await browser.close()
    print(
        "PASS: nickname selection, exact UID, retained legacy admins, cancellation, "
        "duplicates, group exclusion, escaped names, failure recovery, manual "
        "fallback, readonly, mobile layout; no write requests"
    )


if __name__ == "__main__":
    asyncio.run(main())
