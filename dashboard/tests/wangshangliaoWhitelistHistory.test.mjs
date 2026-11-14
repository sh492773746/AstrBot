import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import ts from "typescript";
import { createRenderer, h, nextTick, ref } from "vue";
import { compileScript, compileTemplate, parse } from "vue/compiler-sfc";

const require = createRequire(import.meta.url);
const filename = "../plugin-pages/wangshangliao/WhitelistHistoryPanel.vue";
const { descriptor } = parse(readFileSync(new URL(filename, import.meta.url), "utf8"));
const script = compileScript(descriptor, { id: "whitelist-history" });
const module = { exports: {} };
let clipboardWriter = async () => true;
new Function("require", "module", "exports", ts.transpileModule(script.content, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText)(id => id.endsWith("/utils/clipboard")
  ? { copyToClipboard: (...args) => clipboardWriter(...args) } : require(id), module, module.exports);
const renderer = createRenderer({
  createComment: () => ({}), insert() {}, remove() {},
  parentNode: () => null, nextSibling: () => null,
});
const flush = async () => { await nextTick(); await new Promise(setImmediate); };
const members = Array.from({ length: 7 }, (_, index) => ({ id: String(10 + index), name: `中性成员${index + 1}` }));
function snapshot(params) {
  return {
    version: params.list_version, period: params.period, query: params.query,
    from_date: "2026-10-27", to_date: "2026-10-28", timezone: "Asia/Shanghai",
    updated_at: 1793123876, catching_up: false,
    members: params.member_ids.map(id => ({
      id, name: `中性成员${Number(id) - 9}`, count: 101, matched_count: 101, unavailable: 0,
      messages: Array.from({ length: 100 }, (_, index) => ({
        message: `${id}-${index}`, stamp: 1793120000 + index, text: `中性对话 ${index + 1}`,
      })),
    })),
  };
}
function mount(t, api, overrides = {}) {
  const props = ref({
    api, account: "1", group: "5", tenantVersion: 2, listVersion: 3, members,
    ...overrides,
  });
  let instance;
  const app = renderer.createApp({
    render: () => h({ ...module.exports.default, render: () => null }, {
      ...props.value, ref: value => { instance = value; },
    }),
  });
  app.mount({});
  t.after(() => app.unmount());
  return { props, state: () => instance.$.setupState };
}

test("history template compiles without unsafe HTML or automatic loading", async t => {
  assert.deepEqual(compileTemplate({
    source: descriptor.template.content, filename, id: "whitelist-history",
    compilerOptions: { bindingMetadata: script.bindings, expressionPlugins: ["typescript"] },
  }).errors, []);
  assert(!descriptor.template.content.includes("v-html"));
  const calls = [];
  mount(t, async (...args) => { calls.push(args); });
  await flush();
  assert.deepEqual(calls, []);
});

test("one click copies all members in bounded batches and numbers 1 through 100", async t => {
  const calls = [], copied = [];
  clipboardWriter = async text => { copied.push(text); return true; };
  const panel = mount(t, async (action, params) => {
    calls.push([action, params]); return snapshot(params);
  });
  await panel.state().load(true);
  assert.equal(calls.length, 2);
  assert.equal(calls[0][1].member_ids.length, 5);
  assert.equal(calls[1][1].member_ids.length, 2);
  assert(calls.every(([action, params]) => action === "member_whitelist" && params.operation === "history"));
  assert.equal(copied.length, 1);
  assert.match(copied[0], /名字：中性成员1（UID 10）\n有效发言：101句/);
  assert.match(copied[0], /\n100\. \[[^\]]+\] 中性对话 100/);
  assert.equal(panel.state().notice, "已复制7位白名单成员及700条对话");
});

test("member, date and literal content filters are sent together", async t => {
  const calls = [];
  const panel = mount(t, async (_action, params) => { calls.push(params); return snapshot(params); });
  panel.state().selected = "12";
  panel.state().period = "yesterday";
  panel.state().query = "%_";
  await panel.state().load();
  assert.deepEqual(calls[0].member_ids, ["12"]);
  assert.equal(calls[0].period, "yesterday");
  assert.equal(calls[0].query, "%_");
  assert.equal(panel.state().data.members.length, 1);
});

test("cancel discards delayed bodies and never writes the clipboard", async t => {
  const copied = [];
  let release;
  clipboardWriter = async text => { copied.push(text); return true; };
  const panel = mount(t, async (_action, params) =>
    await new Promise(resolve => { release = () => resolve(snapshot(params)); }));
  const pending = panel.state().load(true);
  panel.state().reset();
  release();
  await pending;
  assert.equal(panel.state().data, null);
  assert.equal(panel.state().notice, "");
  assert.deepEqual(copied, []);
});

test("switching groups, permissions or list contents discards delayed history", async t => {
  for (const change of [
    { group: "6" }, { disabled: true }, { listVersion: 4 }, { members: members.slice(1) },
  ]) {
    let release;
    const panel = mount(t, async (_action, params) =>
      await new Promise(resolve => { release = () => resolve(snapshot(params)); }));
    const pending = panel.state().load();
    panel.props.value = { ...panel.props.value, ...change };
    await flush();
    release();
    await pending;
    assert.equal(panel.state().data, null);
  }
});

test("partial failures do not export an incomplete whitelist", async t => {
  const copied = [];
  clipboardWriter = async text => { copied.push(text); return true; };
  let calls = 0;
  const panel = mount(t, async (_action, params) => {
    if (++calls > 1) throw new Error("private database detail");
    return snapshot(params);
  });
  await panel.state().load(true);
  assert.equal(panel.state().data, null);
  assert.match(panel.state().error, /未复制部分结果/);
  assert(!panel.state().error.includes("database"));
  assert.deepEqual(copied, []);
});

test("backlog, mismatched identities and midnight prevent a misleading export", async t => {
  for (const change of ["backlog", "identity", "midnight", "version", "over_limit"]) {
    let calls = 0;
    const panel = mount(t, async (_action, params) => {
      const row = snapshot(params); calls++;
      if (change === "backlog") row.catching_up = true;
      if (change === "identity") row.members[0].id = "999";
      if (change === "midnight" && calls > 1) row.to_date = "2026-10-29";
      if (change === "version") row.version++;
      if (change === "over_limit") row.members[0].messages.push(row.members[0].messages[0]);
      return row;
    });
    await panel.state().load();
    assert.equal(panel.state().data, null);
    assert(panel.state().error.length > 0);
  }
});

test("denied clipboard does not claim success and preserves the loaded result", async t => {
  clipboardWriter = async () => false;
  const panel = mount(t, async (_action, params) => snapshot(params));
  await panel.state().load(true);
  assert.equal(panel.state().notice, "");
  assert.match(panel.state().error, /浏览器未允许复制/);
  assert.equal(panel.state().data.members.length, 7);
});

test("clipboard exceptions keep the result and report a copy failure, not a read failure", async t => {
  clipboardWriter = async () => { throw new Error("Clipboard unavailable"); };
  const panel = mount(t, async (_action, params) => snapshot(params));
  await panel.state().load(true);
  assert.equal(panel.state().notice, "");
  assert.match(panel.state().error, /浏览器未允许复制/);
  assert.equal(panel.state().data.members.length, 7);
});

test("a recent prepared result copies immediately without another asynchronous read", async t => {
  let calls = 0;
  clipboardWriter = async () => false;
  const panel = mount(t, async (_action, params) => { calls++; return snapshot(params); });
  await panel.state().load(true);
  assert.equal(calls, 2);
  clipboardWriter = async () => true;
  await panel.state().load(true);
  assert.equal(calls, 2);
  assert.match(panel.state().notice, /已复制7位/);
  panel.state().query = "changed";
  await panel.state().load(true);
  assert.equal(calls, 4);
});
