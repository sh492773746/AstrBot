<script setup lang="ts">
import { onMounted, ref } from "vue";
import { Check, RefreshCw, RotateCcw } from "@lucide/vue";

const props = defineProps<{ api: Function }>();
const policy = ref<any>(null);
const entries = ref<any[]>([]);
const busy = ref(false);
const error = ref("");
const review = ref<any>(null);
const reason = ref("");
async function run(task: () => Promise<void>) {
  if (busy.value) return;
  busy.value = true; error.value = "";
  try { await task(); } catch (e: any) { error.value = e?.message || "状态未确认，请刷新核对"; }
  finally { busy.value = false; }
}
async function load() {
  const [saved, status] = await Promise.all([
    props.api("component_policy"), props.api("component_status"),
  ]);
  policy.value = saved; entries.value = status.entries;
}
async function toggle() {
  if (!window.confirm(`${policy.value.enabled ? "关闭" : "开启"}受控组件试点？`)) return;
  await props.api("component_policy", { version: policy.value.version, enabled: !policy.value.enabled });
  await load();
}
function changePilot(event: Event) {
  (event.target as HTMLInputElement).checked = policy.value.enabled;
  void run(toggle);
}
async function recover() {
  await props.api("component_recover", {
    capability: review.value.capability, scope: review.value.scope,
    version: review.value.version, operation: "reset_after_review", reason: reason.value,
  });
  review.value = null; reason.value = ""; await load();
}
onMounted(() => void run(load));
</script>

<template>
  <section class="component-runtime" aria-label="组件治理">
    <header><h3>组件治理</h3>
      <button type="button" title="刷新组件状态" :disabled="busy" @click="run(load)"><RefreshCw :size="16" /></button>
    </header>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <template v-if="policy">
      <label class="pilot"><input type="checkbox" :checked="policy.enabled" :disabled="busy" @change="changePilot" />受控试点</label>
      <p class="scope">测试群 {{ policy.group }} · 账号 {{ policy.accounts.join(" / ") }} · 版本 {{ policy.version }}</p>
      <div class="states">
        <article v-for="entry in entries" :key="entry.reference">
          <div><strong>{{ entry.capability }}</strong><span>{{ entry.scope.join(" / ") }}</span></div>
          <span>{{ entry.state }} · 在途 {{ entry.in_flight }} · 故障 {{ entry.failures }}</span>
          <button type="button" title="核对后恢复" :disabled="busy || entry.state === 'closed' || entry.in_flight > 0"
            @click="review = entry; reason = ''"><RotateCcw :size="16" /></button>
        </article>
      </div>
      <form v-if="review" aria-label="核对组件恢复" @submit.prevent="run(recover)">
        <h4>{{ review.capability }} · 状态版本 {{ review.version }}</h4>
        <label>核对记录<textarea v-model="reason" required maxlength="300" rows="3" /></label>
        <div class="actions">
          <button type="submit" :disabled="busy || !reason.trim()"><Check :size="16" />确认恢复</button>
          <button type="button" :disabled="busy" @click="review = null">取消</button>
        </div>
      </form>
    </template>
  </section>
</template>

<style scoped>
.component-runtime { border-top: 1px solid #c3cbd1; padding: 18px 0; margin-top: 16px; }
header, article, .actions { display: flex; align-items: center; gap: 12px; }
header { justify-content: space-between; }
h3, h4 { margin: 0; font-size: 16px; }
.pilot { display: flex; align-items: center; gap: 8px; margin: 12px 0; }
.scope, article span { font-size: 13px; overflow-wrap: anywhere; }
article { padding: 12px 0; border-bottom: 1px solid #d7dce0; flex-wrap: wrap; }
article div { flex: 1 1 200px; min-width: 0; display: grid; gap: 4px; }
article strong { font-size: 14px; overflow-wrap: anywhere; }
button { display: inline-flex; align-items: center; justify-content: center; gap: 6px; min-height: 34px; min-width: 34px; }
form { border-left: 3px solid #718b77; padding: 12px; margin-top: 12px; }
form label { display: grid; gap: 6px; margin: 12px 0; }
textarea { box-sizing: border-box; width: 100%; max-width: 100%; padding: 8px; font: inherit; color: inherit; background: transparent; border: 1px solid #b9c2ca; border-radius: 4px; }
.error { color: #b42318; overflow-wrap: anywhere; }
</style>
