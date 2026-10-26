import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import ts from "typescript";
import { createRenderer, h, nextTick, ref } from "vue";
import { compileScript, compileTemplate, parse } from "vue/compiler-sfc";

const require = createRequire(import.meta.url);
const filename = "../plugin-pages/wangshangliao/RuleAutomationPanel.vue";
const source = readFileSync(new URL(filename, import.meta.url), "utf8");
const { descriptor } = parse(source);
const script = compileScript(descriptor, { id: "automation-status" });
const module = { exports: {} };
const dependencies = id => id === "./componentSchema" ? {
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

function mount(t, row) {
  const tenantVersion = ref(4);
  let instance;
  const api = async action => action === "page_schema" ? {} : structuredClone(row);
  const app = renderer.createApp({
    render: () => h({ ...module.exports.default, render: () => null }, {
      ref: value => { instance = value; }, api,
      account: "99", group: "5", tenantVersion: tenantVersion.value,
    }),
  });
  app.mount({});
  t.after(() => app.unmount());
  return { tenantVersion, state: () => instance.$.setupState };
}

test("automation template compiles with explicit authorization and receipt warnings", () => {
  assert.deepEqual(compileTemplate({
    source: descriptor.template.content, filename, id: "automation-status",
    compilerOptions: { bindingMetadata: script.bindings, expressionPlugins: ["typescript"] },
  }).errors, []);
  assert.match(source, /authorizationRequired/);
  assert.match(source, /群配置授权已变化/);
  assert.match(source, /deliveryError\(ad.id\)/);
});

test("stale confirmed authorization is visible without changing saved rules", async t => {
  const row = { version: 2, tenant_version: 3, config: { keywords: [], ads: [] } };
  const panel = mount(t, row);
  await flush();
  assert.equal(panel.state().authorizationRequired, true);
  assert.equal(panel.state().version, 2);
  panel.tenantVersion.value = 3;
  await flush();
  assert.equal(panel.state().authorizationRequired, false);
});

test("missing initial rules never display a false revoked authorization", async t => {
  const panel = mount(t, { version: 0, config: { keywords: [], ads: [] } });
  await flush();
  assert.equal(panel.state().authorizationRequired, false);
});

test("receipt feedback distinguishes expiry, preflight, rejection and uncertainty", async t => {
  const panel = mount(t, {
    version: 1, tenant_version: 4, config: { keywords: [], ads: [] },
    schedules: [
      { rule_id: "expired", last_state: "skipped", last_error: "advertisement_slot_expired" },
      { rule_id: "restart", last_state: "skipped", last_error: "advertisement_before_runtime" },
      { rule_id: "guard", last_state: "skipped", last_error: "business_rules_inactive" },
      { rule_id: "rejected", last_state: "rejected", last_error: "" },
      { rule_id: "accepted", last_state: "accepted", last_error: "" },
      { rule_id: "unknown", blocked: "delivery_unknown", last_state: "unknown" },
    ],
  });
  await flush();
  const state = panel.state();
  assert.match(state.deliveryError("expired"), /等待时限/);
  assert.match(state.deliveryError("restart"), /服务启动/);
  assert.match(state.deliveryError("guard"), /校验未通过/);
  assert.match(state.deliveryError("rejected"), /被拒绝/);
  assert.equal(state.deliveryError("accepted"), "");
  assert.equal(state.deliveryError("unknown"), "");
  assert.equal(state.deliveryError("missing"), "");
});
