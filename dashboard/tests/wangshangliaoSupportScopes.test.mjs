import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import ts from "typescript";
import { createRenderer, h, nextTick, ref } from "vue";
import { compileScript, parse } from "vue/compiler-sfc";

const require = createRequire(import.meta.url);
const source = readFileSync(
  new URL("../plugin-pages/wangshangliao/SupportSettingsPanel.vue", import.meta.url), "utf8",
);
const script = compileScript(parse(source).descriptor, { id: "support-scopes" });
const module = { exports: {} };
const dependencies = id => id.endsWith(".vue") ? { default: {} }
  : id === "./componentSchema" ? {
    componentConstraints: () => ({}), componentField: () => ({}),
  } : require(id);
new Function("require", "module", "exports", ts.transpileModule(script.content, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText)(dependencies, module, module.exports);
const renderer = createRenderer({
  createComment: () => ({}), insert() {}, remove() {},
  parentNode: () => null, nextSibling: () => null,
});
const flush = async () => { await nextTick(); await new Promise(setImmediate); };
const row = account => ({ version: 1, config: {
  wording: account, style: "简洁", length: 300, links: [], knowledge: [],
} });

function mount(t, api, profiles = []) {
  const account = ref("99");
  const saved = [];
  let instance;
  const app = renderer.createApp({
    render: () => h({ ...module.exports.default, render: () => null }, {
      ref: value => { instance = value; }, api, account: account.value, profiles,
      onSaved: () => saved.push(true),
    }),
  });
  app.mount({});
  t.after(() => app.unmount());
  return { account, saved, state: () => instance.$.setupState };
}

test("late legacy responses cannot overwrite settings after changing robot", async t => {
  let release;
  const calls = [];
  const api = async (action, payload) => {
    calls.push([action, payload]);
    if (action === "owner_support") return { owners: [] };
    if (action === "support_editor") return { editors: [] };
    if (action === "page_schema") return {};
    if (payload.account === "99") return new Promise(resolve => { release = resolve; });
    return row(payload.account);
  };
  const panel = mount(t, api);
  await flush();
  assert.equal(panel.state().busy, true);
  panel.account.value = "199";
  await flush();
  release(row("STALE"));
  await flush();
  await flush();
  assert.equal(panel.state().busy, false);
  assert.equal(panel.state().settings.wording, "199");
  assert.deepEqual(calls.filter(([a]) => a === "owner_support")
    .map(([, p]) => p.account), ["99", "199"]);
});

test("selected owner parameters use its own version and refresh after provisioning", async t => {
  const calls = [];
  const api = async (action, payload) => {
    calls.push([action, payload]);
    if (action === "owner_support" && payload.operation === "list") {
      return { owners: [{ uid: "1", name: "Owner", active: true }] };
    }
    if (action === "support_editor") return { editors: [] };
    if (action === "page_schema") return {};
    return row(payload.account || payload.uid);
  };
  const panel = mount(t, api, [
    { kind: "consultation", account: "99", group_id: "", version: 2,
      selection: { config_id: "legacy", persona_id: "legacy" } },
    { kind: "consultation", account: "owner:1", group_id: "", version: 7,
      selection: { config_id: "dedicated", persona_id: "Owner", temperature: 0.5 } },
  ]);
  await flush();
  panel.state().target = "1";
  await flush();
  assert.equal(panel.state().settings.wording, "1");
  assert.equal(panel.state().profileDraft.config_id, "dedicated");
  await panel.state().saveProfile();
  const write = calls.find(([a, p]) => a === "owner_support" && p.operation === "profile");
  assert.equal(write[1].uid, "1");
  assert.equal(write[1].version, 7);
  assert.equal(write[1].selection.config_id, "dedicated");
  assert.equal(calls.some(([a]) => a === "ai_profile"), false);
  await panel.state().provisionOwner();
  assert.equal(panel.saved.length, 2);
});
