import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import ts from "typescript";
import { createRenderer, h, nextTick, ref } from "vue";
import { compileScript, compileStyle, compileTemplate, parse } from "vue/compiler-sfc";

const require = createRequire(import.meta.url);
const source = readFileSync(
  new URL("../plugin-pages/wangshangliao/MemberBlacklistPanel.vue", import.meta.url), "utf8",
);
const { descriptor } = parse(source);
const script = compileScript(descriptor, { id: "blacklist-test" });
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
const member = { id: "4", name: "同名成员" };
const entry = { ...member, peer: "1004", kicks: 3, phase: "mute", status: "unknown", cycle: 1 };
const flush = () => new Promise(resolve => setImmediate(resolve));

function mount(t, handler = responses, initial = {}) {
  const calls = [];
  const props = ref({ account: "1", group: "5", tenantVersion: 7, disabled: false, ...initial });
  let instance;
  const app = renderer.createApp({
    render: () => h({ ...component, render: () => null }, {
      ...props.value, ref: value => { instance = value; },
      api: async (action, payload) => {
        assert.equal(action, "member_blacklist");
        assert(!["uid", "id", "peer", "member"].some(key => key in payload));
        calls.push(structuredClone(payload));
        return handler(payload);
      },
    }),
  });
  app.mount({});
  t.after(() => app.unmount());
  return { calls, props, app, state: () => instance.$.setupState };
}

function responses(payload) {
  if (payload.operation === "read") return { version: 1, members: [entry], mute_minutes: 41760 };
  if (payload.operation === "search") return {
    selection: "selection", version: 1,
    members: [{ id: "3", name: member.name }, member],
  };
  if (payload.operation === "preview") return {
    token: "preview-token", version: 1, mode: "add", member,
  };
  if (payload.operation === "settings_preview") return {
    token: "settings-token", version: 1, mode: "settings", mute_minutes: payload.minutes,
  };
  return { saved: true, mode: "add", member: member.name };
}

async function select(state) {
  state().query = " 同名 ";
  await state().execute("search");
  state().chosen = 2;
}

test("compiles the accessible, bounded panel and integrates it beside whitelist", () => {
  assert.deepEqual(compileTemplate({
    source: descriptor.template.content, filename: "MemberBlacklistPanel.vue", id: "blacklist-test",
    compilerOptions: { bindingMetadata: script.bindings, expressionPlugins: ["typescript"] },
  }).errors, []);
  assert.deepEqual(compileStyle({
    source: descriptor.styles[0].content, filename: "MemberBlacklistPanel.vue",
    id: "blacklist-test", scoped: true,
  }).errors, []);
  assert.doesNotMatch(source, /v-html|api:\s*Function|:\s*any\b|username|user_name/);
  assert.match(source, /加入名单不立即处罚/);
  assert.match(source, /每次真实重新入群直接禁言/);
  assert.match(source, /时长仅影响以后重入，不重设已有禁言；移除不自动解禁/);
  assert.doesNotMatch(source, /踢出|踢人|member\.kicks|mute_29_days|发言或入群|等待后续发言/);
  const parent = readFileSync(new URL("../plugin-pages/wangshangliao/OpenService.vue", import.meta.url), "utf8");
  assert.match(parent, /<MemberBlacklistPanel v-if="view === 'rules'"/);
  assert.match(parent, /:api="blacklistApi" :account="tenant.account" :group="tenant.group_id"/);
  assert.match(parent, /const blacklistApi: MemberBlacklistApi = api/);
});

test("initialization and refresh only read, retaining absent members and unknown receipts", async t => {
  const { state, calls } = mount(t);
  await flush();
  assert.deepEqual(calls, [{ account: "1", group_id: "5", tenant_version: 7, operation: "read" }]);
  assert.equal(state().loaded, true);
  assert.deepEqual(JSON.parse(JSON.stringify(state().members)), [entry]);
  await state().execute("read");
  assert.deepEqual(calls.map(c => c.operation), ["read", "read"]);
  assert.equal(state().statuses.unknown, "结果未知 · 待核对，不自动重试");
  assert.equal(state().phases.mute, "重入禁言");
  assert.equal(state().muteMinutes, 41760);
  assert.equal(state().duration, 29);
  assert.equal(state().durationUnit, 1440);
});

for (const [savedMinutes, unit, value] of [[41760, 1440, 29], [120, 60, 2], [61, 1, 61], [1, 1, 1]]) {
  test(`read displays the server duration ${savedMinutes} without changing it`, async t => {
    const { state, calls } = mount(t, p => p.operation === "read"
      ? { version: 1, members: [], mute_minutes: savedMinutes } : responses(p));
    await flush();
    assert.equal(state().muteMinutes, savedMinutes);
    assert.equal(state().durationUnit, unit);
    assert.equal(state().duration, value);
    assert.equal(state().minutes, savedMinutes);
    assert.deepEqual(calls.map(c => c.operation), ["read"]);
  });
}

test("duration preview uses integer minutes and settings commits contain only the token", async t => {
  let savedMinutes = 41760;
  const { state, calls } = mount(t, p => {
    if (p.operation === "read") return { version: 1, members: [entry], mute_minutes: savedMinutes };
    if (p.operation === "settings_preview") return {
      token: "settings-token", version: 1, mode: "settings", mute_minutes: 90,
    };
    if (p.operation === "commit") {
      savedMinutes = 90;
      return { saved: true, mode: "settings", mute_minutes: savedMinutes };
    }
    return responses(p);
  });
  await flush();
  await select(state);
  state().duration = 1.5;
  state().durationUnit = 60;
  await state().execute("settings_preview");
  assert.deepEqual(calls.at(-1), {
    account: "1", group_id: "5", tenant_version: 7, operation: "settings_preview", minutes: 90,
  });
  assert.equal(state().muteMinutes, 41760);
  assert.equal(state().preview.mode, "settings");
  assert.equal(state().preview.mute_minutes, 90);
  assert.equal(state().results.length, 0);
  assert.equal(state().selection, "");
  await state().execute("commit");
  assert.deepEqual(calls.at(-2), {
    account: "1", group_id: "5", tenant_version: 7, operation: "commit", token: "settings-token",
  });
  assert.equal(state().notice, "已保存禁言时长：90 分钟");
  assert.equal(state().preview, null);
  assert.equal(state().muteMinutes, 90);
  assert.equal(state().duration, 90);
  assert.equal(state().durationUnit, 1);
  await state().execute("commit");
  assert.equal(calls.filter(c => c.operation === "commit").length, 1);
});

test("settings preview displays the server-confirmed value, not a client summary", async t => {
  const { state } = mount(t, p => p.operation === "settings_preview"
    ? { token: "settings-token", version: 2, mode: "settings", mute_minutes: 120 } : responses(p));
  await flush();
  state().duration = 1;
  state().durationUnit = 60;
  await state().execute("settings_preview");
  assert.equal(state().minutes, 60);
  assert.equal(state().preview.mute_minutes, 120);
});

test("member previews retain the server's current mute duration after a remote settings change", async t => {
  const { state } = mount(t, p => p.operation === "preview"
    ? { token: "member-token", version: 2, mode: "add", member, mute_minutes: 90 } : responses(p));
  await flush();
  await select(state);
  await state().execute("preview");
  assert.equal(state().muteMinutes, 41760);
  assert.equal(state().preview.mute_minutes, 90);
  assert.match(descriptor.template.content, /preview\.mute_minutes \?\? muteMinutes/);
});

test("pending settings requests serialize all writes and failed previews produce no confirmation", async t => {
  let finish;
  const { state, calls } = mount(t, p => p.operation === "settings_preview"
    ? new Promise((_, reject) => { finish = reject; }) : responses(p));
  await flush();
  const task = state().execute("settings_preview");
  await state().execute("settings_preview");
  await state().execute("commit");
  await state().execute("search");
  assert.equal(calls.length, 2);
  finish(new Error("blacklist_minutes"));
  await task;
  assert.equal(state().preview, null);
  assert.equal(state().pending, null);
  assert.equal(state().loaded, true);
  assert.match(state().error, /禁言时长须为1至41760整分钟/);
  await state().execute("commit");
  assert(!calls.some(c => c.operation === "commit"));
});

for (const [value, unit, expected] of [
  [1, 1, 1], [41760, 1, 41760], [696, 60, 41760], [29, 1440, 41760],
  [1.5, 60, 90], [0.5, 1440, 720], [0.575, 1440, 828],
  ["", 1, null], [0, 1, null], [-1, 1, null], [41761, 1, null],
  [30, 1440, null], [697, 60, null], [1.5, 1, null], [0.001, 60, null],
  [Infinity, 1, null], [NaN, 1, null], ["abc", 1, null],
]) {
  test(`duration ${value} × ${unit} previews only whole minutes within bounds`, async t => {
    const { state, calls } = mount(t);
    await flush();
    state().duration = value;
    state().durationUnit = unit;
    assert.equal(state().validDuration, expected !== null);
    await state().execute("settings_preview");
    assert.equal(calls.length, expected === null ? 1 : 2);
    if (expected !== null) assert.equal(calls.at(-1).minutes, expected);
  });
}

test("editing duration or its unit invalidates a settings confirmation without writing", async t => {
  const { state, calls } = mount(t);
  await flush();
  for (const edit of [{ duration: 2 }, { durationUnit: 60 }]) {
    await state().execute("settings_preview");
    assert.equal(state().preview.mode, "settings");
    Object.assign(state(), edit);
    assert.equal(state().preview, null);
    await state().execute("commit");
  }
  assert(!calls.some(c => c.operation === "commit"));
});

test("search, one-based homonym selection and preview require an explicit token-only commit", async t => {
  const { state, calls } = mount(t, responses);
  await flush();
  await state().execute("commit");
  await state().execute("preview");
  assert.equal(calls.length, 1);
  await select(state);
  assert.equal(calls[1].query, "同名");
  state().chosen = 3;
  await state().execute("preview");
  assert.equal(calls.length, 2);
  state().chosen = 2;
  await state().execute("preview");
  assert.equal(state().preview.member.id, "4");
  assert.deepEqual(calls.map(c => c.operation), ["read", "search", "preview"]);
  assert.deepEqual(calls[2], {
    account: "1", group_id: "5", tenant_version: 7, operation: "preview",
    selection: "selection", index: 2,
  });
  await state().execute("commit");
  assert.deepEqual(calls.map(c => c.operation), ["read", "search", "preview", "commit", "read"]);
  assert.deepEqual(calls[3], {
    account: "1", group_id: "5", tenant_version: 7, operation: "commit", token: "preview-token",
  });
  assert.equal(state().preview, null);
  assert.equal(state().notice, "已添加：同名成员");
  await state().execute("commit");
  assert.equal(calls.length, 5);
});

test("remove mode searches the saved blacklist and uses the server member name", async t => {
  const { state, calls } = mount(t, p => {
    const row = responses(p);
    return ["preview", "commit"].includes(p.operation) ? { ...row, mode: "remove" } : row;
  });
  await flush();
  state().mode = "remove";
  await select(state);
  assert.equal(calls[1].mode, "remove");
  await state().execute("preview");
  await state().execute("commit");
  assert.equal(state().notice, "已移除：同名成员");
});

test("empty searches never enable selection or confirmation", async t => {
  const { state, calls } = mount(t, p => p.operation === "search"
    ? { selection: "", version: 1, members: [] } : responses(p));
  await flush();
  state().query = "无匹配";
  await state().execute("search");
  assert.equal(state().searched, true);
  assert.equal(state().results.length, 0);
  state().chosen = 1;
  await state().execute("preview");
  await state().execute("commit");
  assert.deepEqual(calls.map(c => c.operation), ["read", "search"]);
});

for (const operation of ["search", "preview", "settings_preview"]) {
  for (const failure of [false, true]) {
    test(`cancelling ${operation} ignores its late ${failure ? "error" : "response"}`, async t => {
      let finish;
      const { state, calls } = mount(t, p => p.operation === operation
        ? new Promise((resolve, reject) => { finish = failure ? reject : resolve; }) : responses(p));
      await flush();
      if (operation === "preview") await select(state);
      else state().query = "同名";
      const task = state().execute(operation);
      assert.equal(state().pending, operation);
      state().clearSearch();
      finish(failure ? new Error("blacklist_changed") : responses({ operation, mode: "add" }));
      await task;
      assert.equal(state().pending, null);
      assert.equal(state().preview, null);
      assert.equal(state().results.length, 0);
      assert.equal(state().error, "");
      assert(!calls.some(c => c.operation === "commit"));
    });
  }
}

for (const operation of ["read", "search", "preview", "settings_preview", "commit"]) {
  test(`switching group discards a late ${operation} response`, async t => {
    let finish;
    let delay = false;
    const { state, props, calls } = mount(t, p => {
      if (delay && p.operation === operation && p.group_id === "5") {
        return new Promise(resolve => { finish = resolve; });
      }
      return p.operation === "read" && p.group_id === "6"
        ? { version: 2, members: [], mute_minutes: 60 } : responses(p);
    });
    await flush();
    if (["preview", "commit"].includes(operation)) await select(state);
    if (operation === "commit") await state().execute("preview");
    if (operation === "search") state().query = "同名";
    delay = true;
    const task = state().execute(operation);
    props.value.group = "6";
    await nextTick();
    await flush();
    finish(responses({ operation, mode: "add" }));
    await task;
    assert.equal(state().loaded, true);
    assert.equal(state().members.length, 0);
    assert.equal(state().preview, null);
    assert.equal(state().results.length, 0);
    assert.equal(state().notice, "");
    assert.equal(state().pending, null);
    assert.equal(state().muteMinutes, 60);
    assert(!calls.some(c => c.group_id === "6" && c.operation !== "read"));
  });
}

test("account/version changes and loss of write permission invalidate confirmations", async t => {
  const { state, props, calls } = mount(t, responses);
  await flush();
  for (const change of [{ account: "2" }, { tenantVersion: 8 }, { disabled: true }]) {
    await select(state);
    await state().execute("preview");
    Object.assign(props.value, change);
    await nextTick();
    await flush();
    assert.equal(state().preview, null);
    await state().execute("commit");
  }
  assert(!calls.some(c => c.operation === "commit"));
  const before = calls.length;
  await state().execute("search");
  assert.equal(calls.length, before);
});

test("settings tokens are invalidated by account/version changes and permission loss", async t => {
  const { state, props, calls } = mount(t);
  await flush();
  for (const change of [{ account: "2" }, { tenantVersion: 8 }, { disabled: true }]) {
    await state().execute("settings_preview");
    assert.equal(state().preview.mode, "settings");
    Object.assign(props.value, change);
    await nextTick();
    await flush();
    assert.equal(state().preview, null);
    await state().execute("commit");
  }
  assert(!calls.some(c => c.operation === "commit"));
  const before = calls.length;
  await state().execute("settings_preview");
  assert.equal(calls.length, before);
});

for (const permissionLost of [false, true]) {
  test(`late settings preview is ignored after ${permissionLost ? "permission loss" : "an input edit"}`, async t => {
    let finish;
    const { state, props, calls } = mount(t, p => p.operation === "settings_preview"
      ? new Promise(resolve => { finish = resolve; }) : responses(p));
    await flush();
    const task = state().execute("settings_preview");
    if (permissionLost) {
      props.value.disabled = true;
      await nextTick();
    } else state().duration = 2;
    finish({ token: "late-token", version: 1, mode: "settings", mute_minutes: 41760 });
    await task;
    assert.equal(state().preview, null);
    assert.equal(state().pending, null);
    await state().execute("commit");
    assert(!calls.some(c => c.operation === "commit"));
  });
}

test("failed settings commits consume the token and refresh instead of replaying", async t => {
  const { state, calls } = mount(t, p => {
    if (p.operation === "commit") throw new Error("timeout");
    return responses(p);
  });
  await flush();
  await state().execute("settings_preview");
  const commit = state().execute("commit");
  await state().execute("commit");
  await commit;
  assert.equal(state().preview, null);
  assert.equal(state().loaded, false);
  assert.match(state().error, /结果未确认.*不会自动重试/);
  await state().execute("settings_preview");
  await state().execute("read");
  await state().execute("commit");
  assert.equal(calls.filter(c => c.operation === "commit").length, 1);
  assert.equal(calls.filter(c => c.operation === "settings_preview").length, 1);
});

test("saved settings remain acknowledged when the follow-up read fails", async t => {
  let saved = false;
  const { state } = mount(t, p => {
    if (p.operation === "read" && saved) throw new Error("offline");
    if (p.operation === "commit") {
      saved = true;
      return { saved: true, mode: "settings", mute_minutes: 41760 };
    }
    return responses(p);
  });
  await flush();
  await state().execute("settings_preview");
  await state().execute("commit");
  assert.equal(state().notice, "已保存禁言时长：41760 分钟");
  assert.match(state().error, /变更已保存，但名单读取失败/);
  assert.equal(state().loaded, false);
  assert.equal(state().preview, null);
});

test("late settings commit cannot overwrite another group's duration", async t => {
  let finish;
  const { state, props, calls } = mount(t, p => {
    if (p.group_id === "6") return { version: 2, members: [], mute_minutes: 120 };
    if (p.operation === "commit") return new Promise(resolve => { finish = resolve; });
    return responses(p);
  });
  await flush();
  await state().execute("settings_preview");
  const task = state().execute("commit");
  props.value.group = "6";
  await nextTick();
  await flush();
  finish({ saved: true, mode: "settings", mute_minutes: 41760 });
  await task;
  assert.equal(state().muteMinutes, 120);
  assert.equal(state().duration, 2);
  assert.equal(state().durationUnit, 60);
  assert.equal(state().notice, "");
  assert.equal(state().pending, null);
  assert(!calls.some(c => c.group_id === "6" && c.operation !== "read"));
});

test("failed commits consume the confirmation and never retry it, even after refresh", async t => {
  const { state, calls } = mount(t, p => {
    if (p.operation === "commit") throw new Error("timeout");
    return responses(p);
  });
  await flush();
  await select(state);
  await state().execute("preview");
  await state().execute("commit");
  assert.match(state().error, /结果未确认.*不会自动重试/);
  assert.equal(state().loaded, false);
  assert.equal(state().members.length, 0);
  assert.equal(state().preview, null);
  await state().execute("commit");
  await state().execute("read");
  await state().execute("commit");
  assert.equal(calls.filter(c => c.operation === "commit").length, 1);
});

test("a failed post-commit read does not disguise a saved change or keep stale rows", async t => {
  let saved = false;
  const { state } = mount(t, p => {
    if (p.operation === "read" && saved) throw new Error("offline");
    if (p.operation === "commit") saved = true;
    return responses(p);
  });
  await flush();
  await select(state);
  await state().execute("preview");
  await state().execute("commit");
  assert.equal(state().notice, "已添加：同名成员");
  assert.match(state().error, /变更已保存，但名单读取失败/);
  assert.equal(state().loaded, false);
  assert.equal(state().members.length, 0);
});

test("switching group while refreshing a saved change rejects the old follow-up read", async t => {
  let saved = false;
  let finish;
  const { state, props } = mount(t, p => {
    if (p.group_id === "6") return { version: 2, members: [], mute_minutes: 41760 };
    if (p.operation === "commit") saved = true;
    if (p.operation === "read" && saved) return new Promise(resolve => { finish = resolve; });
    return responses(p);
  });
  await flush();
  await select(state);
  await state().execute("preview");
  const commit = state().execute("commit");
  await flush();
  props.value.group = "6";
  await nextTick();
  await flush();
  finish({ version: 2, members: [entry], mute_minutes: 41760 });
  await commit;
  assert.equal(state().members.length, 0);
  assert.equal(state().notice, "");
  assert.equal(state().pending, null);
  assert.equal(state().loaded, true);
});

test("failed reads show no empty-list success and require a successful refresh before searching", async t => {
  let fail = true;
  const { state, calls } = mount(t, p => {
    if (fail) throw new Error("blacklist_permission");
    return responses(p);
  });
  await flush();
  assert.equal(state().loaded, false);
  assert.match(state().error, /不可管理/);
  state().query = "同名";
  await state().execute("search");
  await state().execute("settings_preview");
  assert.equal(calls.length, 1);
  fail = false;
  await state().execute("read");
  assert.equal(state().loaded, true);
  assert.equal(state().error, "");
});

test("unmount and query changes reject stale results without reviving controls", async t => {
  let finish;
  const { state, app } = mount(t, p => p.operation === "search"
    ? new Promise(resolve => { finish = resolve; }) : responses(p));
  await flush();
  const setup = state();
  setup.query = "同名";
  const task = setup.execute("search");
  setup.query = "新搜索";
  finish(responses({ operation: "search", mode: "add" }));
  await task;
  assert.equal(setup.results.length, 0);
  const unmounted = setup.execute("search");
  app.unmount();
  finish(responses({ operation: "search", mode: "add" }));
  await unmounted;
  assert.equal(setup.results.length, 0);
});
