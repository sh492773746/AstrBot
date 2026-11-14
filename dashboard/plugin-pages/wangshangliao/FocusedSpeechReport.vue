<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { Check, Copy, Download, LoaderCircle } from "@lucide/vue";
import { copyToClipboard } from "../../src/utils/clipboard";
import { reportTable, reportTsv, reportPng, type SpeechReport, type ReportView } from "./speechReportExport";

const props = defineProps<{
  botId: string; day: number;
  bridge: { apiGet: (endpoint: string, params: Record<string, string>) => Promise<unknown> };
}>();
const report = ref<SpeechReport | null>(null);
const view = ref<ReportView>("summary");
const group = ref("");
const busy = ref(false);
const copying = ref(false);
const error = ref("");
const notice = ref("");
const selected = computed(() => report.value?.days?.[props.day]);
const table = computed(() => report.value ? reportTable(report.value, props.day, view.value, group.value) : null);
const exportDisabled = computed(() => !table.value || report.value?.catching_up || busy.value || copying.value);
let generation = 0;
let copyGeneration = 0;
let exportAbort: AbortController | undefined;
let timer: ReturnType<typeof setInterval> | undefined;
async function refresh() {
  if (!props.botId || busy.value) return;
  const current = generation;
  busy.value = true;
  try {
    const result = await props.bridge.apiGet("speech-report", { bot_id: props.botId }) as SpeechReport;
    if (current !== generation) return;
    report.value = result; error.value = "";
    if (group.value && !result.groups?.some(item => item.id === group.value)) group.value = "";
  } catch {
    if (current === generation) {
      report.value = null;
      error.value = "专门汇报读取失败，请刷新统计后重试；未使用旧汇总。";
    }
  } finally {
    if (current === generation) busy.value = false;
  }
}
async function exportReport(kind: "text" | "png") {
  if (exportDisabled.value) return;
  const current = ++copyGeneration;
  const snapshot = table.value!;
  const filename = `专门发言汇报-${selected.value!.date}-${group.value || "全部群"}-${view.value}.png`;
  let url = "";
  copying.value = true; error.value = ""; notice.value = "";
  try {
    if (kind === "text") {
      const copied = await copyToClipboard(reportTsv(snapshot));
      if (current !== copyGeneration) return;
      if (copied) notice.value = "已复制表格";
      else error.value = "浏览器未允许复制，请重试或下载PNG图片。";
    } else {
      exportAbort = new AbortController();
      const image = await reportPng(snapshot, exportAbort.signal);
      if (current !== copyGeneration) return;
      url = URL.createObjectURL(image);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link); link.click(); link.remove();
      // Keep the object URL alive until the browser has started its download.
      const downloadUrl = url;
      setTimeout(() => URL.revokeObjectURL(downloadUrl), 1000);
      url = "";
      notice.value = "已生成PNG下载";
    }
  } catch {
    if (current === copyGeneration) error.value = "图片或表格导出未完成，请重试；仍失败可先复制表格。";
  } finally {
    if (url) URL.revokeObjectURL(url);
    if (current === copyGeneration) { copying.value = false; exportAbort = undefined; }
  }
}
watch(() => [props.day, view.value, group.value], () => {
  exportAbort?.abort(); exportAbort = undefined;
  copyGeneration++; copying.value = false; notice.value = ""; error.value = "";
}, { flush: "sync" });
watch(() => props.botId, () => {
  exportAbort?.abort(); exportAbort = undefined;
  generation++; copyGeneration++; report.value = null; busy.value = false; copying.value = false;
  error.value = ""; notice.value = ""; group.value = "";
  if (timer) clearInterval(timer);
  void refresh();
  timer = setInterval(() => { void refresh(); }, 30_000);
}, { immediate: true, flush: "sync" });
onBeforeUnmount(() => { generation++; copyGeneration++; exportAbort?.abort(); if (timer) clearInterval(timer); });
</script>

<template>
  <section v-if="report?.configured || busy || error" class="focused-report" aria-label="专门发言汇报">
    <header>
      <h3>专门发言汇报</h3>
      <div class="export-actions">
        <button type="button" class="icon-button" aria-label="复制专门汇报表格" title="复制表格"
          :disabled="exportDisabled" @click="exportReport('text')">
          <LoaderCircle v-if="copying" :size="16" class="spin" />
          <Check v-else-if="notice === '已复制表格'" :size="16" /><Copy v-else :size="16" />
        </button>
        <button type="button" class="icon-button" aria-label="下载专门汇报PNG图片" title="下载PNG图片"
          :disabled="exportDisabled" @click="exportReport('png')"><Download :size="16" /></button>
      </div>
    </header>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-if="notice" role="status" class="muted">{{ notice }}</p>
    <p v-if="busy && !report" role="status" class="muted">正在读取专门汇报</p>
    <template v-if="selected && report">
      <p class="muted">{{ selected.date }} · 北京时间 · {{ report.groups?.length }}群有效文字聊天</p>
      <p v-if="report.catching_up" role="status" class="muted">正在补齐已接收消息，当前汇报尚不完整，暂不可复制。</p>
      <div class="report-controls">
        <div class="view-switch" role="group" aria-label="专门汇报视图">
          <button type="button" :aria-pressed="view === 'summary'" @click="view = 'summary'">每日汇总</button>
          <button type="button" :aria-pressed="view === 'hourly'" @click="view = 'hourly'">每小时明细</button>
        </div>
        <label class="group-filter">统计范围
          <select v-model="group" aria-label="专门汇报统计范围">
            <option value="">全部统计群</option>
            <option v-for="item in report.groups" :key="item.id" :value="item.id">{{ item.name }}（{{ item.id }}）</option>
          </select>
        </label>
      </div>
      <p v-if="!table && view === 'hourly'" class="error" role="alert">小时明细暂不可读，请刷新统计；未用每日总数代替。</p>
      <div v-if="table" class="report-scroll" tabindex="0" :aria-label="view === 'hourly' ? '成员每小时发言明细表' : '成员分群汇总表'">
        <table>
          <thead><tr><th v-for="(column, index) in table.columns" :key="index" scope="col">{{ column }}</th></tr></thead>
          <tbody><tr v-for="(row, index) in table.rows.slice(0, -1)" :key="index">
            <th scope="row">{{ row[0] }}</th>
            <td v-for="(value, column) in row.slice(1)" :key="column">{{ value }}</td>
          </tr></tbody>
          <tfoot><tr><th scope="row">{{ table.rows[table.rows.length - 1][0] }}</th><td v-for="(value, index) in table.rows[table.rows.length - 1].slice(1)" :key="index">{{ value }}</td></tr></tfoot>
        </table>
      </div>
      <p class="muted definition">按固定UID核对，不按昵称混算。只计有效文字聊天；撤回、违规、机器人、管理命令和图片／视频不计。最近30秒待核对，仅保存今日和昨日，不改变自动白名单规则。</p>
    </template>
  </section>
</template>

<style scoped>
.focused-report { min-width: 0; border-block: 1px solid #d9dfe5; padding: 12px 0; margin: 16px 0; }
header { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.export-actions { display: flex; gap: 6px; }
.export-actions button { width: 34px; height: 34px; flex: 0 0 34px; display: inline-flex; align-items: center; justify-content: center; }
.report-controls { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; margin: 12px 0; }
.view-switch { display: inline-flex; border: 1px solid #c4cdd3; border-radius: 4px; overflow: hidden; }
.view-switch button { border: 0; border-radius: 0; padding: 8px 10px; font-size: 12px; background: transparent; }
.view-switch button[aria-pressed="true"] { background: #e4ede9; color: #214a39; font-weight: 600; }
.group-filter { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; font-size: 12px; min-width: 0; max-width: 100%; }
.group-filter select { min-width: 0; max-width: 100%; padding: 6px; font: inherit; border: 1px solid #c4cdd3; border-radius: 4px; }
h3 { font-size: 15px; margin: 8px 0; }
.muted, small { color: #5f6b75; font-size: 12px; }
.definition { line-height: 1.7; }
.error { color: #b42318; }
.report-scroll { width: 100%; overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 10px 8px; border-bottom: 1px solid #d9dfe5; text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
th:first-child { text-align: left; position: sticky; left: 0; background: white; }
thead th { max-width: 180px; min-width: 100px; white-space: normal; overflow-wrap: anywhere; font-weight: 600; background: #f0f3f5; }
small { display: block; font-weight: normal; margin-top: 4px; }
tfoot { font-weight: 600; }
.spin { animation: rotate 1s linear infinite; }
@keyframes rotate { to { transform: rotate(360deg); } }
</style>
