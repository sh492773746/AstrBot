import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import ts from "typescript";
import { createRenderer, h, nextTick, ref } from "vue";
import { compileScript, compileStyle, compileTemplate, parse } from "vue/compiler-sfc";

const require = createRequire(import.meta.url);
const source = readFileSync(
  new URL("../plugin-pages/wangshangliao/GroupRulesEditor.vue", import.meta.url), "utf8",
);
const { descriptor } = parse(source);
const script = compileScript(descriptor, { id: "group-rules-test" });
const compiled = ts.transpileModule(script.content, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
});
const module = { exports: {} };
new Function("require", "module", "exports", compiled.outputText)(require, module, module.exports);
const component = module.exports.default;
const renderer = createRenderer({
  createComment: () => ({}), insert() {}, remove() {},
  parentNode: () => null, nextSibling: () => null,
});
const rule = { name: "收款招募", terms: ["收款截图", "加我"], enabled: true };

function mountEditor(t, initial = [structuredClone(rule)]) {
  const rules = ref(initial);
  const disabled = ref(false);
  let instance;
  const app = renderer.createApp({
    render: () => h({ ...component, render: () => null }, {
      ref: value => { instance = value; }, modelValue: rules.value,
      disabled: disabled.value,
      "onUpdate:modelValue": value => { rules.value = value; },
    }),
  });
  app.mount({});
  t.after(() => app.unmount());
  return { state: () => instance.$.setupState, rules, disabled };
}

test("compiles the bounded accessible editor with non-submitting icon controls", () => {
  assert.deepEqual(compileTemplate({
    source: descriptor.template.content, filename: "GroupRulesEditor.vue",
    id: "group-rules-test",
    compilerOptions: { bindingMetadata: script.bindings, expressionPlugins: ["typescript"] },
  }).errors, []);
  assert.deepEqual(compileStyle({
    source: descriptor.styles[0].content, filename: "GroupRulesEditor.vue",
    id: "group-rules-test", scoped: true,
  }).errors, []);
  assert.match(source, /<Trash2/);
  assert.match(source, /type="button"/);
  assert.match(source, /aria-label=/);
  assert.doesNotMatch(source, /v-html|eval\(/);
});

test("editing and toggling emit detached data; deletion does not affect other rules", async t => {
  const original = [structuredClone(rule), { ...structuredClone(rule), name: "群B" }];
  const { state, rules } = mountEditor(t, original);
  state().update(0, { name: "新规则", terms: ["联系人", "付款图"] });
  await nextTick();
  assert.equal(original[0].name, "收款招募");
  assert.equal(rules.value[0].name, "新规则");
  state().update(0, { enabled: false });
  await nextTick();
  assert.equal(rules.value[0].enabled, false);
  state().remove(0);
  await nextTick();
  assert.deepEqual(rules.value.map(r => r.name), ["群B"]);
});

test("the actual input handler accepts both separators and preserves URL terms with pipes", async t => {
  const { state, rules } = mountEditor(t);
  const template = compileTemplate({
    source: descriptor.template.content, filename: "GroupRulesEditor.vue",
    id: "group-rules-test",
    compilerOptions: {
      mode: "function", bindingMetadata: script.bindings, expressionPlugins: ["typescript"],
    },
  });
  assert.deepEqual(template.errors, []);
  const js = ts.transpileModule(template.code, {
    compilerOptions: { target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const render = new Function("Vue", js)(require("vue"));
  for (const [text, expected] of [
    [" 收款截图 / 加我 ", ["收款截图", "加我"]],
    [" 收款截图 | 加我 ", ["收款截图", "加我"]],
    [" example.com/path | 联系我 ", ["example.com/path", "联系我"]],
    ["收款截图//加我", ["收款截图", "", "加我"]],
  ]) {
    const tree = render({}, [], { modelValue: rules.value }, state(), {}, {});
    const row = tree.children[1].children[0];
    const input = row.children[2].children.find(n => n.type === "input");
    assert.equal(input.props["aria-label"], "第1条组合词");
    input.props.onInput({ target: { value: text } });
    await nextTick();
    assert.deepEqual([...rules.value[0].terms], expected);
  }
});

test("twenty-rule limit and disabled state apply to functions as well as buttons", async t => {
  const { state, rules, disabled } = mountEditor(t, []);
  for (let i = 0; i < 25; i++) {
    state().add();
    await nextTick();
  }
  assert.equal(rules.value.length, 20);
  disabled.value = true;
  await nextTick();
  const before = structuredClone(JSON.parse(JSON.stringify(rules.value)));
  state().remove(0);
  state().update(0, { name: "Must not change" });
  state().add();
  await nextTick();
  assert.deepEqual(JSON.parse(JSON.stringify(rules.value)), before);
});
