<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { Check, Copy, LoaderCircle } from "@lucide/vue";
import { copyToClipboard } from "../../src/utils/clipboard";

type Report = {
  configured: boolean; catching_up?: boolean;
  groups?: Array<{ id: string; name: string }>;
  days?: Array<{
    date: string; total: number; totals: number[];
    members: Array<{ id: string; name: string; counts: number[]; total: number }>;
  }>;
};
const props = defineProps<{
  botId: string; day: number;
  bridge: { apiGet: (endpoint: string, params: Record<string, string>) => Promise<unknown> };
}>();
const report = ref<Report | null>(null);
const busy = ref(false);
const copying = ref(false);
const error = ref("");
const notice = ref("");
const selected = computed(() => report.value?.days?.[props.day]);
let generation = 0;
let copyGeneration = 0;
let timer: ReturnType<typeof setInterval> | undefined;
async function refresh() {
  if (!props.botId || busy.value) return;
  const current = generation;
  busy.value = true;
  try {
    const result = await props.bridge.apiGet("speech-report", { bot_id: props.botId }) as Report;
    if (current !== generation) return;
    report.value = result; error.value = "";
  } catch {
    if (current === generation) {
      report.value = null;
      error.value = "专门汇报读取失败，请刷新统计后重试；未使用旧汇总。";
    }
  } finally {
    if (current === generation) busy.value = false;
  }
}
async function copy() {
  if (!selected.value || report.value?.catching_up || busy.value || copying.value) return;
  const current = ++copyGeneration;
  const clean = (value: string) => value.replace(/[\s\u0000-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e\u2066-\u2069]+/g, " ").trim();
  const text = [
    `专门发言汇报 · ${selected.value.date}（北京时间）`,
    "统计有效文字聊天，撤回、违规消息、管理命令及图片／视频不计。",
    ...selected.value.members.map(member => [
      `${clean(member.name)}（UID ${member.id}）：合计 ${member.total} 句`,
      ...report.value!.groups!.map((group, index) =>
        `  ${clean(group.name)}（${group.id}）：${member.counts[index]} 句`),
    ].join("\n")),
    `${selected.value.members.length}位成员合计：${selected.value.total} 句`,
    "仅保留今日和昨日；最近30秒待核对。",
  ].join("\n");
  copying.value = true; error.value = ""; notice.value = "";
  try {
    const copied = await copyToClipboard(text);
    if (current !== copyGeneration) return;
    if (copied) notice.value = "已复制专门发言汇报";
    else error.value = "浏览器未允许复制，请再点一次复制汇报。";
  } catch {
    if (current === copyGeneration) error.value = "汇报复制失败，请重试。";
  } finally {
    if (current === copyGeneration) copying.value = false;
  }
}
watch(() => props.day, () => {
  copyGeneration++; copying.value = false; notice.value = ""; error.value = "";
}, { flush: "sync" });
watch(() => props.botId, () => {
  generation++; copyGeneration++; report.value = null; busy.value = false; copying.value = false;
  error.value = ""; notice.value = "";
  if (timer) clearInterval(timer);
  void refresh();
  timer = setInterval(() => { void refresh(); }, 30_000);
}, { immediate: true, flush: "sync" });
onBeforeUnmount(() => { generation++; copyGeneration++; if (timer) clearInterval(timer); });
</script>

<template>
  <section v-if="report?.configured || busy || error" class="focused-report" aria-label="专门发言汇报">
    <header>
      <h3>专门发言汇报</h3>
      <button type="button" class="icon-button" aria-label="复制专门汇报" title="复制所选日期的专门汇报"
        :disabled="!selected || report?.catching_up || busy || copying" @click="copy">
        <LoaderCircle v-if="copying" :size="16" class="spin" />
        <Check v-else-if="notice" :size="16" /><Copy v-else :size="16" />
      </button>
    </header>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-if="notice" role="status" class="muted">{{ notice }}</p>
    <p v-if="busy && !report" role="status" class="muted">正在读取专门汇报</p>
    <template v-if="selected && report">
      <p class="muted">{{ selected.date }} · 北京时间 · {{ report.groups?.length }}群有效文字聊天</p>
      <p v-if="report.catching_up" role="status" class="muted">正在补齐已接收消息，当前汇报尚不完整，暂不可复制。</p>
      <div class="report-scroll" tabindex="0" aria-label="成员分群汇总表">
        <table>
          <thead><tr><th scope="col">成员</th><th scope="col">合计</th><th v-for="group in report.groups" :key="group.id" scope="col">{{ group.name }}</th></tr></thead>
          <tbody><tr v-for="member in selected.members" :key="member.id">
            <th scope="row">{{ member.name }}<small>UID {{ member.id }}</small></th>
            <td><strong>{{ member.total }} 句</strong></td><td v-for="(count, index) in member.counts" :key="index">{{ count }} 句</td>
          </tr></tbody>
          <tfoot><tr><th scope="row">合计</th><td>{{ selected.total }} 句</td><td v-for="(count, index) in selected.totals" :key="index">{{ count }} 句</td></tr></tfoot>
        </table>
      </div>
      <p class="muted definition">按固定UID核对，不按昵称混算。只计有效文字聊天；撤回、违规、机器人、管理命令和图片／视频不计。最近30秒待核对，仅保存今日和昨日，不改变自动白名单规则。</p>
    </template>
  </section>
</template>

<style scoped>
.focused-report { min-width: 0; border-block: 1px solid #d9dfe5; padding: 12px 0; margin: 16px 0; }
header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
h3 { font-size: 15px; margin: 8px 0; }
.muted, small { color: #5f6b75; font-size: 12px; }
.definition { line-height: 1.7; }
.error { color: #b42318; }
.report-scroll { width: 100%; overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 10px 8px; border-bottom: 1px solid #d9dfe5; text-align: right; white-space: nowrap; }
th:first-child { text-align: left; }
thead th { font-weight: 600; background: #f0f3f5; }
small { display: block; font-weight: normal; margin-top: 4px; }
tfoot { font-weight: 600; }
.spin { animation: rotate 1s linear infinite; }
@keyframes rotate { to { transform: rotate(360deg); } }
</style>
