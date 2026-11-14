import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import ts from "typescript";
import { createRenderer, h, nextTick, ref } from "vue";
import { compileScript, compileTemplate, parse } from "vue/compiler-sfc";

const require = createRequire(import.meta.url);
const helper = { exports: {} };
new Function("require", "module", "exports", ts.transpileModule(
  readFileSync(new URL("../plugin-pages/wangshangliao/speechReportExport.ts", import.meta.url), "utf8"),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } },
).outputText)(require, helper, helper.exports);
const { reportTable, reportTsv, reportSvg } = helper.exports;
let renderPng = (...args) => helper.exports.reportPng(...args);
const filename = "../plugin-pages/wangshangliao/FocusedSpeechReport.vue";
const { descriptor } = parse(readFileSync(new URL(filename, import.meta.url), "utf8"));
const script = compileScript(descriptor, { id: "focused-speech" });
let writeClipboard = async () => true;
const component = { exports: {} };
new Function("require", "module", "exports", ts.transpileModule(script.content, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText)(id => id.endsWith("/utils/clipboard")
  ? { copyToClipboard: (...args) => writeClipboard(...args) }
  : id === "./speechReportExport" ? { ...helper.exports, reportPng: (...args) => renderPng(...args) }
    : require(id), component, component.exports);
const renderer = createRenderer({
  createComment: () => ({}), insert() {}, remove() {},
  parentNode: () => null, nextSibling: () => null,
});
const flush = async () => { await nextTick(); await new Promise(setImmediate); };
function snapshot() {
  return {
    configured: true, catching_up: false, updated_at: 1793897000,
    groups: [{ id: "5", name: "群甲" }, { id: "6", name: "群乙" }],
    days: ["2026-11-06", "2026-11-05"].map((date, index) => ({
      date, total: 3 + index, totals: [1 + index, 2],
      members: [
        { id: "10", name: "中性成员甲", counts: [1 + index, 0], total: 1 + index,
          hours: Array.from({ length: 24 }, (_, hour) => hour === 0 ? [1 + index, 0] : [0, 0]) },
        { id: "11", name: "中性成员乙", counts: [0, 2], total: 2,
          hours: Array.from({ length: 24 }, (_, hour) => hour === 23 ? [0, 2] : [0, 0]) },
      ],
    })),
  };
}
function mount(t, api = async () => snapshot()) {
  const props = ref({ botId: "bot", day: 0, bridge: { apiGet: api } });
  let instance;
  const child = { ...component.exports.default, render: () => null };
  const app = renderer.createApp({
    render: () => h(child, {
      ...props.value, ref: value => { instance = value; },
    }),
  });
  app.mount({});
  t.after(() => { if (instance) app.unmount(); });
  return { props, state: () => instance.$.setupState, unmount: () => app.unmount() };
}

test("hourly table has 24 Beijing slots, exact group filtering and day totals", () => {
  const report = snapshot();
  const all = reportTable(report, 0, "hourly");
  assert.equal(all.rows.length, 25);
  assert.deepEqual(all.rows[0], ["00:00-01:00", "1", "0", "1"]);
  assert.deepEqual(all.rows[23], ["23:00-24:00", "0", "2", "2"]);
  assert.deepEqual(all.rows[24], ["全日合计", "1", "2", "3"]);
  const single = reportTable(report, 1, "hourly", "5");
  assert.deepEqual(single.rows[24], ["全日合计", "2", "0", "2"]);
  assert.match(single.subtitle, /群甲（5）/);
  assert.equal(reportTable(report, 0, "hourly", "unknown"), null);
});
test("summary and TSV include exact UID, scope, date and freshness", () => {
  const table = reportTable(snapshot(), 1, "summary", "6");
  assert.deepEqual(table.rows[0], ["中性成员甲（10）", "0", "0"]);
  assert.deepEqual(table.rows.at(-1), ["合计", "2", "2"]);
  const tsv = reportTsv(table);
  assert.match(tsv, /2026-11-05/);
  assert.match(tsv, /中性成员甲（10）\t0\t0/);
  assert.match(tsv, /北京时间/);
  assert.match(tsv, /最近核对/);
});
test("spreadsheet cells cannot inject formulas from member or group names", () => {
  const report = snapshot();
  report.days[0].members[0].name = "=SUM(A1)";
  report.groups[0].name = "+SUM(A1)";
  assert.match(reportTsv(reportTable(report, 0, "summary")), /'\+SUM\(A1\)/);
  assert.match(reportTsv(reportTable(report, 0, "summary")), /'=SUM\(A1\)/);
  assert.match(reportTsv(reportTable(report, 0, "hourly")), /'=SUM\(A1\)/);
});
test("missing or malformed hourly data never becomes invented zeroes", () => {
  const report = snapshot();
  delete report.days[0].members[0].hours;
  assert.equal(reportTable(report, 0, "hourly"), null);
  assert(reportTable(report, 0, "summary"));
  report.days[0].members[0].hours = [[1, 0]];
  assert.equal(reportTable(report, 0, "hourly"), null);
});
test("SVG escapes member and group names and has no external assets or executable HTML", () => {
  const report = snapshot();
  report.days[0].members[0].name = '<script>&"\'';
  report.groups[0].name = "群<&>";
  const svg = reportSvg(reportTable(report, 0, "hourly", "5"));
  assert.match(svg, /&lt;script&gt;/);
  assert.match(svg, /&amp;/);
  assert.doesNotMatch(svg.replace('xmlns="http://www.w3.org/2000/svg"', ""), /<script|<foreignObject|<image|https?:|href=/);
  assert.match(svg, /23:00-24:00/);
  assert.match(svg, /xmlns="http:\/\/www.w3.org\/2000\/svg"/);
});
test("component template compiles, uses text bindings and labelled icon exports", () => {
  assert.deepEqual(compileTemplate({
    source: descriptor.template.content, filename, id: "focused-speech",
    compilerOptions: { bindingMetadata: script.bindings, expressionPlugins: ["typescript"] },
  }).errors, []);
  assert(!descriptor.template.content.includes("v-html"));
  assert.match(descriptor.template.content, /每小时明细/);
  assert.match(descriptor.template.content, /下载专门汇报PNG图片/);
  assert.doesNotMatch(descriptor.template.content, /下载.*SVG/);
});
test("copy exports the current view, selected group and selected date", async t => {
  const copied = [];
  writeClipboard = async text => { copied.push(text); return true; };
  const panel = mount(t);
  await flush();
  panel.state().view = "hourly";
  panel.state().group = "6";
  panel.props.value.day = 1;
  await flush();
  await panel.state().exportReport("text");
  assert.equal(copied.length, 1);
  assert.match(copied[0], /2026-11-05/);
  assert.match(copied[0], /群乙（6）/);
  assert.match(copied[0], /23:00-24:00\t0\t2\t2/);
});
test("incomplete, busy or failed reads cannot export stale data", async t => {
  const copied = [];
  writeClipboard = async text => { copied.push(text); return true; };
  const panel = mount(t, async () => ({ ...snapshot(), catching_up: true }));
  await flush();
  await panel.state().exportReport("text");
  assert.deepEqual(copied, []);
  panel.state().report.catching_up = false;
  panel.state().busy = true;
  await panel.state().exportReport("text");
  assert.deepEqual(copied, []);
  panel.state().busy = false;
  panel.props.value.bridge = { apiGet: async () => { throw new Error("private-error"); } };
  await flush();
  await panel.state().refresh();
  assert.equal(panel.state().report, null);
  assert(!panel.state().error.includes("private-error"));
  await panel.state().exportReport("text");
  assert.deepEqual(copied, []);
});
test("bot switch discards delayed report and resets group selection", async t => {
  let release;
  const panel = mount(t, async (_endpoint, params) => params.bot_id === "bot"
    ? await new Promise(resolve => { release = resolve; }) : { configured: false });
  panel.state().group = "5";
  panel.props.value.botId = "other";
  await flush();
  release(snapshot());
  await flush();
  assert.deepEqual(panel.state().report, { configured: false });
  assert.equal(panel.state().group, "");
});
test("scope change discards delayed clipboard notice and browser refusal is visible", async t => {
  let release;
  writeClipboard = async () => await new Promise(resolve => { release = resolve; });
  const panel = mount(t);
  await flush();
  const work = panel.state().exportReport("text");
  panel.state().view = "hourly";
  release(true);
  await work;
  assert.equal(panel.state().notice, "");
  writeClipboard = async () => false;
  await panel.state().exportReport("text");
  assert.match(panel.state().error, /未允许复制/);
});

test("PNG download uses the snapshot filename, releases its URL and blocks duplicate clicks", async t => {
  let release;
  const links = [], urls = [], revoked = [], delayed = [];
  renderPng = async () => await new Promise(resolve => { release = resolve; });
  t.mock.method(globalThis, "setTimeout", callback => { delayed.push(callback); return 1; });
  t.mock.method(URL, "createObjectURL", blob => { urls.push(blob); return "blob:local-report"; });
  t.mock.method(URL, "revokeObjectURL", url => revoked.push(url));
  const oldDocument = Object.getOwnPropertyDescriptor(globalThis, "document");
  t.after(() => {
    if (oldDocument) Object.defineProperty(globalThis, "document", oldDocument);
    else delete globalThis.document;
  });
  Object.defineProperty(globalThis, "document", { value: {
    createElement: () => {
      const link = { click() { links.push({ ...link }); }, remove() {} };
      return link;
    },
    body: { appendChild() {} },
  }, configurable: true });
  const panel = mount(t);
  await flush();
  panel.state().view = "hourly";
  const work = panel.state().exportReport("png");
  await panel.state().exportReport("png");
  assert.equal(panel.state().copying, true);
  release(new Blob(["png"], { type: "image/png" }));
  await work;
  assert.equal(links.length, 1);
  assert.equal(urls[0].type, "image/png");
  assert.equal(links[0].download, "专门发言汇报-2026-11-06-全部群-hourly.png");
  assert.equal(panel.state().notice, "已生成PNG下载");
  assert.equal(panel.state().copying, false);
  delayed.forEach(callback => callback());
  assert.deepEqual(revoked, ["blob:local-report"]);
});

test("scope changes and unmount abort pending images without downloading stale results", async t => {
  const panel = mount(t);
  await flush();
  for (const change of [
    () => { panel.state().group = "6"; },
    () => { panel.props.value.day = 1; },
    () => { panel.state().view = "hourly"; },
    () => { panel.props.value.botId = "other"; },
    () => panel.unmount(),
  ]) {
    let release, signal;
    renderPng = async (_table, value) => {
      signal = value;
      return await new Promise(resolve => { release = resolve; });
    };
    const state = panel.state();
    const work = state.exportReport("png");
    change();
    await flush();
    assert.equal(signal.aborted, true);
    release(new Blob(["stale"], { type: "image/png" }));
    await work;
    assert.equal(state.notice, "");
    assert.equal(state.error, "");
  }
});

test("PNG encoder failures are visible and leave exports usable", async t => {
  renderPng = async () => { throw new Error("private-error"); };
  const panel = mount(t);
  await flush();
  await panel.state().exportReport("png");
  assert.match(panel.state().error, /导出未完成/);
  assert(!panel.state().error.includes("private-error"));
  assert.equal(panel.state().exportDisabled, false);
});
