import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import ts from "typescript";
import { createRenderer, nextTick } from "vue";
import { compileScript, compileStyle, compileTemplate, parse } from "vue/compiler-sfc";

const require = createRequire(import.meta.url);
const source = readFileSync(
  new URL("../plugin-pages/wangshangliao/AIPlansPanel.vue", import.meta.url), "utf8",
);
const { descriptor } = parse(source);
const script = compileScript(descriptor, { id: "ai-budget-test" });
const compiled = ts.transpileModule(script.content, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
});
const module = { exports: {} };
new Function("require", "module", "exports", compiled.outputText)(require, module, module.exports);
const renderer = createRenderer({
  createComment: () => ({}), insert() {}, remove() {},
  parentNode: () => null, nextSibling: () => null,
});

function mount(t, settings = {}) {
  const billing = {
    settings: {
      version: 7, contact_name: "Support", contact: "Verified contact",
      monthly_micro: 25_000_000, trial_monthly_micro: 20_000_000, ...settings,
    },
    prices: [], pools: [], payers: [], notices: [], unknown: [],
  };
  const calls = [];
  const saved = [];
  const app = renderer.createApp({ ...module.exports.default, render: () => null }, {
    billing, tenant: null, members: [], providers: [], disabled: false,
    api: async (action, payload) => calls.push({ action, payload: structuredClone(payload) }),
    onSaved: () => saved.push(true),
  });
  const instance = app.mount({});
  t.after(() => app.unmount());
  return { state: instance.$.setupState, calls, instance, saved };
}

test("trial, paid and global safety budgets have separate labeled totals", () => {
  assert.deepEqual(compileTemplate({
    source: descriptor.template.content, filename: "AIPlansPanel.vue",
    id: "ai-budget-test",
    compilerOptions: { bindingMetadata: script.bindings, expressionPlugins: ["typescript"] },
  }).errors, []);
  assert.deepEqual(compileStyle({
    source: descriptor.styles[0].content, filename: "AIPlansPanel.vue",
    id: "ai-budget-test", scoped: true,
  }).errors, []);
  for (const name of ["trial_cost_micro", "paid_cost_micro", "legacy_cost_micro", "limit_micro"]) {
    assert.ok(descriptor.template.content.includes(`billing.site.${name}`), name);
  }
  assert.match(source, /全站免费试用预算（元／自然月）/);
  assert.match(source, /全站安全基础预算（元／自然月）/);
  assert.match(source, /基础额＋已授予付费预算/);
  assert.match(descriptor.styles[0].content, /@media \(max-width: 600px\)/);
});

test("saves both fixed-point budgets under the same settings version without enabling AI", async t => {
  const { state, calls, saved } = mount(t);
  assert.equal(state.settings.trial_monthly_cny, "20");
  assert.equal(state.settings.monthly_cny, "25");
  state.settings.trial_monthly_cny = "30.01";
  state.settings.monthly_cny = "35";
  await state.run(state.saveSettings);
  assert.deepEqual(calls, [{
    action: "billing_settings",
    payload: {
      version: 7, contact_name: "Support", contact: "Verified contact",
      monthly_cny: "35", trial_monthly_cny: "30.01",
    },
  }]);
  assert.equal(state.notice, "升级客服、免费池和安全预算已保存");
  assert.deepEqual(saved, [true]);
});

test("older status defaults only the missing trial budget, never the global safety base", t => {
  const { state } = mount(t, { trial_monthly_micro: undefined, monthly_micro: 0 });
  assert.equal(state.settings.trial_monthly_cny, "20");
  assert.equal(state.settings.monthly_cny, "0");
  assert.equal(state.preview, null);
});

test("failed versioned save does not report success or retry automatically", async t => {
  const { state, instance, saved } = mount(t);
  instance.$.props.api = async () => { throw new Error("Settings changed; refresh"); };
  await nextTick();
  await state.run(state.saveSettings);
  assert.equal(state.error, "Settings changed; refresh");
  assert.equal(state.notice, "");
  assert.equal(state.busy, false);
  assert.deepEqual(saved, []);
});
