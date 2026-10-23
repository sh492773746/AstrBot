import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import ts from "typescript";
import { createRenderer, nextTick } from "vue";
import { compileScript, compileTemplate, parse } from "vue/compiler-sfc";

const require = createRequire(import.meta.url);
const source = readFileSync(
  new URL("../plugin-pages/wangshangliao/AIProfileEditor.vue", import.meta.url), "utf8",
);
const { descriptor } = parse(source);
const script = compileScript(descriptor, { id: "ai-profile-levels" });
const module = { exports: {} };
new Function("require", "module", "exports", ts.transpileModule(script.content, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText)(require, module, module.exports);
const renderer = createRenderer({
  createComment: () => ({}), insert() {}, remove() {},
  parentNode: () => null, nextSibling: () => null,
});

function mount(t, privateChat = false, modelValue = {}) {
  const changes = [];
  const app = renderer.createApp({ ...module.exports.default, render: () => null }, {
    privateChat, modelValue, label: "测试配置",
    choices: { configs: [], personas: [] }, providers: [],
    "onUpdate:modelValue": value => changes.push(value),
  });
  const instance = app.mount({});
  t.after(() => app.unmount());
  return { state: instance.$.setupState, instance, changes };
}

test("all four numeric controls use accessible low, medium and high selectors", () => {
  assert.deepEqual(compileTemplate({
    source: descriptor.template.content, filename: "AIProfileEditor.vue",
    id: "ai-profile-levels",
    compilerOptions: { bindingMetadata: script.bindings, expressionPlugins: ["typescript"] },
  }).errors, []);
  assert.match(source, /<option value="0">低<\/option>/);
  assert.match(source, /<option value="1">中<\/option>/);
  assert.match(source, /<option value="2">高<\/option>/);
  assert.doesNotMatch(descriptor.template.content, /type="number"/);
  assert.match(source, /aria-label/);
});

test("configuration and persona are fixed outputs without rebinding handlers", () => {
  assert.doesNotMatch(source, /update\('(?:config_id|persona_id)'/);
  assert.doesNotMatch(source, /<option v-for="[cp] in choices\./);
  assert.match(source, /<output :aria-label="`\$\{label\} · AstrBot配置`"/);
  assert.match(source, /<output :aria-label="`\$\{label\} · 人格`"/);
});
test("persona selections and legacy custom values survive without silent rewriting", async t => {
  const initial = { temperature: 0.3, top_p: 1, max_tokens: 512,
    history_turns: null, persona_id: "existing", config_id: "default" };
  const { state, changes, instance } = mount(t, false, initial);
  assert.equal(state.level("temperature"), "existing");
  assert.equal(state.level("top_p"), "2");
  assert.equal(state.level("history_turns"), "");
  assert.deepEqual(changes, []);
  state.chooseLevel("temperature", { target: { value: "1" } });
  assert.deepEqual(changes[0], { ...initial, temperature: 0.5 });
  instance.$.props.modelValue = changes[0];
  await nextTick();
  assert.equal(state.level("temperature"), "1");
  state.chooseLevel("temperature", { target: { value: "" } });
  assert.equal(changes[1].temperature, null);
  assert.equal(changes[1].persona_id, "existing");
});

test("group presets bound context to 4, 8, 12 messages and output to 512 tokens", t => {
  const { state, changes } = mount(t);
  assert.deepEqual(state.levels("history_turns"), [2, 4, 6]);
  assert.deepEqual(state.levels("max_tokens"), [128, 256, 512]);
  for (const key of ["temperature", "top_p", "max_tokens", "history_turns"]) {
    state.chooseLevel(key, { target: { value: "2" } });
    assert.equal(changes.at(-1)[key], state.levels(key)[2]);
  }
  const before = changes.length;
  for (const invalid of ["existing", "3", "-1", "NaN"]) {
    state.chooseLevel("max_tokens", { target: { value: invalid } });
  }
  assert.equal(changes.length, before);
});

test("consultation history and output presets remain separate from casual group AI", t => {
  const { state } = mount(t, true);
  assert.deepEqual(state.levels("history_turns"), [2, 6, 12]);
  assert.deepEqual(state.levels("max_tokens"), [256, 512, 1024]);
});
