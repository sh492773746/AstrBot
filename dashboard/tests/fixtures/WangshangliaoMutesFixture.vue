<script setup lang="ts">
import CurrentMutesPanel from "../../plugin-pages/wangshangliao/CurrentMutesPanel.vue";

const fixture = {
  commits: 0,
  result: { status: "accepted", verified: true, blacklist_status: "removed" },
};
(window as unknown as { mutesFixture: typeof fixture }).mutesFixture = fixture;
const locale = new URLSearchParams(location.search).get("lang") || "zh-CN";
const bridge = {
  async apiPost(endpoint: string, body: any) {
    const p = body.payload;
    if (endpoint !== "open-service" || body.action !== "member_mutes"
      || p.account !== "99" || p.group_id !== "5") throw new Error("Invalid fixture scope");
    if (p.operation === "read") return {
      version: 1, checked_at: 1793552400,
      members: [{ id: "2", name: "长名称换行检查成员 Long Member Name", can_unmute: true }],
    };
    if (p.member !== "2" || p.version !== 1) throw new Error("Invalid fixture target");
    if (p.operation === "preview") return {
      confirmation: "offline-confirmation-token", expires_in: 600, removes_blacklist: true,
    };
    if (p.operation !== "commit" || p.confirmation !== "offline-confirmation-token") {
      throw new Error("Invalid fixture confirmation");
    }
    fixture.commits++;
    return { ...fixture.result };
  },
};
</script>

<template>
  <main><CurrentMutesPanel :bridge="bridge" account="99" group="5" :locale="locale" /></main>
</template>

<style>
body { margin: 0; background: white; color: #222; font-family: Arial, sans-serif; }
main { max-width: 960px; margin: 0 auto; padding: 16px; }
* { box-sizing: border-box; }
</style>
