"""Verify deployed account labels without changing group or financial state."""

import argparse
import asyncio
import hashlib
import json
import sqlite3
import time
from pathlib import Path

import httpx
import jwt
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "/api/v1/plugins/extensions/wangshangliao_moderation/"


def snapshot():
    """Fingerprint protected state before and after a display-only deployment.

    Returns:
        Financial fingerprint, row count, saved configuration and tenant states.
    """
    data = ROOT / "data/plugin_data"
    with sqlite3.connect(
        (data / "wangshangliao_cashback/cashback.sqlite3").resolve().as_uri()
        + "?mode=ro",
        uri=True,
    ) as db:
        entries = db.execute(
            "SELECT id,group_id,member,inviter,amount,state,verification,eligible,"
            "present,ever_left,paid_at,paid_by,member_class FROM entries ORDER BY id"
        ).fetchall()
        groups = db.execute(
            "SELECT id,enabled,rate,agent_rate,qualification_seconds,"
            "require_speech,agent_only FROM groups ORDER BY id"
        ).fetchall()
    with sqlite3.connect(
        (data / "wangshangliao_open/open-service.sqlite3").resolve().as_uri()
        + "?mode=ro",
        uri=True,
    ) as db:
        tenants = db.execute(
            "SELECT account,group_id,active,paused,version,managers,rules "
            "FROM tenants ORDER BY account,group_id"
        ).fetchall()
    return {
        "finance": hashlib.sha256(json.dumps(entries).encode()).hexdigest(),
        "entries": len(entries),
        "groups": groups,
        "tenants": tenants,
        "config": hashlib.sha256(
            (ROOT / "data/cmd_config.json").read_bytes()
        ).hexdigest(),
    }


async def main():
    """Check real dashboard names and responsive views with read-only browser IO.

    Raises:
        AssertionError: If labels, authorization or protected state are inconsistent.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reload", action="store_true", help="Reload only the WSL plugin"
    )
    parser.add_argument(
        "--docs", action="store_true", help="Refresh the account-label screenshot"
    )
    parser.add_argument(
        "--lifecycle",
        action="store_true",
        help="Verify lifecycle state and group labels",
    )
    args = parser.parse_args()
    before = snapshot()
    config = json.loads((ROOT / "data/cmd_config.json").read_text(encoding="utf-8-sig"))
    token = jwt.encode(
        {"username": config["dashboard"]["username"], "exp": int(time.time()) + 600},
        config["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )
    output = Path("/opt/backups") / time.strftime(
        "wsl-account-names-%Y%m%dT%H%M%S", time.gmtime()
    )
    output.mkdir(mode=0o700)
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:6185",
        headers={"Authorization": "Bearer " + token},
        timeout=30,
        trust_env=False,
    ) as client:
        if args.reload:
            response = await client.post(
                "/api/v1/plugins/wangshangliao_moderation/reload"
            )
            response.raise_for_status()
            assert response.json()["status"] == "ok"
        response = await client.post(
            PREFIX + "open-service", json={"action": "status", "payload": {}}
        )
        response.raise_for_status()
        state = response.json()["data"]
        assert len(state["accounts"]) == 2
        for account in state["accounts"]:
            assert account["nickname"] and account["state"] == "online", account
            assert account["enabled_groups"] == 0, account
        if args.lifecycle:
            assert all(
                {
                    "admission",
                    "native_role",
                    "cleanup_epoch",
                    "cleanup_done",
                    "leave_state",
                    "membership_pending",
                }
                <= group.keys()
                for group in state["groups"]
            )
            assert all(
                group["paused"] or not group["active"]
                for group in state["groups"]
                if group["group_id"] in {"1516387", "1488006"}
            )
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                executable_path="/root/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome",
                args=["--no-sandbox"],
            )
            for width in (390, 1440):
                page = await browser.new_page(viewport={"width": width, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.add_init_script(
                    "if (window === window.top) {"
                    "localStorage.setItem('token', " + json.dumps(token) + ");"
                    "localStorage.setItem('astrbot-locale', 'zh-CN');"
                    "localStorage.setItem('astrbot:first_notice_seen:v1', '1');"
                    "}"
                )

                async def read_only(route):
                    """Reject browser mutations while allowing runtime status reads.

                    Args:
                        route: Intercepted browser request.
                    """
                    request = route.request
                    if (
                        request.method in {"GET", "HEAD", "OPTIONS"}
                        or (
                            request.url.endswith(PREFIX + "open-service")
                            and request.method == "POST"
                            and request.post_data_json
                            == {"action": "status", "payload": {}}
                        )
                        or (
                            request.url.endswith(
                                "/api/v1/bot-types/wangshangliao/registration"
                            )
                            and request.method == "POST"
                            and request.post_data_json.get("action") == "groups"
                            and set(request.post_data_json) == {"action", "instance_id"}
                            and request.post_data_json["instance_id"]
                            in {a["instance_id"] for a in state["accounts"]}
                        )
                    ):
                        await route.continue_()
                    else:
                        await route.abort()

                await page.route("**/api/**", read_only)
                await page.goto("http://127.0.0.1:6185/#/platforms")
                for account in state["accounts"]:
                    await page.wait_for_function(
                        "(name) => document.querySelector('.platform-page')"
                        "?.textContent.includes(name)",
                        arg=account["nickname"],
                    )
                if width == 390:
                    await page.locator(
                        ".bot-list-panel__mobile-controls .v-select .v-field"
                    ).click()
                    for account in state["accounts"]:
                        await (
                            page.locator(".v-overlay--active .v-list-item")
                            .filter(has_text=account["nickname"])
                            .wait_for()
                        )
                    await (
                        page.locator(".v-overlay--active .v-list-item")
                        .filter(has_text=state["accounts"][-1]["nickname"])
                        .click()
                    )
                else:
                    for account in state["accounts"]:
                        await (
                            page.locator(".bot-list-item__main")
                            .filter(has_text=account["nickname"])
                            .click()
                        )
                        try:
                            await page.wait_for_function(
                                "(name) => document.querySelector('.bot-editor__title')"
                                "?.textContent.trim() === name",
                                arg=account["nickname"],
                            )
                        except Exception:
                            print("Expected:", account["nickname"])
                            print(
                                "Editor:",
                                await page.locator(
                                    ".bot-editor__title"
                                ).all_text_contents(),
                            )
                            print(
                                "List:",
                                await page.locator(
                                    ".bot-list-item__title"
                                ).all_text_contents(),
                            )
                            print("Errors:", errors)
                            await page.screenshot(path=str(output / "error.png"))
                            raise
                await page.locator(".bot-editor__title").wait_for()
                await page.wait_for_function(
                    "(name) => document.querySelector('.bot-editor__title')"
                    "?.textContent.trim() === name",
                    arg=state["accounts"][-1]["nickname"],
                )
                await page.locator(".bot-editor__loading").wait_for(state="hidden")
                await page.locator(
                    ".bot-list-panel__mobile-controls .v-select .v-field"
                ).evaluate("(field) => field.blur()")
                await page.get_by_text("已启用群 0", exact=True).wait_for()
                await page.get_by_text("业务暂停", exact=True).wait_for()
                await page.wait_for_function(
                    "!document.querySelector('.bot-editor__body .v-btn--loading')"
                )
                assert not await page.get_by_text(
                    "群聊读取失败，请重试；账号登录状态保留", exact=False
                ).count()
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth"
                )
                await page.screenshot(
                    path=str(output / f"platforms-{width}.png"),
                    full_page=True,
                    animations="disabled",
                )
                await page.goto(
                    "http://127.0.0.1:6185/#/plugin-page/wangshangliao_moderation/management"
                )
                frame = page.frame_locator("iframe")
                for account in state["accounts"]:
                    await (
                        frame.locator(".open-service .runtime")
                        .get_by_text(account["nickname"], exact=False)
                        .wait_for()
                    )
                if args.lifecycle:
                    select = frame.locator(".open-service select[aria-label='群聊']")
                    await select.select_option("43506527/1459282")
                    await frame.locator(".open-service .capability").first.wait_for()
                    assert await frame.locator(".open-service").evaluate(
                        "(element) => element.scrollWidth <= element.clientWidth"
                    )
                    await frame.locator(".open-service").screenshot(
                        path=str(output / f"lifecycle-{width}.png")
                    )
                    if args.docs and width == 1440:
                        await frame.locator(".open-service").evaluate(
                            """element => {
                              element.querySelector('.runtime').remove();
                              const select = element.querySelector('select[aria-label="群聊"]');
                              select.replaceChildren(new Option('受控测试群 · 测试机器人', select.value, true, true));
                              for (const field of element.querySelectorAll('textarea')) field.value = '';
                            }"""
                        )
                        section_box = await frame.locator(
                            ".open-service"
                        ).bounding_box()
                        heading_box = (
                            await frame.locator(".open-service h3")
                            .filter(has_text="群聊配置")
                            .bounding_box()
                        )
                        status_box = await frame.locator(
                            ".open-service .capability"
                        ).last.bounding_box()
                        await page.screenshot(
                            path=str(
                                ROOT
                                / "docs/zh/platform/images/wangshangliao-lifecycle.png"
                            ),
                            clip={
                                "x": section_box["x"],
                                "y": heading_box["y"] - 8,
                                "width": section_box["width"],
                                "height": status_box["y"]
                                + status_box["height"]
                                + 16
                                - heading_box["y"]
                                + 8,
                            },
                        )
                if args.docs and width == 1440 and not args.lifecycle:
                    await frame.locator(".open-service .runtime").screenshot(
                        path=str(
                            ROOT
                            / "docs/zh/platform/images/wangshangliao-account-names.png"
                        )
                    )
                assert not errors, errors
                await page.screenshot(
                    path=str(output / f"management-{width}.png"),
                    full_page=True,
                    animations="disabled",
                )
                await page.close()
            await browser.close()
        assert snapshot() == before, (
            "Protected state changed during naming verification"
        )
        result = {
            "accounts": state["accounts"],
            "entries_unchanged": before["entries"],
            "config_and_tenants_unchanged": True,
            "viewports": [390, 1440],
        }
        path = output / "verified.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        path.chmod(0o600)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print("Evidence:", output)


if __name__ == "__main__":
    asyncio.run(main())
