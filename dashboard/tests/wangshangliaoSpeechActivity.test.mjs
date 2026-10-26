import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import ts from "typescript";
import { createRenderer, h, nextTick, ref } from "vue";
import { compileScript, compileTemplate, parse } from "vue/compiler-sfc";

const require = createRequire(import.meta.url);
const filename = "../plugin-pages/wangshangliao/SpeechActivityPanel.vue";
const { descriptor } = parse(readFileSync(new URL(filename, import.meta.url), "utf8"));
const script = compileScript(descriptor, { id: "speech-activity" });
const module = { exports: {} };
let clipboardWriter = async () => true;
new Function("require", "module", "exports", ts.transpileModule(script.content, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText)(id => id.endsWith("/utils/clipboard")
  ? { copyToClipboard: (...args) => clipboardWriter(...args) }
  : id.endsWith("FocusedSpeechReport.vue") ? {} : require(id), module, module.exports);
const renderer = createRenderer({
  createComment: () => ({}), insert() {}, remove() {},
  parentNode: () => null, nextSibling: () => null,
});
const flush = async () => { await nextTick(); await new Promise(setImmediate); };
const snapshot = (scope = "5") => ({
  updated_at: 1792944000, catching_up: false, has_more: false,
  days: [0, 1].map(index => ({
    date: `2026-10-${26 - index}`, total: 110, active_members: 2,
    kinds: { text: 100, image: 10, video: 0, forward: 0 },
    top: [{ id: "10", name: `群${scope}成员`, count: 100 }],
    hours: Array(24).fill(0), member: null,
  })),
  members: [{ id: "10", name: `群${scope}成员` }],
  selected_member: null,
});

function mount(t, apiGet) {
  const group = ref("5");
  let instance;
  const app = renderer.createApp({
    render: () => h({ ...module.exports.default, render: () => null }, {
      ref: value => { instance = value; }, botId: "bot", groupId: group.value, groupName: `测试群${group.value}`,
      bridge: { apiGet },
    }),
  });
  app.mount({});
  t.after(() => app.unmount());
  return { group, state: () => instance.$.setupState };
}

test("activity template compiles and reads never mutate group settings", async t => {
  assert.deepEqual(compileTemplate({
    source: descriptor.template.content, filename, id: "speech-activity",
    compilerOptions: { bindingMetadata: script.bindings, expressionPlugins: ["typescript"] },
  }).errors, []);
  const calls = [];
  const panel = mount(t, async (endpoint, params) => {
    calls.push([endpoint, params]); return snapshot(params.group_id);
  });
  await flush();
  assert.equal(panel.state().groupDay.date, "2026-10-26");
  panel.state().day = 1;
  await flush();
  assert.equal(panel.state().groupDay.date, "2026-10-25");
  assert.equal(calls.length, 1);
  assert(calls.every(([endpoint]) => endpoint === "speech-activity"));
});

test("search then explicit selection loads the exact member in current group", async t => {
  const calls = [];
  const panel = mount(t, async (_endpoint, params) => {
    calls.push(params);
    const row = snapshot(params.group_id);
    if (params.member_id) {
      row.selected_member = { id: params.member_id, name: "已选择成员" };
      row.days[0].member = { count: 100, first_at: 1, last_at: 2 };
    }
    return row;
  });
  await flush();
  panel.state().query = "成员";
  await panel.state().search();
  assert.equal(panel.state().choices.length, 1);
  assert.equal(panel.state().selected, null);
  await panel.state().choose(panel.state().choices[0]);
  assert.equal(panel.state().selectedDay.member.count, 100);
  assert.deepEqual(calls.at(-1), { bot_id: "bot", group_id: "5", member_id: "10" });
  panel.group.value = "6";
  await flush();
  assert.equal(panel.state().selected, null);
  assert.equal(panel.state().memberData, null);
  assert.equal(panel.state().data.days[0].top[0].name, "群6成员");
});

test("late member and group responses cannot refill a different group", async t => {
  let release;
  const panel = mount(t, async (_endpoint, params) => {
    if (params.query) return await new Promise(resolve => { release = resolve; });
    return snapshot(params.group_id);
  });
  await flush();
  panel.state().query = "成员";
  const search = panel.state().search();
  panel.group.value = "6";
  await flush();
  release(snapshot("5"));
  await search;
  assert.equal(panel.state().choices.length, 0);
  assert.equal(panel.state().searched, false);
  assert.equal(panel.state().data.days[0].top[0].name, "群6成员");
});

test("a failed refresh clears stale totals instead of presenting them as current", async t => {
  let fail = false;
  const panel = mount(t, async () => {
    if (fail) throw new Error("private internal diagnostic");
    return snapshot();
  });
  await flush();
  fail = true;
  await panel.state().refresh();
  assert.equal(panel.state().data, null);
  assert.equal(panel.state().error, "统计读取失败，请稍后刷新。");
});

test("one-click copy exports only displayed date, order and scoped identities", async t => {
  const copied = [];
  clipboardWriter = async text => { copied.push(text); return true; };
  const panel = mount(t, async () => {
    const row = snapshot();
    row.days[0].top.push({ id: "11", name: "同名\n成员\u202e", count: 8 });
    row.days[1].top = [{ id: "12", name: "昨日成员", count: 200 }];
    return row;
  });
  await flush();
  await panel.state().copyRanking();
  assert.equal(copied[0], "测试群5（5）\n2026-10-26 发言活跃排行榜（北京时间）\n1. 群5成员（UID 10） 100 条\n2. 同名 成员（UID 11） 8 条");
  assert.equal(panel.state().copyNotice, "已复制2位成员的排名");
  panel.state().day = 1;
  assert.equal(panel.state().copyNotice, "");
  await panel.state().copyRanking();
  assert.match(copied[1], /2026-10-25/);
  assert.match(copied[1], /昨日成员（UID 12） 200 条/);
  assert.doesNotMatch(copied[1], /群5成员/);
});

test("empty, incomplete and loading rankings cannot be copied", async t => {
  let copied = 0;
  clipboardWriter = async () => { copied++; return true; };
  const row = snapshot();
  const panel = mount(t, async () => row);
  await flush();
  panel.state().busy = true;
  await panel.state().copyRanking();
  panel.state().busy = false;
  panel.state().data.catching_up = true;
  await panel.state().copyRanking();
  panel.state().data.catching_up = false;
  panel.state().data.days[0].top = [];
  await panel.state().copyRanking();
  assert.equal(copied, 0);
});

test("copy failure never claims success and the button recovers", async t => {
  clipboardWriter = async () => false;
  const panel = mount(t, async () => snapshot());
  await flush();
  await panel.state().copyRanking();
  assert.equal(panel.state().copyNotice, "");
  assert.match(panel.state().copyError, /浏览器未允许复制/);
  assert.equal(panel.state().copying, false);
  clipboardWriter = async () => { throw new Error("private detail"); };
  await panel.state().copyRanking();
  assert.equal(panel.state().copyError, "复制失败，请再次点击复制排名。");
  clipboardWriter = async () => true;
  await panel.state().copyRanking();
  assert.equal(panel.state().copyError, "");
  assert.equal(panel.state().copyNotice, "已复制1位成员的排名");
});

test("scope or date changes clear pending copy feedback without another write", async t => {
  let release, calls = 0;
  clipboardWriter = async () => { calls++; return new Promise(resolve => { release = resolve; }); };
  const panel = mount(t, async (_endpoint, params) => snapshot(params.group_id));
  await flush();
  const copying = panel.state().copyRanking();
  assert.equal(panel.state().copying, true);
  await panel.state().copyRanking();
  assert.equal(calls, 1);
  panel.group.value = "6";
  await flush();
  release(true);
  await copying;
  assert.equal(panel.state().copyNotice, "");
  assert.equal(panel.state().copyError, "");
  assert.equal(panel.state().copying, false);
});
