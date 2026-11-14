// Isolated local statistics only; no production API or messages are sent.
import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const output = process.env.WSL_REPORT_SCREENSHOTS || await mkdtemp(join(tmpdir(), "wsl-hourly-report-"));
await mkdir(output, { recursive: true });
const server = await createServer({
  configFile: false, root: fileURLToPath(new URL("..", import.meta.url)),
  cacheDir: join(output, "vite-cache"), plugins: [vue()],
  optimizeDeps: { entries: ["tests/fixtures/wangshangliao-speech-report.html"] },
  server: { host: "127.0.0.1", port: 0, cors: true }, clearScreen: false,
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
    const context = await browser.newContext({ viewport: { width, height: 1000 }, permissions: ["clipboard-read", "clipboard-write"] });
    const page = await context.newPage(), errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/*", route => {
      const url = new URL(route.request().url());
      return url.origin === origin && !url.pathname.startsWith("/api/") ? route.continue() : route.abort();
    });
    await page.goto(`${origin}/tests/fixtures/wangshangliao-speech-report.html`);
    const panel = page.getByRole("region", { name: "专门发言汇报", exact: true });
    const copy = panel.getByRole("button", { name: "复制专门汇报表格", exact: true });
    await panel.getByRole("button", { name: "每小时明细", exact: true }).click();
    const table = panel.getByRole("table");
    assert.equal(await table.getByRole("row").count(), 26);
    assert.match(await table.getByRole("row").nth(1).textContent(), /00:00-01:001111116/);
    assert.match(await table.getByRole("row").nth(24).textContent(), /23:00-24:0022222212/);
    await panel.screenshot({ path: join(output, `hourly-report-${width}.png`) });
    await copy.click();
    await panel.getByText("已复制表格", { exact: true }).waitFor();
    let text = await page.evaluate(() => navigator.clipboard.readText());
    assert.match(text, /每小时明细/);
    assert.match(text, /全日合计\t3\t3\t3\t3\t3\t3\t18/);
    await panel.getByLabel("专门汇报统计范围").selectOption("6");
    await page.getByLabel("统计日期", { exact: true }).selectOption("1");
    await copy.click();
    await panel.getByText("已复制表格", { exact: true }).waitFor();
    text = await page.evaluate(() => navigator.clipboard.readText());
    assert.match(text, /2026-11-05/);
    assert.match(text, /测试群乙（6）/);
    assert.match(text, /全日合计\t2\t2\t2\t2\t2\t2\t12/);
    const [download] = await Promise.all([
      page.waitForEvent("download"),
      panel.getByRole("button", { name: "下载专门汇报PNG图片", exact: true }).click(),
    ]);
    assert.equal(download.suggestedFilename(), "专门发言汇报-2026-11-05-6-hourly.png");
    const pngPath = join(output, `hourly-report-export-${width}.png`);
    await download.saveAs(pngPath);
    const png = await readFile(pngPath);
    assert.equal(png.subarray(0, 8).toString("hex"), "89504e470d0a1a0a");
    assert(png.length < 300_000);
    const imageCheck = await page.evaluate(async encoded => {
      const url = `data:image/png;base64,${encoded}`;
      const image = new Image();
      await new Promise((resolve, reject) => { image.onload = resolve; image.onerror = reject; image.src = url; });
      const canvas = document.createElement("canvas");
      canvas.width = image.width; canvas.height = image.height;
      const ctx = canvas.getContext("2d"); ctx.drawImage(image, 0, 0);
      const pixels = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
      let dark = 0;
      for (let i = 0; i < pixels.length; i += 4) if (pixels[i] < 150 && pixels[i + 1] < 150 && pixels[i + 2] < 150) dark++;
      return { dark, width: canvas.width, height: canvas.height };
    }, png.toString("base64"));
    assert(imageCheck.dark > 1000);
    assert.equal(imageCheck.width, png.readUInt32BE(16));
    assert.equal(imageCheck.height, png.readUInt32BE(20));
    assert(imageCheck.width * imageCheck.height < 4_000_000);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    const clipping = await panel.locator("button,select").evaluateAll(elements =>
      elements.filter(el => el.scrollWidth > el.clientWidth + 1).map(el => el.outerHTML));
    assert.deepEqual(clipping, []);
    // The SVG text must fit its bounds even for maximum-length names.
    const extremes = await page.evaluate(async () => {
      const { reportTable, reportSvg } = await import("/plugin-pages/wangshangliao/speechReportExport.ts");
      const data = structuredClone(window.speechFixture.data);
      data.days[1].members[0].name = "超长昵称".repeat(32);
      data.groups[0].name = "超长群名".repeat(32);
      const svg = reportSvg(reportTable(data, 1, "hourly", "5"));
      const holder = document.createElement("div");
      holder.innerHTML = svg; document.body.appendChild(holder);
      const root = holder.querySelector("svg"), bounds = root.viewBox.baseVal;
      const clipped = [...root.querySelectorAll("text")].filter(el => {
        const box = el.getBBox(); return box.x < 0 || box.x + box.width > bounds.width || box.y + box.height > bounds.height;
      }).length;
      holder.remove(); return clipped;
    });
    assert.equal(extremes, 0);
    assert.deepEqual(errors, []);
    console.log(`PASS ${width}px: 24 slots, selected date/group, TSV, PNG pixels (${png.length} bytes) and layout bounds`);
    await context.close();
  }
  const page = await browser.newPage();
  await page.goto(`${origin}/tests/fixtures/wangshangliao-speech-report.html`);
  await page.evaluate(url => {
    const frame = document.createElement("iframe");
    frame.sandbox = "allow-scripts allow-forms allow-downloads";
    frame.src = url; document.body.replaceChildren(frame);
  }, `${origin}/tests/fixtures/wangshangliao-speech-report.html`);
  const frame = page.frameLocator("iframe");
  const panel = frame.getByRole("region", { name: "专门发言汇报", exact: true });
  await panel.getByRole("button", { name: "每小时明细", exact: true }).click();
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    panel.getByRole("button", { name: "下载专门汇报PNG图片", exact: true }).click(),
  ]);
  await download.saveAs(join(output, "sandbox-hourly.png"));
  await panel.getByText("已生成PNG下载", { exact: true }).waitFor();
  const memory = await panel.evaluate(async element => {
    const win = element.ownerDocument.defaultView;
    const { reportPng, reportTable } = await import("/plugin-pages/wangshangliao/speechReportExport.ts");
    const table = reportTable(win.speechFixture.data, 0, "hourly");
    const create = win.URL.createObjectURL, revoke = win.URL.revokeObjectURL;
    let allocated = 0, released = 0;
    const tracked = new Set();
    win.URL.createObjectURL = blob => {
      const url = create.call(win.URL, blob);
      allocated++; tracked.add(url); return url;
    };
    win.URL.revokeObjectURL = url => {
      if (tracked.delete(url)) released++;
      return revoke.call(win.URL, url);
    };
    try {
      const blob = await reportPng(table, new AbortController().signal);
      const controller = new AbortController();
      const cancelled = reportPng(table, controller.signal);
      controller.abort();
      let aborted = false;
      try { await cancelled; } catch (error) { aborted = error.name === "AbortError"; }
      let limitRejected = false, encodingRejected = false;
      try {
        await reportPng({ ...table, columns: Array(26).fill("超宽测试"), rows: [Array(26).fill("0")] }, new AbortController().signal);
      } catch (error) { limitRejected = error.message.includes("pixel limit"); }
      const toBlob = win.HTMLCanvasElement.prototype.toBlob;
      try {
        win.HTMLCanvasElement.prototype.toBlob = callback => callback(null);
        await reportPng(table, new AbortController().signal);
      } catch (error) { encodingRejected = error.message.includes("encoded"); }
      finally { win.HTMLCanvasElement.prototype.toBlob = toBlob; }
      return { allocated, released, aborted, limitRejected, encodingRejected, type: blob.type, size: blob.size };
    } finally {
      win.URL.createObjectURL = create; win.URL.revokeObjectURL = revoke;
    }
  });
  assert.equal(memory.type, "image/png");
  assert(memory.size < 300_000);
  assert.equal(memory.aborted, true);
  assert.equal(memory.limitRejected, true);
  assert.equal(memory.encodingRejected, true);
  assert.equal(memory.allocated, memory.released);
  console.log(`PASS opaque sandbox: PNG download, cancellation and all ${memory.released} temporary URLs released`);
  await page.close();
  console.log(`Screenshots: ${output}`);
} finally {
  await browser?.close();
  await server.close();
}
