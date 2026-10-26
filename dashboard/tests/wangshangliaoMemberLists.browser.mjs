// Offline bridge fixture only; no production requests or live member mutations.
import assert from "node:assert/strict";
import { mkdir, mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const output = process.env.MEMBER_LIST_SCREENSHOTS || await mkdtemp(join(tmpdir(), "wsl-member-lists-"));
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
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch({
    headless: true,
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined,
    args: ["--no-sandbox"],
  });
  for (const width of [320, 390, 1440]) {
    for (const kind of ["whitelist", "blacklist"]) {
      const white = kind === "whitelist";
      const action = `member_${kind}`;
      const name = white ? "本群发言白名单" : "本群成员黑名单";
      const label = white ? "白名单" : "黑名单";
      const target = white ? "12" : "8";
      const pair = white ? "13" : "16";
      const neighbor = white ? "14" : "17";
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
      const group = page.getByLabel("群聊", { exact: true });
      await group.selectOption("1/5");
      const panel = page.getByRole("region", { name, exact: true });
      const otherPanel = page.getByRole("region", {
        name: white ? "本群成员黑名单" : "本群发言白名单", exact: true,
      });
      const list = panel.getByRole("list", { name: `已加入${label}的成员` });
      const dialog = panel.getByRole("alertdialog");
      const calls = () => page.evaluate(action => window.blacklistFixture.calls
        .filter(call => call.action === action).map(call => call.payload), action);
      const remove = id => panel.getByRole("button", { name: new RegExp(`（UID ${id}）移出${label}$`) });
      await list.getByText(`UID ${target}`, { exact: true }).waitFor();
      assert.match(await list.textContent(), white ? /同时在黑名单/ : /同时在白名单/);
      if (white) {
        assert.match(await list.textContent(), /昨日前30名 · 昨日 101 条有效发言/);
        assert.doesNotMatch(await list.textContent(), /今日前30名/);
      }
      assert.equal(await list.getByRole("button").count(), await list.getByRole("listitem").count());
      assert.equal(await remove(target).getAttribute("title"), `移出${label}`);
      assert.equal(await remove(target).locator("svg").count(), 1);
      assert((await calls()).every(call => call.operation === "read"));
      await panel.screenshot({ path: join(output, `${kind}-rows-${width}.png`) });

      const beforeInput = (await calls()).length;
      await panel.getByRole("searchbox").fill("只改输入");
      await panel.getByRole("searchbox").fill("不会查询");
      await page.waitForTimeout(100);
      assert.equal((await calls()).length, beforeInput);
      await panel.getByRole("searchbox").fill("4");
      await panel.getByRole("button", { name: "搜索", exact: true }).click();
      await panel.getByText(white ? "未找到符合条件的本群成员" : "未找到可添加的本群成员", { exact: true }).waitFor();
      assert.equal(await panel.getByRole("radiogroup").count(), 0);
      assert.equal(await dialog.count(), 0);

      await panel.getByRole("searchbox").fill(white ? "正在处理的成员" : "同名名单成员");
      await panel.getByRole("button", { name: "搜索", exact: true }).click();
      await panel.getByRole("radiogroup").getByRole("radio").first().check();
      await panel.getByRole("button", { name: "选择并预览" }).click();
      await panel.getByRole("alert").waitFor();
      assert.match(await panel.getByRole("alert").textContent(),
        white ? /先移出黑名单，再加入白名单/ : /先移出白名单，再加入黑名单/);
      assert.doesNotMatch(await panel.getByRole("alert").textContent(), /结果未确认|读取或预览失败/);
      assert.equal(await dialog.count(), 0);
      assert(!(await calls()).some(call => call.operation === "commit"));
      await panel.screenshot({ path: join(output, `${kind}-conflict-${width}.png`) });

      await page.evaluate(() => {
        window.blacklistFixture.fail = "search";
        window.blacklistFixture.error = "名单未修改：未知内部错误 private-debug-secret <script>bad()</script>";
      });
      await panel.getByRole("button", { name: "搜索", exact: true }).click();
      await page.waitForFunction(({ name }) => document.querySelector(`[aria-label="${name}"]`)
        .querySelector('[role="alert"]')?.textContent === "读取或预览失败，请刷新后重试。", { name });
      assert.doesNotMatch(await panel.textContent(), /private-debug-secret|bad\(\)/);

      for (const operation of ["search", "preview"]) {
        const before = (await calls()).length;
        await page.evaluate(operation => { window.blacklistFixture.hold = operation; }, operation);
        await remove(target).click();
        await page.waitForFunction(() => window.blacklistFixture.waiting.length === 1);
        assert(await remove(neighbor).isDisabled());
        await panel.getByRole("button", { name: "取消", exact: true }).click();
        await page.evaluate(() => window.blacklistFixture.release(true));
        assert.equal(await dialog.count(), 0);
        assert.equal(await panel.getByRole("alert").count(), 0);
        assert.deepEqual((await calls()).slice(before).map(call => call.operation),
          operation === "search" ? ["search"] : ["search", "preview"]);
      }

      for (const operation of ["search", "preview"]) {
        await page.evaluate(operation => { window.blacklistFixture.hold = operation; }, operation);
        await remove(target).click();
        await page.waitForFunction(() => window.blacklistFixture.waiting.length === 1);
        await group.selectOption("1/6");
        await panel.getByText(`暂无${label}成员`, { exact: true }).waitFor();
        await page.evaluate(() => window.blacklistFixture.release());
        assert.equal(await dialog.count(), 0);
        assert(!(await calls()).some(call => call.group_id === "6" && call.operation !== "read"));
        await group.selectOption("1/5");
        await list.getByText(`UID ${target}`, { exact: true }).waitFor();
      }

      const beforeRemoval = (await calls()).length;
      await remove(target).click();
      await dialog.waitFor();
      assert.match(await dialog.textContent(), new RegExp(`UID ${target}`));
      assert.match(await dialog.textContent(), /机器人 1 · 群 5/);
      assert.match(await dialog.textContent(), white ? /不会解除已有禁言/ : /不自动解禁/);
      assert.deepEqual((await calls()).slice(beforeRemoval).map(call => call.operation), ["search", "preview"]);
      assert.equal((await calls()).at(-2).mode, "remove");
      assert.equal((await calls()).at(-2).query, target);
      await panel.screenshot({ path: join(output, `${kind}-remove-${width}.png`) });
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      assert.deepEqual(await panel.locator("button,input,select,dd,strong,p,small").evaluateAll(elements =>
        elements.filter(el => el.scrollWidth > el.clientWidth + 1).map(el => el.outerHTML)), []);
      await dialog.getByRole("button", { name: "确认保存" }).click();
      await panel.getByText("已移除：已离群成员", { exact: true }).waitFor();
      await list.getByText(`UID ${neighbor}`, { exact: true }).waitFor();
      assert.equal(await list.getByText(`UID ${target}`, { exact: true }).count(), 0);
      const removalCalls = (await calls()).slice(beforeRemoval);
      assert.deepEqual(removalCalls.map(call => call.operation), ["search", "preview", "commit", "read"]);
      assert.deepEqual(Object.keys(removalCalls[2]).sort(),
        ["account", "group_id", "operation", "tenant_version", "token"]);

      await remove(pair).click();
      await dialog.waitFor();
      assert.match(await dialog.textContent(), new RegExp(`UID ${pair}`));
      await page.evaluate(() => { window.blacklistFixture.hold = "commit"; });
      await dialog.getByRole("button", { name: "确认保存" }).click();
      await page.waitForFunction(() => window.blacklistFixture.waiting.length === 1);
      assert(await remove(neighbor).isDisabled());
      await page.evaluate(() => window.blacklistFixture.release(true));
      await panel.getByRole("alert").waitFor();
      assert.match(await panel.getByRole("alert").textContent(), /结果未确认.*不会自动重试/);
      assert.equal(await dialog.count(), 0);
      const commits = (await calls()).filter(call => call.operation === "commit").length;
      await panel.getByRole("button", { name: `刷新${label}`, exact: true }).click();
      await list.getByText(`UID ${neighbor}`, { exact: true }).waitFor();
      assert.equal(await list.getByText(`UID ${pair}`, { exact: true }).count(), 0);
      assert.equal((await calls()).filter(call => call.operation === "commit").length, commits);

      await remove("11").click();
      await dialog.waitFor();
      await dialog.getByRole("button", { name: "确认保存" }).click();
      await page.waitForFunction(({ name }) => {
        const region = document.querySelector(`[aria-label="${name}"]`);
        return region.querySelector('[role="status"]')?.textContent.includes("已移除：");
      }, { name });
      await list.getByText(`UID ${neighbor}`, { exact: true }).waitFor();
      assert.equal(await list.getByText("UID 11", { exact: true }).count(), 0);
      await otherPanel.getByRole("list").getByText("UID 11", { exact: true }).waitFor();

      await panel.getByRole("radio", { name: "添加成员", exact: true }).check();
      await panel.getByRole("searchbox").fill("同名");
      await panel.getByRole("button", { name: "搜索", exact: true }).click();
      await panel.getByRole("radiogroup").getByRole("radio").first().check();
      await panel.getByRole("button", { name: "选择并预览" }).click();
      await dialog.waitFor();
      assert.match(await dialog.textContent(), /UID 3/);
      await page.evaluate(() => {
        window.blacklistFixture.fail = "commit";
        window.blacklistFixture.error = "名单未修改：该成员已在本群另一份名单中，请先移除另一名单，再重新添加。黑白名单不能同时添加，历史记录不会自动删除。";
      });
      await dialog.getByRole("button", { name: "确认保存" }).click();
      await panel.getByRole("alert").waitFor();
      assert.match(await panel.getByRole("alert").textContent(),
        white ? /先移出黑名单，再加入白名单/ : /先移出白名单，再加入黑名单/);
      assert.doesNotMatch(await panel.getByRole("alert").textContent(), /结果未确认|读取或预览失败/);
      assert.equal(await dialog.count(), 0);
      const conflictCommits = (await calls()).filter(call => call.operation === "commit").length;
      await panel.getByRole("button", { name: `刷新${label}`, exact: true }).click();
      await list.getByText(`UID ${neighbor}`, { exact: true }).waitFor();
      assert.equal((await calls()).filter(call => call.operation === "commit").length, conflictCommits);
      assert.equal(await list.getByText("UID 3", { exact: true }).count(), 0);
      await panel.getByRole("button", { name: "搜索", exact: true }).click();
      await panel.getByRole("radiogroup").getByRole("radio").first().check();
      await panel.getByRole("button", { name: "选择并预览" }).click();
      await dialog.waitFor();
      await dialog.getByRole("button", { name: "确认保存" }).click();
      await list.getByText("UID 3", { exact: true }).waitFor();
      await page.evaluate(() => { document.documentElement.dataset.theme = "dark"; });
      await panel.screenshot({ path: join(output, `${kind}-dark-${width}.png`) });
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      assert.deepEqual(errors, []);
      assert.deepEqual(blocked, []);
      assert((await calls()).every(call => !["id", "uid", "peer", "member"].some(key => key in call)));
      assert(!(await calls()).some(call => call.group_id === "6" && call.operation !== "read"));
      console.log(`PASS ${kind} ${width}px: row removal, exact UID/homonyms, Chinese preview/commit conflicts, unknown error redaction, cancellation, stale responses, lost commit, no idle requests, light/dark`);
      await page.close();
    }
  }
  console.log(`Screenshots: ${output}`);
} finally {
  await browser?.close();
  await server.close();
}
