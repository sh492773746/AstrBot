"""Exercise the built whitelist UI with isolated, synthetic group members."""

import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = """
(() => {
  const groups = ["5", "6"].map(group_id => ({
    account: "1", group_id, name: group_id === "5" ? "白名单验收群" : "第二验收群",
    active: true, present: true, version: 1, epoch: 1, paused: 0,
    native_role: "GROUP_ROLE_ADMIN", admission: "verified", rules_only: true,
    rules: {advertising: true, flood: true, keywords: [], custom_rules: []},
    managers: ["2"], native_capability: {state: "available"},
  }));
  const candidates = [{id: "3", name: "芋泥波波"}, {id: "4", name: "芋泥波波"}];
  const lists = {"5": [], "6": []};
  let selected = null;
  window.whitelistCalls = [];
  const apiPost = async (endpoint, {action, payload}) => {
    if (endpoint !== "open-service") throw Error("Unexpected endpoint");
    if (action === "status") return {
      mode: "integrated", settings: {paused: 0, global_limit: 100},
      groups, accounts: [{account: "1", nickname: "测试机器人"}],
      ai_profiles: [], providers: [], queue_health: [], audit: [], admins: [],
    };
    if (action === "tenant_members") return {members: [{id: "2", name: "测试管理", role: "GROUP_ROLE_OWNER"}]};
    if (action === "business_rules") return {config: {keywords: [], ads: []}, version: 0, schedules: []};
    if (action === "page_schema") return {schema: {properties: {}}};
    if (action !== "member_whitelist") throw Error("Unexpected action: " + action);
    window.whitelistCalls.push(structuredClone(payload));
    const group = payload.group_id;
    if (payload.operation === "read") return {version: 0, members: lists[group]};
    if (payload.operation === "search") return {
      selection: "s".repeat(24), version: 0,
      members: group === "5" && payload.query === "芋泥" ? candidates : [],
    };
    if (payload.operation === "preview") {
      if (payload.selection !== "s".repeat(24)) throw Error("Invalid selection");
      selected = candidates[payload.index - 1];
      return {token: "t".repeat(24), mode: "add", member: selected};
    }
    if (payload.operation === "commit") {
      if (payload.token !== "t".repeat(24) || !selected) throw Error("Invalid preview");
      lists[group] = [selected];
      return {saved: true, mode: "add", member: selected.name};
    }
    throw Error("Unexpected operation");
  };
  Object.defineProperty(window, "AstrBotPluginPage", {value: {
    ready: async () => {}, getLocale: () => "zh-CN", onContext: () => () => {},
    apiGet: async () => ({open_mode: "integrated", bots: [], providers: []}), apiPost,
  }, writable: false, configurable: false});
})();
"""


async def main():
    """Verify name search, disambiguation, confirmation and responsive geometry."""
    output = ROOT / "docs/public/images/wangshangliao"
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(args=["--no-sandbox"])
        try:
            for label, width, height in [("desktop", 1280, 960), ("mobile", 390, 844)]:
                page = await browser.new_page(
                    viewport={"width": width, "height": height}
                )
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.add_init_script(FIXTURE)
                await page.goto(
                    (
                        ROOT
                        / "astrbot/builtin_stars/wangshangliao_moderation/pages/management/index.html"
                    ).as_uri()
                )
                await page.get_by_label("群聊", exact=True).select_option("1/5")
                panel = page.get_by_role("region", name="本群发言白名单")
                await expect(panel.get_by_text("暂无白名单成员")).to_be_visible()
                await panel.get_by_label("搜索本群成员名称").fill("芋泥")
                await panel.get_by_role("button", name="搜索", exact=True).click()
                radios = panel.get_by_role(
                    "radiogroup", name="本群成员搜索结果"
                ).get_by_role("radio")
                await expect(radios).to_have_count(2)
                await radios.nth(1).check()
                if label == "desktop":
                    await panel.screenshot(
                        path=str(output / "member-whitelist-desktop.png")
                    )
                await panel.get_by_role("button", name="选择并预览").click()
                preview = panel.get_by_role("alertdialog", name="确认本群白名单变更")
                await expect(preview.get_by_text("芋泥波波 · UID 4")).to_be_visible()
                calls = await page.evaluate("window.whitelistCalls")
                assert not any(row["operation"] == "commit" for row in calls)
                if label == "mobile":
                    await panel.screenshot(
                        path=str(output / "member-whitelist-mobile.png")
                    )
                await preview.get_by_role("button", name="取消", exact=True).click()
                await expect(preview).to_have_count(0)
                await panel.get_by_role("button", name="搜索", exact=True).click()
                await (
                    panel.get_by_role("radiogroup").get_by_role("radio").nth(1).check()
                )
                await panel.get_by_role("button", name="选择并预览").click()
                await preview.get_by_role("button", name="确认保存").click()
                await expect(
                    panel.get_by_label("已加入白名单的成员").get_by_text("UID 4")
                ).to_be_visible()
                await page.get_by_label("群聊", exact=True).select_option("1/6")
                await expect(panel.get_by_text("暂无白名单成员")).to_be_visible()
                await expect(panel.get_by_role("radiogroup")).to_have_count(0)
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth"
                )
                calls = await page.evaluate("window.whitelistCalls")
                assert len([row for row in calls if row["operation"] == "commit"]) == 1
                assert not any(
                    {"id", "uid", "member", "peer"} & row.keys() for row in calls
                )
                assert not errors, json.dumps(errors, ensure_ascii=False)
                print(
                    f"{label}: search, homonyms, cancellation, confirmation and group isolation passed"
                )
                await page.close()
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
