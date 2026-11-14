import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import ts from "typescript";
import { createRenderer, h, nextTick, ref } from "vue";
import { compileScript, compileTemplate, parse } from "vue/compiler-sfc";

const require = createRequire(import.meta.url);
const { descriptor } = parse(readFileSync(
  new URL("../plugin-pages/wangshangliao/WelcomePanel.vue", import.meta.url), "utf8",
));
const script = compileScript(descriptor, { id: "welcome" });
const module = { exports: {} };
new Function("require", "module", "exports", ts.transpileModule(script.content, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText)(require, module, module.exports);
const renderer = createRenderer({
  createComment: () => ({}), insert() {}, remove() {},
  parentNode: () => null, nextSibling: () => null,
});
const flush = async () => { await nextTick(); await new Promise(setImmediate); };
function mount(t, handler, disabled = false) {
  const props = ref({ account: "99", group: "5", tenantVersion: 1, disabled });
  const calls = [];
  let instance;
  const app = renderer.createApp({
    render: () => h({ ...module.exports.default, render: () => null }, {
      ...props.value, ref: value => { instance = value; },
      api: async (action, payload) => {
        assert.equal(action, "group_welcome");
        calls.push(structuredClone(payload));
        return handler(payload);
      },
    }),
  });
  app.mount({});
  t.after(() => app.unmount());
  return { props, calls, state: () => instance.$.setupState };
}
const saved = { version: 1, config: { enabled: false, text: "欢迎" }, last: null };
test("welcome compiles accessible controls and escaped confirmation", () => {
  assert.deepEqual(compileTemplate({
    source: descriptor.template.content, filename: "WelcomePanel.vue", id: "welcome",
    compilerOptions: { bindingMetadata: script.bindings, expressionPlugins: ["typescript"] },
  }).errors, []);
  assert(!descriptor.template.content.includes("v-html"));
});
test("initial read is allowed during parent loading, mutations are not", async t => {
  const { state, calls } = mount(t, () => structuredClone(saved), true);
  await flush();
  assert.equal(state().ready, true);
  await state().run("preview");
  assert.deepEqual(calls.map(c => c.operation), ["read"]);
});
test("preview freezes a copy and does not commit until confirmation", async t => {
  const { state, calls } = mount(t, p => p.operation === "preview"
    ? { token: "token", config: structuredClone(p.config) } : structuredClone(saved));
  await flush();
  state().config.enabled = true;
  await state().run("preview");
  assert.equal(state().preview.config.enabled, true);
  assert.deepEqual(calls.map(c => c.operation), ["read", "preview"]);
  await state().run("commit");
  assert.deepEqual(calls.map(c => c.operation), ["read", "preview", "commit", "read"]);
  assert.equal(calls[2].token, "token");
  assert.equal(state().preview, null);
});
test("late preview from a previous group cannot appear in the new group", async t => {
  let resolve;
  const { state, props } = mount(t, p => p.operation === "preview"
    ? new Promise(done => { resolve = done; }) : structuredClone(saved));
  await flush();
  const pending = state().run("preview");
  props.value.group = "6";
  await flush();
  resolve({ token: "old", config: { enabled: true, text: "旧群欢迎" } });
  await pending;
  assert.equal(state().preview, null);
  assert.equal(state().config.text, "欢迎");
});
test("a failed read never enables editing unsaved defaults", async t => {
  const { state } = mount(t, () => { throw new Error("目录未核验"); });
  await flush();
  assert.equal(state().ready, false);
  assert.equal(state().error, "目录未核验");
});
