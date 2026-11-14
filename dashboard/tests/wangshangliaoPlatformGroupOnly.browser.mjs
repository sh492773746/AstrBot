// Build and exercise the real platform controls with synthetic, intercepted APIs.
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { build, preview } from "vite";
import vue from "@vitejs/plugin-vue";
import vuetify from "vite-plugin-vuetify";

const root = fileURLToPath(new URL("..", import.meta.url));
const repo = fileURLToPath(new URL("../..", import.meta.url));
const output = await mkdtemp(join(tmpdir(), "wsl-platform-group-only-"));
const candidate = process.env.WSL_PLATFORM_BUILT_DIR || join(output, "candidate");
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
if (!process.env.WSL_PLATFORM_BUILT_DIR) await build({
  configFile: false, root, publicDir: false, cacheDir: join(output, "vite-cache"),
  plugins: [vue(), vuetify({ autoImport: true })],
  resolve: { alias: [
    { find: "@", replacement: join(root, "src") },
    { find: /^shiki$/, replacement: join(root, "src/utils/shikiLimitedBundle.js") },
    { find: /^stream-monaco$/, replacement: join(root, "src/utils/streamMonacoDisabled.js") },
  ] },
  build: {
    outDir: candidate, emptyOutDir: true, target: "esnext",
    rollupOptions: { input: join(root, "tests/fixtures/wangshangliao.html") },
  },
});
const server = await preview({
  configFile: false, root, build: { outDir: candidate },
  preview: { host: "127.0.0.1", port: 0 },
});
let browser;
let checkedLayouts = 0;
const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
async function inspectLayout(page) {
  const issues = await page.evaluate(() => {
    const controls = [...document.querySelectorAll("button,input,select,textarea")].filter(el => {
      const box = el.getBoundingClientRect(), style = getComputedStyle(el);
      return box.width > 2 && box.height > 2 && style.visibility !== "hidden" &&
        style.opacity !== "0" && el.type !== "hidden";
    });
    const overlaps = [];
    for (let i = 0; i < controls.length; i++) {
      const a = controls[i].getBoundingClientRect();
      for (let j = i + 1; j < controls.length; j++) {
        const b = controls[j].getBoundingClientRect();
        if (Math.min(a.right, b.right) - Math.max(a.left, b.left) > 1 &&
            Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 1) {
          overlaps.push([controls[i].outerHTML.slice(0, 160), controls[j].outerHTML.slice(0, 160)]);
        }
      }
    }
    return { overflow: document.documentElement.scrollWidth > innerWidth, overlaps };
  });
  assert.deepEqual(issues, { overflow: false, overlaps: [] });
  checkedLayouts++;
}
try {
  browser = await chromium.launch({
    headless: true, args: ["--no-sandbox"],
    ...(process.env.PLAYWRIGHT_CHROMIUM ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM } : {}),
  });
  for (const width of [320, 390, 1440]) {
    for (const locale of ["zh-CN", "en-US"]) {
      const page = await browser.newPage({ viewport: { width, height: 1000 } });
      const errors = [], apiCalls = [];
      page.on("pageerror", e => errors.push(e.message));
      await page.addInitScript(locale => localStorage.setItem("astrbot-locale", locale), locale);
      await page.route("**/*", async route => {
        const url = new URL(route.request().url());
        if (url.origin !== origin) return route.abort();
        if (!url.pathname.startsWith("/api/")) return route.continue();
        apiCalls.push(url.pathname);
        if (url.pathname.includes("/registration")) {
          const input = route.request().postDataJSON();
          assert.equal(input.action, "groups");
          return route.fulfill({ json: { status: "ok", data: {
            groups: [{ group_id: "34", group_name: "Fixture group" }], complete: true,
          } } });
        }
        return route.fulfill({ json: { status: "ok", data: {} } });
      });
      await page.goto(`${origin}/tests/fixtures/wangshangliao.html?permissions=1`);
      await page.getByText(locale === "zh-CN" ? "机器人群管授权" : "Bot moderation capabilities", { exact: true }).waitFor();
      assert.doesNotMatch(await page.locator("body").innerText(), /私信|私聊|好友|private|friend/i);
      assert.equal(apiCalls.filter(path => path.includes("sessions")).length, 0);
      const target = page.locator(".v-select").first();
      assert.doesNotMatch(await target.innerText(), /private\/42/);
      assert.match(await target.innerText(), /Fixture group/);
      const before = await page.evaluate(() => window.getWslFixtureConfig());
      const recall = page.getByRole("checkbox", { name: locale === "zh-CN" ? "撤回违规消息" : "Recall violation", exact: true });
      await recall.uncheck();
      assert.deepEqual((await page.evaluate(() => window.getWslFixtureConfig())).moderation.permissions["34"], ["mute", "unmute"]);
      await recall.check();
      const after = await page.evaluate(() => window.getWslFixtureConfig());
      assert.deepEqual([...after.moderation.permissions["34"]].sort(), [...before.moderation.permissions["34"]].sort());
      await inspectLayout(page);
      await page.screenshot({ path: join(output, `permissions-${locale}-${width}.png`), fullPage: true });

      await page.goto(`${origin}/tests/fixtures/wangshangliao.html?existing=1&retired=1`);
      await page.getByRole("button", { name: locale === "zh-CN" ? "重新登录" : "Log in again", exact: true }).waitFor();
      assert.doesNotMatch(await page.locator("form").innerText(), /私信|私聊|好友|private (texts|messages|replies)|friend/i);
      await inspectLayout(page);
      await page.screenshot({ path: join(output, `login-${locale}-${width}.png`), fullPage: true });
      assert.equal(apiCalls.filter(path => path.includes("sessions")).length, 0);
      assert.deepEqual(errors, []);
      await page.close();
    }
  }
  if (process.env.WSL_LOGIN_PYTEST === "1") {
    const result = await promisify(execFile)(join(repo, ".venv/bin/python"), [
      "-m", "pytest", "-q", "tests/test_wangshangliao_login_ui.py",
    ], {
      cwd: repo, maxBuffer: 1024 * 1024,
      env: { ...process.env, WSL_LOGIN_TEST_URL: origin,
        PLAYWRIGHT_CHROMIUM_EXECUTABLE: process.env.PLAYWRIGHT_CHROMIUM || "" },
    });
    console.log(result.stdout);
  }
  console.log(JSON.stringify({ passed: true, checkedLayouts, viewports: [320, 390, 1440],
    locales: ["zh-CN", "en-US"], candidate, output }));
} finally {
  if (browser) await browser.close();
  await new Promise((resolve, reject) => server.httpServer.close(error => error ? reject(error) : resolve()));
}
