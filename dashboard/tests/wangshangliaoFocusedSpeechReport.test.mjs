import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import ts from "typescript";
import { createRenderer, h, nextTick, ref } from "vue";
import { compileScript, compileTemplate, parse } from "vue/compiler-sfc";

const require = createRequire(import.meta.url);
const filename = "../plugin-pages/wangshangliao/FocusedSpeechReport.vue";
const { descriptor } = parse(readFileSync(new URL(filename, import.meta.url), "utf8"));
const script = compileScript(descriptor, { id: "focused-report" });
const module = { exports: {} };
let clipboard = async () => true;
new Function("require", "module", "exports", ts.transpileModule(script.content, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText)(id => id.endsWith("/utils/clipboard")
  ? { copyToClipboard: (...args) => clipboard(...args) } : require(id), module, module.exports);
const renderer = createRenderer({
  createComment: () => ({}), insert() {}, remove() {},
  parentNode: () => null, nextSibling: () => null,
});
const flush = async () => { await nextTick(); await new Promise(setImmediate); };
const snapshot = () => ({
  configured: true, catching_up: false,
  groups: [{ id: "5", name: "群甲" }, { id: "6", name: "群乙" }],
  days: [0, 1].map(index => ({
    date: `2026-10-${26-index}`, total: index ? 7 : 3, totals: index ? [4, 3] : [1, 2],
    members: [{ id: "10", name: "成员甲", counts: index ? [4, 3] : [1, 2], total: index ? 7 : 3 }],
  })),
});
function mount(t, apiGet) {
  const bot = ref("bot"), day = ref(0);
  let instance;
  const app = renderer.createApp({ render: () => h({
    ...module.exports.default, render: () => null,
  }, {
    ref: value => { instance = value; }, botId: bot.value, day: day.value,
    bridge: { apiGet },
  }) });
  app.mount({});
  t.after(() => app.unmount());
  return { bot, day, state: () => instance.$.setupState };
}
test("focused report template compiles and uses a read-only bot-scoped endpoint", async t => {
  assert.deepEqual(compileTemplate({
    source: descriptor.template.content, filename, id: "focused-report",
    compilerOptions: { bindingMetadata: script.bindings, expressionPlugins: ["typescript"] },
  }).errors, []);
  const calls = [];
  const panel = mount(t, async (...args) => { calls.push(args); return snapshot(); });
  await flush();
  assert.deepEqual(calls, [["speech-report", { bot_id: "bot" }]]);
  assert.equal(panel.state().selected.total, 3);
  panel.day.value = 1;
  await flush();
  assert.equal(panel.state().selected.total, 7);
});
test("dedicated report copies date, exact UID and each group's totals", async t => {
  const texts = [];
  clipboard = async text => { texts.push(text); return true; };
  const panel = mount(t, async () => snapshot());
  await flush();
  await panel.state().copy();
  assert.match(texts[0], /2026-10-26/);
  assert.match(texts[0], /成员甲（UID 10）：合计 3 句/);
  assert.match(texts[0], /群乙（6）：2 句/);
  panel.day.value = 1;
  await flush();
  assert.equal(panel.state().notice, "");
  await panel.state().copy();
  assert.match(texts[1], /2026-10-25/);
  assert.match(texts[1], /合计 7 句/);
});
test("incomplete and failed reports never produce confirmed clipboard success", async t => {
  let copied = 0;
  clipboard = async () => { copied++; return false; };
  const panel = mount(t, async () => snapshot());
  await flush();
  panel.state().report.catching_up = true;
  await panel.state().copy();
  assert.equal(copied, 0);
  panel.state().report.catching_up = false;
  await panel.state().copy();
  assert.equal(panel.state().notice, "");
  assert.match(panel.state().error, /浏览器未允许复制/);
});
test("late report and copy results cannot refill another bot or date", async t => {
  let release;
  clipboard = async () => new Promise(resolve => { release = resolve; });
  const panel = mount(t, async () => snapshot());
  await flush();
  const pending = panel.state().copy();
  panel.day.value = 1;
  await flush();
  release(true);
  await pending;
  assert.equal(panel.state().notice, "");
  let resolveRead;
  const other = mount(t, async (_endpoint, params) => params.bot_id === "bot"
    ? new Promise(resolve => { resolveRead = resolve; }) : { configured: false });
  other.bot.value = "other";
  await flush();
  resolveRead(snapshot());
  await flush();
  assert.deepEqual(other.state().report, { configured: false });
});
