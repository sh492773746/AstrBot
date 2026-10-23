<script setup lang="ts">
import { ref } from "vue";
import OpenService from "../../plugin-pages/wangshangliao/OpenService.vue";

const saved = ref<any[]>([]);
const groups = ["5", "6"].map((group_id, index) => ({
  account: "1", group_id, name: `Test group ${index + 1}`,
  active: 1, paused: 0, present: 1, admission: "legacy", version: 1,
  epoch: 1, cleanup_epoch: 0, cleanup_done: 0,
  native_role: "GROUP_ROLE_ADMIN", managers: ["2"],
  rules: { advertising: true, flood: true, keywords: ["Existing keyword"] },
}));
const state = {
  mode: "integrated", groups,
  accounts: [{ instance_id: "test", account: "1", nickname: "Fixture Bot", state: "online", enabled_groups: 2 }],
  settings: { version: 1, paused: 0, global_limit: 100000 },
  ai_budget: { confirmed: false, provider: "" },
  allocated_count: 2, operating_count: 2, group_limit: 20,
  admins: [], model: {}, ai_profiles: [], providers: [], audit: [],
};
const bridge = {
  apiGet() { throw new Error("Unexpected GET"); },
  async apiPost(endpoint: string, body: { action: string; payload: any }) {
    if (endpoint !== "open-service") throw new Error("Unexpected endpoint");
    if (body.action === "status") return structuredClone(state);
    if (body.action === "finance_status") return { version: 1, grants: [], groups: [], test_active: false };
    const row = groups.find(g => g.account === body.payload.account && g.group_id === body.payload.group_id);
    if (!row) throw new Error("Wrong test scope");
    if (body.action === "tenant_members") return {
      members: [{ id: "2", name: "Manager" }, { id: "3", name: "Member" }],
      native_capability: { state: "available" },
    };
    if (body.action !== "tenant" || row.version !== body.payload.version) throw new Error("Wrong write or stale version");
    if (JSON.stringify(body.payload.managers) !== JSON.stringify(row.managers)) throw new Error("Rules test cannot grant managers");
    row.rules = structuredClone(body.payload.rules);
    row.version++;
    saved.value.push(structuredClone(body.payload));
    return { saved: true };
  },
};
</script>

<template>
  <main><OpenService :bridge="bridge" /></main>
  <output id="fixture-saved" hidden>{{ JSON.stringify(saved) }}</output>
</template>

<style>
body { margin: 0; font: 14px sans-serif; color: #17252d; background: #fff; }
main { width: min(960px, 100%); margin: auto; box-sizing: border-box; padding: 12px; }
* { box-sizing: border-box; }
</style>
