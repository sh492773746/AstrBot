"""Verify the live Dashboard page without login, saves, sends or platform actions."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import jwt
from playwright.sync_api import expect, sync_playwright

from data.plugins.astrbot_plugin_lajiaoliao.storage import atomic_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check-automation", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[2]
    config_file = root / "data/cmd_config.json"
    config_before = config_file.read_bytes()
    config = json.loads(config_before.decode("utf-8-sig"))
    bot = next(row for row in config["platform"] if row["type"] == "lajiaoliao")
    session = json.loads(
        (
            root
            / "data/plugin_data/astrbot_plugin_lajiaoliao"
            / bot["session_ref"]
            / "session.json"
        ).read_text()
    )
    token = jwt.encode(
        {"username": session["owner"], "exp": int(time.time()) + 600},
        config["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )
    original_pid = subprocess.check_output(
        ["systemctl", "show", "astrbot.service", "--property=MainPID", "--value"],
        text=True,
    ).strip()
    report = {"views": [], "mutations_blocked": [], "page_errors": []}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"),
            headless=True,
        )
        try:
            for name, size in [
                ("desktop", {"width": 1440, "height": 1000}),
                ("mobile", {"width": 489, "height": 779}),
            ]:
                context = browser.new_context(viewport=size)
                context.add_init_script(
                    "if (window === window.top) {"
                    "localStorage.setItem('token', " + json.dumps(token) + ");"
                    "localStorage.setItem('user', "
                    + json.dumps(session["owner"])
                    + ");"
                    "localStorage.setItem('astrbot:first_notice_seen:v1', '1');"
                    "}"
                )

                def read_only(route):
                    request = route.request
                    path = urlparse(request.url).path
                    if request.method in {"GET", "HEAD", "OPTIONS"}:
                        route.continue_()
                        return
                    endpoint = path.rsplit("/", 1)[-1]
                    body = request.post_data_json or {}
                    permitted = (
                        path.startswith(
                            "/api/v1/plugins/extensions/astrbot_plugin_lajiaoliao/"
                        )
                        and endpoint
                        in {
                            "groups",
                            "group-state",
                            "business",
                            "group-users",
                            "ai-config",
                            "ai-usage",
                            "commerce-usage",
                        }
                        and body.get("action") in (None, "status")
                    )
                    if permitted:
                        route.continue_()
                    else:
                        report["mutations_blocked"].append(path)
                        route.abort()

                context.route("**/api/**", read_only)
                page = context.new_page()
                page.on(
                    "pageerror",
                    lambda error: report["page_errors"].append(
                        re.sub(r"https?://\S+", "[url]", str(error))[:220]
                    ),
                )
                page.goto(
                    "http://127.0.0.1:6185/#/plugin-page/astrbot_plugin_lajiaoliao/management",
                    wait_until="domcontentloaded",
                )
                panel = page.frame_locator("iframe")
                expect(
                    panel.get_by_role("tab", name="消息权限", exact=True)
                ).to_be_visible(timeout=45000)
                expect(panel.locator("#action-group")).not_to_contain_text("暂无群聊")
                expect(panel.locator("#action-group")).not_to_contain_text("群名待加载")
                expect(panel.locator("#group-list")).to_have_count(0)
                expect(panel.locator("#current-group-enabled")).to_be_checked()
                expect(panel.locator("#main-tabs [role=tab]")).to_have_count(9)
                page.screenshot(path=str(args.output / (name + "-permissions.png")))
                for view in (
                    "自动化状态",
                    "自动化设置",
                    "管理授权",
                    "AI 配额",
                    "活动任务",
                    "记录",
                    "能力",
                    "套餐运营",
                ):
                    panel.get_by_role("tab", name=view, exact=True).click()
                    expect(panel.locator("[data-main-panel]:visible")).to_have_count(1)
                    expect(panel.locator("#status")).to_be_empty()
                    assert panel.locator("html").evaluate(
                        "el => el.scrollWidth <= innerWidth"
                    )
                    if view == "自动化状态":
                        expect(panel.locator("#native-form")).to_have_count(0)
                        expect(panel.locator("#nickname-form")).to_have_count(0)
                        expect(panel.locator("#action-dialog")).to_have_count(0)
                        expect(
                            panel.locator("#automation-status-summary")
                        ).to_contain_text("已关闭")
                        page.screenshot(
                            path=str(args.output / (name + "-automation-status.png"))
                        )
                    if view == "自动化设置":
                        expect(panel.locator("#business-form")).to_be_visible()
                        expect(panel.locator("#group-ai-enabled")).to_be_visible()
                        expect(panel.locator("#commands-enabled")).to_be_checked()
                        expect(panel.locator("#astrbot-ai-status")).to_contain_text(
                            "AstrBot 配置文件"
                        )
                        panel.get_by_text("抽奖参数", exact=True).click()
                        expect(panel.locator("#lottery-notify-results")).to_be_visible()
                        expect(
                            panel.locator("#lottery-notify-results")
                        ).not_to_be_checked()
                        panel.locator(
                            "#lottery-notify-results"
                        ).scroll_into_view_if_needed()
                        assert panel.locator("html").evaluate(
                            "el => el.scrollWidth <= innerWidth"
                        )
                        page.screenshot(
                            path=str(args.output / (name + "-lottery-results.png"))
                        )
                        panel.get_by_text("抽奖参数", exact=True).click()
                        page.screenshot(
                            path=str(args.output / (name + "-business.png"))
                        )
                        if args.check_automation:
                            panel.locator("#automation-rules summary").click()
                            expect(
                                panel.locator("#automation-enabled")
                            ).not_to_be_checked()
                            expect(panel.locator("#automation-ai")).not_to_be_checked()
                            expect(
                                panel.locator("#ai-management-enabled")
                            ).not_to_be_checked()
                            expect(panel.locator("#automation-mute-0")).to_have_value(
                                "5"
                            )
                            expect(panel.locator("#automation-mute-2")).to_have_value(
                                "60"
                            )
                            expect(
                                panel.locator("#review-provider option").first
                            ).to_have_text("不使用 AI 审核")
                            assert panel.locator("html").evaluate(
                                "el => el.scrollWidth <= innerWidth"
                            )
                            page.screenshot(
                                path=str(args.output / (name + "-automation.png"))
                            )
                            panel.get_by_text("群作息与成员维护", exact=True).click()
                            expect(panel.locator("#quiet-enabled")).not_to_be_checked()
                            expect(panel.locator("#cards-enabled")).to_be_enabled()
                            expect(panel.locator("#cards-enabled")).not_to_be_checked()
                            expect(panel.locator("#cleanup-enabled")).to_be_disabled()
                            expect(
                                panel.locator("#reply-recall-enabled")
                            ).not_to_be_checked()
                            expect(
                                panel.locator("#maintenance-capability-status")
                            ).not_to_be_empty()
                            assert panel.locator("html").evaluate(
                                "el => el.scrollWidth <= innerWidth"
                            )
                            panel.locator("#quiet-enabled").scroll_into_view_if_needed()
                            page.screenshot(
                                path=str(args.output / (name + "-maintenance.png"))
                            )
                    if view == "能力":
                        expect(
                            panel.locator('[data-capability="revoke_others"] .badge')
                        ).to_have_text("原生动作已验证")
                        expect(
                            panel.locator('[data-capability="rename_others"] .badge')
                        ).to_have_text("原生动作已验证")
                        expect(
                            panel.locator('[data-capability="cleanup"]')
                        ).to_contain_text("执行关闭")
                        assert panel.locator("html").evaluate(
                            "el => el.scrollWidth <= innerWidth"
                        )
                        page.screenshot(
                            path=str(args.output / (name + "-capabilities.png"))
                        )
                        native_row = panel.locator(
                            '[data-capability="revoke_others"]'
                        )
                        native_row.scroll_into_view_if_needed()
                        expect(native_row).to_be_visible()
                        page.screenshot(
                            path=str(args.output / (name + "-capability-matrix.png"))
                        )
                    if view == "管理授权":
                        expect(
                            panel.locator("#users-list .user-row").first
                        ).to_be_visible()
                        page.screenshot(
                            path=str(args.output / (name + "-managers.png"))
                        )
                    if view == "AI 配额":
                        expect(panel.locator("#ai-user-limit")).to_have_value("20")
                        expect(panel.locator("#ai-management-limit")).to_have_value(
                            "100"
                        )
                        expect(panel.locator("#ai-group-limit")).to_have_value("500")
                        expect(panel.locator("#ai-private-recipient")).to_have_value("")
                        expect(panel.locator("#ai-group-recipient")).to_have_value("")
                        expect(panel.locator("#ai-chat-persona")).to_have_value(
                            "小满｜高拟真生活型"
                        )
                        expect(panel.locator("#ai-management-persona")).to_have_value(
                            "辣椒聊群管｜权限隔离助手"
                        )
                        page.screenshot(path=str(args.output / (name + "-ai.png")))
                    if view == "套餐运营":
                        expect(panel.locator("#commerce")).to_be_visible()
                        expect(panel.locator("#commerce-customer")).to_contain_text(
                            "暂无客户"
                        )
                        panel.get_by_text("实例归属与运营记录", exact=True).click()
                        expect(panel.locator("#commerce-bindings")).to_contain_text(
                            "内部实例"
                        )
                        page.screenshot(
                            path=str(args.output / (name + "-commerce.png"))
                        )
                assert panel.locator("img").evaluate_all(
                    "items => items.every(img => img.complete && img.naturalWidth > 0)"
                )
                report["views"].append(
                    {
                        "viewport": name,
                        "tabs": 9,
                        "assets_loaded": True,
                        "directory_loaded": True,
                        "no_horizontal_overflow": True,
                        "single_group_selector": True,
                        "current_group_switch": True,
                    }
                )
                context.close()
        finally:
            browser.close()
    report["global_config_unchanged"] = (
        hashlib.sha256(config_file.read_bytes()).digest()
        == hashlib.sha256(config_before).digest()
    )
    report["main_pid_unchanged"] = (
        original_pid
        == subprocess.check_output(
            ["systemctl", "show", "astrbot.service", "--property=MainPID", "--value"],
            text=True,
        ).strip()
    )
    report["passed"] = (
        not report["mutations_blocked"]
        and not report["page_errors"]
        and report["global_config_unchanged"]
        and report["main_pid_unchanged"]
    )
    report["automation_checked"] = args.check_automation
    atomic_json(args.output / "report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
