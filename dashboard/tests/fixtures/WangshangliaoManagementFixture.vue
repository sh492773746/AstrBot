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
    { kind: "consultation", account: "1", group_id: "", version: 1, selection: {
      config_id: "test", persona_id: "", provider_id: "", temperature: 0.3,
      top_p: 1, max_tokens: 512, history_turns: 3,
    } },
    { kind: "group", account: "", group_id: "", version: 1, selection: {
      config_id: "test", persona_id: "", provider_id: "", temperature: 0.8,
      top_p: 1, max_tokens: 256, history_turns: 4,
    } },
    { kind: "admin_guidance", account: "", group_id: "", version: 2, selection: {
      config_id: "test", persona_id: "__builtin__", provider_id: "", temperature: 0.2,
      top_p: 0.85, max_tokens: 1024, history_turns: 6,
    } },
    { kind: "consultation", account: "owner:2", group_id: "", version: 1, selection: {
      config_id: "owner-test", persona_id: "owner-persona", provider_id: "", temperature: 0.2,
      top_p: 0.85, max_tokens: 1024, history_turns: 6,
    } },
  ],
  ai_choices: {
    configs: [
      { id: "test", name: "测试配置", compatible: true },
      { id: "owner-test", name: "测试业务负责人专属配置", compatible: true },
      { id: "unrelated", name: "无关Telegram配置", compatible: true },
    ],
    personas: [{ id: "owner-persona", name: "测试业务负责人专属人格" }, { id: "unrelated", name: "无关人格" }],
  },
  guidance_status: { profile_ready: true, manual_current: true, manual_entries: 34, manual_expected: 34 },
  providers: [], audit: [],
  billing: {
    settings: { version: 1, contact_name: "", contact: "", monthly_micro: 0 },
    pools: [], payers: [], prices: [], notices: [], unknown: [],
  },
};
const query = new URLSearchParams(location.search);
const business = new Map<string, any>();
const support = new Map<string, any>();
const drafts = new Map<string, any>();
const bridge = {
  ready: async () => {}, getLocale: () => query.get("locale") || "zh-CN",
  onContext: () => () => {},
  async apiGet(endpoint: string) {
    calls.value.push({ method: "GET", endpoint });
    if (endpoint === "settings") return {
      open_mode: query.has("legacy") ? "legacy" : "integrated", providers: [],
      bots: [1, 2].map(id => ({
        id: `fixture-${id}`, account_id: String(id), nickname: `测试机器人${id}`, revision: "1",
        state: "online", enable: true, business_active: true, moderation: {},
        enabled_groups: ["5", "6"], group_names: { "5": "测试群1", "6": "测试群2" },
      })),
    };
    if (endpoint === "audit") return { reviews: [], sanctions: [], counts: [], rules: [] };
    if (endpoint === "schedule") return { schedule: null, executions: [], history: [] };
    if (endpoint === "activities") return {
      lottery: { prize: "", winners: 3, duration_minutes: 10, capacity: 15, invite_gate: 0, contact: "" },
      invitation: { rate: "0.00", enabled: false }, current_lottery: null, revision: "1",
    };
    if (endpoint === "operations") return { changes: [], logs: [] };
    if (endpoint === "friend-approval") return {
      version: 1, enabled: true, pending: 0, accepted: 0, retry_after: 0,
    };
    throw new Error(`Unexpected GET: ${endpoint}`);
  },
  async apiPost(endpoint: string, request: { action: string; payload: any }) {
    calls.value.push({ method: "POST", endpoint, action: request.action, payload: structuredClone(request.payload) });
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
    if (request.action === "owner_support") {
      const p = request.payload;
      if (p.operation === "list") return { owners: [{ uid: "2", peer: "1002", name: "测试业务负责人", version: 1, active: 1 }] };
      const key = `owner/${p.uid}`;
      if (p.operation === "read") return structuredClone(support.get(key) || {
        version: 0, config: { wording: "仅按已确认资料解答", style: "简洁", length: 300, links: [], knowledge: [] },
      });
      if (query.has("rules") && p.operation === "preview") {
        drafts.set("owner-fixture", structuredClone(p));
        return { token: "owner-fixture", version: p.version, config: structuredClone(p.config) };
      }
      if (query.has("rules") && p.operation === "commit") {
        const draft = drafts.get(p.token);
        if (!draft || draft.uid !== p.uid) throw new Error("wrong owner");
        support.set(key, { version: draft.version + 1, config: draft.config });
        return { saved: true };
      }
    }
    if (request.action === "command_capabilities") return { commands: [
      { command: "关键词回复", role: "group_manager", locations: ["group", "private"], preview: false },
      { command: "客服设置", role: "support_editor", locations: ["private"], preview: false },
      { command: "开启自主接话", role: "server", locations: ["group", "private"], preview: false },
    ] };
    if (request.action === "tenant_members") return {
      members: [
        { id: "2", name: "管理甲", role: "GROUP_ROLE_ADMIN" },
        { id: "3", name: "成员乙", role: "GROUP_ROLE_MEMBER" },
      ],
      native_capability: { state: "available" },
    };
    if (request.action === "finance_status") return {
      version: 1, grants: [], test_active: false,
      groups: [{ account: "1", group_id: "5", name: "测试群1", configured: true, bot_id: "fixture-1" }],
    };
    if (request.action === "finance_members") return { members: [{ uid: "2", name: "管理甲" }] };
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
    if (request.action === "support_editor" && request.payload.operation === "read") return { editors: [] };
    if (request.action === "support_settings") {
      const p = request.payload;
      if (p.operation === "read") return structuredClone(support.get(p.account) || {
        version: 0, config: { wording: "", style: "简洁", length: 300, links: [], knowledge: [] },
      });
      if (query.has("rules") && p.operation === "preview") {
        drafts.set("support-fixture", structuredClone(p));
        return { token: "support-fixture", version: p.version, config: structuredClone(p.config) };
      }
      if (query.has("rules") && p.operation === "commit") {
        const draft = drafts.get(p.token);
        if (!draft || draft.account !== p.account) throw new Error("wrong scope");
        support.set(p.account, { version: draft.version + 1, config: draft.config });
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
