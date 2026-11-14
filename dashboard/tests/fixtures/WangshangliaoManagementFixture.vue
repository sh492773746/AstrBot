<script setup lang="ts">
import { ref } from "vue";
import App from "../../plugin-pages/wangshangliao/App.vue";
import "../../plugin-pages/wangshangliao/style.css";

const calls = ref<any[]>([]);
const groups = ["5", "6"].map((group_id, index) => ({
  account: "1", group_id, name: `测试群${index + 1}`, active: 1, paused: 1,
  present: 1, admission: "legacy", version: 1, managers: ["2"],
  native_role: "GROUP_ROLE_ADMIN", rules: {
    advertising: true, flood: true, keywords: ["Existing keyword"],
  },
}));
const state: any = {
  mode: "integrated", groups, group_ai: [{
    account: "1", group_id: "5", version: 1, config: {
      enabled: true, mentions: true, autonomous: false, idle: false,
      interval_minutes: 15, daily_limit: 0, start_hour: 9, end_hour: 23,
      topic: "友善、轻松的日常交流，不发布广告。",
    },
    readiness: { ready: false, reasons: [
      "模型调用尚未开放：供应商硬预算未核验。",
      "热群模型计费依据未配置、已失效或已暂停。",
    ] },
  }],
  accounts: [{ instance_id: "fixture", account: "1", nickname: "测试机器人", state: "online", enabled_groups: 0 }],
  settings: { version: 1, paused: 0, global_limit: 100000 },
  ai_budget: { confirmed: false, provider: "", version: 1 },
  allocated_count: 2, operating_count: 0, group_limit: 20, queue_health: [],
  usage: { used: 0, exempt_used: 0 }, admins: [], model: {}, ai_profiles: [
    { kind: "group", account: "", group_id: "", version: 1, selection: {
      config_id: "test", persona_id: "", provider_id: "", temperature: 0.8,
      top_p: 1, max_tokens: 256, history_turns: 4,
    } },
  ],
  ai_choices: {
    configs: [
      { id: "test", name: "测试配置", compatible: true },
      { id: "unrelated", name: "无关Telegram配置", compatible: true },
    ],
    personas: [{ id: "unrelated", name: "无关人格" }],
  },
  providers: [], audit: [],
  billing: {
    settings: { version: 1, contact_name: "", contact: "", monthly_micro: 0 },
    pools: [], payers: [], prices: [], notices: [], unknown: [],
  },
};
const query = new URLSearchParams(location.search);
const business = new Map<string, any>();
const drafts = new Map<string, any>();
const relays = new Map<string, any>();
const relayDefaults = {
  cms_url: "", category_id: 0, enabled: false, interval_minutes: 20,
  max_video_mb: 32, min_free_gb: 2,
};
const bridge = {
  ready: async () => {}, getLocale: () => query.get("locale") || "zh-CN",
  onContext: () => () => {},
  async apiGet(endpoint: string, params: Record<string, string> = {}) {
    calls.value.push({ method: "GET", endpoint });
    if (endpoint === "settings") return {
      open_mode: query.has("legacy") ? "legacy" : "integrated", providers: [],
      bots: [1, 2].map(id => ({
        id: `fixture-${id}`, account_id: String(id), nickname: `测试机器人${id}`, revision: "1",
        state: "online", enable: true, business_active: true, moderation: {},
        enabled_groups: ["5", "6"], group_names: { "5": "测试群1", "6": "测试群2" },
        reply_private: true,
        proactive_send: { enabled: true, targets: ["5", "private/42"] },
        developer_test: { scopes: ["private_commands", "private_ai", "group_rules"] },
      })),
    };
    if (endpoint === "video-relay") {
      const key = `${params.bot_id}/${params.group_id}`;
      const current = relays.get(key) || { version: 0, config: query.has("follow")
        ? { ...relayDefaults, cms_url: "https://news.example.com/api", enabled: true, follow_group_id: "6" }
        : relayDefaults };
      return structuredClone({
        ...current, revision: "relay-fixture", updated_at: null,
        destination: { platform: "wangshangliao", account: params.bot_id.replace("fixture-", ""), group_id: params.group_id },
        runtime: { state: current.config.enabled ? "scheduled" : "stopped", tasks: [],
          video_sender: "single_video_ready", can_preview_video: Boolean(current.config.cms_url && !current.config.follow_group_id) },
      });
    }
    if (endpoint === "audit") return { reviews: [], sanctions: [], counts: [], rules: [] };
    if (endpoint === "schedule") return { schedule: null, executions: [], history: [] };
    if (endpoint === "activities") return {
      lottery: { prize: "", winners: 3, duration_minutes: 10, capacity: 15, invite_gate: 0, contact: "" },
      invitation: { rate: "0.00", enabled: false }, current_lottery: null, revision: "1",
    };
    if (endpoint === "operations") return { changes: [], logs: [] };
    if (endpoint === "groups") return {
      groups: [{ id: "5", name: "测试群1" }, { id: "6", name: "测试群2" }],
      private_targets: [{ value: "private/42", name: "退役目标", uid: "42" }],
    };
    if (endpoint === "test-window") return { window: { active: false }, instances: [] };
    throw new Error(`Unexpected GET: ${endpoint}`);
  },
  async apiPost(endpoint: string, request: any) {
    calls.value.push({ method: "POST", endpoint, action: request.action, payload: structuredClone(request.payload) });
    if (endpoint === "video-relay") {
      const key = `${request.bot_id}/${request.group_id}`;
      if (request.operation === "preview") {
        const config = structuredClone(request.config);
        if (config.cms_url) {
          const url = new URL(config.cms_url);
          config.cms_url = url.origin + url.pathname;
        }
        const token = `relay-${key}`;
        drafts.set(token, { ...request, config });
        return { token, config, version: request.version, expires_at: Date.now() / 1000 + 300 };
      }
      if (request.operation === "commit") {
        const draft = drafts.get(request.token);
        if (!draft || draft.bot_id !== request.bot_id || draft.group_id !== request.group_id) throw new Error("wrong scope");
        relays.set(key, { version: draft.version + 1, config: draft.config });
        drafts.delete(request.token);
        return { saved: true, version: draft.version + 1, started: draft.config.enabled };
      }
      if (request.operation === "preview_video") {
        drafts.set(`video-${key}`, { ...request, token: `video-${key}` });
        return { id: "video-fixture", token: `video-${key}`, state: "prepared",
          metadata: { title: "中性新闻测试视频", vod_id: 123, duration: 2, size: 2048, width: 160, height: 90 } };
      }
      if (["send_video", "cancel_video"].includes(request.operation)) {
        if (!drafts.has(request.token) || request.token !== `video-${key}`) throw new Error("wrong video scope");
        drafts.delete(request.token);
        return { id: "video-fixture", state: request.operation === "send_video" ? "accepted" : "cancelled" };
      }
      throw new Error("Unsupported relay operation");
    }
    if (endpoint !== "open-service") throw new Error("No fixture writes allowed");
    if (request.action === "status") return structuredClone(state);
    if (request.action === "page_schema") return {
      schema_version: 1, id: "fixture",
      fields: { properties: {
        wording: { maxLength: 4000 }, style: { enum: ["简洁", "亲切", "正式"] },
        length: { minimum: 50, maximum: 2000 }, knowledge: { maxItems: 100, items: { maxLength: 8192 } },
        keywords: { maxItems: 100 }, ads: { maxItems: 20 },
      } },
    };
    if (request.action === "command_capabilities") return { commands: [
      { command: "关键词回复", role: "group_manager", locations: ["group", "private"], preview: false },
      { command: "开启自主接话", role: "server", locations: ["group", "private"], preview: false },
    ] };
    if (request.action === "tenant_members") return {
      members: [
        { id: "2", name: "管理甲", role: "GROUP_ROLE_ADMIN" },
        { id: "3", name: "成员乙", role: "GROUP_ROLE_MEMBER" },
      ],
      native_capability: { state: "available" },
    };
    if (request.action === "business_rules") {
      const p = request.payload;
      const key = `${p.account}/${p.group_id}`;
      if (p.operation === "read") return structuredClone(business.get(key) || {
        version: 0, schedules: [], config: { keywords: [], ads: [] },
      });
      if (query.has("rules") && p.operation === "preview") {
        drafts.set("rules-fixture", structuredClone(p));
        return { token: "rules-fixture", version: p.version, config: structuredClone(p.config) };
      }
      if (query.has("rules") && p.operation === "commit") {
        const draft = drafts.get(p.token);
        if (!draft || draft.account !== p.account || draft.group_id !== p.group_id) throw new Error("wrong scope");
        business.set(key, { version: draft.version + 1, config: draft.config, schedules: [] });
        return { saved: true };
      }
    }
    throw new Error(`No fixture writes allowed: ${request.action}`);
  },
};
(window as any).AstrBotPluginPage = bridge;
</script>
<template>
  <App />
  <output id="fixture-calls" hidden>{{ JSON.stringify(calls) }}</output>
</template>
