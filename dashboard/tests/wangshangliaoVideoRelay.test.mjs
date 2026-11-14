import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import ts from "typescript";
import { createRenderer, h, nextTick, ref } from "vue";
import { compileScript, compileTemplate, parse } from "vue/compiler-sfc";

const require = createRequire(import.meta.url);
const { descriptor } = parse(readFileSync(new URL(
  "../plugin-pages/wangshangliao/VideoRelayPanel.vue", import.meta.url), "utf8"));
const script = compileScript(descriptor, { id: "relay-test" });
assert.deepEqual(compileTemplate({
  source: descriptor.template.content, filename: "VideoRelayPanel.vue", id: "relay-test",
  compilerOptions: { bindingMetadata: script.bindings, expressionPlugins: ["typescript"] },
}).errors, []);
const module = { exports: {} };
new Function("require", "module", "exports", ts.transpileModule(script.content, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText)(require, module, module.exports);
const Panel = module.exports.default;
const renderer = createRenderer({
  createComment: () => ({}), insert() {}, remove() {},
  parentNode: () => null, nextSibling: () => null,
});
const defaults = () => ({
  cms_url: "", category_id: 0, enabled: false, interval_minutes: 20,
  max_video_mb: 32, min_free_gb: 2,
});
const reply = group => ({
  version: 0, revision: "r", config: defaults(), updated_at: null,
  destination: { platform: "wangshangliao", account: "1", group_id: group },
  runtime: { state: "stopped", can_preview_video: false,
    video_sender: "single_video_ready", tasks: [] },
});
const flush = () => new Promise(resolve => setImmediate(resolve));
function mount(t, bridge) {
  const group = ref("5");
  const events = [];
  let instance;
  const app = renderer.createApp({ render: () => h({ ...Panel, render: () => null }, {
    botId: "fixture", groupId: group.value, locale: "zh-CN", bridge,
    onDirty: value => events.push(["dirty", value]), onBusy: value => events.push(["busy", value]),
    ref: value => { instance = value; },
  }) });
  app.mount({});
  t.after(() => app.unmount());
  return { state: () => instance.$.setupState, group, events };
}

test("lazy panel read keeps automatic sending stopped until confirmed", async t => {
  const calls = [];
  const { state } = mount(t, {
    async apiGet(endpoint, params) { calls.push([endpoint, params]); return reply(params.group_id); },
    async apiPost() { throw new Error("Read must not write"); },
  });
  await flush();
  assert.equal(state().state.runtime.state, "stopped");
  assert.deepEqual(calls, [["video-relay", { bot_id: "fixture", group_id: "5" }]]);
  assert.equal(state().dirty, false);
});

test("an older backend produces an upgrade notice without rendering a broken form", async t => {
  const { state } = mount(t, {
    async apiGet(_endpoint, params) { return { ...reply(params.group_id), config: { sources: [] } }; },
    async apiPost() { throw new Error("No writes against an older backend"); },
  });
  await flush();
  assert.equal(state().state, null);
  assert.match(state().error, /尚未加载/);
});

test("preview and confirmation preserve scope and normalize CMS endpoints", async t => {
  const calls = [];
  const { state } = mount(t, {
    async apiGet(_endpoint, params) { return reply(params.group_id); },
    async apiPost(endpoint, body) {
      calls.push([endpoint, body]);
      if (body.operation === "preview") return {
        token: "fixture-token", version: 0,
        config: { ...body.config, cms_url: "https://news.example.com/api" },
      };
      return { saved: true, version: 1, started: false };
    },
  });
  await flush();
  state().config.cms_url = "https://news.example.com/api?ac=list";
  await nextTick();
  assert.equal(state().dirty, true);
  await state().execute("preview");
  assert.equal(calls[0][1].version, 0);
  assert.equal(state().preview.config.cms_url, "https://news.example.com/api");
  await state().execute("commit");
  assert.equal(state().preview, null);
  assert.equal(state().state.version, 1);
  assert.equal(state().config.cms_url, "https://news.example.com/api");
  assert.equal(state().dirty, false);
  assert.match(state().notice, /已停用/);
  assert.equal(calls[1][1].token, "fixture-token");
});

test("automatic sending requires a reviewed configuration confirmation", async t => {
  const calls = [];
  const { state } = mount(t, {
    async apiGet(_endpoint, params) { return reply(params.group_id); },
    async apiPost(_endpoint, body) {
      calls.push(body);
      return body.operation === "preview"
        ? { token: "auto-token", version: 0, config: body.config }
        : { saved: true, version: 1, started: true };
    },
  });
  await flush();
  state().config.cms_url = "https://news.example.com/api";
  state().config.enabled = true;
  state().config.interval_minutes = 15;
  await state().execute("preview");
  assert.equal(calls.length, 1);
  assert.equal(state().preview.config.enabled, true);
  await state().execute("commit");
  assert.equal(calls[1].token, "auto-token");
  assert.equal(state().state.runtime.state, "scheduled");
  assert.match(state().notice, /自动发送已启用/);
});

test("invalid CMS URLs, intervals and resource limits cannot preview", async t => {
  const { state } = mount(t, {
    async apiGet(_endpoint, params) { return reply(params.group_id); },
    async apiPost() { throw new Error("Invalid draft must not write"); },
  });
  await flush();
  for (const change of [
    { enabled: true }, { interval_minutes: 0 }, { max_video_mb: 113 },
    { category_id: -1 }, { cms_url: "http://news.example.com/api" },
    { cms_url: "https://user:secret@news.example.com/api" },
  ]) {
    state().config = { ...defaults(), ...change };
    assert.equal(state().valid, false);
    await state().execute("preview");
  }
});

test("a following destination preserves its binding and cannot independently select a category", async t => {
  const calls = [];
  const { state } = mount(t, {
    async apiGet(endpoint, params) {
      calls.push(params);
      return { ...reply(params.group_id),
        config: { ...defaults(), cms_url: "https://news.example.com/api", enabled: true, follow_group_id: "6" },
        runtime: { state: "scheduled", can_preview_video: false, video_sender: "single_video_ready", tasks: [] } };
    },
    async apiPost() { throw new Error("Read must not send"); },
  });
  await flush();
  assert.equal(state().config.follow_group_id, "6");
  assert.equal(state().state.runtime.can_preview_video, false);
  assert.equal(calls.length, 1);
  await state().previewCategory({ id: 1, name: "News" });
  assert.equal(state().videoPreview, null);
});

test("late read and preview cannot replace a newly selected group", async t => {
  let finishRead, finishPreview;
  const { state, group } = mount(t, {
    apiGet(_endpoint, params) {
      return params.group_id === "5" ? new Promise(resolve => { finishRead = resolve; }) : Promise.resolve(reply(params.group_id));
    },
    apiPost() { return new Promise(resolve => { finishPreview = resolve; }); },
  });
  group.value = "6";
  await flush();
  finishRead(reply("5"));
  await flush();
  assert.equal(state().state.destination.group_id, "6");
  state().config.interval_minutes = 12;
  const pending = state().execute("preview");
  group.value = "7";
  await flush();
  finishPreview({ token: "old", config: defaults(), version: 0 });
  await pending;
  assert.equal(state().preview, null);
  assert.equal(state().state.destination.group_id, "7");
});

test("lost commit response invalidates the token and requires readback without a retry", async t => {
  let writes = 0;
  const { state } = mount(t, {
    async apiGet(_endpoint, params) { return reply(params.group_id); },
    async apiPost(_endpoint, body) {
      writes++;
      if (body.operation === "preview") return { token: "token", config: body.config, version: 0 };
      throw new Error("connection lost");
    },
  });
  await flush();
  state().config.interval_minutes = 12;
  await state().execute("preview");
  await state().execute("commit");
  assert.equal(state().preview, null);
  assert.equal(state().state, null);
  assert.match(state().error, /未确认/);
  await state().execute("commit");
  assert.equal(writes, 2);
});

test("single video preview never sends until explicit confirmation and consumes its token", async t => {
  const calls = [];
  const { state } = mount(t, {
    async apiGet(_endpoint, params) {
      const value = reply(params.group_id);
      value.config.cms_url = "https://news.example.com/api";
      value.runtime.can_preview_video = true;
      return value;
    },
    async apiPost(endpoint, body) {
      calls.push([endpoint, body]);
      return body.operation === "preview_video"
        ? { id: "v", token: "scoped-video-token", state: "prepared",
          metadata: { title: "Neutral fixture", vod_id: 123, size: 1024, duration: 2, width: 160, height: 90 } }
        : { id: "v", state: "accepted" };
    },
  });
  await flush();
  await state().executeVideo("preview_video");
  assert.equal(calls.length, 1);
  assert.equal(state().videoPreview.token, "scoped-video-token");
  await state().executeVideo("send_video");
  assert.equal(calls[1][1].token, "scoped-video-token");
  assert.equal(state().videoPreview, null);
  assert.match(state().notice, /待收件/);
  await state().executeVideo("send_video");
  assert.equal(calls.length, 2);
});

test("late video previews cannot attach to another group and uncertain sends never retry", async t => {
  let finish;
  let writes = 0;
  const { state, group } = mount(t, {
    async apiGet(_endpoint, params) {
      const value = reply(params.group_id);
      value.config.cms_url = "https://news.example.com/api";
      value.runtime.can_preview_video = true;
      return value;
    },
    apiPost(_endpoint, body) {
      writes++;
      if (body.operation === "preview_video") return new Promise(resolve => { finish = resolve; });
      throw new Error("connection lost after submission");
    },
  });
  await flush();
  const pending = state().executeVideo("preview_video");
  group.value = "6";
  await flush();
  finish({ id: "old", token: "old-token" });
  await pending;
  assert.equal(state().videoPreview, null);
  state().videoPreview = { id: "v", token: "token", state: "prepared" };
  await state().executeVideo("send_video");
  assert.equal(state().videoPreview, null);
  assert.match(state().error, /不会自动重发/);
  await state().executeVideo("send_video");
  assert.equal(writes, 2);
});
