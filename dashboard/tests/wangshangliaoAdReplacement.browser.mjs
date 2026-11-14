// Isolated fake APIs only; no robot messages or production requests.
import assert from "node:assert/strict";
import { mkdir, mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const output = process.env.WSL_AD_SCREENSHOTS || await mkdtemp(join(tmpdir(), "wsl-ad-replacement-"));
await mkdir(output, { recursive: true });
const server = await createServer({
  configFile: false, root: fileURLToPath(new URL("..", import.meta.url)),
  cacheDir: join(output, "vite-cache"), plugins: [vue()],
  optimizeDeps: { entries: ["tests/fixtures/wangshangliao-ad-replacement.html"] },
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
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/*", route => {
      const url = new URL(route.request().url());
      return url.origin === origin && !url.pathname.startsWith("/api/") ? route.continue() : route.abort();
    });
    await page.goto(`${origin}/tests/fixtures/wangshangliao-ad-replacement.html`);
    const toggle = page.getByRole("checkbox", { name: "新广告发送成功后撤回上一组广告", exact: true });
    await page.waitForFunction(() => !document.querySelector(".replacement-toggle input")?.disabled);
    assert.equal(await toggle.isChecked(), false);
    await toggle.check();
    await page.getByRole("button", { name: "预览修改", exact: true }).click();
    const confirmation = page.getByRole("alertdialog", { name: "确认业务规则", exact: true });
    await confirmation.getByText("新广告撤回上一组：开启", { exact: true }).waitFor();
    assert.equal(await toggle.isDisabled(), true);
    await confirmation.getByRole("button", { name: "确认保存", exact: true }).click();
    await page.waitForFunction(() => !document.querySelector(".replacement-toggle input")?.disabled);
    assert.equal(await toggle.isChecked(), true);
    const previewCall = await page.evaluate(() => window.adFixture.calls.find(call => call.operation === "preview"));
    assert.equal(previewCall.config.replace_previous_ad, true);
    assert.equal(previewCall.group_id, "5");
    await page.evaluate(() => { window.adFixture.state = { state: "unknown" }; });
    await page.getByRole("button", { name: "刷新业务规则", exact: true }).click();
    await page.getByText("新广告已发送，上一组广告撤回未全部确认，请核对群内消息；不会自动重试。", { exact: true }).waitFor();
    await page.screenshot({ path: join(output, `ad-replacement-${width}.png`), fullPage: true });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    const clipping = await page.locator("button,input,select,textarea").evaluateAll(elements =>
      elements.filter(el => el.scrollWidth > el.clientWidth + 1).map(el => el.outerHTML));
    assert.deepEqual(clipping, []);
    await page.evaluate(() => {
      window.adFixture.state = null;
      window.adFixture.switchGroup("6");
    });
    await page.waitForFunction(() => window.adFixture.calls.at(-1).group_id === "6"
      && !document.querySelector(".replacement-toggle input")?.checked);
    assert.deepEqual(errors, []);
    console.log(`PASS ${width}px: opt-in, preview/confirm, failure status, group isolation, layout`);
    await page.close();
  }
  console.log(`Screenshots: ${output}`);
} finally {
  await browser?.close();
  await server.close();
}
