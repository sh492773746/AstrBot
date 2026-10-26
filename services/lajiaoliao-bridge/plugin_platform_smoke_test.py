"""Exercise the standard platform form with real plugin configuration metadata."""

import copy
import os
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

from astrbot.core.config.default import CONFIG_METADATA_2
from astrbot.core.platform.register import platform_registry
from data.plugins.astrbot_plugin_lajiaoliao import adapter  # noqa: F401


def main():
    root = Path(__file__).resolve().parents[2]
    plugin = root / "data/plugins/astrbot_plugin_lajiaoliao"
    meta = next(item for item in platform_registry if item.name == "lajiaoliao")
    metadata = copy.deepcopy(CONFIG_METADATA_2)
    platform = metadata["platform_group"]["metadata"]["platform"]
    template = copy.deepcopy(meta.default_config_tmpl)
    template["logo_token"] = "synthetic-logo"
    existing = {
        **template,
        "id": "lajiaoliao-edit-regression",
        "session_ref": "a" * 32,
        "enable": False,
    }
    platform["config_template"] = {
        "lajiaoliao": template,
        "telethon_ai": {"type": "telethon_ai"},
        "telethon_ai_service": {"type": "telethon_ai_service"},
    }
    platform.setdefault("items", {}).update(meta.config_metadata)
    artifacts = Path("/opt/lajiaoliao-migration-backups/20261008-plugin/ui")
    artifacts.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"),
        )
        try:
            for name, width, height in [("desktop", 1280, 900), ("mobile", 390, 844)]:
                context = browser.new_context(
                    viewport={"width": width, "height": height}
                )
                context.add_init_script(
                    "if(window===top){localStorage.setItem('token','mock-ui-test');"
                    "localStorage.setItem('astrbot-locale','zh-CN');}"
                )
                page = context.new_page()
                errors = []
                saves = []
                saved_bot = copy.deepcopy(existing)
                sessions = [
                    {
                        "session_ref": "a" * 32,
                        "state": "bound",
                        "instance_id": existing["id"],
                        "account_id": "account-existing",
                    }
                ]
                logins = []
                sdk_failure = [False]
                reauth = {"state": "none"}
                reauth_logins = []
                page.on("pageerror", lambda error: errors.append(str(error)))

                def api(route):
                    path = urlparse(route.request.url).path
                    if path == "/api/v1/files/tokens/synthetic-logo":
                        route.fulfill(
                            body=(plugin / "logo.ico").read_bytes(),
                            content_type="image/x-icon",
                        )
                        return
                    data = {}
                    if path.startswith(
                        "/api/v1/plugins/extensions/astrbot_plugin_lajiaoliao/"
                    ):
                        action = path.rsplit("/", 1)[-1]
                        if action == "sessions":
                            route.fulfill(json={"sessions": sessions})
                            return
                        if action == "reauth-status":
                            route.fulfill(json=reauth)
                            return
                        if action == "prepare-reauth":
                            body = route.request.post_data_json
                            assert (
                                body["session_ref"] == "a" * 32
                                and "password" not in body
                            )
                            reauth.update(state="draft", operation_ref="e" * 32)
                            route.fulfill(
                                json={
                                    **reauth,
                                    "challenge": {
                                        "required": True,
                                        "region": "cn",
                                        "prefix": "test",
                                        "scene_id": "test",
                                    },
                                }
                            )
                            return
                        if action == "reauthenticate":
                            body = route.request.post_data_json
                            assert (
                                body["session_ref"] == "a" * 32
                                and body["operation_ref"] == "e" * 32
                            )
                            assert (
                                body["captcha_verify_param"] == "synthetic-human-proof"
                            )
                            reauth_logins.append(body)
                            reauth.update(state="completed")
                            route.fulfill(json=reauth)
                            return
                        if action == "cancel-reauth":
                            reauth.update(state="none")
                            route.fulfill(json={"state": "cancelled"})
                            return
                        if action == "prepare-login":
                            body = route.request.post_data_json
                            assert "password" not in body
                            if len(sessions) == 1:
                                sessions.append(
                                    {
                                        "session_ref": "b" * 32,
                                        "state": "draft",
                                        "instance_id": body["instance_id"],
                                        "account_id": "",
                                    }
                                )
                            route.fulfill(
                                json={
                                    **sessions[1],
                                    "challenge": {
                                        "required": True,
                                        "region": "cn",
                                        "prefix": "test",
                                        "scene_id": "test",
                                    },
                                }
                            )
                            return
                        if action == "poll":
                            route.fulfill(json=sessions[1])
                            return
                        if action == "cancel-login":
                            ref = route.request.post_data_json["session_ref"]
                            target = next(
                                item for item in sessions if item["session_ref"] == ref
                            )
                            assert target["state"] == "draft"
                            sessions.remove(target)
                            route.fulfill(json={"state": "cancelled"})
                            return
                        if action == "revoke":
                            body = route.request.post_data_json
                            assert body["confirm"] is True
                            target = next(
                                item
                                for item in sessions
                                if item["session_ref"] == body["session_ref"]
                            )
                            assert target["instance_id"] != saved_bot["id"]
                            sessions.remove(target)
                            route.fulfill(json={"state": "revoked"})
                            return
                        if action == "login":
                            body = route.request.post_data_json
                            assert (
                                body["captcha_verify_param"] == "synthetic-human-proof"
                            ), repr(body.get("captcha_verify_param"))
                            logins.append(body)
                            sessions[1].update(
                                state="bound",
                                instance_id=body["instance_id"],
                                account_id="account-new",
                            )
                            route.fulfill(json=sessions[1])
                            return
                        assert action != "bind", "Manual binding must not be required"
                    if (
                        path
                        == "/api/v1/plugins/extensions/astrbot_plugin_lajiaoliao/groups"
                    ):
                        assert route.request.post_data_json["session_ref"] == "a" * 32
                        route.fulfill(
                            json={
                                "groupInfos": [
                                    {"groupID": "test-a", "groupName": "模拟测试群甲"},
                                    {"groupID": "test-b", "groupName": "模拟测试群乙"},
                                ]
                            }
                        )
                        return
                    if path.startswith("/api/v1/bots") and route.request.method in {
                        "PUT",
                        "PATCH",
                        "POST",
                    }:
                        saves.append(route.request.post_data_json)
                        saved_bot.update(saves[-1].get("config", saves[-1]))
                    if path == "/api/v1/system-config/runtime":
                        data = {
                            "config": {"platform": [saved_bot]},
                            "metadata": metadata,
                        }
                    elif path == "/api/v1/config-profiles":
                        data = {"info_list": [{"id": "default", "name": "默认配置"}]}
                    elif path.startswith("/api/v1/config-profiles/"):
                        data = {"config": {}, "metadata": {}}
                    elif path.endswith("/stats"):
                        data = {"platforms": []}
                    elif "version" in path:
                        data = {"version": "test", "dashboard_version": "test"}
                    assert not path.endswith("/registration"), (
                        "Retired core login API was called"
                    )
                    route.fulfill(json={"status": "ok", "data": data})

                context.route("**/api/**", api)
                context.route(
                    "https://o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js",
                    lambda route: (
                        route.abort()
                        if sdk_failure[0]
                        else route.fulfill(
                            content_type="application/javascript",
                            body="window.initAliyunCaptcha=o=>{window.finishMock=()=>o.success('synthetic-human-proof');"
                            "window.failMock=()=>o.fail();o.getInstance({show(){}})};",
                        )
                    ),
                )
                page.goto("http://127.0.0.1:17901/#/platforms")
                page.locator(".bot-editor").get_by_text(
                    "已登录：account-existing", exact=True
                ).wait_for()
                editor = page.locator(".bot-editor")
                editor.get_by_role("button", name="重新登录", exact=True).click()
                editor.get_by_label("手机号", exact=True).fill("13800000000")
                editor.get_by_label("密码", exact=True).fill("synthetic-password")
                editor.get_by_role("button", name="验证并重新登录", exact=True).click()
                page.get_by_title("官方滑块验证码").wait_for()
                frame = page.frames[1]
                frame.wait_for_function("typeof finishMock === 'function'")
                page.get_by_role("button", name="取消验证", exact=True).click()
                assert not reauth_logins
                expect(editor.get_by_label("密码", exact=True)).to_have_value("")
                editor.get_by_role("button", name="取消重新登录", exact=True).click()
                editor.get_by_role("button", name="重新登录", exact=True).click()
                editor.get_by_label("密码", exact=True).fill("synthetic-password")
                editor.get_by_role("button", name="验证并重新登录", exact=True).click()
                page.get_by_title("官方滑块验证码").wait_for()
                frame = page.frames[1]
                frame.wait_for_function("typeof finishMock === 'function'")
                frame.evaluate("finishMock()")
                editor.get_by_role("button", name="重新登录", exact=True).wait_for()
                assert len(reauth_logins) == 1 and saved_bot["session_ref"] == "a" * 32
                expect(editor.get_by_label("密码", exact=True)).to_have_count(0)
                page.wait_for_timeout(250)
                page.screenshot(
                    path=str(artifacts / f"reauth-{name}.png"), full_page=True
                )
                editor.get_by_text("群聊范围：白名单（固定）", exact=True).wait_for()
                editor.get_by_role("button", name="刷新群列表", exact=True).click()
                editor.get_by_text("模拟测试群甲", exact=True).wait_for()
                group_a = editor.get_by_role(
                    "checkbox", name="允许群 test-a", exact=True
                )
                group_b = editor.get_by_role(
                    "checkbox", name="允许群 test-b", exact=True
                )
                expect(group_a).not_to_be_checked()
                group_a.check()
                group_b.check()
                group_a.uncheck()
                expect(group_b).to_be_checked()
                editor.get_by_role("checkbox", name="允许私聊", exact=True).check()
                editor.get_by_role("button", name="保存更改", exact=True).click()
                page.wait_for_timeout(300)
                assert saves, "Policy save did not reach the standard bot API"
                payload = saves[-1].get("config", saves[-1])
                assert payload["message_policy"] == {
                    "group_mode": "allowlist",
                    "group_ids": ["test-b"],
                    "private_enabled": True,
                }
                assert payload["enable"] is False
                page.reload()
                editor.get_by_text("群聊范围：白名单（固定）", exact=True).wait_for()
                expect(
                    editor.get_by_role("checkbox", name="允许群 test-b", exact=True)
                ).to_be_checked()
                editor.get_by_role("button", name="刷新群列表", exact=True).click()
                expect(
                    editor.get_by_role("checkbox", name="允许群 test-a", exact=True)
                ).not_to_be_checked()
                page.screenshot(
                    path=str(artifacts / f"groups-{name}.png"), full_page=True
                )
                assert page.get_by_role("button", name="使用已登录会话").count() == 0
                page.get_by_role("button", name="创建机器人", exact=True).first.click()
                dialog = page.get_by_role("dialog").filter(
                    has=page.get_by_role("button", name="保存", exact=True)
                )
                dialog.locator(".v-select").first.click()
                option = page.get_by_role("listbox").get_by_text("辣椒聊", exact=True)
                option.wait_for()
                assert (
                    page.get_by_role("listbox")
                    .get_by_text("telethon_ai", exact=True)
                    .count()
                    == 0
                )
                assert (
                    page.get_by_role("listbox")
                    .get_by_text("telethon_ai_service", exact=True)
                    .count()
                    == 0
                )
                icon = page.get_by_role("listbox").locator("img").first
                assert icon.evaluate("(img) => img.complete && img.naturalWidth > 0")
                assert (
                    icon.evaluate("(img) => getComputedStyle(img).borderRadius")
                    == "8px"
                )
                option.click()
                assert dialog.get_by_label("机器人 ID", exact=True).count() == 0
                assert (
                    dialog.get_by_role(
                        "button", name="确认绑定此机器人", exact=True
                    ).count()
                    == 0
                )
                dialog.get_by_label("手机号", exact=True).fill("13800000000")
                dialog.get_by_label("密码", exact=True).fill("synthetic-password")
                dialog.get_by_role("button", name="登录", exact=True).click()
                page.get_by_title("官方滑块验证码").wait_for()
                frame = page.frames[1]
                frame.wait_for_function("typeof finishMock === 'function'")
                assert (
                    frame.evaluate(
                        "(()=>{try {return !!parent.document.querySelector('input')}catch(_){return false}})()"
                    )
                    is False
                )
                assert not logins
                page.get_by_role("button", name="取消验证", exact=True).click()
                dialog.get_by_text("已取消验证，未提交密码。", exact=True).wait_for()
                assert dialog.get_by_label("密码", exact=True).input_value() == ""
                assert not logins
                dialog.get_by_role("button", name="取消本次登录", exact=True).click()
                expect(
                    dialog.get_by_role("button", name="取消本次登录", exact=True)
                ).to_have_count(0)
                assert len(sessions) == 1
                sdk_failure[0] = True
                dialog.get_by_label("密码", exact=True).fill("synthetic-password")
                dialog.get_by_role("button", name="登录", exact=True).click()
                dialog.get_by_text(
                    "官方验证码加载失败，未提交密码，请稍后重试。", exact=True
                ).wait_for()
                assert not logins
                assert dialog.get_by_label("密码", exact=True).input_value() == ""
                sdk_failure[0] = False
                dialog.get_by_label("密码", exact=True).fill("synthetic-password")
                dialog.get_by_role("button", name="登录", exact=True).click()
                page.get_by_title("官方滑块验证码").wait_for()
                frame = page.frames[1]
                frame.wait_for_function("typeof finishMock === 'function'")
                page.evaluate(
                    "postMessage({source:'lajiao-captcha',type:'success',requestID:'forged',value:'forged'},'*')"
                )
                frame.evaluate("failMock()")
                page.wait_for_timeout(100)
                assert not logins
                frame.evaluate("finishMock()")
                dialog.get_by_text("已登录：account-new", exact=True).wait_for()
                expect(page.get_by_title("官方滑块验证码")).not_to_be_visible()
                page.wait_for_timeout(250)
                assert len(logins) == 1
                expect(
                    dialog.get_by_role("checkbox", name="启用机器人", exact=True)
                ).not_to_be_checked()
                expect(
                    dialog.get_by_role("checkbox", name="允许私聊", exact=True)
                ).not_to_be_checked()
                assert dialog.get_by_role("button", name="使用已登录会话").count() == 0
                page.screenshot(
                    path=str(artifacts / f"platform-{name}.png"), full_page=True
                )
                assert dialog.evaluate("(el) => el.scrollWidth <= el.clientWidth + 1")
                dialog.get_by_role("button", name="保存", exact=True).click()
                page.wait_for_timeout(400)
                created = saves[-1].get("config", saves[-1])
                assert (
                    created["id"].startswith("lajiaoliao_") and len(created["id"]) == 27
                )
                assert created["session_ref"] == "b" * 32 and created["enable"] is False
                assert created["message_policy"]["group_ids"] == []
                assert not any(
                    key in created for key in ["password", "token", "authorization_url"]
                )
                sessions.extend(
                    [
                        {
                            "session_ref": "c" * 32,
                            "state": "bound",
                            "instance_id": "lajiaoliao_orphan",
                            "account_id": "account-orphan",
                        },
                        {
                            "session_ref": "d" * 32,
                            "state": "draft",
                            "instance_id": "",
                            "pending_instance_id": "lajiaoliao_draft",
                            "account_id": "",
                        },
                    ]
                )
                page.get_by_role("button", name="创建机器人", exact=True).first.click()
                dialog = page.get_by_role("dialog").filter(
                    has=page.get_by_role("button", name="保存", exact=True)
                )
                dialog.locator(".v-select").first.click()
                page.get_by_role("listbox").get_by_text("辣椒聊", exact=True).click()
                recovery = dialog.locator(".recovery-bar")
                recovery.wait_for()
                recovery.locator(".v-select").click()
                page.get_by_role("listbox").get_by_text(
                    "account-orphan · 待保存", exact=True
                ).click()
                recovery.get_by_role("button", name="恢复", exact=True).click()
                dialog.get_by_text("已登录：account-orphan", exact=True).wait_for()
                assert (
                    dialog.get_by_role(
                        "checkbox", name="启用机器人", exact=True
                    ).count()
                    == 1
                )
                assert not any(
                    item.get("config", item).get("session_ref") == "c" * 32
                    for item in saves
                )
                dialog.get_by_role("button", name="取消", exact=True).first.click()

                page.get_by_role("button", name="创建机器人", exact=True).first.click()
                dialog = page.get_by_role("dialog").filter(
                    has=page.get_by_role("button", name="保存", exact=True)
                )
                dialog.locator(".v-select").first.click()
                page.get_by_role("listbox").get_by_text("辣椒聊", exact=True).click()
                recovery = dialog.locator(".recovery-bar")
                recovery.locator(".v-select").click()
                page.get_by_role("listbox").get_by_text(
                    "待登录 · 待验证", exact=True
                ).click()
                recovery.get_by_role("button", name="恢复", exact=True).click()
                dialog.get_by_role("button", name="取消本次登录", exact=True).wait_for()
                dialog.get_by_role("button", name="取消本次登录", exact=True).click()
                assert not any(row["session_ref"] == "d" * 32 for row in sessions)
                recovery.locator(".v-select").click()
                page.get_by_role("listbox").get_by_text(
                    "account-orphan · 待保存", exact=True
                ).click()
                recovery.get_by_role("button", name="注销", exact=True).click()
                page.get_by_role("button", name="确认注销", exact=True).click()
                assert not any(row["session_ref"] == "c" * 32 for row in sessions)
                recovery.locator(".v-select").click()
                expect(
                    page.get_by_role("listbox").get_by_text(
                        "account-orphan · 待保存", exact=True
                    )
                ).to_have_count(0)
                assert not errors, errors
                context.close()
                print(name + ": standard config form and plugin logo passed")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
