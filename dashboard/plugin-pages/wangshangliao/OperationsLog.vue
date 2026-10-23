<script setup lang="ts">
import { ref, watch } from "vue";
import { RefreshCw } from "@lucide/vue";
const props = defineProps<{ botId: string; bridge: any; t: (zh: string, en: string) => string }>();
const data = ref<{ changes: any[]; logs: any[] }>({ changes: [], logs: [] });
const error = ref("");
const busy = ref(false);
let sequence = 0;
async function refresh() {
  const current = ++sequence;
  busy.value = true;
  error.value = "";
  try {
    const result = await props.bridge.apiGet("operations", { bot_id: props.botId });
    if (current === sequence) data.value = result;
  } catch (err) {
    if (current === sequence) error.value = err instanceof Error ? err.message : String(err);
  } finally {
    if (current === sequence) busy.value = false;
  }
}
function date(value: number) {
  return new Date(value * 1000).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });
}
watch(() => props.botId, refresh, { immediate: true });
</script>
<template>
  <section>
    <div class="log-heading"><h2>{{ t("操作日志", "Operation Logs") }}</h2><button type="button" class="icon-button" :disabled="busy" :title="t('刷新日志', 'Refresh logs')" @click="refresh"><RefreshCw :size="18" /></button></div>
    <div v-if="error" class="message error" role="alert">{{ error }}</div>
    <div v-for="row in data.logs" :key="row.id" class="log-row">
      <strong>{{ row.action }} · {{ row.outcome }}</strong>
      <span>{{ date(row.created) }} · {{ row.actor }}</span>
      <span v-if="row.code">{{ row.code }}</span>
    </div>
    <h2>{{ t("配置变更", "Configuration Changes") }}</h2>
    <details v-for="row in data.changes" :key="row.id" class="log-row">
      <summary>{{ date(row.effective_at) }} · {{ row.actor }} · v{{ row.version }}</summary>
      <h3>{{ t("修改前", "Before") }}</h3><pre>{{ JSON.stringify(JSON.parse(row.before_json), null, 2) }}</pre>
      <h3>{{ t("修改后", "After") }}</h3><pre>{{ JSON.stringify(JSON.parse(row.after_json), null, 2) }}</pre>
    </details>
  </section>
</template>
<style scoped>
.log-heading { display: flex; justify-content: space-between; align-items: center; }
.log-row { display: grid; gap: 6px; padding: 12px 0; border-bottom: 1px solid #d9dfe5; }
.log-row span { font-size: 13px; overflow-wrap: anywhere; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 13px; }
h3 { font-size: 14px; }
</style>
