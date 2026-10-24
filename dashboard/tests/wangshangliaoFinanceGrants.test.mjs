import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import ts from "typescript";
import { createRenderer, nextTick } from "vue";
import { compileScript, compileStyle, compileTemplate, parse } from "vue/compiler-sfc";

const require = createRequire(import.meta.url);
const source = readFileSync(
  new URL("../plugin-pages/wangshangliao/FinanceGrantsPanel.vue", import.meta.url), "utf8",
);
const { descriptor } = parse(source);
const script = compileScript(descriptor, { id: "finance-grants-test" });
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
const groups = [
  { account: "bot-a", group_id: "group-1", name: "Group A", configured: true, bot_id: "instance-a" },
  { account: "bot-b", group_id: "group-1", name: "Group B", configured: true, bot_id: "instance-b" },
  { account: "bot-c", group_id: "group-2", name: "Unconfigured", configured: false, bot_id: "instance-c" },
];
const grant = {
  account: "bot-a", group_id: "group-1", uid: "member-1", peer: "peer-1",
  name: "Same Name", role: "cashier", active: true, version: 7,
};
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const key = group => JSON.stringify([group.account, group.group_id]);
const settle = async () => { await new Promise(setImmediate); await nextTick(); };

function mountPanel(t, handler = () => undefined) {
  const data = { version: 1, grants: [], groups: structuredClone(groups), test_active: false };
  const calls = [];
  const bridge = {
    apiGet() { throw new Error("Finance reads must use the POST action contract"); },
    async apiPost(endpoint, body) {
      assert.equal(endpoint, "open-service");
      const { action, payload } = structuredClone(body);
      calls.push({ action, payload });
      const override = handler(action, payload, data);
      if (override !== undefined) return override;
      if (action === "finance_status") return structuredClone(data);
      if (action === "finance_members") return {
        members: [{ uid: "member-1", name: "Same Name" }, { uid: "member-2", name: "Same Name" }],
      };
      assert.equal(action, "finance_grant");
      const saved = data.grants.find(row =>
        row.account === payload.account && row.group_id === payload.group_id
        && row.uid === payload.uid,
      );
      assert.equal(payload.version, saved?.version ?? 0);
      if (saved) Object.assign(saved, { role: payload.role, active: payload.active, version: saved.version + 1 });
      else data.grants.push({ ...payload, version: 1, peer: "server-peer", name: "Server Name" });
      return { status: "saved" };
    },
  };
  // Mount the real setup with no DOM; template and styles are compiled separately.
  const app = renderer.createApp({ ...component, render: () => null }, { bridge });
  const instance = app.mount({});
  t.after(() => app.unmount());
  return { state: instance.$.setupState, calls, data };
}

test("compiles the compact panel and keeps finance authorization separate from group management", () => {
  assert.deepEqual(compileTemplate({
    source: descriptor.template.content, filename: "FinanceGrantsPanel.vue",
    id: "finance-grants-test", compilerOptions: { bindingMetadata: script.bindings },
  }).errors, []);
  assert.deepEqual(compileStyle({
    source: descriptor.styles[0].content, filename: "FinanceGrantsPanel.vue",
    id: "finance-grants-test", scoped: true,
  }).errors, []);
  assert.deepEqual(
    [...descriptor.template.content.matchAll(/<select v-model="([^"]+)"/g)].map(match => match[1]),
    ["scopeKey", "uid", "role"],
  );
  assert.doesNotMatch(descriptor.template.content, /<(?:input|textarea)\b/);
  assert.match(descriptor.template.content, /:disabled="!canSave"/);
  assert.match(descriptor.template.content, /<UserMinus/);
  assert.match(descriptor.template.content, /group\.configured \? "" : " · 未配置"/);
  assert.doesNotMatch(descriptor.template.content, /:disabled="!group\.configured"/);
  assert.match(source, /结账员/);
  assert.doesNotMatch(source, /收银员/);
  assert.doesNotMatch(source, /finance_test|test_toggle|tenant_members/);
  const parent = readFileSync(new URL("../plugin-pages/wangshangliao/OpenService.vue", import.meta.url), "utf8");
  assert.equal((parent.match(/<FinanceGrantsPanel\b/g) || []).length, 1);
  assert.match(parent, /<FinanceGrantsPanel v-if="visited\.has\('finance'\)" v-show="view === 'finance'" :bridge="bridge"/);
});

test("requires a listed scope, fresh server member, and role; never trusts a typed UID or name", async t => {
  const { state, calls } = mountPanel(t);
  await settle();
  assert.deepEqual(calls, [{ action: "finance_status", payload: {} }]);
  assert.equal(state.canSave, false);
  await state.changeGrant();
  state.scopeKey = key({ account: "unknown-account", group_id: "unknown-group" });
  await settle();
  assert.equal(calls.length, 1);
  state.scopeKey = key(groups[0]);
  assert.equal(state.membersReady, false);
  await state.changeGrant();
  await settle();
  assert.deepEqual(calls[1], {
    action: "finance_members", payload: { account: "bot-a", group_id: "group-1" },
  });
  for (const uid of ["", "Same Name", "self-declared-uid"]) {
    state.uid = uid;
    state.role = "cashier";
    assert.equal(state.canSave, false);
    await state.changeGrant();
  }
  assert.equal(calls.filter(call => call.action === "finance_grant").length, 0);
  state.uid = "member-2";
  assert.equal(state.role, "");
  state.role = "not-a-role";
  assert.equal(state.canSave, false);
  state.role = "cashier";
  assert.equal(state.canSave, true);
  await state.changeGrant();
  const payload = calls.find(call => call.action === "finance_grant").payload;
  assert.deepEqual({ ...payload, request_id: "uuid" }, {
    account: "bot-a", group_id: "group-1", uid: "member-2",
    role: "cashier", active: true, version: 0, request_id: "uuid",
  });
  assert.match(payload.request_id, uuid);
  assert.equal(state.canSave, false);
});

test("scope switches discard stale members and stale errors, including overlapping requests", async t => {
  const pending = [];
  const { state } = mountPanel(t, action => {
    if (action === "finance_members") return new Promise((resolve, reject) => pending.push({ resolve, reject }));
  });
  await settle();
  state.scopeKey = key(groups[0]);
  state.scopeKey = key(groups[1]);
  pending[0].resolve({ members: [{ uid: "old-member", name: "Old" }] });
  await settle();
  assert.equal(state.membersReady, false);
  assert.deepEqual(state.members, []);
  pending[1].resolve({ members: [{ uid: "current-member", name: "Current" }] });
  await settle();
  assert.equal(state.membersReady, true);
  assert.equal(state.members[0].uid, "current-member");
  state.uid = "current-member";
  state.role = "finance_admin";
  state.scopeKey = key(groups[0]);
  assert.equal(state.uid, "");
  assert.equal(state.role, "");
  assert.equal(state.canSave, false);
  state.scopeKey = key(groups[1]);
  pending[2].reject(new Error("Stale member failure"));
  await settle();
  assert.equal(state.error, "");
  assert.equal(state.membersLoading, true);
  pending[3].resolve({ members: [] });
  await settle();
  assert.equal(state.membersReady, true);
  assert.equal(state.canSave, false);
});

test("failed member reads keep submission disabled until refreshed", async t => {
  let fail = true;
  const { state, calls } = mountPanel(t, action => {
    if (action === "finance_members" && fail) return Promise.reject(new Error("Members unavailable"));
  });
  await settle();
  state.scopeKey = key(groups[0]);
  await settle();
  assert.equal(state.error, "Members unavailable");
  assert.equal(state.membersReady, false);
  state.uid = "member-1";
  state.role = "cashier";
  await state.changeGrant();
  assert.equal(calls.filter(call => call.action === "finance_grant").length, 0);
  fail = false;
  await state.load();
  assert.equal(state.membersReady, true);
  assert.equal(state.uid, "");
});

test("uses versions from the exact account, group and member, including revoked grants", async t => {
  const { state, calls, data } = mountPanel(t);
  data.grants.push(
    { ...grant, active: false },
    { ...grant, account: "bot-b", version: 31 },
    { ...grant, group_id: "other-group", version: 43 },
    { ...grant, uid: "member-2", version: 57 },
  );
  await settle();
  await state.load();
  state.scopeKey = key(groups[0]);
  await settle();
  state.uid = "member-1";
  state.role = "cashier";
  await state.changeGrant();
  const first = calls.filter(call => call.action === "finance_grant")[0].payload;
  assert.equal(first.version, 7);
  assert.equal(first.active, true);
  state.uid = "member-2";
  state.role = "cashier";
  await state.changeGrant();
  const second = calls.filter(call => call.action === "finance_grant")[1].payload;
  assert.equal(second.version, 57);
  assert.notEqual(first.request_id, second.request_id);
});

test("role changes reuse the existing member grant version rather than creating a new grant", async t => {
  const { state, calls, data } = mountPanel(t);
  data.grants.push({ ...grant });
  await settle();
  await state.load();
  state.scopeKey = key(groups[0]);
  await settle();
  state.uid = "member-1";
  state.role = "finance_admin";
  await state.changeGrant();
  const first = calls.find(call => call.action === "finance_grant").payload;
  assert.equal(first.version, 7);
  assert.equal(first.role, "finance_admin");
  assert.equal(state.pending, null);
  assert.equal(data.grants.length, 1);
  assert.equal(state.status.grants[0].role, "finance_admin");
  assert.equal(state.status.grants[0].version, 8);
  state.uid = "member-1";
  state.role = "cashier";
  await state.changeGrant();
  const second = calls.filter(call => call.action === "finance_grant")[1].payload;
  assert.equal(second.version, 8);
  assert.equal(second.role, "cashier");
  assert.notEqual(first.request_id, second.request_id);
  assert.equal(data.grants.length, 1);
  assert.equal(state.status.grants[0].role, "cashier");
  assert.equal(state.status.grants[0].version, 9);
});

test("allows the first finance admin grant in an unconfigured group and preserves its scope on refresh", async t => {
  const { state, calls, data } = mountPanel(t);
  await settle();
  state.scopeKey = key(groups[2]);
  assert.equal(state.membersLoading, true);
  assert.equal(state.canSave, false);
  await settle();
  assert.deepEqual(calls[1], {
    action: "finance_members", payload: { account: "bot-c", group_id: "group-2" },
  });
  assert.equal(state.membersReady, true);
  await state.load();
  assert.equal(state.scopeKey, key(groups[2]));
  assert.equal(state.membersReady, true);
  state.uid = "member-1";
  state.role = "finance_admin";
  assert.equal(state.canSave, true);
  await state.changeGrant();
  const payload = calls.find(call => call.action === "finance_grant").payload;
  assert.deepEqual({ ...payload, request_id: "uuid" }, {
    account: "bot-c", group_id: "group-2", uid: "member-1",
    role: "finance_admin", active: true, version: 0, request_id: "uuid",
  });
  assert.match(payload.request_id, uuid);
  assert.equal(state.pending, null);
  assert.equal(state.scopeKey, key(groups[2]));
  assert.equal(state.scope.configured, false);
  assert.equal(data.groups[2].configured, false);
  assert.equal(state.status.grants[0].role, "finance_admin");
  assert.equal(state.status.grants[0].version, 1);
});

test("retries ambiguous writes with the same UUID and payload and blocks concurrent writes", async t => {
  let finish;
  let attempts = 0;
  const { state, calls } = mountPanel(t, action => {
    if (action === "finance_grant") {
      attempts++;
      if (attempts === 1) return new Promise((_resolve, reject) => { finish = reject; });
    }
  });
  await settle();
  state.scopeKey = key(groups[0]);
  await settle();
  state.uid = "member-1";
  state.role = "cashier";
  const saving = state.changeGrant();
  assert.equal(state.saving, true);
  await state.changeGrant();
  await state.submit();
  assert.equal(attempts, 1);
  finish(new Error("Write outcome unknown"));
  await saving;
  assert.equal(state.locked, true);
  assert.equal(state.canSave, false);
  assert.match(state.pending.request_id, uuid);
  const request = structuredClone(calls.find(call => call.action === "finance_grant").payload);
  await state.changeGrant();
  assert.equal(attempts, 1);
  await state.submit();
  const writes = calls.filter(call => call.action === "finance_grant");
  assert.deepEqual(writes.map(call => call.payload), [request, request]);
  assert.equal(state.pending, null);
  assert.equal(state.status.grants[0].version, 1);
});

test("revokes only the confirmed exact row, independently of selected scope or current membership", async t => {
  const confirmations = [];
  const originalWindow = globalThis.window;
  let confirmed = false;
  globalThis.window = { confirm: message => { confirmations.push(message); return confirmed; } };
  t.after(() => { globalThis.window = originalWindow; });
  const { state, calls, data } = mountPanel(t);
  data.grants.push({ ...grant, account: "bot-b" }, { ...grant, role: "finance_admin", version: 19 });
  await settle();
  await state.load();
  state.scopeKey = key(groups[1]);
  await settle();
  const row = state.status.grants[1];
  await state.changeGrant(row);
  assert.equal(calls.filter(call => call.action === "finance_grant").length, 0);
  confirmed = true;
  await state.changeGrant({ ...row, version: 18 });
  assert.equal(calls.filter(call => call.action === "finance_grant").length, 0);
  await state.changeGrant(row);
  const payload = calls.find(call => call.action === "finance_grant").payload;
  assert.deepEqual({ ...payload, request_id: "uuid" }, {
    account: "bot-a", group_id: "group-1", uid: "member-1",
    role: "finance_admin", active: false, version: 19, request_id: "uuid",
  });
  assert.match(payload.request_id, uuid);
  assert.ok(confirmations.every(message => message.includes("bot-a") && message.includes("member-1")));
  assert.equal(data.grants[0].active, true);
  assert.equal(data.grants[1].active, false);
});

test("unsupported or failed status refreshes fail closed without permitting stale grant writes", async t => {
  let unavailable = false;
  const { state, calls } = mountPanel(t, action => {
    if (action === "finance_status" && unavailable) return { version: 2, grants: [], groups };
  });
  await settle();
  state.scopeKey = key(groups[0]);
  await settle();
  state.uid = "member-1";
  state.role = "cashier";
  assert.equal(state.canSave, true);
  unavailable = true;
  await state.load();
  assert.equal(state.status, null);
  assert.equal(state.canSave, false);
  await state.changeGrant();
  assert.equal(calls.filter(call => call.action === "finance_grant").length, 0);
});
