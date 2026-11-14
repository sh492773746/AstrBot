<script setup lang="ts">
import { ref } from "vue";
import RaidDefensePanel from "../../plugin-pages/wangshangliao/RaidDefensePanel.vue";
const group = ref("5");
const fixture = {
  calls: [] as any[], fail: "", hold: false,
  releases: [] as Array<() => void>, ended: false,
};
(window as any).raidFixture = fixture;
async function api(action: string, payload: any) {
  fixture.calls.push({ action, ...structuredClone(payload) });
  const originGroup = payload.group_id;
  const ended = fixture.ended;
  if (fixture.hold) {
    fixture.hold = false;
    await new Promise<void>(resolve => fixture.releases.push(resolve));
  }
  if (fixture.fail === payload.operation) { fixture.fail = ""; throw new Error("请求超时，请刷新核对"); }
  if (payload.operation === "preview") return { token: "fixture-preview", expires_in: 300 };
  if (payload.operation === "commit") {
    if (payload.token !== "fixture-preview") throw new Error("Invalid confirmation");
    fixture.ended = true;
    return { ended: true };
  }
  return {
    enabled: originGroup === "5", operational: true,
    state: { protection_until: originGroup === "5" && !ended ? Date.now() / 1000 + 900 : 0 },
    members: originGroup === "5" ? [{ observed_until: Date.now() / 1000 + 600 }] : [],
    outcomes: originGroup === "5" ? [
      { member: "2", kind: "burst", minutes: 10, status: "unknown" },
      { member: "3", kind: "hold", minutes: 1, status: "accepted" },
      { member: "4", kind: "restore", minutes: 8, status: "rejected" },
    ] : [],
    recalls: originGroup === "5" ? [{ member: "2", message: "100", status: "accepted" }] : [],
  };
}
</script>
<template>
  <main>
    <h1>离线测试群规则</h1>
    <label for="raid-fixture-group">群聊</label>
    <select id="raid-fixture-group" v-model="group"><option value="5">离线测试群5</option><option value="6">离线测试群6</option></select>
    <RaidDefensePanel :api="api" account="1" :group="group" :tenant-version="1" />
  </main>
</template>
<style>
* { box-sizing: border-box; }
body { margin: 0; font: 14px sans-serif; color: #17252d; background: white; }
main { padding: 16px; max-width: 980px; margin: 0 auto; }
h1 { font-size: 20px; }
select { min-height: 36px; margin-left: 12px; max-width: 100%; }
</style>
