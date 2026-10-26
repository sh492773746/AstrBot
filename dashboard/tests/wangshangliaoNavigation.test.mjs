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
const load = name => {
  const { descriptor } = parse(readFileSync(
    new URL(`../plugin-pages/wangshangliao/${name}.vue`, import.meta.url), "utf8",
  ));
  const script = compileScript(descriptor, { id: `navigation-${name}` });
  assert.deepEqual(compileTemplate({
    source: descriptor.template.content, filename: `${name}.vue`,
    id: `navigation-${name}`,
    compilerOptions: { bindingMetadata: script.bindings, expressionPlugins: ["typescript"] },
  }).errors, []);
  const js = ts.transpileModule(script.content, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const module = { exports: {} };
  new Function("require", "module", "exports", js)(id => {
    if (id.endsWith(".vue")) return { __esModule: true, default: {} };
    if (id.endsWith("platformInstanceName.mjs")) {
      return { getPlatformInstanceName: bot => bot.nickname || bot.id };
    }
    return require(id);
  }, module, module.exports);
  return module.exports.default;
};
const App = load("App");
const OpenService = load("OpenService");

function mount(t, component, props = {}) {
  let instance;
  const app = renderer.createApp({
    render: () => h({ ...component, render: () => null }, {
      ...props, ref: value => { instance = value; },
    }),
  });
  app.mount({});
  t.after(() => app.unmount());
  return () => instance.$.setupState;
}

async function mountPage(t, integrated = true) {
  const calls = [];
  const oldWindow = globalThis.window, oldDocument = globalThis.document;
  globalThis.document = { documentElement: {} };
  globalThis.window = { AstrBotPluginPage: {
    ready: async () => {}, getLocale: () => "zh-CN", onContext: () => () => {},
    async apiGet(endpoint) {
      calls.push(endpoint);
      if (endpoint === "settings") return {
        open_mode: integrated ? "integrated" : "legacy", providers: [],
        bots: [{ id: "fixture", enabled_groups: ["5"], moderation: {}, revision: "1" }],
      };
      if (endpoint === "audit") return { reviews: [], sanctions: [], counts: [], rules: [] };
      if (endpoint === "activities") return { lottery: {} };
      if (endpoint === "schedule") return { schedule: null, executions: [], history: [] };
      throw new Error("Unexpected endpoint");
    },
    async apiPost() { throw new Error("Navigation must not write"); },
  } };
  t.after(() => {
    globalThis.window = oldWindow;
    globalThis.document = oldDocument;
  });
  const state = mount(t, App);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(state().error, "");
  return { state, calls };
}

test("every current feature has one category and only its subtabs are shown", async t => {
  const { state } = await mountPage(t);
  const s = state();
  assert.equal(s.sections.length, 5);
  assert.equal(s.tab, "open");
  assert.deepEqual(s.sectionTabs.map(x => x.id), ["open", "rules", "permissions", "friend-approval", "speech-activity"]);
  const inventory = s.sections.flatMap(x => x.tabs)
    .filter(id => s.tabs.some(tab => tab.id === id));
  assert.equal(new Set(inventory).size, s.tabs.length);
  assert.deepEqual([...inventory].sort(), s.tabs.map(x => x.id).sort());
  assert(!s.tabs.some(x => ["business", "settings"].includes(x.id)));
});

test("navigation retains drafts, selected scope and last subtab without writes or reloads", async t => {
  const { state, calls } = await mountPage(t);
  const before = [...calls];
  state().form.group.card_suffix = "Draft suffix";
  state().selectTab("group-ai");
  assert.equal(typeof state().pending, "function");
  state().pending();
  state().pending = null;
  await nextTick();
  assert.equal(state().section.id, "ai");
  assert.equal(state().openView, "group-ai");
  state().selectTab("schedule");
  state().pending();
  state().pending = null;
  await nextTick();
  assert.equal(state().lastTabs.ai, "group-ai");
  assert.equal(state().openView, "group-ai");
  assert.equal(state().openVisible, false);
  state().selectTab(state().lastTabs.ai);
  state().pending();
  state().pending = null;
  await nextTick();
  assert.equal(state().tab, "group-ai");
  assert.equal(state().groupId, "5");
  assert.equal(state().form.group.card_suffix, "Draft suffix");
  assert.deepEqual(calls, before);
});

test("legacy feature tabs and the existing dirty-manager confirmation remain accessible", async t => {
  const { state } = await mountPage(t, false);
  assert(state().sectionTabs.some(x => x.id === "business"));
  assert(state().sectionTabs.some(x => x.id === "settings"));
  const before = state().tab;
  state().managersDirty = true;
  state().selectTab("schedule");
  await nextTick();
  assert.equal(state().tab, before);
  assert.equal(typeof state().pending, "function");
  state().pending = null;
  assert.equal(state().lastTabs.runtime, undefined);
});

test("Enter in unrelated panels cannot submit hidden permission or moderation edits", () => {
  const { descriptor } = parse(readFileSync(
    new URL("../plugin-pages/wangshangliao/App.vue", import.meta.url), "utf8",
  ));
  const nodes = [descriptor.template.ast];
  let form;
  while (nodes.length && !form) {
    const node = nodes.pop();
    if (node.tag === "form") form = node;
    else nodes.push(...(node.children || []));
  }
  assert.ok(form);
  const handler = form.props.find(p => p.type === 7 && p.name === "on" && p.arg.content === "submit");
  assert(handler.modifiers.includes("prevent"));
  const submit = new Function("tab", "save", `return ${handler.exp.content};`);
  let calls = 0;
  for (const tab of ["open", "consultation", "admin-guidance", "plans", "finance", "activities", "runtime", "schedule"]) {
    submit(tab, () => calls++);
  }
  assert.equal(calls, 0);
  for (const tab of ["settings", "permissions", "business"]) submit(tab, () => calls++);
  assert.equal(calls, 3);
});

test("service views retain selected group and edited data; expensive panels are visited lazily", async t => {
  const calls = [];
  const view = ref("groups");
  const status = {
    mode: "integrated", settings: {}, admins: [], providers: [], ai_profiles: [],
    groups: [{ account: "1", group_id: "5", active: 1, present: 1, admission: "legacy",
      managers: ["2"], rules: { keywords: ["Existing keyword"] } }],
  };
  const bridge = { async apiPost(endpoint, request) {
    calls.push(request.action);
    if (request.action === "status") return structuredClone(status);
    if (request.action === "tenant_members") return { members: [
      { id: "2", name: "Manager", role: "GROUP_ROLE_ADMIN" },
      { id: "3", name: "Owner", role: "GROUP_ROLE_OWNER" },
      { id: "4", name: "Member", role: "GROUP_ROLE_MEMBER" },
      { id: "5", name: "Unverified" },
    ] };
    throw new Error("Navigation must not write");
  } };
  let instance;
  const component = { ...OpenService, render: () => null };
  const app = renderer.createApp({
    render: () => h(component, {
      bridge, view: view.value, ref: value => { instance = value; },
    }),
  });
  app.mount({});
  t.after(() => app.unmount());
  await new Promise(resolve => setImmediate(resolve));
  const state = () => instance.$.setupState;
  state().selected = "1/5";
  await state().choose();
  assert.deepEqual(state().managerCandidates.map(m => m.id), ["2", "3"]);
  state().selectedManagers.push("4");
  assert.deepEqual(state().invalidManagers, ["4"]);
  assert(!state().visibleManagers.some(m => m.id === "4"));
  state().keywordText = "Unsaved keyword";
  state().privateProfile.temperature = 0.3;
  state().adminProfile.temperature = 0.2;
  assert.equal(state().visited.has("plans"), false);
  assert.equal(state().visited.has("finance"), false);
  for (const id of ["consultation", "admin-guidance", "plans", "finance", "runtime", "groups"]) {
    view.value = id;
    await nextTick();
  }
  assert.equal(state().selected, "1/5");
  assert.equal(state().keywordText, "Unsaved keyword");
  assert.equal(state().privateProfile.temperature, 0.3);
  assert.equal(state().adminProfile.temperature, 0.2);
  assert(state().visited.has("plans") && state().visited.has("finance"));
  assert.deepEqual(calls, ["status", "tenant_members"]);
});

test("administrator profile saves its own version and never posts a customer or group selection", async t => {
  const calls = [];
  const defaults = { config_id: "", persona_id: "", provider_id: "",
    temperature: null, top_p: null, max_tokens: null, history_turns: null };
  const status = { mode: "integrated", settings: {}, admins: [], providers: [], groups: [],
    accounts: [{ account: "1" }],
    ai_profiles: [
      { kind: "consultation", account: "1", group_id: "", version: 5,
        selection: { ...defaults, persona_id: "customer" } },
      { kind: "admin_guidance", account: "", group_id: "", version: 2,
        selection: { ...defaults, persona_id: "admin", max_tokens: 1024 } },
      { kind: "group", account: "", group_id: "", version: 9,
        selection: { ...defaults, persona_id: "group" } },
    ] };
  const state = mount(t, OpenService, { view: "admin-guidance",
    bridge: { async apiPost(endpoint, request) {
      assert.equal(endpoint, "open-service");
      calls.push(structuredClone(request));
      return request.action === "status" ? structuredClone(status) : { saved: true };
    } } });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(state().adminProfile.persona_id, "admin");
  state().adminProfile.temperature = 0.2;
  await state().saveProfile("admin_guidance");
  const changes = calls.filter(request => request.action !== "status");
  assert.equal(changes.length, 1);
  assert.deepEqual(changes[0], { action: "ai_profile", payload: {
    kind: "admin_guidance", account: "", group_id: "", version: 2,
    selection: { ...defaults, persona_id: "admin", max_tokens: 1024, temperature: 0.2 },
  } });
  assert.equal(state().privateProfile.persona_id, "customer");
  assert.equal(state().defaultGroupProfile.persona_id, "group");
});

test("video-only policy saves in the selected group without changing managers or pause", async t => {
  const calls = [];
  const status = { mode: "integrated", settings: {}, admins: [], providers: [], groups: [
    { account: "1", group_id: "5", version: 7, active: 1, present: 1, paused: 1,
      managers: ["2"], rules: { advertising: false, flood: false, keywords: ["keep"] } },
    { account: "1", group_id: "6", version: 3, active: 1, present: 1, paused: 0,
      managers: ["2"], rules: { advertising: true, flood: true, keywords: [] } },
  ] };
  const state = mount(t, OpenService, { view: "rules", bridge: {
    async apiPost(endpoint, request) {
      calls.push(structuredClone(request));
      if (request.action === "status") return structuredClone(status);
      if (request.action === "tenant_members") return { members: [
        { id: "2", name: "Manager", role: "GROUP_ROLE_ADMIN" },
      ] };
      if (request.action === "tenant") return { saved: true };
      throw new Error("Unexpected write");
    },
  } });
  await new Promise(resolve => setImmediate(resolve));
  state().selected = "1/5";
  await state().choose();
  state().tenant.rules.video_whitelist_only = true;
  await state().saveTenant();
  const writes = calls.filter(c => c.action === "tenant");
  assert.equal(writes.length, 1);
  assert.deepEqual(writes[0].payload, {
    account: "1", group_id: "5", version: 7, paused: true, managers: ["2"],
    rules: { advertising: false, flood: false, keywords: ["keep"], video_whitelist_only: true },
  });
  assert.equal(status.groups[1].rules.video_whitelist_only, undefined);
});
