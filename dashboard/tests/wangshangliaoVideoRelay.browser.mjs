// Local fixtures only: all external requests and production APIs are blocked.
import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const output = await mkdtemp(join(tmpdir(), "wsl-video-relay-"));
const screenshots = process.env.WSL_RELAY_SCREENSHOTS || output;
const built = process.env.WSL_BUILT_PAGE ? await readFile(process.env.WSL_BUILT_PAGE, "utf8") : null;
await mkdir(screenshots, { recursive: true });
const server = await createServer({
  configFile: false, root: fileURLToPath(new URL("..", import.meta.url)),
  cacheDir: join(output, "vite-cache"), plugins: [vue()],
  optimizeDeps: { entries: ["tests/fixtures/wangshangliao-management.html"] },
  server: { host: "127.0.0.1", port: 0 }, clearScreen: false,
});
let browser;
let layouts = 0;
try {
  await server.listen();
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch({
    headless: true, args: ["--no-sandbox"],
    ...(process.env.PLAYWRIGHT_CHROMIUM ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM } : {}),
  });
  for (const locale of ["zh-CN", "en-US"]) for (const width of [320, 390, 1440]) {
    const page = await browser.newPage({ viewport: { width, height: 1000 } });
    const errors = [];
    page.on("pageerror", e => errors.push(e.message));
    await page.route("**/*", route => {
      const url = new URL(route.request().url());
      return url.origin === origin && !url.pathname.startsWith("/api/") ? route.continue() : route.abort();
    });
    await page.goto(`${origin}/tests/fixtures/wangshangliao-management.html?locale=${locale}`);
    await page.locator("#tab-video-relay").waitFor();
    if (built) {
      const assets = await page.evaluate(html => {
        const doc = new DOMParser().parseFromString(html, "text/html");
        return { styles: [...doc.querySelectorAll("style")].map(el => el.textContent),
          scripts: [...doc.querySelectorAll("script:not([data-astrbot-bridge])")].filter(el => !el.src).map(el => el.textContent) };
      }, built);
      assert.equal(assets.scripts.length, 1);
      await page.evaluate(() => {
        document.querySelector("#app").__vue_app__.unmount();
        document.querySelectorAll("style,link[rel=stylesheet]").forEach(el => el.remove());
      });
      for (const content of assets.styles) await page.addStyleTag({ content });
      await page.addScriptTag({ content: assets.scripts[0] });
      await page.locator("#tab-video-relay").waitFor();
    }
    await page.locator("#tab-video-relay").click();
    const panel = page.locator("#panel-video-relay");
    const zh = locale.startsWith("zh");
    assert.equal(await panel.getByText("Telegram", { exact: false }).count(), 0);
    const cms = panel.getByRole("textbox", { name: zh ? "CMS 接口地址" : "CMS API URL", exact: true });
    await cms.fill("https://news.example.com/api?ac=list");
    await panel.getByRole("button", { name: zh ? "预览配置" : "Preview Configuration", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: zh ? "确认视频配置" : "Confirm Relay Configuration", exact: true });
    await dialog.waitFor();
    assert.match(await dialog.innerText(), /news\.example\.com\/api/);
    assert.equal(await page.evaluate(() => document.activeElement?.closest("dialog") !== null), true);
    assert.equal(await dialog.evaluate(el => el.scrollWidth <= el.clientWidth + 1), true);
    await page.screenshot({ path: join(output, `preview-${locale}-${width}.png`), fullPage: true });
    await dialog.getByRole("button", { name: zh ? "确认保存" : "Confirm Save", exact: true }).click();
    await panel.getByText(zh ? "配置已保存，自动发送已停用。" : "Configuration saved; automatic sending disabled.", { exact: true }).waitFor();
    assert.equal(await cms.inputValue(), "https://news.example.com/api");
    await panel.getByRole("button", { name: zh ? "预览一条视频" : "Preview One Video", exact: true }).click();
    const videoDialog = page.getByRole("dialog", { name: zh ? "确认发送一条视频" : "Confirm One Video", exact: true });
    await videoDialog.waitFor();
    assert.match(await videoDialog.innerText(), /160 × 90/);
    assert.equal(await videoDialog.evaluate(el => el.scrollWidth <= el.clientWidth + 1), true);
    await page.screenshot({ path: join(output, `video-confirm-${locale}-${width}.png`), fullPage: true });
    await videoDialog.getByRole("button", { name: zh ? "确认发送" : "Confirm Send", exact: true }).click();
    await panel.getByText(zh ? "平台已受理，待收件确认" : "Accepted; Receipt Check Pending", { exact: true }).first().waitFor();
    await panel.getByRole("checkbox", { name: zh ? "自动发送" : "Automatic Sending", exact: true }).check();
    await panel.getByRole("button", { name: zh ? "预览配置" : "Preview Configuration", exact: true }).click();
    assert.match(await dialog.innerText(), zh ? /确认后按上述间隔自动发送/ : /Confirmation schedules automatic delivery/);
    await dialog.getByRole("button", { name: zh ? "确认保存" : "Confirm Save", exact: true }).click();
    await panel.getByText(zh ? "配置已保存，自动发送已启用。" : "Configuration saved; automatic sending enabled.", { exact: true }).waitFor();
    const layout = await page.evaluate(() => {
      const panel = document.querySelector("#panel-video-relay");
      const controls = [...panel.querySelectorAll("button,input,select")].filter(el => el.getBoundingClientRect().width > 0 && !el.closest("dialog:not([open])"));
      const overlaps = [];
      for (let i = 0; i < controls.length; i++) for (let j = i + 1; j < controls.length; j++) {
        const a = controls[i].getBoundingClientRect(), b = controls[j].getBoundingClientRect();
        if (Math.min(a.right, b.right) - Math.max(a.left, b.left) > 1 && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 1) overlaps.push([i, j]);
      }
      return { overflow: document.documentElement.scrollWidth > innerWidth,
        clipped: controls.filter(el => el.tagName === "BUTTON" && el.scrollWidth > el.clientWidth + 1).length, overlaps };
    });
    assert.deepEqual(layout, { overflow: false, clipped: 0, overlaps: [] });
    layouts++;
    await page.screenshot({ path: join(screenshots, `video-relay-${locale}-${width}.png`), fullPage: true });
    // Destination isolation and the existing unsaved-change guard both remain active.
    await cms.fill("https://unsaved.example.com/api");
    await page.locator(".scope-bar select").last().selectOption("6");
    await page.getByRole("button", { name: zh ? "继续编辑" : "Keep editing", exact: true }).click();
    assert.equal(await cms.inputValue(), "https://unsaved.example.com/api");
    await page.locator(".scope-bar select").last().selectOption("6");
    await page.getByRole("button", { name: zh ? "放弃更改" : "Discard", exact: true }).click();
    await panel.getByRole("textbox", { name: zh ? "CMS 接口地址" : "CMS API URL", exact: true }).waitFor();
    assert.equal(await cms.inputValue(), "");
    await page.locator(".scope-bar select").last().selectOption("5");
    await panel.getByRole("textbox", { name: zh ? "CMS 接口地址" : "CMS API URL", exact: true }).waitFor();
    assert.equal(await cms.inputValue(), "https://news.example.com/api");
    assert.deepEqual(errors, []);
    await page.close();
  }
  for (const locale of ["zh-CN", "en-US"]) for (const width of [320, 390, 1440]) {
    const page = await browser.newPage({ viewport: { width, height: 1000 } });
    await page.route("**/*", route => {
      const url = new URL(route.request().url());
      return url.origin === origin && !url.pathname.startsWith("/api/") ? route.continue() : route.abort();
    });
    await page.goto(`${origin}/tests/fixtures/wangshangliao-management.html?follow&locale=${locale}`);
    await page.locator("#tab-video-relay").click();
    const panel = page.locator("#panel-video-relay");
    const zh = locale.startsWith("zh");
    await panel.getByText(zh ? "同步来源群" : "Follow Source Group", { exact: true }).waitFor();
    assert.equal(await panel.getByRole("button", { name: zh ? "预览一条视频" : "Preview One Video", exact: true }).isDisabled(), true);
    assert.equal(await panel.locator(".relay-categories").count(), 0);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.screenshot({ path: join(screenshots, `video-follow-${locale}-${width}.png`), fullPage: true });
    layouts++;
    await page.close();
  }
  console.log(JSON.stringify({ layouts, screenshots, built_page: Boolean(built), external_network: false, real_collection_or_sends: false }));
} finally {
  await browser?.close();
  await server.close();
}
