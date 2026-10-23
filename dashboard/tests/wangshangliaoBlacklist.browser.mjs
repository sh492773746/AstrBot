// Run with node; set PLAYWRIGHT_MODULE to a local Playwright module if needed.
// The fixture server has no backend proxy, and browser API requests are blocked.
import assert from "node:assert/strict";
import { mkdir, mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const output = process.env.BLACKLIST_SCREENSHOTS || await mkdtemp(join(tmpdir(), "wsl-blacklist-"));
await mkdir(output, { recursive: true });
const server = await createServer({
  configFile: false,
  root: fileURLToPath(new URL("..", import.meta.url)),
  cacheDir: join(output, "vite-cache"),
  plugins: [vue()],
  optimizeDeps: { entries: ["tests/fixtures/wangshangliao-blacklist.html"] },
  server: { host: "127.0.0.1", port: 0 },
  clearScreen: false,
});
let browser;
try {
  await server.listen();
  const address = server.httpServer.address();
  const origin = `http://127.0.0.1:${address.port}`;
  browser = await chromium.launch({ headless: true, args: ["--no-sandbox"] });
  for (const width of [320, 390, 1440]) {
    const page = await browser.newPage({ viewport: { width, height: 1000 } });
    const errors = [];
    const blocked = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/*", route => {
      const url = new URL(route.request().url());
      if (url.origin !== origin || url.pathname.startsWith("/api/")) {
        blocked.push(url.pathname);
        return route.abort();
      }
      return route.continue();
    });
    await page.goto(`${origin}/tests/fixtures/wangshangliao-blacklist.html`);
    await page.getByLabel("群聊", { exact: true }).selectOption("1/5");
    const panel = page.getByRole("region", { name: "本群成员黑名单" });
    const list = panel.getByRole("list", { name: "已加入黑名单的成员" });
    const text = () => panel.textContent();
    await panel.getByText("已离群成员", { exact: true }).waitFor();
    assert.match(await text(), /每次真实重新入群直接禁言/);
    assert.doesNotMatch(await text(), /踢出|踢人|第4次|发言或入群|等待后续发言/);
    assert.match(await text(), /加入名单不立即处罚/);
    assert.match(await text(), /时长仅影响以后重入，不重设已有禁言；移除不自动解禁/);
    assert.match(await list.textContent(), /重入禁言/);
    assert.match(await list.textContent(), /处理中 · 不重复提交/);
    assert.match(await list.textContent(), /结果未知 · 待核对，不自动重试/);
    assert.match(await list.textContent(), /执行出错 · 待核对/);
    assert.match(await list.textContent(), /名单有效 · 执行状态未提供/);

    const search = async (query = "同名") => {
      await panel.getByRole("searchbox").fill(query);
      await panel.getByRole("button", { name: "搜索", exact: true }).click();
      await panel.getByRole("radiogroup").waitFor();
    };
    const preview = async () => {
      await search();
      await panel.getByRole("radiogroup").locator("label")
        .filter({ hasText: /^同名成员\s*UID / }).getByRole("radio").last().check();
      await panel.getByRole("button", { name: "选择并预览" }).click();
      await panel.getByRole("alertdialog").waitFor();
    };
    const calls = () => page.evaluate(() =>
      window.blacklistFixture.calls.filter(c => c.action === "member_blacklist").map(c => c.payload));

    const duration = panel.getByRole("spinbutton", { name: "重入禁言时长" });
    const unit = panel.getByLabel("禁言时长单位");
    const settingsButton = panel.getByRole("button", { name: "预览时长", exact: true });
    const dialog = panel.getByRole("alertdialog");
    assert.equal(await duration.inputValue(), "29");
    assert.equal(await unit.inputValue(), "1440");
    assert.match(await text(), /当前 41760 分钟/);
    assert((await calls()).every(c => c.operation === "read"));
    await unit.selectOption("1");
    for (const invalid of ["", "0", "1.5", "41761"]) {
      await duration.fill(invalid);
      assert(await settingsButton.isDisabled());
      assert.equal(await duration.getAttribute("aria-invalid"), "true");
    }
    for (const [value, scale, expected] of [["1", "1", 1], ["29", "1440", 41760], ["0.575", "1440", 828]]) {
      await duration.fill(value);
      await unit.selectOption(scale);
      await settingsButton.click();
      await dialog.waitFor();
      assert.match(await dialog.textContent(), new RegExp(`${expected} 分钟`));
      assert.equal((await calls()).at(-1).minutes, expected);
      await dialog.getByRole("button", { name: "取消", exact: true }).click();
    }
    await duration.fill("1.5");
    await unit.selectOption("60");
    await settingsButton.click();
    await dialog.waitFor();
    assert.match(await dialog.textContent(), /确认禁言时长.*90 分钟/s);
    assert.match(await dialog.textContent(), /机器人 1 · 群 5/);
    assert.match(await dialog.textContent(), /仅影响以后重入，不立即处罚、不重设已有禁言/);
    assert(!(await calls()).some(c => c.operation === "commit"));
    await panel.screenshot({ path: join(output, `blacklist-settings-${width}.png`) });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    const settingsToken = await page.evaluate(() => [...window.blacklistFixture.calls]
      .reverse().find(c => c.payload.operation === "settings_preview").payload);
    assert.deepEqual(settingsToken, {
      account: "1", group_id: "5", tenant_version: 1, operation: "settings_preview", minutes: 90,
    });
    await dialog.getByRole("button", { name: "确认保存" }).click();
    await panel.getByText("已保存禁言时长：90 分钟", { exact: true }).waitFor();
    assert.equal(await duration.inputValue(), "90");
    assert.equal(await unit.inputValue(), "1");
    const settingsCommit = (await calls()).find(c => c.operation === "commit");
    assert.deepEqual(Object.keys(settingsCommit).sort(), ["account", "group_id", "operation", "tenant_version", "token"]);
    assert.match(settingsCommit.token, /^settings-/);
    assert.match(await text(), /当前 90 分钟/);
    assert.deepEqual((await calls()).slice(-2).map(c => c.operation), ["commit", "read"]);
    const settingsCommits = (await calls()).filter(c => c.operation === "commit").length;

    await page.evaluate(() => { window.blacklistFixture.muteMinutes["5"] = 120; });
    await preview();
    assert.match(await dialog.textContent(), /同名成员 · UID 4/);
    assert.match(await dialog.textContent(), /机器人 1 · 群 5/);
    assert.match(await text(), /当前 90 分钟/);
    assert.match(await dialog.textContent(), /加入名单不立即处罚.*直接禁言120分钟/s);
    assert.equal((await calls()).filter(c => c.operation === "commit").length, settingsCommits);
    await dialog.getByRole("button", { name: "取消", exact: true }).click();
    assert.equal(await dialog.count(), 0);
    await page.evaluate(() => { window.blacklistFixture.muteMinutes["5"] = 90; });
    await preview();
    await panel.screenshot({ path: join(output, `blacklist-${width}.png`) });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    const overflow = await panel.locator("button,input,select,dd,strong,p").evaluateAll(elements =>
      elements.filter(el => el.scrollWidth > el.clientWidth + 1).map(el => el.outerHTML));
    assert.deepEqual(overflow, []);
    await dialog.getByRole("button", { name: "确认保存" }).click();
    await list.getByText("UID 4", { exact: true }).waitFor();
    assert.match(await text(), /已添加：同名成员/);
    assert.equal((await calls()).filter(c => c.operation === "commit").length, settingsCommits + 1);

    await panel.getByRole("radio", { name: "移除成员", exact: true }).check();
    await search("已离群");
    await panel.getByRole("radiogroup").getByRole("radio").check();
    await panel.getByRole("button", { name: "选择并预览" }).click();
    await dialog.waitFor();
    assert.match(await dialog.textContent(), /移除.*不自动解禁/s);
    await dialog.getByRole("button", { name: "确认保存" }).click();
    await page.waitForFunction(() => {
      const region = document.querySelector('[aria-label="本群成员黑名单"]');
      return region.textContent.includes("已移除：已离群成员");
    });
    assert.equal(await list.getByText("UID 8", { exact: true }).count(), 0);

    await panel.getByRole("radio", { name: "添加成员", exact: true }).check();
    await page.evaluate(() => { window.blacklistFixture.hold = "search"; });
    await panel.getByRole("searchbox").fill("同名");
    await panel.getByRole("button", { name: "搜索", exact: true }).click();
    await panel.getByRole("button", { name: "取消", exact: true }).click();
    await page.evaluate(() => window.blacklistFixture.release());
    assert.equal(await panel.getByRole("radiogroup").count(), 0);
    assert.equal(await dialog.count(), 0);

    await search();
    await panel.getByRole("radiogroup").locator("label")
      .filter({ hasText: /^同名成员\s*UID / }).getByRole("radio").last().check();
    await page.evaluate(() => { window.blacklistFixture.hold = "preview"; });
    await panel.getByRole("button", { name: "选择并预览" }).click();
    await panel.getByRole("button", { name: "取消", exact: true }).click();
    await page.evaluate(() => window.blacklistFixture.release(true));
    assert.equal(await panel.getByRole("alert").count(), 0);

    for (const fail of [false, true]) {
      await page.evaluate(() => { window.blacklistFixture.hold = "settings_preview"; });
      await settingsButton.click();
      await panel.getByRole("button", { name: "取消", exact: true }).click();
      await page.evaluate(fail => window.blacklistFixture.release(fail), fail);
      assert.equal(await dialog.count(), 0);
      assert.equal(await panel.getByRole("alert").count(), 0);
    }

    await settingsButton.click();
    await dialog.waitFor();
    await page.getByLabel("群聊", { exact: true }).selectOption("1/6");
    await panel.getByText("暂无黑名单成员", { exact: true }).waitFor();
    assert.equal(await dialog.count(), 0);
    assert.equal(await duration.inputValue(), "2");
    assert.equal(await unit.inputValue(), "60");
    await page.getByLabel("群聊", { exact: true }).selectOption("1/5");
    await list.getByText("UID 4", { exact: true }).waitFor();
    await page.evaluate(() => { window.blacklistFixture.hold = "settings_preview"; });
    await settingsButton.click();
    await page.getByLabel("群聊", { exact: true }).selectOption("1/6");
    await panel.getByText("暂无黑名单成员", { exact: true }).waitFor();
    await page.evaluate(() => window.blacklistFixture.release());
    assert.equal(await dialog.count(), 0);
    assert.equal(await duration.inputValue(), "2");
    assert.equal(await unit.inputValue(), "60");
    await page.getByLabel("群聊", { exact: true }).selectOption("1/5");
    await list.getByText("UID 4", { exact: true }).waitFor();

    await preview();
    await page.getByLabel("群聊", { exact: true }).selectOption("1/6");
    await panel.getByText("暂无黑名单成员", { exact: true }).waitFor();
    assert.equal(await dialog.count(), 0);
    assert.equal(await panel.getByRole("radiogroup").count(), 0);
    await page.getByLabel("群聊", { exact: true }).selectOption("1/5");
    await list.getByText("UID 4", { exact: true }).waitFor();
    await duration.fill("30");
    await unit.selectOption("1");
    await settingsButton.click();
    await dialog.waitFor();
    await page.evaluate(() => { window.blacklistFixture.hold = "commit"; });
    await dialog.getByRole("button", { name: "确认保存" }).click();
    await page.waitForFunction(() => window.blacklistFixture.waiting.length === 1);
    assert(await settingsButton.isDisabled());
    await page.evaluate(() => window.blacklistFixture.release(true));
    await panel.getByRole("alert").waitFor();
    assert.match(await panel.getByRole("alert").textContent(), /结果未确认.*不会自动重试/);
    assert(await settingsButton.isDisabled());
    const failedSettingsCommits = (await calls()).filter(c => c.operation === "commit").length;
    await panel.getByRole("button", { name: "刷新黑名单", exact: true }).click();
    await list.getByText("UID 4", { exact: true }).waitFor();
    assert.match(await text(), /当前 30 分钟/);
    assert.equal(await duration.inputValue(), "30");
    assert.equal((await calls()).filter(c => c.operation === "commit").length, failedSettingsCommits);

    await page.evaluate(() => { window.blacklistFixture.hold = "search"; });
    await panel.getByRole("searchbox").fill("同名");
    await panel.getByRole("button", { name: "搜索", exact: true }).click();
    await page.getByLabel("群聊", { exact: true }).selectOption("1/6");
    await panel.getByText("暂无黑名单成员", { exact: true }).waitFor();
    await page.evaluate(() => window.blacklistFixture.release());
    assert.equal(await panel.getByRole("radiogroup").count(), 0);
    assert.equal(await dialog.count(), 0);

    await page.getByLabel("群聊", { exact: true }).selectOption("1/5");
    await list.getByText("UID 4", { exact: true }).waitFor();
    await preview();
    await page.evaluate(() => { window.blacklistFixture.fail = "commit"; });
    await dialog.getByRole("button", { name: "确认保存" }).click();
    await panel.getByRole("alert").waitFor();
    assert.equal(await dialog.count(), 0);
    assert.equal(await panel.getByRole("button", { name: "确认保存" }).count(), 0);
    const commitCount = (await calls()).filter(c => c.operation === "commit").length;
    await panel.getByRole("button", { name: "刷新黑名单", exact: true }).click();
    await list.getByText("UID 4", { exact: true }).waitFor();
    assert.equal((await calls()).filter(c => c.operation === "commit").length, commitCount);
    await page.evaluate(() => { document.documentElement.dataset.theme = "dark"; });
    await panel.screenshot({ path: join(output, `blacklist-dark-${width}.png`) });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    assert.deepEqual(errors, []);
    assert.deepEqual(blocked, []);
    assert((await calls()).every(c => !["uid", "id", "peer", "member"].some(key => key in c)));
    assert(!(await calls()).some(c => c.group_id === "6" && c.operation !== "read"));
    console.log(`PASS ${width}px: duration units/bounds, token-only settings, add/remove, no kick counts, cancellation, stale responses, failed commits, light/dark layout`);
    await page.close();
  }
  console.log(`Screenshots: ${output}`);
} finally {
  await browser?.close();
  await server.close();
}
