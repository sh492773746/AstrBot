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
const load = (name, api = {}) => {
  const source = readFileSync(new URL(
    `../src/components/platform/${name}.vue`, import.meta.url,
  ), "utf8");
  const { descriptor } = parse(source);
  const script = compileScript(descriptor, { id: name });
  assert.deepEqual(compileTemplate({
    source: descriptor.template.content, filename: `${name}.vue`, id: name,
    compilerOptions: { bindingMetadata: script.bindings, expressionPlugins: ["typescript"] },
  }).errors, []);
  const module = { exports: {} };
  new Function("require", "module", "exports", ts.transpileModule(script.content, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText)(id => {
    if (id === "@/i18n/composables") return { useI18n: () => ({ locale: ref("en-US") }) };
    if (id === "@/api/v1") return { botApi: api };
    if (id === "vue-router") return { onBeforeRouteLeave() {} };
    if (id.endsWith(".vue")) return { __esModule: true, default: {} };
    return require(id);
  }, module, module.exports);
  return { component: module.exports.default, source };
};
function mount(t, component, props) {
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

test("permissions project only group targets without any directory fetch", async t => {
  const { component, source } = load("WangshangliaoPermissions");
  assert.doesNotMatch(source, /sessionApi|loadTargets|reply_private|私聊|私信|private/i);
  const config = { id: "fixture", enabled_groups: ["34"], proactive_send: {
    enabled: true, targets: ["34", "private/42", "35"],
  }, moderation: { enabled: true, permissions: { "34": ["mute", "recall"] },
    manual_kick_only: true, auto_kick: { "34": false } } };
  const state = mount(t, component, {
    modelValue: config, groups: [{ id: "34", name: "First" }, { id: "35", name: "Second" }],
  });
  await nextTick();
  assert.deepEqual(state().groupTargets, ["34", "35"]);
  assert.deepEqual(state().proactiveTargets.map(row => row.value), ["34", "35"]);
  assert.deepEqual(state().groupActions, ["mute", "recall"]);
  assert.deepEqual(state().actions.map(row => row.value), [
    "cleanup", "rename", "mute", "kick", "recall", "unmute", "announce", "mute_all", "unmute_all",
  ]);
  assert.equal(state().policy.manual_kick_only, true);
  assert.deepEqual(config.proactive_send.targets, ["34", "private/42", "35"]);
});

test("login saves group policy and account transport without retired DM fields", async t => {
  const calls = [];
  const { component, source } = load("WangshangliaoLogin", {
    async create(config) { calls.push(structuredClone(config)); return { data: { status: "ok" } }; },
  });
  assert.doesNotMatch(source, /reply_private|私聊|私信|好友|private (texts|messages|replies)|friend/i);
  const oldWindow = globalThis.window;
  let reloads = 0;
  globalThis.window = { location: { reload() { reloads++; } }, removeEventListener() {}, addEventListener() {} };
  const config = {
    id: "fixture", type: "wangshangliao", enable: false, login_method: "password",
    enabled_groups: ["34"], reply_private: true, reply_groups: { "34": false },
    ai_routes: { admin_provider_id: "retired", groups: { "34": "group-model" } },
    proactive_send: { enabled: true, targets: ["34", "private/42"] },
    developer_test: { groups: ["34"], scopes: ["private_ai", "group_commands", "group_ai", "group_rules"] },
    moderation: { enabled: true, permissions: { "34": ["mute", "recall", "kick"] }, manual_kick_only: true },
    password: "never-save", _business_active: true,
  };
  const state = mount(t, component, { modelValue: config });
  // Restore after the component's unmount cleanup.
  t.after(() => { globalThis.window = oldWindow; });
  state().flow = { session_ref: "verified-ref", account_id: "42", nickname: "Fixture" };
  await state().saveAuthenticated();
  assert.equal(state().error, "");
  assert.equal(reloads, 1);
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0], {
    id: "fixture", type: "wangshangliao", enable: false, login_method: "password",
    enabled_groups: ["34"], reply_groups: { "34": false },
    ai_routes: { groups: { "34": "group-model" } },
    proactive_send: { enabled: true, targets: ["34"] },
    developer_test: { groups: ["34"], scopes: ["group_commands", "group_ai", "group_rules"] },
    moderation: config.moderation,
    session_ref: "verified-ref", account_id: "42", nickname: "Fixture",
  });
  assert.equal(config.ai_routes.admin_provider_id, "retired");
  assert.deepEqual(config.proactive_send.targets, ["34", "private/42"]);
});
