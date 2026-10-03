"""Verify the real plugin iframe with read-only live access and mocked edits."""

import argparse
import asyncio
import copy
import json
import time
from pathlib import Path

import jwt
from PIL import Image, ImageStat
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = "wangshangliao_moderation"


async def main():
    """Run desktop/mobile interactions without saving production settings."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:6185")
    parser.add_argument("--browser", required=True)
    parser.add_argument("--access-cookie-guard", action="store_true")
    args = parser.parse_args()
    config = json.loads((ROOT / "data/cmd_config.json").read_text(encoding="utf-8-sig"))
    token = jwt.encode(
        {
            "username": config["dashboard"]["username"],
            "exp": int(time.time()) + 600,
        },
        config["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )
    initialization = (
        "if (window === window.top) {"
        f"localStorage.setItem('token', {json.dumps(token)});"
        f"localStorage.setItem('user', {json.dumps(config['dashboard']['username'])});"
        "localStorage.setItem('astrbot-locale', 'zh-CN');"
        "localStorage.setItem('astrbot:first_notice_seen:v1', '1');"
        "localStorage.setItem('themeMode', 'light');"
        "localStorage.setItem('uiTheme', 'PurpleTheme');"
        "}"
    )
    fixture = {
        "bots": [
            {
                "id": "fixture",
                "nickname": "群管机器人",
                "account_id": "10001",
                "enable": True,
                "enabled_groups": ["100001", "100002"],
                "revision": "fixture-revision",
                "state": "online",
                "reply_private": True,
                "reply_groups": {"100001": True, "100002": False},
                "moderation": {
                    "enabled": True,
                    "automation_enabled": True,
                    "recall_enabled": True,
                    "cooldown_seconds": 60,
                    "content_rules_since": 1700000000,
                    "semantic": {
                        "enabled": True,
                        "provider_id": "Gemini/审核模型",
                        "timeout_seconds": 12,
                        "context_limit": 20,
                    },
                    "permissions": {
                        "100001": ["mute", "unmute", "kick", "recall"],
                        "100002": ["mute", "unmute", "recall"],
                    },
                    "auto_kick": {"100001": True, "100002": False},
                    "card_auto": {},
                },
            }
        ],
        "providers": [{"id": "Gemini/审核模型", "model": "审核模型"}],
    }
    audit = {
        "counts": [
            {"member": "20001", "accepted": 2, "unknown": 0, "created": 1790848800},
            {"member": "20002", "accepted": 1, "unknown": 1, "created": 1790848400},
        ],
        "sanctions": [
            {
                "operation": "automatic-kick/example",
                "member": "20003",
                "action": "kick",
                "status": "verified",
                "created": 1790848900,
            },
            {
                "operation": "automatic-mute/example",
                "member": "20002",
                "action": "mute",
                "status": "unknown",
                "minutes": 15,
                "recall_status": "rejected",
                "created": 1790848800,
            },
        ],
        "reviews": [
            {
                "message": "sample-1",
                "decision": "allow",
                "category": "none",
                "reason": "成员正在举报广告，引用内容不属于推广。",
                "created": 1790848800,
            },
            {
                "message": "sample-2",
                "decision": "violation",
                "category": "external_promotion",
                "reason": "当前发言与同一成员前文共同构成明确的外部引流。",
                "created": 1790848400,
            },
        ],
        "rules": [],
    }
    saves = []
    failures = {"save": False, "audit": False}
    screenshots = ROOT / "docs/zh/platform/images"
    errors = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=args.browser, headless=True
        )
        context = await browser.new_context(viewport={"width": 1280, "height": 1000})
        if args.access_cookie_guard:
            await context.add_cookies(
                [
                    {
                        "name": "wsl_access_fixture",
                        "value": "fixture",
                        "url": args.base_url,
                        "sameSite": "Lax",
                    }
                ]
            )
        await context.add_init_script(initialization)
        page = await context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        blocked_assets = []

        async def access_guard(route):
            headers = await route.request.all_headers()
            if "wsl_access_fixture=fixture" not in headers.get("cookie", ""):
                asset = route.request.url.split("?")[0].rsplit("/", 1)[-1]
                blocked_assets.append(asset)
                await route.fulfill(
                    status=403,
                    body="Login required by simulated access protection",
                    content_type="text/plain",
                )
            else:
                await route.continue_()

        if args.access_cookie_guard:
            await page.route("**/api/plugin/page/**", access_guard)
        await page.goto(
            f"{args.base_url}/#/plugin-page/{PLUGIN}/management",
            wait_until="domcontentloaded",
        )
        frame = page.frame_locator("iframe.plugin-page-frame")
        try:
            await frame.get_by_role("heading", name="旺商聊群管", exact=True).wait_for(
                timeout=15000
            )
        except Exception:
            print("blocked_plugin_assets:", blocked_assets)
            raise
        await frame.locator("#provider").wait_for()
        assert await frame.locator("#semantic").is_checked()
        print("live_plugin_iframe: loaded; AI setting enabled")

        async def settings_route(route):
            if route.request.method == "POST":
                body = route.request.post_data_json
                saves.append(body)
                if failures["save"]:
                    await route.fulfill(
                        status=409,
                        json={
                            "status": "error",
                            "message": "配置已被修改，请刷新后重新编辑",
                        },
                    )
                    return
                current = fixture["bots"][0]
                current["moderation"].update(copy.deepcopy(body["policy"]))
                group = body["group_id"]
                for field in ("permissions", "auto_kick", "card_auto"):
                    current["moderation"].setdefault(field, {})[group] = body["group"][
                        field
                    ]
                current["reply_groups"][group] = body["group"]["reply"]
                current["reply_private"] = body["reply_private"]
                current["revision"] = "saved-revision"
                await route.fulfill(
                    json={"status": "ok", "data": {"revision": "saved-revision"}}
                )
            else:
                await route.fulfill(json={"status": "ok", "data": fixture})

        async def audit_route(route):
            if failures["audit"]:
                await route.fulfill(
                    status=503,
                    json={"status": "error", "message": "审计记录暂时不可读取"},
                )
            else:
                await route.fulfill(json={"status": "ok", "data": audit})

        await page.route(
            f"**/api/v1/plugins/extensions/{PLUGIN}/settings", settings_route
        )
        await page.route(f"**/api/v1/plugins/extensions/{PLUGIN}/audit?*", audit_route)
        await page.reload(wait_until="domcontentloaded")
        await frame.locator("#provider").wait_for()
        assert await frame.locator("#timeout").input_value() == "12"
        await frame.locator("#recall").uncheck()
        await frame.locator("#progressive-mute").check()
        assert await frame.locator("#recall").is_checked()
        assert await frame.locator("#recall").is_disabled()
        assert await frame.locator("#cooldown").is_disabled()
        await frame.locator("#timeout").fill("15")
        await frame.locator(".scope-bar select").nth(1).select_option("100002")
        await frame.get_by_role("dialog").wait_for()
        await frame.get_by_role("button", name="继续编辑", exact=True).click()
        assert await frame.locator(".scope-bar select").nth(1).input_value() == "100001"
        await frame.get_by_role("button", name="保存更改", exact=True).click()
        await frame.get_by_role("status").filter(has_text="配置已保存").wait_for()
        assert len(saves) == 1
        assert saves[0]["policy"]["semantic"]["timeout_seconds"] == 15
        assert saves[0]["policy"]["progressive_mute"] is True
        assert saves[0]["policy"]["recall_enabled"] is True
        assert saves[0]["group_id"] == "100001"
        assert fixture["bots"][0]["moderation"]["permissions"]["100002"] == [
            "mute",
            "unmute",
            "recall",
        ]
        await frame.locator("#timeout").fill("16")
        failures["save"] = True
        await frame.get_by_role("button", name="保存更改", exact=True).click()
        await frame.get_by_role("alert").filter(has_text="配置已被修改").wait_for()
        assert await frame.locator("#timeout").input_value() == "16"
        failures["save"] = False
        await frame.get_by_role("button", name="还原", exact=True).click()
        await frame.locator(".scope-bar select").nth(1).select_option("100002")
        await frame.get_by_role("tab", name="群授权", exact=True).click()
        assert not await frame.locator("#auto-kick").is_checked()
        await frame.locator("#auto-kick").check()
        await (
            frame.get_by_role("alert")
            .filter(has_text="自动升级所需授权不完整")
            .wait_for()
        )
        await frame.get_by_role("button", name="还原", exact=True).click()
        await frame.locator(".scope-bar select").nth(1).select_option("100001")
        await frame.get_by_role("tab", name="群内命令", exact=True).click()
        assert (
            await frame.locator(".commands").get_by_text("排名", exact=True).count()
            == 1
        )
        assert (
            await frame.locator(".commands").get_by_text("普通群员", exact=True).count()
            == 5
        )
        assert "/群管" not in await frame.locator("#panel-commands").inner_text()
        assert (
            await frame.locator(".commands").get_by_text("已授权", exact=True).count()
            == 2
        )
        assert (
            await frame.locator(".commands").get_by_text("未授权", exact=True).count()
            == 3
        )
        saves_before_reference = len(saves)
        await frame.get_by_role("tab", name="架构表", exact=True).click()
        assert await frame.locator(".architecture-table tbody tr").count() == 14
        await frame.get_by_text(
            "astrbot/core/platform/sources/wangshangliao/reply_recall.py", exact=True
        ).wait_for()
        await frame.get_by_role("tab", name="权限表", exact=True).click()
        assert await frame.locator(".permission-matrix tbody tr").count() == 20
        assert await frame.locator(".authority-table tbody tr").count() == 6
        ranking = frame.locator('[data-feature="ranking"] td')
        assert await ranking.nth(0).inner_text() == "允许"
        assert await ranking.nth(2).inner_text() == "无权限"
        join = frame.locator('[data-feature="lottery-join"] td')
        assert await join.nth(3).inner_text() == "仅群内"
        kick = frame.locator('[data-feature="kick"]')
        assert await kick.locator("td").nth(1).inner_text() == "群内静默"
        assert await kick.locator(".badge").inner_text() == "已授权"
        await frame.get_by_role("tab", name="群授权", exact=True).click()
        await frame.locator('input[value="announce"]').check()
        await frame.get_by_role("tab", name="权限表", exact=True).click()
        assert (
            await frame.locator('[data-feature="announce"] .badge').inner_text()
            == "未授权"
        )
        await frame.get_by_role("tab", name="群授权", exact=True).click()
        await frame.get_by_role("button", name="还原", exact=True).click()
        await frame.locator(".scope-bar select").nth(1).select_option("100002")
        await frame.get_by_role("tab", name="权限表", exact=True).click()
        assert await kick.locator(".badge").inner_text() == "未授权"
        await frame.locator(".scope-bar select").nth(1).select_option("100001")
        assert await kick.locator(".badge").inner_text() == "已授权"
        assert len(saves) == saves_before_reference
        print(
            "reference_tables: role matrix, saved-only grants, group isolation, read-only passed"
        )
        await frame.get_by_role("tab", name="处罚记录", exact=True).click()
        assert (
            await frame.locator("#panel-audit").get_by_text("2 / 3", exact=True).count()
            == 1
        )
        assert (
            await frame.locator("#panel-audit").get_by_text("未知", exact=True).count()
            == 2
        )
        audit["reviews"][0]["reason"] = (
            '<img src=x onerror="window.auditInjected=true">'
        )
        await frame.get_by_role("button", name="刷新记录", exact=True).click()
        await frame.get_by_text(audit["reviews"][0]["reason"], exact=True).wait_for()
        assert await frame.locator("img").count() == 0
        failures["audit"] = True
        await frame.get_by_role("button", name="刷新记录", exact=True).click()
        await (
            frame.get_by_role("alert")
            .filter(has_text="审计记录暂时不可读取")
            .wait_for()
        )
        failures["audit"] = False
        audit["reviews"][0]["reason"] = "成员正在举报广告，引用内容不属于推广。"
        await frame.get_by_role("button", name="刷新记录", exact=True).click()
        await frame.get_by_text(audit["reviews"][0]["reason"], exact=True).wait_for()
        print(
            "mocked_edits: save, stale rejection, discard guard, group isolation, escaped audit passed"
        )

        saves_before_examples = len(saves)
        for width, height in ((1280, 1000), (390, 844), (320, 720)):
            await page.set_viewport_size({"width": width, "height": height})
            await page.reload(wait_until="domcontentloaded")
            await frame.locator("#provider").wait_for()
            bounds = await page.locator("iframe.plugin-page-frame").bounding_box()
            assert bounds and bounds["x"] >= -1, (width, bounds)
            assert bounds["x"] + bounds["width"] <= width + 1, (width, bounds)
            for tab in (
                "审核配置",
                "群授权",
                "处罚记录",
                "群内命令",
                "对话示例",
                "架构表",
                "权限表",
            ):
                await frame.get_by_role("tab", name=tab, exact=True).click()
                panel = page.frames[-1]
                dimensions = await panel.evaluate(
                    "({width: innerWidth, scrollWidth: document.documentElement.scrollWidth})"
                )
                assert dimensions["scrollWidth"] <= dimensions["width"], (
                    width,
                    tab,
                    dimensions,
                )
                if tab == "对话示例":
                    await frame.get_by_text(
                        "示例数据 · 不会发送", exact=True
                    ).wait_for()
                    await (
                        frame.locator(".section-switch")
                        .get_by_role("tab", name="私聊与权限", exact=True)
                        .click()
                    )
                    await (
                        frame.locator(".private-switch")
                        .get_by_role("tab", name="名片设置", exact=True)
                        .click()
                    )
                    await (
                        frame.locator(".chat-bubble pre")
                        .filter(has_text="关闭改为开启，仅影响本群")
                        .wait_for()
                    )
                    await (
                        frame.locator(".chat-bubble pre")
                        .filter(has_text="账号昵称未修改")
                        .wait_for()
                    )
                    assert len(saves) == saves_before_examples
                    await frame.locator(".chat-demo").screenshot(
                        path=f"/tmp/wsl-card-ai-examples-{width}.png"
                    )
                    await (
                        frame.locator(".section-switch")
                        .get_by_role("tab", name="群抽奖", exact=True)
                        .click()
                    )
                    admin_commands = await frame.locator(
                        ".chat-line.admin .chat-bubble pre"
                    ).all_text_contents()
                    assert all("\n" not in command for command in admin_commands)
                    assert {
                        "中奖人数 3",
                        "抽奖倒计时 10",
                        "参与上限 15",
                        "抽奖邀请门槛 0",
                        "领奖联系人 秦铭",
                    }.issubset(set(admin_commands))
                    await (
                        frame.locator(".section-switch")
                        .get_by_role("tab", name="邀请奖励", exact=True)
                        .click()
                    )
                    await (
                        frame.locator(".chat-bubble pre")
                        .filter(has_text="设置邀请奖励 5")
                        .wait_for()
                    )
                    await frame.get_by_text(
                        "机器人将在后台周期核验", exact=False
                    ).wait_for()
                    await (
                        frame.locator(".chat-line.result pre")
                        .filter(has_text="累计奖励：5.00 积分")
                        .wait_for()
                    )
                    assert len(saves) == saves_before_examples, saves
            if width < 640:
                matrix_scroll = frame.locator("#panel-matrix .table-scroll").first
                assert await matrix_scroll.evaluate(
                    "(node) => node.scrollWidth > node.clientWidth"
                )
                first_column = frame.locator('[data-feature="ranking"] th')
                before = await first_column.bounding_box()
                await matrix_scroll.evaluate("(node) => { node.scrollLeft = 400; }")
                after = await first_column.bounding_box()
                assert before and after and abs(before["x"] - after["x"]) < 1
                await matrix_scroll.evaluate("(node) => { node.scrollLeft = 0; }")
            await page.frames[-1].evaluate(
                "window.AstrBotPluginPage.__setInitialContext({locale: 'en-US', isDark: true})"
            )
            await frame.get_by_role("tab", name="Chat examples", exact=True).click()
            await frame.get_by_text(
                "Static preview only. No bot message is sent and no settings are saved.",
                exact=True,
            ).wait_for()
            await frame.get_by_role("tab", name="Permissions", exact=True).click()
            await frame.get_by_role(
                "heading", name="Caller permission matrix", exact=True
            ).wait_for()
            dimensions = await page.frames[-1].evaluate(
                "({width: innerWidth, scrollWidth: document.documentElement.scrollWidth, dark: document.documentElement.dataset.theme})"
            )
            assert dimensions["scrollWidth"] <= dimensions["width"]
            assert dimensions["dark"] == "dark"
            assert len(saves) == saves_before_examples, saves
            await page.frames[-1].evaluate(
                "window.AstrBotPluginPage.__setInitialContext({locale: 'zh-CN', isDark: false})"
            )
            print(
                f"viewport_{width}: all seven tabs, English matrix and dark theme fit"
            )
        await page.set_viewport_size({"width": 1280, "height": 2600})
        await page.reload(wait_until="domcontentloaded")
        await frame.locator("#provider").wait_for()
        await frame.get_by_role("tab", name="群内命令", exact=True).click()
        await frame.locator("main").screenshot(
            path=str(screenshots / "wangshangliao-plugin-commands.png")
        )
        await frame.get_by_role("tab", name="对话示例", exact=True).click()
        await (
            frame.locator(".section-switch")
            .get_by_role("tab", name="群抽奖", exact=True)
            .click()
        )
        await frame.get_by_text("示例数据 · 不会发送", exact=True).wait_for()
        await frame.locator(".chat-line.result").wait_for()
        await frame.locator(".chat-demo").screenshot(
            path=str(screenshots / "wangshangliao-plugin-conversation.png")
        )
        assert len(saves) == saves_before_examples, saves
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.reload(wait_until="domcontentloaded")
        await frame.locator("#provider").wait_for()
        await frame.get_by_role("tab", name="群内命令", exact=True).click()
        await page.evaluate("window.scrollTo(0, 0)")
        await page.frames[-1].evaluate("window.scrollTo(0, 0)")
        await page.locator("iframe.plugin-page-frame").screenshot(
            path=str(screenshots / "wangshangliao-plugin-commands-mobile.png")
        )
        await frame.get_by_role("tab", name="对话示例", exact=True).click()
        await (
            frame.locator(".section-switch")
            .get_by_role("tab", name="邀请奖励", exact=True)
            .click()
        )
        await page.set_viewport_size({"width": 390, "height": 1600})
        await page.evaluate("window.scrollTo(0, 0)")
        await page.frames[-1].evaluate("window.scrollTo(0, 0)")
        await frame.locator(".chat-line.result").wait_for()
        await frame.locator(".chat-demo").screenshot(
            path=str(screenshots / "wangshangliao-plugin-conversation-mobile.png")
        )
        await page.set_viewport_size({"width": 1280, "height": 1000})
        await page.reload(wait_until="domcontentloaded")
        await frame.locator("#provider").wait_for()
        await frame.get_by_role("tab", name="处罚记录", exact=True).click()
        await frame.locator("main").screenshot(
            path=str(screenshots / "wangshangliao-plugin-audit.png")
        )
        await page.set_viewport_size({"width": 1600, "height": 3800})
        await frame.get_by_role("tab", name="架构表", exact=True).click()
        await frame.locator("main").screenshot(
            path=str(screenshots / "wangshangliao-plugin-architecture.png")
        )
        await frame.get_by_role("tab", name="权限表", exact=True).click()
        await frame.locator("main").screenshot(
            path=str(screenshots / "wangshangliao-plugin-permissions.png")
        )
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.reload(wait_until="domcontentloaded")
        await frame.locator("#provider").wait_for()
        await frame.get_by_role("tab", name="群授权", exact=True).click()
        await page.evaluate("window.scrollTo(0, 0)")
        await page.frames[-1].evaluate("window.scrollTo(0, 0)")
        await page.locator("iframe.plugin-page-frame").screenshot(
            path=str(screenshots / "wangshangliao-plugin-mobile.png")
        )
        await frame.get_by_role("tab", name="权限表", exact=True).click()
        await page.frames[-1].evaluate("window.scrollTo(0, 0)")
        await page.locator("iframe.plugin-page-frame").screenshot(
            path=str(screenshots / "wangshangliao-plugin-permissions-mobile.png")
        )
        await frame.get_by_role("tab", name="审核配置", exact=True).click()
        await frame.locator("#semantic").uncheck()
        assert not await frame.locator("#provider").count()
        await frame.locator("#semantic").check()
        assert await frame.locator("#provider").input_value() == "Gemini/审核模型"
        print("semantic_switch: restores selected provider")
        for name in (
            "wangshangliao-plugin-commands.png",
            "wangshangliao-plugin-commands-mobile.png",
            "wangshangliao-plugin-conversation.png",
            "wangshangliao-plugin-conversation-mobile.png",
            "wangshangliao-plugin-audit.png",
            "wangshangliao-plugin-mobile.png",
            "wangshangliao-plugin-architecture.png",
            "wangshangliao-plugin-permissions.png",
            "wangshangliao-plugin-permissions-mobile.png",
        ):
            with Image.open(screenshots / name) as image:
                assert max(ImageStat.Stat(image.convert("RGB")).stddev) > 5, name
        print("screenshots: all nine are nonblank")
        assert not errors, errors
        assert not blocked_assets, blocked_assets
        await browser.close()
        print("page_errors: 0; production settings were never submitted")


if __name__ == "__main__":
    asyncio.run(main())
