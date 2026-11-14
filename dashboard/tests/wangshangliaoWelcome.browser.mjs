// Local fixture only: no production configuration or native messages.
import assert from "node:assert/strict";
import { mkdir, mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const output = process.env.WSL_WELCOME_SCREENSHOTS || await mkdtemp(join(tmpdir(), "wsl-welcome-"));
await mkdir(output, { recursive: true });
const server = await createServer({
  configFile: false, root: fileURLToPath(new URL("..", import.meta.url)),
  cacheDir: join(output, "vite-cache"), plugins: [vue()],
  optimizeDeps: { entries: ["tests/fixtures/wangshangliao-welcome.html"] },
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
    const page = await browser.newPage({ viewport: { width, height: 900 } });
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/*", route => {
      const url = new URL(route.request().url());
      return url.origin === origin && !url.pathname.startsWith("/api/") ? route.continue() : route.abort();
    });
    await page.goto(`${origin}/tests/fixtures/wangshangliao-welcome.html`);
    const panel = page.getByRole("region", { name: "入群欢迎", exact: true });
    await page.waitForFunction(() => window.welcomeFixture?.calls.length >= 1, { timeout: 10000 })
      .catch(async error => { throw new Error(JSON.stringify({ error: error.message, errors,
        calls: await page.evaluate(() => window.welcomeFixture?.calls) })); });
    assert.equal(await panel.getByRole("checkbox").isDisabled(), true);
    await page.evaluate(() => { window.welcomeFixture.state.value.disabled = false; });
    await panel.getByRole("checkbox").check();
    await panel.getByRole("textbox", { name: "欢迎语", exact: true }).fill("欢迎加入，祝大家交流愉快！");
    await panel.getByRole("button", { name: "预览修改", exact: true }).click();
    const dialog = panel.getByRole("alertdialog", { name: "确认入群欢迎" });
    await dialog.waitFor();
    assert.equal(await panel.getByRole("textbox").isDisabled(), true);
    assert.match(await dialog.textContent(), /@新成员 欢迎加入/);
    const calls = await page.evaluate(() => window.welcomeFixture.calls);
    assert.deepEqual(calls.map(c => c.payload.operation), ["read", "preview"]);
    await panel.screenshot({ path: join(output, `welcome-${width}.png`) });
    await dialog.getByRole("button", { name: "确认保存", exact: true }).click();
    await panel.getByText("欢迎语已保存", { exact: true }).waitFor();
    assert.deepEqual(await page.evaluate(() => window.welcomeFixture.calls.map(c => c.payload.operation)),
      ["read", "preview", "commit", "read"]);
    await panel.getByRole("button", { name: "预览修改", exact: true }).click();
    await dialog.waitFor();
    await page.evaluate(() => { window.welcomeFixture.state.value.group = "6"; });
    await dialog.waitFor({ state: "hidden" });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    assert.deepEqual(await panel.locator("button,textarea").evaluateAll(elements =>
      elements.filter(el => el.scrollWidth > el.clientWidth + 1).map(el => el.outerHTML)), []);
    assert.deepEqual(errors, []);
    console.log(`PASS ${width}px: load while parent busy, preview/confirm, scope reset, no clipping`);
    await page.close();
  }
  console.log(`Screenshots: ${output}`);
} finally {
  await browser?.close();
  await server.close();
}
