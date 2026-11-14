<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { Check, Copy, LoaderCircle, Search, X } from "@lucide/vue";
import { copyToClipboard } from "../../src/utils/clipboard";
import type {
  HistoryMember, HistoryPeriod, HistoryResponse, WhitelistHistoryApi, WhitelistMember,
} from "./whitelistHistory";

const props = defineProps<{
  api: WhitelistHistoryApi; account: string; group: string; tenantVersion: number;
  listVersion: number; members: WhitelistMember[]; disabled?: boolean;
}>();
const selected = ref("");
const period = ref<HistoryPeriod>("recent");
const query = ref("");
const data = ref<HistoryResponse | null>(null);
const busy = ref(false);
const progress = ref("");
const error = ref("");
const notice = ref("");
const locked = computed(() => Boolean(props.disabled) || busy.value || !props.members.length);
let generation = 0;
let loadedAt = 0;

function reset() {
  generation++;
  loadedAt = 0;
  data.value = null; busy.value = false; progress.value = ""; error.value = ""; notice.value = "";
}
function clock(stamp: number) {
  return new Date(stamp * 1000).toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai", hour12: false,
  });
}
async function load(copy = false) {
  if (locked.value) return;
  const chosen = props.members.filter(member => !selected.value || member.id === selected.value);
  if (!chosen.length) return;
  // A recent loaded snapshot also permits a synchronous clipboard retry in opaque frames.
  const cached = copy && data.value && Date.now() - loadedAt < 30_000 ? data.value : null;
  const current = ++generation;
  const target = {
    account: props.account, group_id: props.group, tenant_version: props.tenantVersion,
    list_version: props.listVersion, period: period.value, query: query.value.trim(),
  };
  busy.value = true; error.value = ""; notice.value = ""; data.value = null;
  const rows: HistoryMember[] = cached ? [...cached.members] : [];
  let snapshot: HistoryResponse | null = cached;
  let updatedAt = Infinity;
  let size = 0;
  try {
    for (let offset = 0; !cached && offset < chosen.length; offset += 5) {
      if (current !== generation) return;
      progress.value = `正在读取 ${Math.min(offset + 5, chosen.length)} / ${chosen.length} 位成员`;
      const ids = chosen.slice(offset, offset + 5).map(member => member.id);
      const result = await props.api("member_whitelist", {
        ...target, operation: "history", member_ids: ids,
      });
      if (current !== generation) return;
      if (result.version !== target.list_version || result.period !== target.period
          || result.query !== target.query || result.timezone !== "Asia/Shanghai"
          || result.members.length !== ids.length
          || result.members.some((member, index) => member.id !== ids[index] || member.messages.length > 100)
          || (snapshot && (snapshot.from_date !== result.from_date || snapshot.to_date !== result.to_date))) {
        throw new Error("member_whitelist_changed");
      }
      if (result.catching_up) throw new Error("history_catching_up");
      for (const member of result.members) {
        size += member.messages.reduce((sum, message) => sum + message.text.length, 0) + member.name.length;
        if (size > 2_000_000) throw new Error("history_too_large");
      }
      rows.push(...result.members);
      updatedAt = Math.min(updatedAt, result.updated_at);
      snapshot = { ...result, updated_at: updatedAt };
    }
    if (!snapshot || current !== generation) return;
    data.value = { ...snapshot, members: rows };
    if (!cached) loadedAt = Date.now();
    progress.value = "";
    if (!copy) return;
    const label = (value: string) => value.replace(/[\s\u0000-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e\u2066-\u2069]+/g, " ").trim();
    const text = [
      `群 ${props.group} · 发言白名单对话汇总`,
      `${snapshot.from_date}—${snapshot.to_date}（北京时间）`,
      "有效发言仅计文字；撤回、违规、机器人及管理命令不计；最近30秒待核对。",
      ...(target.query ? [`内容筛选：${label(target.query)}`] : []),
      ...rows.flatMap(member => [
        "",
        `名字：${label(member.name) || member.id}（UID ${member.id}）`,
        `有效发言：${member.count}句`,
        `历史对话内容：最近${member.messages.length}条${target.query ? ` / 筛选命中${member.matched_count}条` : ""}（由旧到新，最多100条）`,
        ...(member.unavailable ? [`原记录不可读：${member.unavailable}条，未补写内容。`] : []),
        ...(member.messages.length ? member.messages.map((message, index) =>
          `${index + 1}. [${clock(message.stamp)}] ${message.text.replace(/\r?\n/g, "\n   ")}`) : ["暂无符合条件的有效文字对话。"]),
      ]),
    ].join("\n");
    if (current !== generation) return;
    const copied = await copyToClipboard(text).catch(() => false);
    if (current !== generation) return;
    if (copied) notice.value = `已复制${rows.length}位白名单成员及${rows.reduce((sum, member) => sum + member.messages.length, 0)}条对话`;
    else error.value = "浏览器未允许复制，结果已保留，请再次点击复制。";
  } catch (e) {
    if (current !== generation) return;
    const code = e instanceof Error ? e.message : "";
    error.value = code === "history_catching_up" ? "统计正在补齐，请稍后再筛选或复制，未导出不完整记录。"
      : code === "history_too_large" ? "记录较多，请选择单个成员或缩小日期、关键词范围后再复制。"
      : code === "member_whitelist_changed" || code === "tenant_changed"
        ? "白名单、群或日期已变化，请刷新白名单后重新读取。"
        : code === "service_busy" ? "历史查询繁忙，请稍后重试，不影响群消息处理。"
          : code === "member_whitelist_history_unavailable" ? "原消息账本暂不可读，请稍后重试；未复制部分结果。"
          : "历史记录读取失败，请核对当前群授权后重试；未复制部分结果。";
    data.value = null;
  } finally {
    if (current === generation) { busy.value = false; progress.value = ""; }
  }
}
watch([selected, period, query], reset, { flush: "sync" });
watch(() => [
  props.account, props.group, props.tenantVersion, props.listVersion, props.disabled,
  JSON.stringify(props.members),
], reset, { flush: "sync" });
onBeforeUnmount(reset);
</script>

<template>
  <section class="history" aria-label="白名单历史对话">
    <header><h4>白名单历史对话</h4>
      <button type="button" class="icon-button" title="复制白名单及历史对话" aria-label="复制白名单及历史对话"
        :disabled="locked" @click="load(true)">
        <Check v-if="notice" :size="16" /><Copy v-else :size="16" />
      </button>
    </header>
    <div class="filters">
      <label>白名单成员<select v-model="selected" aria-label="白名单成员" :disabled="Boolean(disabled)">
        <option value="">全部白名单成员</option>
        <option v-for="member in members" :key="member.id" :value="member.id">{{ member.name || member.id }} · {{ member.id }}</option>
      </select></label>
      <label>日期<select v-model="period" aria-label="历史对话日期" :disabled="Boolean(disabled)">
        <option value="recent">今日和昨日</option><option value="today">今日</option><option value="yesterday">昨日</option>
      </select></label>
    </div>
    <form class="search" @submit.prevent="load(false)">
      <label>对话关键词<input v-model="query" type="search" maxlength="64" autocomplete="off"
        aria-label="对话关键词" placeholder="不限关键词" :disabled="Boolean(disabled)" /></label>
      <button type="submit" :disabled="locked"><Search :size="16" />筛选最近100条</button>
    </form>
    <p v-if="busy" class="muted" role="status"><LoaderCircle :size="16" class="spin" />{{ progress }}
      <button type="button" class="icon-button" title="取消读取历史" aria-label="取消读取历史" @click="reset"><X :size="16" /></button>
    </p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="notice" class="muted" role="status">{{ notice }}</p>
    <div v-if="data" class="records">
      <p class="muted">{{ data.from_date }}—{{ data.to_date }} · 北京时间 · 文字有效发言 · 每人最多100条</p>
      <details v-for="member in data.members" :key="member.id" :open="data.members.length === 1">
        <summary>{{ member.name || member.id }} · 有效发言 {{ member.count }} 句 · 对话 {{ member.messages.length }} 条</summary>
        <p class="muted">UID {{ member.id }}<template v-if="data.query"> · 筛选命中 {{ member.matched_count }} 条</template></p>
        <p v-if="member.unavailable" class="muted">{{ member.unavailable }}条原记录不可读，未补写内容。</p>
        <ol v-if="member.messages.length">
          <li v-for="message in member.messages" :key="message.message">
            <time>{{ clock(message.stamp) }}</time><p>{{ message.text }}</p>
          </li>
        </ol>
        <p v-else class="muted">暂无符合条件的有效文字对话。</p>
      </details>
    </div>
  </section>
</template>

<style scoped>
.history { margin: 16px 0; padding-block: 12px; border-block: 1px solid var(--border, #d9dde1); min-width: 0; }
header { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
h4 { margin: 6px 0; font-size: 15px; }
.filters { display: grid; grid-template-columns: minmax(0, 2fr) minmax(0, 1fr); gap: 10px; margin-block: 12px; }
label { display: grid; gap: 5px; min-width: 0; font-size: 13px; }
select, input { box-sizing: border-box; width: 100%; min-width: 0; padding: 8px; font: inherit; color: inherit; background: var(--surface, #fff); border: 1px solid var(--border, #b9c2ca); border-radius: 4px; }
.search { display: flex; align-items: end; gap: 8px; }
.search label { flex: 1; }
button { display: inline-flex; justify-content: center; align-items: center; gap: 5px; min-height: 36px; }
.search button { flex-shrink: 0; font-size: 13px; }
.muted { color: var(--muted, #5f6b75); font-size: 12px; overflow-wrap: anywhere; }
p[role="status"] { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.records { margin-top: 12px; }
details { padding-block: 10px; border-top: 1px solid var(--border, #d9dde1); }
summary { cursor: pointer; font-size: 13px; overflow-wrap: anywhere; }
ol { max-height: 420px; overflow: auto; padding-left: 30px; }
li { padding: 8px 0; border-bottom: 1px solid var(--border, #d9dde1); font-size: 13px; }
time { color: var(--muted, #5f6b75); font-size: 11px; }
li p { white-space: pre-wrap; overflow-wrap: anywhere; margin: 5px 0; }
.error { color: #b42318; font-size: 13px; overflow-wrap: anywhere; }
.spin { animation: rotate 1s linear infinite; }
@keyframes rotate { to { transform: rotate(360deg); } }
@media (max-width: 360px) { .filters { grid-template-columns: minmax(0, 1fr); } }
</style>
