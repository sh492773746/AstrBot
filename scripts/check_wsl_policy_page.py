"""Exercise the compiled policy page using a local, non-production bridge."""

import argparse
import asyncio
from pathlib import Path

from PIL import Image, ImageStat
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = r"""
const bot = {
  id: "fixture", account_id: "10001", nickname: "演示群管机器人",
  enable: true, state: "online", business_active: true, revision: "10001:1",
  enabled_groups: ["100001"], group_names: {"100001":"演示兼职群","100002":"第二个演示群"},
  reply_private: true, reply_groups: {"100001":true},
  proactive_send: {enabled:true, targets:["100001"]},
  moderation: {enabled:true, manual_kick_only:true, permissions:{"100001":["rename","cleanup","mute","recall"]}},
};
window.policyRequests = [];
Object.defineProperty(window, "AstrBotPluginPage", {value:{
  ready: async () => ({}), getLocale: () => "zh-CN", onContext: () => () => {},
  apiGet: async (endpoint) => {
    if (endpoint === "settings") return {bots:[structuredClone(bot)], providers:[]};
    if (endpoint === "groups") return {groups:[{id:"100001",name:"演示兼职群"},{id:"100002",name:"第二个演示群"}]};
    if (endpoint === "test-window") return {window:{active:false,remaining:0},instances:[{id:"fixture-source",account:"10002"}]};
    if (endpoint === "operations") return {changes:[],logs:[]};
    if (endpoint === "audit") return {counts:[],reviews:[],sanctions:[],rules:[]};
    if (endpoint === "schedules") return {schedule:null,executions:[],history:[]};
    if (endpoint === "activities") return {
      lottery:{prize:"",winners:3,duration_minutes:15,capacity:15,invite_gate:0,contact:""},
      invite_rate:"0.00",invitation:{enabled:false,rate:"0.00"},current_lottery:null,revision:"activity:1"
    };
    return {};
  },
  apiPost: async (endpoint, body) => {
    window.policyRequests.push({endpoint,body:structuredClone(body)});
    if (endpoint === "settings") {
      bot.enabled_groups = body.enabled_groups;
      bot.revision = "10001:2";
      return {revision:bot.revision};
    }
    if (endpoint === "cards") return {
      id:"fixture-task",state:body.action==="card_execute"?"queued":"preview",
      kind:body.action==="cleanup_preview"?"cleanup":"rename",
      items:[{member:"20001",original:"演示成员",name:"演示·群员",state:"pending"}]
    };
    if (endpoint === "test-window") return {window:{active:body.action==="test_open",remaining:3},instances:[]};
    throw new Error("Unexpected fixture endpoint");
  }
}});
"""


async def main():
    """Verify desktop and mobile controls without using production credentials."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser", required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/zh/platform/images")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    asset = (
        ROOT
        / "astrbot/builtin_stars/wangshangliao_moderation/pages/management/index.html"
    )
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=args.browser, headless=True, args=["--no-sandbox"]
        )
        for name, viewport in (
            ("desktop", {"width": 1440, "height": 1000}),
            ("mobile", {"width": 390, "height": 844}),
        ):
            page = await browser.new_page(viewport=viewport)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.add_init_script(FIXTURE)
            await page.goto(asset.as_uri())
            await page.get_by_role("heading", name="旺商聊群管", exact=True).wait_for()
            assert (
                await page.get_by_role(
                    "option", name="演示兼职群 · 100001", exact=True
                ).count()
                == 1
            )
            inactive = page.locator('select option[value="100002"]')
            assert await inactive.evaluate("element => element.disabled")
            assert "（未启用）" in await inactive.inner_text()
            await page.get_by_role("tab", name="群聊与运行", exact=True).click()
            inactive_toggle = page.get_by_role(
                "checkbox", name="第二个演示群", exact=False
            )
            await inactive_toggle.wait_for()
            assert not await inactive_toggle.is_checked()
            assert await page.evaluate(
                "document.documentElement.scrollWidth <= window.innerWidth"
            )
            shot = args.output / f"wangshangliao-policy-{name}.png"
            await page.screenshot(path=str(shot), full_page=True)
            with Image.open(shot) as image:
                assert max(ImageStat.Stat(image.convert("RGB")).stddev) > 10
            await page.get_by_role("button", name="生成预览", exact=True).click()
            execute = page.get_by_role("button", name="确认执行", exact=True)
            await execute.wait_for()
            assert await execute.is_enabled()
            await execute.click()
            assert await execute.is_disabled()
            await page.get_by_role("radio", name="封禁／注销清理", exact=True).check()
            await page.get_by_role("button", name="生成预览", exact=True).click()
            await page.get_by_text(
                "确认移出预览中的封禁／注销成员", exact=True
            ).wait_for()
            assert await execute.is_disabled()
            await page.get_by_role(
                "checkbox", name="确认移出预览中的封禁／注销成员", exact=True
            ).check()
            assert await execute.is_enabled()
            assert not errors, errors
            assert len(await page.evaluate("window.policyRequests")) == 3
            await page.close()
        await browser.close()
    print("Desktop/mobile policy controls passed; mock bridge only, no live mutations.")


if __name__ == "__main__":
    asyncio.run(main())
