// Uses only local fixtures; production APIs and external network requests are blocked.
import assert from "node:assert/strict";
import { mkdtemp, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";
import vue from "@vitejs/plugin-vue";

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const output = await mkdtemp(join(tmpdir(), "wsl-group-only-browser-"));
const built = process.env.WSL_BUILT_PAGE ? await readFile(process.env.WSL_BUILT_PAGE, "utf8") : null;
const server = await createServer({
  configFile: false, root: fileURLToPath(new URL("..", import.meta.url)),
  cacheDir: join(output, "vite-cache"), plugins: [vue()],
  optimizeDeps: { entries: ["tests/fixtures/wangshangliao-management.html"] },
  server: { host: "127.0.0.1", port: 0 }, clearScreen: false,
});
let browser;
let checkedLayouts = 0;
async function inspectLayout(page) {
  const issues = await page.evaluate(() => {
    const controls = [...document.querySelectorAll("button,input,select,textarea")].filter(el => {
      const box = el.getBoundingClientRect(), style = getComputedStyle(el);
      return box.width > 2 && box.height > 2 && style.visibility !== "hidden" && style.opacity !== "0";
    });
    const overlaps = [];
    for (let i = 0; i < controls.length; i++) {
      const a = controls[i].getBoundingClientRect();
      for (let j = i + 1; j < controls.length; j++) {
        const b = controls[j].getBoundingClientRect();
        if (Math.min(a.right, b.right) - Math.max(a.left, b.left) > 1
            && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 1) {
          overlaps.push([controls[i].outerHTML.slice(0, 160), controls[j].outerHTML.slice(0, 160)]);
        }
      }
    }
    return {
      overflow: document.documentElement.scrollWidth > innerWidth,
      overlaps,
      clippedButtons: controls.filter(el => el.tagName === "BUTTON"
        && (el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1))
        .map(el => el.outerHTML.slice(0, 160)),
    };
  });
  assert.deepEqual(issues, { overflow: false, overlaps: [], clippedButtons: [] });
  checkedLayouts++;
}
try {
  await server.listen();
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch({
    headless: true, args: ["--no-sandbox"],
    ...(process.env.PLAYWRIGHT_CHROMIUM ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM } : {}),
  });
  for (const width of [320, 390, 1440]) {
    for (const locale of ["zh-CN", "en-US"]) {
      const page = await browser.newPage({ viewport: { width, height: 1000 } });
      const errors = [];
      page.on("pageerror", e => errors.push(e.message));
      await page.route("**/*", route => {
        const url = new URL(route.request().url());
        return url.origin === origin && !url.pathname.startsWith("/api/")
          ? route.continue() : route.abort();
      });
      await page.goto(`${origin}/tests/fixtures/wangshangliao-management.html?locale=${locale}`);
      await page.locator("#section-ai").waitFor();
      await page.evaluate(() => {
        window.groupOnlyCalls = [];
        for (const key of ["apiGet", "apiPost"]) {
          const bridge = window.AstrBotPluginPage, original = bridge[key];
          bridge[key] = (endpoint, payload) => {
            window.groupOnlyCalls.push({ endpoint, payload });
            return original(endpoint, payload);
          };
        }
      });
      if (built) {
        const assets = await page.evaluate(html => {
          const doc = new DOMParser().parseFromString(html, "text/html");
          return {
            styles: [...doc.querySelectorAll("style")].map(s => s.textContent),
            scripts: [...doc.querySelectorAll("script:not([data-astrbot-bridge])")]
              .filter(s => !s.src).map(s => s.textContent),
          };
        }, built);
        assert.equal(assets.scripts.length, 1);
        await page.evaluate(() => {
          document.querySelector("#app").__vue_app__.unmount();
          document.querySelectorAll("style,link[rel=stylesheet]").forEach(s => s.remove());
        });
        for (const content of assets.styles) await page.addStyleTag({ content });
        await page.addScriptTag({ content: assets.scripts[0] });
        await page.locator("#section-ai").waitFor();
      }
      for (const section of ["groups", "ai"]) {
        await page.locator(`#section-${section}`).click();
        for (const retired of ["friend-approval", "consultation", "admin-guidance"]) {
          assert.equal(await page.locator(`#tab-${retired}`).count(), 0);
        }
      }
      await page.locator("#tab-group-ai").click();
      await page.getByLabel("群聊", { exact: true }).selectOption("1/5");
      await page.getByLabel("普通聊天自主接话", { exact: true }).waitFor();
      assert.equal(await page.getByLabel("普通聊天自主接话", { exact: true }).isChecked(), false);
      await page.getByLabel("全天可接话", { exact: true }).check();
      assert.equal(await page.getByLabel("开始时间（北京时间，小时）", { exact: true }).count(), 0);
      await page.getByLabel("全天可接话", { exact: true }).uncheck();
      assert.equal(await page.getByLabel("群聊默认 · 聊天上下文", { exact: true }).inputValue(), "1");
      await page.screenshot({ path: join(output, `ai-${locale}-${width}.png`), fullPage: true });
      await inspectLayout(page);

      await page.locator("#section-groups").click();
      await page.locator("#tab-open").click();
      await page.locator(".member-grants .member-list").getByText("管理甲", { exact: false }).waitFor();
      assert.equal(await page.locator(".member-grants .member-list").getByText("成员乙", { exact: false }).count(), 0);
      await page.locator("#section-finance").click();
      await page.locator("#tab-activities").click();
      assert.equal(await page.locator("#panel-activities").count(), 1);
      assert.equal(await page.locator("#tab-finance").count(), 0);
      assert.equal(await page.locator("#tab-cashback").count(), 1);

      await page.locator("#section-help").click();
      await page.locator("#tab-commands").click();
      const help = page.getByRole("region", { name: "命令能力目录" });
      assert.equal(await help.getByRole("tab").count(), 2);
      assert.doesNotMatch(await help.innerText(), /私信|私聊|客服|Private commands|guidance/i);
      await page.locator("#tab-examples").click();
      const examples = page.locator("#panel-examples");
      assert.equal(await examples.getByRole("tab").count(), 2);
      await examples.getByRole("tab").last().click();
      assert.doesNotMatch(await examples.innerText(), /私信|私聊|Admin DM|\/chat|\/exit/i);
      await page.locator("#tab-matrix").click();
      assert.equal(await page.locator(".permission-matrix thead th").count(), 5);
      assert.doesNotMatch(await page.locator("#panel-matrix").innerText(), /私信|私聊|好友|private|friend/i);
      await inspectLayout(page);
      await page.screenshot({ path: join(output, `matrix-${locale}-${width}.png`), fullPage: true });
      const calls = await page.evaluate(() => window.groupOnlyCalls);
      assert(!calls.some(c => c.endpoint === "friend-approval"
        || /support|guidance|provision_profiles|finance_(status|members|grant)/.test(c.payload?.action || "")));
      assert.deepEqual(errors, []);
      await page.close();
    }
    const page = await browser.newPage({ viewport: { width, height: 1000 } });
    const errors = [];
    page.on("pageerror", e => errors.push(e.message));
    await page.route("**/*", route => {
      const url = new URL(route.request().url());
      return url.origin === origin && !url.pathname.startsWith("/api/")
        ? route.continue() : route.abort();
    });
    await page.goto(`${origin}/tests/fixtures/wangshangliao-management.html?legacy`);
    await page.locator("#panel-settings").waitFor();
    if (built) {
      const assets = await page.evaluate(html => {
        const doc = new DOMParser().parseFromString(html, "text/html");
        return {
          styles: [...doc.querySelectorAll("style")].map(s => s.textContent),
          scripts: [...doc.querySelectorAll("script:not([data-astrbot-bridge])")]
            .filter(s => !s.src).map(s => s.textContent),
        };
      }, built);
      assert.equal(assets.scripts.length, 1);
      await page.evaluate(() => {
        document.querySelector("#app").__vue_app__.unmount();
        document.querySelectorAll("style,link[rel=stylesheet]").forEach(s => s.remove());
      });
      for (const content of assets.styles) await page.addStyleTag({ content });
      await page.addScriptTag({ content: assets.scripts[0] });
      await page.locator("#panel-settings").waitFor();
    }
    assert.equal(await page.locator("#private-reply").count(), 0);
    assert.doesNotMatch(await page.locator("#panel-settings").innerText(), /私聊|私信|private/i);
    await page.locator("#tab-business").click();
    await page.locator("#panel-business").getByText("测试窗口", { exact: true }).waitFor();
    assert.doesNotMatch(await page.locator("#panel-business").innerText(), /私聊|私信|private\/42|退役目标/i);
    assert.equal(await page.locator('input[value="group_commands"]').count(), 1);
    assert.equal(await page.locator('input[value="group_ai"]').count(), 1);
    assert.equal(await page.locator('input[value="group_rules"]').count(), 1);
    await inspectLayout(page);
    await page.screenshot({ path: join(output, `legacy-${width}.png`), fullPage: true });
    assert.deepEqual(errors, []);
    await page.close();
  }
  console.log(JSON.stringify({ built: Boolean(built), viewports: [320, 390, 1440],
    locales: ["zh-CN", "en-US"], checkedLayouts, output, passed: true }));
} finally {
  if (browser) await browser.close();
  await server.close();
}
