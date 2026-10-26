import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import ts from "typescript";
import { createRenderer, h, nextTick, ref } from "vue";
import { compileScript, compileStyle, compileTemplate, parse } from "vue/compiler-sfc";

const require = createRequire(import.meta.url);
const renderer = createRenderer({
  createComment: () => ({}), insert() {}, remove() {},
  parentNode: () => null, nextSibling: () => null,
});
const member = { id: "12", name: "", conflict: true };
const other = { id: "13", name: "同名成员" };
const flush = () => new Promise(resolve => setImmediate(resolve));
const conflictPrefix = "名单未修改：该成员已在本群另一份名单中，请先移除另一名单，再重新添加。";
const conflictMessage = `${conflictPrefix}黑白名单不能同时添加，历史记录不会自动删除。`;

for (const kind of ["Whitelist", "Blacklist"]) {
  const action = kind === "Whitelist" ? "member_whitelist" : "member_blacklist";
  const filename = `Member${kind}Panel.vue`;
  const source = readFileSync(new URL(`../plugin-pages/wangshangliao/${filename}`, import.meta.url), "utf8");
  const { descriptor } = parse(source);
  const script = compileScript(descriptor, { id: `member-${kind}` });
  const compiled = ts.transpileModule(script.content, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  });
  const module = { exports: {} };
  new Function("require", "module", "exports", compiled.outputText)(require, module, module.exports);
  const component = module.exports.default;

  function responses(p) {
    if (p.operation === "read") return {
      version: 1, members: p.group_id === "6" ? [] : [member, other], mute_minutes: 41760,
    };
    if (p.operation === "search") return { version: 1, selection: "selection-token", members: [other, member] };
    if (p.operation === "preview") return { token: "preview-token", version: 1, mode: "remove", member };
    if (p.operation === "commit") return { saved: true, mode: "remove", member: "已离群成员" };
    throw new Error(`Unexpected operation: ${p.operation}`);
  }

  function mount(t, handler = responses, initial = {}) {
    const calls = [];
    const props = ref({ account: "1", group: "5", tenantVersion: 7, disabled: false, ...initial });
    let instance;
    const app = renderer.createApp({
      render: () => h({ ...component, render: () => null }, {
        ...props.value, ref: value => { instance = value; },
        api: async (name, payload) => {
          assert.equal(name, action);
          assert(!["id", "uid", "peer", "member"].some(key => key in payload));
          calls.push(structuredClone(payload));
          return handler(payload);
        },
      }),
    });
    app.mount({});
    t.after(() => app.unmount());
    return { calls, props, app, state: () => instance.$.setupState };
  }

  test(`${kind}: compiles icon removals, conflict labels and confirmation without untyped API`, () => {
    assert.deepEqual(compileTemplate({
      source: descriptor.template.content, filename, id: `member-${kind}`,
      compilerOptions: { bindingMetadata: script.bindings, expressionPlugins: ["typescript"] },
    }).errors, []);
    assert.deepEqual(compileStyle({
      source: descriptor.styles[0].content, filename, id: `member-${kind}`, scoped: true,
    }).errors, []);
    assert.match(source, /<UserMinus/);
    assert.match(source, /title="移出[黑白]名单"/);
    assert.match(source, /:aria-label="`将/);
    assert.match(source, /member\.conflict/);
    assert.match(source, /已离群成员/);
    if (kind === "Whitelist") {
      assert.match(source, /昨日前30名 · 昨日/);
      assert.doesNotMatch(source, /今日前30名/);
    }
    assert.doesNotMatch(source, /api:\s*Function|:\s*any\b|v-html/);
  });

  test(`${kind}: a listed row searches its UID, selects exact identity then waits for confirmation`, async t => {
    let saved = false;
    const { state, calls } = mount(t, p => {
      if (p.operation === "commit") saved = true;
      return p.operation === "read" && saved
        ? { version: 2, members: [other], mute_minutes: 41760 } : responses(p);
    });
    await flush();
    assert.equal(state().members[0].conflict, true);
    assert.deepEqual(calls.map(p => p.operation), ["read"]);
    await state().execute("search", member);
    assert.deepEqual(calls.map(p => p.operation), ["read", "search", "preview"]);
    assert.deepEqual(calls[1], {
      account: "1", group_id: "5", tenant_version: 7,
      operation: "search", mode: "remove", query: "12",
    });
    assert.deepEqual(calls[2], {
      account: "1", group_id: "5", tenant_version: 7,
      operation: "preview", selection: "selection-token", index: 2,
    });
    assert.equal(state().preview.member.id, "12");
    assert.equal(state().results.length, 0);
    assert.equal(state().members.length, 2);
    await state().execute("commit");
    assert.deepEqual(calls[3], {
      account: "1", group_id: "5", tenant_version: 7,
      operation: "commit", token: "preview-token",
    });
    assert.deepEqual(calls.map(p => p.operation), ["read", "search", "preview", "commit", "read"]);
    assert.equal(state().notice, "已移除：已离群成员");
    assert.deepEqual(JSON.parse(JSON.stringify(state().members)), [other]);
    await state().execute("commit");
    assert.equal(calls.length, 5);
  });

  test(`${kind}: arbitrary rows, missing choices and unconfirmed commits never make requests`, async t => {
    const { state, calls } = mount(t);
    await flush();
    await state().execute("commit");
    await state().execute("preview");
    await state().execute("search", { id: "999", name: "伪造" });
    assert.equal(calls.length, 1);
    state().query = "同名";
    await state().execute("search");
    for (const choice of [0, -1, 3, 1.5]) {
      state().chosen = choice;
      await state().execute("preview");
    }
    assert.equal(calls.length, 2);
  });

  for (const matches of [[], [other], [member, member]]) {
    test(`${kind}: missing or ambiguous UID matches never generate a preview (${matches.length})`, async t => {
      const { state, calls } = mount(t, p => p.operation === "search"
        ? { version: 1, selection: "selection-token", members: matches } : responses(p));
      await flush();
      await state().execute("search", member);
      assert.equal(state().preview, null);
      assert.match(state().error, /已变化/);
      assert.deepEqual(calls.map(p => p.operation), ["read", "search"]);
    });
  }

  for (const confirmation of [
    { token: "wrong", mode: "add", member },
    { token: "wrong", mode: "remove", member: other },
    { token: "", mode: "remove", member },
  ]) {
    test(`${kind}: a mismatched removal preview cannot become a commit (${confirmation.mode}/${confirmation.member.id}/${confirmation.token})`, async t => {
      const { state, calls } = mount(t, p => p.operation === "preview" ? confirmation : responses(p));
      await flush();
      await state().execute("search", member);
      await state().execute("commit");
      assert.equal(state().preview, null);
      assert.match(state().error, /已变化/);
      assert(!calls.some(p => p.operation === "commit"));
    });
  }

  for (const operation of ["search", "preview"]) {
    for (const change of ["cancel", "group", "account", "version", "permission", "query", "unmount"]) {
      test(`${kind}: ${change} invalidates a late row-removal ${operation}`, async t => {
        let finish;
        const { state, props, calls, app } = mount(t, p => p.operation === operation
          ? new Promise(resolve => { finish = () => resolve(responses(p)); }) : responses(p));
        await flush();
        const setup = state();
        const task = setup.execute("search", member);
        await flush();
        assert.equal(setup.pending, operation);
        if (change === "cancel") setup.clearSearch();
        else if (change === "query") setup.query = "新搜索";
        else if (change === "unmount") app.unmount();
        else {
          Object.assign(props.value, change === "group" ? { group: "6" }
            : change === "account" ? { account: "2" }
              : change === "version" ? { tenantVersion: 8 } : { disabled: true });
          await nextTick();
          await flush();
        }
        finish();
        await task;
        assert.equal(setup.preview, null);
        assert.equal(setup.results.length, 0);
        assert.equal(setup.error, "");
        if (operation === "search") assert(!calls.some(p => p.operation === "preview"));
        assert(!calls.some(p => p.operation === "commit"));
      });
    }
  }

  test(`${kind}: a double row click cannot duplicate search/preview; failed commits never replay`, async t => {
    let finish;
    const { state, calls } = mount(t, p => {
      if (p.operation === "search") return new Promise(resolve => { finish = () => resolve(responses(p)); });
      if (p.operation === "commit") throw new Error("timeout");
      return responses(p);
    });
    await flush();
    const task = state().execute("search", member);
    await state().execute("search", other);
    assert.equal(calls.filter(p => p.operation === "search").length, 1);
    finish();
    await task;
    const commit = state().execute("commit");
    await state().execute("commit");
    await commit;
    assert.equal(state().preview, null);
    assert.equal(state().loaded, false);
    assert.equal(state().members.length, 0);
    assert.match(state().error, /结果未确认.*不会自动重试/);
    await state().execute("search", member);
    await state().execute("read");
    await state().execute("commit");
    assert.equal(calls.filter(p => p.operation === "commit").length, 1);
  });

  for (const operation of ["read", "commit", "follow-up"]) {
    test(`${kind}: switching group rejects a late ${operation} response`, async t => {
      let finish;
      let saved = false;
      let delayed = operation === "read";
      const { state, props, calls } = mount(t, p => {
        if (p.group_id === "6") return responses(p);
        if (delayed && (p.operation === operation || (operation === "follow-up" && saved && p.operation === "read"))) {
          return new Promise(resolve => { finish = () => resolve(responses(p)); });
        }
        if (p.operation === "commit") saved = true;
        return responses(p);
      });
      if (operation !== "read") {
        await flush();
        await state().execute("search", member);
        delayed = true;
      }
      const task = operation === "read" ? undefined : state().execute("commit");
      await flush();
      props.value.group = "6";
      await nextTick();
      await flush();
      finish();
      await task;
      await flush();
      assert.equal(state().preview, null);
      assert.equal(state().loaded, true);
      assert.equal(state().members.length, 0);
      assert.equal(state().notice, "");
      assert.equal(state().pending, null);
      assert(!calls.some(p => p.group_id === "6" && p.operation !== "read"));
    });
  }

  for (const [form, message] of [
    ["code", "member_list_conflict"], ["ApiError", conflictMessage],
    ["ApiError with untrusted suffix", `${conflictPrefix} private-debug-secret <script>bad()</script>`],
  ]) {
    for (const operation of ["search", "preview", "commit"]) {
      test(`${kind}: ${operation} ${form} conflicts explicitly require removing the other list first`, async t => {
        const { state, calls } = mount(t, p => {
          if (p.operation === operation) throw new Error(message);
          return responses(p);
        });
        await flush();
        await state().execute("search", member);
        if (operation === "commit") await state().execute("commit");
        assert.equal(state().error, kind === "Whitelist"
          ? "该成员已在黑名单，请先移出黑名单，再加入白名单。"
          : "该成员已在白名单，请先移出白名单，再加入黑名单。");
        assert.equal(state().preview, null);
        assert.equal(calls.filter(p => p.operation === "commit").length, operation === "commit" ? 1 : 0);
        await state().execute("commit");
        assert.equal(calls.filter(p => p.operation === "commit").length, operation === "commit" ? 1 : 0);
      });
    }
  }

  for (const message of [
    "private-debug-secret <script>bad()</script>",
    "名单未修改：未知内部错误 private-debug-secret",
    `其他错误：${conflictMessage}`,
    "__proto__", "constructor", "toString",
  ]) {
    for (const operation of ["search", "commit"]) {
      test(`${kind}: unknown ${operation} error stays generic (${message.slice(0, 24)})`, async t => {
        const { state } = mount(t, p => {
          if (p.operation === operation) throw new Error(message);
          return responses(p);
        });
        await flush();
        await state().execute("search", member);
        if (operation === "commit") await state().execute("commit");
        assert.equal(state().error, operation === "commit"
          ? "变更结果未确认，请刷新名单核对；不会自动重试。"
          : "读取或预览失败，请刷新后重试。");
        assert.equal(state().preview, null);
      });
    }
  }

  test(`${kind}: a translated conflict on the post-save read cannot disguise a successful commit`, async t => {
    let saved = false;
    const { state } = mount(t, p => {
      if (p.operation === "read" && saved) throw new Error(conflictMessage);
      if (p.operation === "commit") saved = true;
      return responses(p);
    });
    await flush();
    await state().execute("search", member);
    await state().execute("commit");
    assert.equal(state().notice, "已移除：已离群成员");
    assert.equal(state().error, "变更已保存，但名单读取失败，请刷新核对。");
    assert.equal(state().loaded, false);
  });

  test(`${kind}: a late translated conflict cannot leak across a group change`, async t => {
    let reject;
    const { state, props, calls } = mount(t, p => p.operation === "preview"
      ? new Promise((_, fail) => { reject = fail; }) : responses(p));
    await flush();
    const task = state().execute("search", member);
    await flush();
    props.value.group = "6";
    await nextTick();
    await flush();
    reject(new Error(conflictMessage));
    await task;
    assert.equal(state().error, "");
    assert.equal(state().members.length, 0);
    assert.equal(state().preview, null);
    assert(!calls.some(p => p.operation === "commit"));
  });

  test(`${kind}: failed reads keep mutations disabled and post-save failures retain an honest notice`, async t => {
    let failedRead = true;
    let saved = false;
    const { state, calls } = mount(t, p => {
      if (p.operation === "read" && (failedRead || saved)) throw new Error("offline");
      if (p.operation === "commit") saved = true;
      return responses(p);
    });
    await flush();
    assert.equal(state().loaded, false);
    state().query = "同名";
    await state().execute("search");
    await state().execute("search", member);
    assert.equal(calls.length, 1);
    failedRead = false;
    await state().execute("read");
    await state().execute("search", member);
    await state().execute("commit");
    assert.equal(state().notice, "已移除：已离群成员");
    assert.match(state().error, /变更已保存，但名单读取失败/);
    assert.equal(state().loaded, false);
    assert.equal(state().members.length, 0);
  });

  test(`${kind}: manual add remains a name search followed by explicit selection and confirmation`, async t => {
    const { state, calls } = mount(t, p => p.operation === "preview"
      ? { token: "add-token", version: 1, mode: "add", member: other }
      : p.operation === "commit" ? { saved: true, mode: "add", member: other.name } : responses(p));
    await flush();
    state().query = " 同名 ";
    await state().execute("search");
    assert.equal(calls[1].mode, "add");
    assert.equal(calls[1].query, "同名");
    state().chosen = 1;
    await state().execute("preview");
    assert.equal(state().preview.member.id, "13");
    assert(!calls.some(p => p.operation === "commit"));
    await state().execute("commit");
    assert.equal(state().notice, "已添加：同名成员");
  });
}
