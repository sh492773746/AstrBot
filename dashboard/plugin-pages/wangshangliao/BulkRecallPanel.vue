<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { Check, LoaderCircle, RefreshCw, Search, Square, Undo2, X } from "@lucide/vue";

const props = defineProps<{
  bridge: { apiPost: (endpoint: string, body: unknown) => Promise<any> };
  account: string;
  group: string;
  locale: string;
}>();
const t = (zh: string, en: string) => props.locale.startsWith("zh") ? zh : en;
type Member = { id: string; name: string; departed?: boolean };
type Message = { id: string; received_at: number; kind: string };
type Preview = { member: string; name: string; confirmation: string; messages: Message[]; skipped: number; departed?: boolean };
type Job = {
  id: string; state: string; error: string; name: string; member: string;
  total: number; counts: Record<string, number>; remaining: number;
};
const query = ref("");
const members = ref<Member[]>([]);
const selected = ref("");
const minutes = ref(15);
const endAt = ref("");
const version = ref(0);
const busy = ref(false);
const error = ref("");
const pending = ref<Preview | null>(null);
const job = ref<Job | null>(null);
const searched = ref(false);
const running = computed(() => !!job.value && ["queued", "running"].includes(job.value.state));
let active = true;
let timer: ReturnType<typeof setTimeout> | undefined;
onBeforeUnmount(() => { active = false; clearTimeout(timer); });
async function api(payload: Record<string, unknown>) {
  return props.bridge.apiPost("open-service", {
    action: "message_recalls",
    payload: { account: props.account, group_id: props.group, ...payload },
  });
}
function poll() {
  clearTimeout(timer);
  if (active && running.value) timer = setTimeout(() => run(read), 2500);
}
async function run(task: () => Promise<void>) {
  if (!active || busy.value) { poll(); return; }
  busy.value = true;
  error.value = "";
  try { await task(); }
  catch (e: any) {
    if (active) error.value = e?.message || t("操作未确认，请刷新核对", "Operation unconfirmed; refresh to check");
  } finally {
    if (active) { busy.value = false; poll(); }
  }
}
async function read() {
  const result = await api({ operation: "read" });
  if (active) { version.value = result.version; job.value = result.job; }
}
async function search() {
  pending.value = null;
  selected.value = "";
  members.value = [];
  searched.value = false;
  const result = await api({ operation: "search", query: query.value.trim() });
  if (active) {
    version.value = result.version;
    members.value = result.members;
    searched.value = true;
  }
}
async function preview() {
  pending.value = null;
  const end = endAt.value ? Date.parse(`${endAt.value}+08:00`) / 1000 : undefined;
  if (end !== undefined && !Number.isFinite(end)) {
    throw Error(t("截止时间无效，请重新选择", "Invalid end time; select it again"));
  }
  const result = await api({
    operation: "preview", version: version.value, member: selected.value, minutes: minutes.value,
    ...(end !== undefined ? { end_at: end } : {}),
  });
  if (active) pending.value = result;
}
async function commit() {
  const preview = pending.value;
  if (!preview) return;
  // Never offer a second submission after an uncertain commit response.
  pending.value = null;
  const result = await api({ operation: "commit", confirmation: preview.confirmation });
  if (active) job.value = result.job;
}
async function cancel() {
  if (!job.value) return;
  await api({ operation: "cancel", job: job.value.id });
  if (active) await read();
}
const stamp = (value: number) => new Date(value * 1000).toLocaleString(
  props.locale, { timeZone: "Asia/Shanghai", hour12: false },
);
const stateName = (state: string) => ({
  queued: t("等待处理", "Queued"), running: t("正在撤回", "Recalling"),
  completed: t("处理结束", "Finished"), stopped: t("已停止，请核对回执", "Stopped; check receipts"),
  cancelled: t("已停止后续撤回", "Remaining recalls cancelled"),
  interrupted: t("任务中断，未自动续跑", "Interrupted; not resumed automatically"),
}[state] || state);
const kindName = (kind: string) => ({
  image: t("图片", "Image"), video: t("视频", "Video"),
  text: t("文字", "Text"), sticker: t("表情", "Sticker"),
}[kind] || t("消息", "Message"));
onMounted(() => run(read));
</script>

<template>
  <section class="bulk-recall" :aria-label="t('刷屏消息批量撤回', 'Bulk flood recall')">
    <header>
      <h2>{{ t("刷屏消息批量撤回", "Bulk flood recall") }}</h2>
      <button type="button" :disabled="busy" :title="t('刷新进度', 'Refresh progress')"
        :aria-label="t('刷新进度', 'Refresh progress')" @click="run(read)"><RefreshCw :size="18" /></button>
    </header>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <form class="search" @submit.prevent="run(search)">
      <label>{{ t("群成员名称或 UID", "Member name or UID") }}
        <input v-model="query" maxlength="80" required :disabled="busy || running" :aria-label="t('群成员名称或 UID', 'Member name or UID')" />
      </label>
      <button type="submit" :disabled="busy || running || !query.trim()"><Search :size="16" />{{ t("搜索", "Search") }}</button>
    </form>
    <p v-if="searched && !members.length">{{ t("没有可核实的当前或历史成员", "No verified current or historical members") }}</p>
    <div v-if="members.length" class="selection">
      <label>{{ t("选择成员", "Member") }}
        <select v-model="selected" :disabled="busy || running" :aria-label="t('选择成员', 'Member')" @change="pending = null">
          <option value="" disabled>{{ t("请选择", "Select") }}</option>
          <option v-for="member in members" :key="member.id" :value="member.id">{{ member.name }} · {{ member.id }}{{ member.departed ? t(" · 已离群", " · Left group") : "" }}</option>
        </select>
      </label>
      <label>{{ t("最近时间", "Lookback") }}
        <select v-model.number="minutes" :disabled="busy || running" :aria-label="t('最近时间', 'Lookback')" @change="pending = null">
          <option v-for="value in [5, 15, 30, 60]" :key="value" :value="value">{{ value }} {{ t("分钟", "minutes") }}</option>
        </select>
      </label>
      <label>{{ t("截止时间（北京时间，留空为现在）", "End time (Asia/Shanghai; blank means now)") }}
        <input v-model="endAt" type="datetime-local" :disabled="busy || running"
          :aria-label="t('截止时间', 'End time')" @change="pending = null" />
      </label>
      <button type="button" :disabled="busy || running || !selected" @click="run(preview)">
        <Undo2 :size="16" />{{ t("预览撤回", "Preview recalls") }}
      </button>
    </div>
    <div v-if="pending" class="confirmation" role="alertdialog" :aria-label="t('确认批量撤回', 'Confirm bulk recall')">
      <strong>{{ pending.name }} · {{ pending.member }}</strong>
      <p v-if="pending.departed">{{ t("已离群 · 历史消息", "Left group · Historical messages") }}</p>
      <p>{{ t("机器人", "Bot") }} {{ account }} · {{ t("群", "Group") }} {{ group }}</p>
      <p>{{ t("命中刷屏消息", "Flood messages") }} {{ pending.messages.length }} · {{ t("已撤回并跳过", "Already recalled; skipped") }} {{ pending.skipped }}</p>
      <ol class="messages" :aria-label="t('待撤回消息 · 北京时间', 'Selected messages · Asia/Shanghai')">
        <li v-for="message in pending.messages" :key="message.id">
          <time>{{ stamp(message.received_at) }}</time><span>{{ kindName(message.kind) }}</span><small>{{ message.id }}</small>
        </li>
      </ol>
      <div class="actions">
        <button type="button" :disabled="busy" @click="run(commit)"><Check :size="16" />{{ t("确认撤回", "Confirm recall") }}</button>
        <button type="button" :disabled="busy" @click="pending = null"><X :size="16" />{{ t("取消", "Cancel") }}</button>
      </div>
    </div>
    <div v-if="job" class="progress" role="status" aria-live="polite">
      <strong>{{ stateName(job.state) }}</strong>
      <p>{{ job.name }} · {{ job.member }}</p>
      <dl>
        <div><dt>{{ t("总数", "Total") }}</dt><dd>{{ job.total }}</dd></div>
        <div><dt>{{ t("平台受理", "Accepted") }}</dt><dd>{{ job.counts.accepted || 0 }}</dd></div>
        <div><dt>{{ t("独立核实", "Verified") }}</dt><dd>{{ job.counts.verified || 0 }}</dd></div>
        <div><dt>{{ t("跳过", "Skipped") }}</dt><dd>{{ job.counts.skipped || 0 }}</dd></div>
        <div><dt>{{ t("未知", "Unknown") }}</dt><dd>{{ job.counts.unknown || 0 }}</dd></div>
        <div><dt>{{ t("拒绝", "Rejected") }}</dt><dd>{{ job.counts.rejected || 0 }}</dd></div>
        <div><dt>{{ t("尚未提交", "Unsubmitted") }}</dt><dd>{{ job.remaining }}</dd></div>
        <div v-if="job.counts.submitting"><dt>{{ t("提交中", "In flight") }}</dt><dd>{{ job.counts.submitting }}</dd></div>
      </dl>
      <button v-if="running" type="button" :disabled="busy" @click="run(cancel)"><Square :size="16" />{{ t("停止后续撤回", "Stop remaining recalls") }}</button>
    </div>
    <p v-if="busy" role="status"><LoaderCircle class="spin" :size="16" />{{ t("核验中", "Checking") }}</p>
  </section>
</template>

<style scoped>
.bulk-recall { padding-bottom: 24px; margin-bottom: 24px; border-bottom: 1px solid #dce2e2; min-width: 0; }
header, .search, .selection, .actions { display: flex; align-items: end; flex-wrap: wrap; gap: 10px; }
header { align-items: center; justify-content: space-between; }
h2 { margin: 0; font-size: 16px; }
form, .selection { margin: 14px 0; }
label { display: grid; gap: 6px; font-size: 13px; min-width: 0; flex: 1 1 180px; }
input, select { box-sizing: border-box; width: 100%; max-width: 100%; min-width: 0; height: 38px; padding: 7px; border: 1px solid #becaca; border-radius: 4px; background: #fff; color: #253635; font: inherit; }
button { display: inline-flex; align-items: center; justify-content: center; gap: 7px; min-height: 38px; padding: 7px 10px; border: 1px solid #becaca; border-radius: 4px; background: #eef5f3; color: #164d46; font: inherit; cursor: pointer; flex-shrink: 0; }
button:disabled { opacity: .5; cursor: default; }
p { font-size: 13px; overflow-wrap: anywhere; }
.error { color: #b52b35; }
.confirmation { border-left: 3px solid #12786b; padding: 12px; margin: 12px 0; overflow-wrap: anywhere; }
.messages { max-height: 240px; overflow: auto; margin: 12px 0; padding: 0; list-style: none; }
li { display: flex; flex-wrap: wrap; gap: 4px 12px; padding: 8px 0; border-top: 1px solid #e5eaea; font-size: 12px; }
li small { flex: 1 1 100%; color: #657676; overflow-wrap: anywhere; }
dl { display: flex; flex-wrap: wrap; gap: 12px 20px; margin: 14px 0; }
dt { color: #657676; font-size: 12px; }
dd { margin: 4px 0 0; font-size: 16px; font-variant-numeric: tabular-nums; }
.progress { margin-top: 16px; overflow-wrap: anywhere; }
.spin { vertical-align: middle; margin-right: 6px; animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
* { letter-spacing: 0; }
</style>
