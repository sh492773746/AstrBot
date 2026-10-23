import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import { parse } from "vue/compiler-sfc";
import ts from "typescript";

const source = parse(readFileSync(
  new URL("../src/views/PlatformPage.vue", import.meta.url), "utf8",
)).descriptor.scriptSetup.content;
const ast = ts.createSourceFile("PlatformPage.js", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
const method = ast.statements.find(
  node => ts.isFunctionDeclaration(node) && node.name.text === "getPlatformStats",
);

test("account names and counts use live tenants, not historical observation scope", async () => {
  const platform = {
    type: "wangshangliao", id: "stable", account_id: "1", enabled_groups: ["5", "6"],
  };
  const stats = { value: {} };
  const state = {
    mode: "integrated", settings: { paused: false },
    accounts: [{ instance_id: "stable", account: "1", nickname: "Current", state: "online" }],
    groups: [
      { account: "1", group_id: "5", active: true, present: true, paused: true },
      { account: "1", group_id: "6", active: true, present: true, paused: true },
      { account: "2", group_id: "7", active: true, present: true, paused: false },
    ],
  };
  const api = {
    botApi: { stats: async () => ({ data: { status: "ok", data: { platforms: [
      { id: "stable", enabled_groups: ["5", "6"], business_active: true },
      { id: "telegram", status: "running" },
    ] } } }) },
    pluginExtensionApi: { post: async (path, payload) => {
      assert.equal(path, "wangshangliao_moderation/open-service");
      assert.deepEqual(JSON.parse(JSON.stringify(payload)), { action: "status", payload: {} });
      return { data: { data: state } };
    } },
    platforms: { value: [platform] }, platformStats: stats,
    getPlatformStat: id => stats.value[id], console,
  };
  const refresh = vm.runInNewContext(source.slice(method.getStart(ast), method.end) + "; getPlatformStats", api);
  await refresh();
  assert.equal(stats.value.stable.nickname, "Current");
  assert.deepEqual(Array.from(stats.value.stable.enabled_groups), []);
  assert.equal(stats.value.stable.business_active, false);
  assert.equal(stats.value.telegram.status, "running");
  assert.deepEqual(platform.enabled_groups, ["5", "6"]);
  state.groups[0].paused = false;
  await refresh();
  assert.deepEqual(Array.from(stats.value.stable.enabled_groups), ["5"]);
  assert.equal(stats.value.stable.business_active, true);
  state.settings.paused = true;
  await refresh();
  assert.deepEqual(Array.from(stats.value.stable.enabled_groups), []);
  assert.equal(stats.value.stable.business_active, false);
  state.accounts[0].account = "2";
  await refresh();
  assert.equal(stats.value.stable.nickname, undefined);
});
