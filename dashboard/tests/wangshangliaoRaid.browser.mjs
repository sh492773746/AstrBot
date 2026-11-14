// Isolated browser acceptance; no production API or network proxy is used.
import assert from "node:assert/strict";
import { mkdir, mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const output = process.env.RAID_SCREENSHOTS || await mkdtemp(join(tmpdir(), "wsl-raid-"));
await mkdir(output, { recursive: true });
const server = await createServer({
  configFile: false, root: fileURLToPath(new URL("..", import.meta.url)),
  cacheDir: join(output, "vite-cache"), plugins: [vue()],
  optimizeDeps: { entries: ["tests/fixtures/wangshangliao-raid.html"] },
  server: { host: "127.0.0.1", port: 0 }, clearScreen: false,
});
let browser;
try {
  await server.listen();
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch({
    headless: true, args: ["--no-sandbox"],
    ...(process.env.PLAYWRIGHT_CHROMIUM ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM } : {}),
  });
  for (const width of [320, 390, 1440]) {
    const page = await browser.newPage({ viewport: { width, height: 1000 } });
    const errors = [];
    page.on("pageerror", e => errors.push(e.message));
    await page.route("**/*", route => {
      const url = new URL(route.request().url());
      return url.origin === origin && !url.pathname.startsWith("/api/")
        ? route.continue() : route.abort();
    });
    await page.goto(`${origin}/tests/fixtures/wangshangliao-raid.html`);
    const panel = page.getByRole("region", { name: "防袭群状态" });
    await panel.getByText("临时防护中", { exact: true }).waitFor();
    assert.match(await panel.textContent(), /结果未知 · 不自动重试/);
    assert.match(await panel.textContent(), /平台已受理/);
    assert.match(await panel.textContent(), /执行被拒绝/);
    assert.match(await panel.textContent(), /撤回消息/);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await panel.screenshot({ path: join(output, `raid-${width}.png`) });
    await panel.getByRole("button", { name: "结束本轮防护", exact: true }).click();
    const dialog = panel.getByRole("alertdialog");
    await dialog.waitFor();
    assert.match(await dialog.textContent(), /已有禁言保持不变/);
    assert.equal(await page.evaluate(() => window.raidFixture.calls.filter(c => c.operation === "commit").length), 0);
    await dialog.getByRole("button", { name: "取消", exact: true }).click();
    await panel.getByRole("button", { name: "结束本轮防护", exact: true }).click();
    await dialog.getByRole("button", { name: "确认结束", exact: true }).click();
    await panel.getByText("观察中", { exact: true }).waitFor();
    assert.equal(await page.evaluate(() => window.raidFixture.calls.filter(c => c.operation === "commit").length), 1);
    await page.evaluate(() => { window.raidFixture.fail = "read"; });
    await panel.getByRole("button", { name: "刷新防袭群状态" }).click();
    await panel.getByRole("alert").waitFor();
    assert.match(await panel.getByRole("alert").textContent(), /超时/);
    await page.evaluate(() => { window.raidFixture.hold = true; });
    await panel.getByRole("button", { name: "刷新防袭群状态" }).click();
    await page.waitForFunction(() => window.raidFixture.releases.length === 1);
    await page.getByLabel("群聊", { exact: true }).selectOption("6");
    await panel.getByText("已关闭", { exact: true }).waitFor();
    await page.evaluate(() => window.raidFixture.releases.shift()());
    assert.equal(await panel.getByText("临时防护中", { exact: true }).count(), 0);
    assert.deepEqual(errors, []);
    await page.close();
  }
  console.log(JSON.stringify({ viewports: [320, 390, 1440], output, passed: true }));
} finally {
  if (browser) await browser.close();
  await server.close();
}
