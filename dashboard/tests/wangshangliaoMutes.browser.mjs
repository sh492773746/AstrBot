// Isolated preview and partial-result checks; never connects to production.
import assert from "node:assert/strict";
import { mkdir, mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const output = process.env.MUTES_SCREENSHOTS || await mkdtemp(join(tmpdir(), "wsl-mutes-"));
await mkdir(output, { recursive: true });
const server = await createServer({
  configFile: false, root: fileURLToPath(new URL("..", import.meta.url)),
  cacheDir: join(output, "vite-cache"), plugins: [vue()],
  optimizeDeps: { entries: ["tests/fixtures/wangshangliao-mutes.html"] },
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
    for (const locale of ["zh-CN", "en-US"]) {
      const page = await browser.newPage({ viewport: { width, height: 900 } });
      const errors = [];
      page.on("pageerror", e => errors.push(e.message));
      await page.route("**/*", route => {
        const url = new URL(route.request().url());
        return url.origin === origin && !url.pathname.startsWith("/api/")
          ? route.continue() : route.abort();
      });
      await page.goto(`${origin}/tests/fixtures/wangshangliao-mutes.html?lang=${locale}`);
      const zh = locale === "zh-CN";
      const panel = page.getByRole("region", { name: zh ? "当前禁言成员" : "Currently muted members" });
      const unmute = panel.getByRole("button", { name: zh ? "解除禁言" : "Unmute", exact: true });
      const confirm = zh ? "确认解禁" : "Confirm";
      await unmute.click();
      const dialog = panel.getByRole("alertdialog");
      assert.match(await dialog.textContent(), zh ? /同时移出本群黑名单/ : /also removes this group's blacklist entry/);
      assert.equal(await page.evaluate(() => window.mutesFixture.commits), 0);
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await panel.screenshot({ path: join(output, `mutes-confirm-${locale}-${width}.png`) });
      await dialog.getByRole("button", { name: confirm, exact: true }).click();
      await panel.getByRole("status").filter({ hasText: zh ? "已同时移出" : "Also removed" }).waitFor();
      for (const [status, verified, blacklist, pattern] of [
        ["unknown", false, "retained", zh ? /回执未知.*黑名单未移除/ : /receipt unknown.*Blacklist retained/],
        ["accepted", true, "changed", zh ? /名单或权限已变化/ : /List or authority changed/],
        ["accepted", true, "failed", zh ? /黑名单移除失败/ : /Blacklist removal failed/],
      ]) {
        await page.evaluate(({ status, verified, blacklist_status }) => {
          window.mutesFixture.result = { status, verified, blacklist_status };
        }, { status, verified, blacklist_status: blacklist });
        await unmute.click();
        await dialog.getByRole("button", { name: confirm, exact: true }).click();
        await panel.getByRole("status").filter({ hasText: pattern }).waitFor();
      }
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      assert.deepEqual(errors, []);
      await page.close();
    }
  }
  console.log(JSON.stringify({ viewports: [320, 390, 1440], locales: ["zh-CN", "en-US"], output, passed: true }));
} finally {
  if (browser) await browser.close();
  await server.close();
}
