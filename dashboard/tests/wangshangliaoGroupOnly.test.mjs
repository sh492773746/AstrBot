import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import ts from "typescript";
import { createRenderer, h, nextTick } from "vue";
import { compileScript, compileTemplate, parse } from "vue/compiler-sfc";

const require = createRequire(import.meta.url);
const root = new URL("../plugin-pages/wangshangliao/", import.meta.url);
function load(name) {
  const source = readFileSync(new URL(`${name}.vue`, root), "utf8");
  const { descriptor } = parse(source);
  const script = compileScript(descriptor, { id: `group-only-${name}` });
  assert.deepEqual(compileTemplate({
    source: descriptor.template.content, filename: `${name}.vue`, id: `group-only-${name}`,
    compilerOptions: { bindingMetadata: script.bindings, expressionPlugins: ["typescript"] },
  }).errors, []);
  const module = { exports: {} };
  new Function("require", "module", "exports", ts.transpileModule(script.content, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText)(require, module, module.exports);
  return module.exports.default;
}
const CommandHelp = load("CommandHelp");
const Examples = load("ConversationExamples");
const renderer = createRenderer({
  createComment: () => ({}), insert() {}, remove() {},
  parentNode: () => null, nextSibling: () => null,
});
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

test("the command directory drops private-only rows even from a legacy backend", async t => {
  const calls = [];
  const state = mount(t, CommandHelp, { locale: "zh-CN", bridge: {
    async apiPost(endpoint, request) {
      calls.push({ endpoint, request });
      return { commands: [
        { command: "规则", role: "group_manager", locations: ["group", "private"] },
        { command: "/chat", role: "public", locations: ["private"] },
        { command: "客服设置", role: "support_editor", locations: ["private"] },
        { command: "群模型", role: "server", locations: ["group"] },
      ] };
    },
  } });
  await new Promise(setImmediate);
  state().all = true;
  await nextTick();
  assert.deepEqual(Object.keys(state().places), ["group", "server"]);
  assert.deepEqual(state().commands.map(row => row.command), ["规则"]);
  state().place = "server";
  assert.deepEqual(state().commands.map(row => row.command), ["群模型"]);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].request.action, "command_capabilities");
});

test("examples cover group activities and WebUI settlement without a private route", t => {
  const state = mount(t, Examples, { locale: "zh-CN" });
  assert.equal(state().activity, "lottery");
  assert(state().messages.some(row => row.text === "参与抽奖"));
  state().activity = "invites";
  assert(state().messages.some(row => row.text === "我的邀请"));
  assert(state().messages.some(row => row.text.includes("邀请记账／结账")));
  assert.doesNotMatch(JSON.stringify(state().messages), /私聊|私信|\/chat|\/exit/);
});

test("reference tables contain only group callers and WebUI responsibilities", () => {
  const reference = JSON.parse(readFileSync(new URL("reference-tables.json", root), "utf8"));
  assert.doesNotMatch(JSON.stringify(reference), /private|好友|私聊|私信|\/chat|\/exit|consultation|guidance/i);
  assert(reference.architecture.some(row => row.id === "raid-defense"));
  assert(reference.architecture.some(row => row.id === "dashboard"));
  for (const row of reference.permissions) {
    assert.equal(row.access.length, 2);
    assert.equal(row.name.length, 2);
    assert.equal(row.rule.length, 2);
  }
  for (const name of ["FriendApproval", "SupportSettingsPanel", "FinanceGrantsPanel"]) {
    assert.equal(existsSync(new URL(`${name}.vue`, root)), false);
  }
});
