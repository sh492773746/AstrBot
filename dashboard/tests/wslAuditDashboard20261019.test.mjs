import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import ts from "typescript";
import { createRenderer, h, nextTick, ref } from "vue";
import { compileScript, compileTemplate, parse } from "vue/compiler-sfc";

const require = createRequire(import.meta.url);
const renderer = createRenderer({
  createComment: () => ({}), insert() {}, remove() {},
  parentNode: () => null, nextSibling: () => null,
});
function load(name) {
  const { descriptor } = parse(readFileSync(
    new URL(`../plugin-pages/wangshangliao/${name}.vue`, import.meta.url), "utf8",
  ));
  const script = compileScript(descriptor, { id: `audit-${name}` });
  assert.deepEqual(compileTemplate({
    source: descriptor.template.content, filename: `${name}.vue`, id: `audit-${name}`,
    compilerOptions: { bindingMetadata: script.bindings, expressionPlugins: ["typescript"] },
  }).errors, []);
  const module = { exports: {} };
  const js = ts.transpileModule(script.content, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  new Function("require", "module", "exports", js)(id => {
    if (id.endsWith(".vue")) return { __esModule: true, default: {} };
    if (id.endsWith("platformInstanceName.mjs")) return { getPlatformInstanceName: bot => bot.id };
    return require(id);
  }, module, module.exports);
  return module.exports.default;
}
const App = load("App");
const FriendApproval = load("FriendApproval");
const settle = async () => { await new Promise(setImmediate); await nextTick(); };

test("moderation audit renders submission state and sanitized failure labels", () => {
  const source = readFileSync(
    new URL("../plugin-pages/wangshangliao/App.vue", import.meta.url), "utf8",
  );
  assert.match(source, /row\.action_submitted === 0/);
  assert.match(source, /row\.recall_submitted === 0/);
  assert.match(source, /label\(row\.action_error\)/);
  assert.match(source, /label\(row\.recall_error\)/);
  assert.match(source, /action_automation_changed:/);
});
function deferred() {
  let resolve, reject;
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}
function mount(t, component, props) {
  let instance;
  const view = { ...component, render: () => null };
  const app = renderer.createApp({
    render: () => h(view, {
      ...props(), ref: value => { instance = value; },
    }),
  });
  app.mount({});
  t.after(() => app.unmount());
  return instance.$.setupState;
}
const activity = group => ({
  lottery: { prize: `Prize ${group}`, winners: 3, duration_minutes: 10, capacity: 15, invite_gate: 0, contact: "" },
  invite_rate: "0.00", invitation: {}, current_lottery: null, revision: `activity-${group}`,
});
const schedule = group => ({
  schedule: { group_id: group, version: 1, status: "active" }, executions: [], history: [],
  revision: `schedule-${group}`,
});
async function mountPage(t, override = () => undefined) {
  const calls = [];
  const oldWindow = globalThis.window, oldDocument = globalThis.document;
  globalThis.document = { documentElement: {} };
  globalThis.window = { AstrBotPluginPage: {
    ready: async () => {}, getLocale: () => "zh-CN", onContext: () => () => {},
    async apiGet(endpoint, params = {}) {
      calls.push({ method: "GET", endpoint, params });
      const result = override("GET", endpoint, params);
      if (result !== undefined) return result;
      if (endpoint === "settings") return {
        open_mode: "isolated", providers: [],
        bots: [{ id: "fixture", enabled_groups: ["5", "6"], moderation: {}, revision: "1" }],
      };
      if (endpoint === "audit") return { reviews: [], sanctions: [], counts: [], rules: [] };
      if (endpoint === "activities") return activity(params.group_id);
      if (endpoint === "schedule") return schedule(params.group_id);
      throw new Error("Unexpected endpoint");
    },
    async apiPost(endpoint, body) {
      calls.push({ method: "POST", endpoint, body: structuredClone(body) });
      const result = override("POST", endpoint, body);
      if (result !== undefined) return result;
      throw new Error("Unexpected write");
    },
  } };
  t.after(() => { globalThis.window = oldWindow; globalThis.document = oldDocument; });
  const state = mount(t, App, () => ({}));
  await settle();
  assert.equal(state.error, "");
  return { state, calls };
}

test("failed reads after switching groups cannot expose previous activity or schedule versions", async t => {
  const { state, calls } = await mountPage(t, (method, endpoint, params) => {
    if (method === "GET" && params.group_id === "6" && ["activities", "schedule"].includes(endpoint))
      return Promise.reject(new Error("Current group unavailable"));
  });
  assert.equal(state.activityState.revision, "activity-5");
  assert.equal(state.scheduleState.schedule.group_id, "5");
  state.select({ target: { value: "6" } }, "group");
  await settle();
  assert.equal(state.activityState, null);
  assert.equal(state.activityOriginal, "");
  assert.equal(state.scheduleState.schedule, null);
  state.activityForm.prize = "Must not write old values to group 6";
  await state.saveActivities();
  await state.pauseSchedule();
  assert.equal(calls.filter(call => call.method === "POST").length, 0);
});

test("activity save responses cannot replace state after scope changed", async t => {
  const write = deferred();
  const { state } = await mountPage(t, (method, endpoint) => {
    if (method === "POST" && endpoint === "activities") return write.promise;
  });
  state.activityForm.prize = "Draft for group 5";
  const saving = state.saveActivities();
  state.groupId = "6";
  await state.loadActivities();
  assert.equal(state.activityState.revision, "activity-6");
  write.resolve(activity("5"));
  await saving;
  assert.equal(state.activityState.revision, "activity-6");
  assert.equal(state.activityForm.prize, "Prize 6");
  assert.equal(state.notice, "");
});

test("activity saves are serialized while the first request is unresolved", async t => {
  const write = deferred();
  const { state, calls } = await mountPage(t, (method, endpoint) => {
    if (method === "POST" && endpoint === "activities") return write.promise;
  });
  state.activityForm.prize = "Draft";
  const first = state.saveActivities(), second = state.saveActivities();
  write.resolve(activity("5"));
  await Promise.all([first, second]);
  assert.equal(calls.filter(call => call.method === "POST").length, 1);
});

test("schedule pause carries exact scope revision and rejects legacy missing revisions", async t => {
  const { state, calls } = await mountPage(t, (method, endpoint) => {
    if (method === "POST" && endpoint === "schedule") return { status: "paused" };
  });
  await state.pauseSchedule();
  const writes = calls.filter(call => call.method === "POST");
  assert.equal(writes.length, 1);
  assert.deepEqual(writes[0].body, {
    bot_id: "fixture", group_id: "5", version: 1, action: "pause", revision: "schedule-5",
  });
  state.scheduleState.revision = "";
  await state.pauseSchedule();
  assert.equal(calls.filter(call => call.method === "POST").length, 1);
});

test("empty scope settles both loading indicators after superseding reads", async t => {
  const reads = [deferred(), deferred()];
  let waiting = false;
  const { state } = await mountPage(t, (method, endpoint) => {
    if (waiting && method === "GET" && ["activities", "schedule"].includes(endpoint))
      return reads[endpoint === "activities" ? 0 : 1].promise;
  });
  waiting = true;
  const old = [state.loadActivities(), state.loadSchedule()];
  state.groupId = "";
  await Promise.all([state.loadActivities(), state.loadSchedule()]);
  assert.equal(state.activityLoading, false);
  assert.equal(state.scheduleLoading, false);
  reads[0].resolve(activity("5"));
  reads[1].resolve(schedule("5"));
  await Promise.all(old);
  assert.equal(state.activityState, null);
  assert.equal(state.scheduleState.schedule, null);
});

test("failed approval refresh removes stale version and disables subsequent writes", async t => {
  let fail = false;
  const writes = [];
  const bridge = {
    async apiGet() {
      if (fail) throw new Error("Approval status unavailable");
      return { enabled: true, version: 1, active: true, paused_reason: "" };
    },
    async apiPost(endpoint, body) { writes.push(body); return {}; },
  };
  const state = mount(t, FriendApproval, () => ({
    bot: { id: "fixture", revision: "1", account_id: "1" }, bridge, t: zh => zh,
  }));
  await settle();
  assert.equal(state.approval.version, 1);
  fail = true;
  await state.load();
  assert.equal(state.approval, null);
  await state.save(false);
  await state.reconcile();
  assert.deepEqual(writes, []);
});

test("approval responses for a previous bot cannot overwrite its replacement", async t => {
  const old = deferred();
  const bot = ref({ id: "old", revision: "1", account_id: "1" });
  const bridge = {
    async apiGet(_endpoint, params) {
      return params.bot_id === "old" ? old.promise : { enabled: false, version: 7 };
    },
    async apiPost() { throw new Error("Read-only test"); },
  };
  const state = mount(t, FriendApproval, () => ({ bot: bot.value, bridge, t: zh => zh }));
  bot.value = { id: "new", revision: "2", account_id: "2" };
  await settle();
  old.resolve({ enabled: true, version: 1 });
  await settle();
  assert.equal(state.approval.version, 7);
  assert.equal(state.approval.enabled, false);
});
