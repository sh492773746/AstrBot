// Read-only local fixture; no production traffic or live member mutations.
import assert from "node:assert/strict";
import { mkdir, mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const output = process.env.ACTIVITY_SCREENSHOTS || await mkdtemp(join(tmpdir(), "wsl-activity-"));
await mkdir(output, { recursive: true });
const server = await createServer({
  configFile: false,
  root: fileURLToPath(new URL("..", import.meta.url)),
  cacheDir: join(output, "vite-cache"), plugins: [vue()],
  optimizeDeps: { entries: ["tests/fixtures/wangshangliao-speech-activity.html"] },
  server: { host: "127.0.0.1", port: 0, cors: true }, clearScreen: false,
});
let browser;
try {
  await server.listen();
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch({
    headless: true, args: ["--no-sandbox"],
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined,
  });
  for (const width of [320, 390, 1440]) {
    const page = await browser.newPage({ viewport: { width, height: 1100 } });
    await page.context().grantPermissions(["clipboard-read"], { origin });
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/*", route => new URL(route.request().url()).origin === origin
      ? route.continue() : route.abort());
    await page.goto(`${origin}/tests/fixtures/wangshangliao-speech-activity.html`);
    await page.getByRole("heading", { name: "每日发言活跃", exact: true }).waitFor();
    await page.getByText("2026-10-26 · 北京时间", { exact: true }).waitFor();
    await page.getByLabel("昨日", { exact: true }).check();
    await page.getByText("2026-10-25 · 北京时间", { exact: true }).waitFor();
    await page.getByRole("searchbox").fill("日常");
    await page.getByRole("button", { name: "搜索", exact: true }).click();
    await page.getByRole("list", { name: "统计成员搜索结果" }).getByRole("button").first().click();
    await page.getByText("2026-10-25 · 101 条有效消息", { exact: true }).waitFor();
    await page.getByRole("button", { name: "复制专门汇报", exact: true }).click();
    await page.getByText("已复制专门发言汇报", { exact: true }).waitFor();
    const reportCopied = await page.evaluate(() => navigator.clipboard.readText());
    assert.match(reportCopied, /专门发言汇报 · 2026-10-25/);
    assert.match(reportCopied, /汇报成员1（UID 10）：合计 3 句/);
    assert.match(reportCopied, /中性测试群C（7）：1 句/);
    assert.match(reportCopied, /5位成员合计：165 句/);
    await page.getByRole("button", { name: "复制排名", exact: true }).click();
    await page.getByText("已复制2位成员的排名", { exact: true }).waitFor();
    const copied = await page.evaluate(() => navigator.clipboard.readText());
    assert.equal(copied, "中性测试群A（5）\n2026-10-25 发言活跃排行榜（北京时间）\n1. 日常交流成员甲（UID 10） 101 条\n2. 日常交流成员乙（UID 11） 68 条");
    await page.screenshot({ path: join(output, `speech-activity-${width}.png`), fullPage: true });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
    assert.deepEqual(errors, []);
    await page.getByLabel("当前群", { exact: true }).selectOption("6");
    assert.equal(await page.getByLabel("所选成员统计", { exact: true }).count(), 0);
    assert.equal(await page.getByRole("searchbox").inputValue(), "");
    assert((await page.evaluate(() => window.activityCalls)).every(params => params.bot_id === "fixture"));
    // Match the production opaque-origin sandbox without loosening its policy.
    await page.evaluate(origin => {
      const frame = document.createElement("iframe");
      frame.setAttribute("sandbox", "allow-scripts allow-forms allow-downloads");
      frame.src = `${origin}/tests/fixtures/wangshangliao-speech-activity.html`;
      frame.style.cssText = "width:100%;height:1050px;border:0";
      document.body.replaceChildren(frame);
    }, origin);
    const frame = page.frameLocator("iframe");
    await frame.getByRole("button", { name: "复制排名", exact: true }).click();
    await frame.getByText("已复制2位成员的排名", { exact: true }).waitFor();
    const sandboxCopied = await page.evaluate(() => navigator.clipboard.readText());
    assert.match(sandboxCopied, /2026-10-26 发言活跃排行榜/);
    assert.match(sandboxCopied, /2\. 日常交流成员乙（UID 11） 68 条/);
    const child = page.frames().find(value => value !== page.mainFrame());
    await child.evaluate(() => {
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: { writeText: async () => { throw new Error("Denied by browser"); } },
      });
      document.execCommand = () => false;
    });
    await frame.getByRole("button", { name: "复制排名", exact: true }).click();
    await frame.getByRole("alert").getByText("浏览器未允许复制，请再次点击复制排名。").waitFor();
    assert.equal(await frame.getByText("已复制2位成员的排名", { exact: true }).count(), 0);
    await page.close();
  }
  console.log(JSON.stringify({ passed: true, widths: [320, 390, 1440], output }));
} finally {
  if (browser) await browser.close();
  await server.close();
}
