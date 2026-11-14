<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { Check, RefreshCw, ShieldOff, X } from "@lucide/vue";

const props = defineProps<{
  api: (action: string, payload: Record<string, unknown>) => Promise<any>;
  account: string; group: string; tenantVersion: number; disabled?: boolean;
}>();
const state = ref<any>(null);
const pending = ref(false);
const token = ref("");
const error = ref("");
const now = ref(Date.now() / 1000);
const remaining = computed(() => Math.min(15, Math.max(0,
  Math.ceil(((state.value?.state?.protection_until || 0) - now.value) / 60))));
const statuses: Record<string, string> = {
  accepted: "平台已受理", verified: "已核验", rejected: "执行被拒绝",
  unknown: "结果未知 · 不自动重试", skipped: "已有禁言 · 未覆盖", revoked: "已撤销",
};
const kinds: Record<string, string> = { burst: "新人刷屏", hold: "防护期入群", restore: "重入补禁言" };
let generation = 0;
const clock = setInterval(() => { now.value = Date.now() / 1000; }, 1000);

async function run(operation: "read" | "preview" | "commit") {
  if (pending.value || props.disabled) return;
  const sequence = ++generation;
  const confirmation = token.value;
  token.value = "";
  pending.value = true;
  error.value = "";
  try {
    const result = await props.api("raid_defense", {
      account: props.account, group_id: props.group, tenant_version: props.tenantVersion,
      operation, ...(operation === "commit" ? { token: confirmation } : {}),
    });
    if (sequence !== generation) return;
    if (operation === "read") state.value = result;
    if (operation === "preview") token.value = result.token;
    if (operation === "commit") {
      const latest = await props.api("raid_defense", {
        account: props.account, group_id: props.group, tenant_version: props.tenantVersion,
        operation: "read",
      });
      if (sequence === generation) state.value = latest;
    }
  } catch (e: any) {
    if (sequence === generation) error.value = e?.message || "结果未确认，请刷新核对";
  } finally {
    if (sequence === generation) pending.value = false;
  }
}
watch(() => [props.account, props.group, props.tenantVersion, props.disabled], () => {
  generation++; pending.value = false; state.value = null; token.value = ""; error.value = "";
  if (props.account && props.group && !props.disabled) void run("read");
}, { immediate: true });
onBeforeUnmount(() => { generation++; clearInterval(clock); });
</script>

<template>
  <section class="raid-defense" aria-label="防袭群状态">
    <header><h3>防袭群状态</h3>
      <button type="button" :disabled="disabled || pending" title="刷新防袭群状态"
        aria-label="刷新防袭群状态" @click="run('read')"><RefreshCw :size="17" /></button>
    </header>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <template v-if="state">
      <dl>
        <div><dt>状态</dt><dd>{{ !state.enabled ? '已关闭' : !state.operational ? '暂停或权限未就绪' : remaining ? '临时防护中' : '观察中' }}</dd></div>
        <div><dt>防护剩余</dt><dd>{{ remaining }} 分钟</dd></div>
        <div><dt>观察成员</dt><dd>{{ state.members.filter((m: any) => m.observed_until > now).length }}</dd></div>
      </dl>
      <p v-if="remaining" class="reason">触发原因：60秒内两个不同的新成员刷屏</p>
      <button v-if="remaining && !token" type="button" :disabled="disabled || pending"
        @click="run('preview')"><ShieldOff :size="17" />结束本轮防护</button>
      <div v-if="token" role="alertdialog" aria-label="确认结束临时防护" class="confirmation">
        <strong>结束本轮临时防护？</strong>
        <p>已有禁言保持不变。</p>
        <div class="actions">
          <button type="button" :disabled="disabled || pending" @click="run('commit')"><Check :size="17" />确认结束</button>
          <button type="button" :disabled="pending" @click="token = ''"><X :size="17" />取消</button>
        </div>
      </div>
      <div class="receipts" v-if="state.outcomes.length">
        <div v-for="(row, index) in state.outcomes" :key="index" class="receipt">
          <span>UID {{ row.member }}</span><span>{{ kinds[row.kind] || row.kind }}</span>
          <span>{{ row.minutes }} 分钟</span><span :class="{ error: ['rejected', 'unknown'].includes(row.status) }">
            {{ statuses[row.status] || row.status }}</span>
        </div>
      </div>
      <p v-else>暂无处罚记录</p>
      <div class="receipts" v-if="state.recalls?.length" aria-label="防袭群撤回记录">
        <div v-for="row in state.recalls" :key="row.message" class="receipt">
          <span>UID {{ row.member }}</span><span>撤回消息</span><span>{{ row.message }}</span>
          <span :class="{ error: ['rejected', 'unknown'].includes(row.status) }">{{ statuses[row.status] || row.status }}</span>
        </div>
      </div>
    </template>
  </section>
</template>

<style scoped>
.raid-defense { margin-top: 24px; padding-top: 20px; border-top: 1px solid #d3dce3; min-width: 0; }
header, .actions { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
h3 { font-size: 16px; margin: 0; }
button { display: inline-flex; align-items: center; justify-content: center; gap: 6px; min-height: 36px; border: 1px solid #a9b6c0; border-radius: 4px; padding: 6px 10px; background: #fff; color: #17252d; font: inherit; cursor: pointer; }
header button { width: 36px; height: 36px; padding: 0; flex-shrink: 0; }
button:disabled { opacity: .5; cursor: default; }
dl { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; margin: 18px 0; }
dt { font-size: 13px; color: #60707b; }
dd { font-size: 16px; margin: 6px 0 0; }
p, dd, .receipt span { overflow-wrap: anywhere; }
.confirmation { margin-top: 12px; padding: 14px; border: 1px solid #c8d6d3; border-radius: 4px; }
.actions { justify-content: flex-start; flex-wrap: wrap; }
.receipt { display: grid; grid-template-columns: 1fr 1fr 90px 1.5fr; gap: 10px; padding: 12px 0; border-bottom: 1px solid #e2e7e9; font-size: 13px; }
.error { color: #b44337; }
.reason { color: #985422; }
@media (max-width: 500px) { dl { grid-template-columns: 1fr; gap: 10px; } dl div { display: flex; justify-content: space-between; gap: 12px; } dd { margin: 0; } .receipt { grid-template-columns: 1fr 1fr; } }
</style>
