<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { Play, Square, RefreshCw, ListChecks } from "@lucide/vue";

const props = defineProps<{
  bot: Record<string, any>;
  form: Record<string, any>;
  groupId: string;
  bridge: any;
  t: (zh: string, en: string) => string;
}>();
const groups = ref<Array<{ id: string; name: string }>>([]);
const privateTargets = ref<Array<{ value: string; name: string; uid: string }>>([]);
const targets = computed(() => [
  ...props.form.enabled_groups.map((id: string) => ({
    value: id, name: groups.value.find(g => g.id === id)?.name || props.bot.group_names?.[id] || id,
  })),
  ...privateTargets.value.map(item => ({ value: item.value, name: `${item.name} · UID ${item.uid}` })),
]);
const loading = ref(false);
const error = ref("");
const job = ref<Record<string, any> | null>(null);
const windowState = ref<Record<string, any>>({});
const sources = ref<Array<Record<string, any>>>([]);
const taskBusy = ref(false);
const mode = ref("rename");
const confirmed = ref(false);
let sequence = 0;
let taskSequence = 0;

async function directory() {
  const current = ++sequence;
  loading.value = true;
  error.value = "";
  groups.value = [];
  privateTargets.value = [];
  try {
    const data = await props.bridge.apiGet("groups", { bot_id: props.bot.id });
    if (current === sequence) {
      groups.value = data.groups || [];
      privateTargets.value = data.private_targets || [];
    }
  } catch (err) {
    if (current === sequence) error.value = err instanceof Error ? err.message : String(err);
  } finally {
    if (current === sequence) loading.value = false;
  }
}
async function testStatus() {
  const bot = props.bot.id;
  const current = sequence;
  const data = await props.bridge.apiGet("test-window", { bot_id: bot });
  if (bot !== props.bot.id || current !== sequence) return;
  windowState.value = data.window;
  sources.value = data.instances || [];
}
async function task(action: string) {
  if (taskBusy.value) return;
  if (action === "card_execute" && (job.value?.state !== "preview" || (job.value.kind === "cleanup" && !confirmed.value))) return;
  const current = ++taskSequence;
  const bot = props.bot.id;
  const group = props.groupId;
  taskBusy.value = true;
  error.value = "";
  try {
    const result = await props.bridge.apiPost("cards", {
      bot_id: bot, revision: props.bot.revision, group_id: group,
      action: action === "card_preview" && mode.value === "cleanup" ? "cleanup_preview" : action,
      ...(job.value ? { card_job_id: job.value.id || job.value.preview_id } : {}),
    });
    if (current === taskSequence && bot === props.bot.id && group === props.groupId) {
      job.value = result;
      confirmed.value = false;
    }
  } catch (err) {
    if (current === taskSequence) error.value = err instanceof Error ? err.message : String(err);
  } finally {
    if (current === taskSequence) taskBusy.value = false;
  }
}
async function test(action: string) {
  if (taskBusy.value) return;
  const current = ++taskSequence;
  const bot = props.bot.id;
  taskBusy.value = true;
  error.value = "";
  try {
    const data = await props.bridge.apiPost("test-window", {
      bot_id: props.bot.id, revision: props.bot.revision, action,
      ...(action === "test_open" ? props.form.developer_test : {}),
    });
    if (current === taskSequence && bot === props.bot.id) {
      windowState.value = data.window;
      sources.value = data.instances || [];
    }
  } catch (err) {
    if (current === taskSequence) error.value = err instanceof Error ? err.message : String(err);
  } finally {
    if (current === taskSequence) taskBusy.value = false;
  }
}
watch(() => props.bot.id, async (bot) => {
  taskSequence++;
  taskBusy.value = false;
  job.value = null;
  windowState.value = {};
  await directory();
  if (bot !== props.bot.id) return;
  try { await testStatus(); } catch (err) {
    if (bot === props.bot.id) error.value = err instanceof Error ? err.message : String(err);
  }
}, { immediate: true });
watch(() => [props.groupId, mode.value], () => {
  taskSequence++;
  job.value = null;
  confirmed.value = false;
  taskBusy.value = false;
});
onBeforeUnmount(() => { sequence++; taskSequence++; });
</script>

<template>
  <section>
    <div v-if="error" role="alert" class="message error">{{ error }}</div>
    <div class="d-flex">
      <h2>{{ t("启用群聊", "Enabled groups") }}</h2>
      <button type="button" class="icon-button" :disabled="loading" :title="t('刷新群目录', 'Refresh directory')" @click="directory"><RefreshCw :size="18" /></button>
    </div>
    <div v-if="loading" role="status">{{ t("读取群目录中", "Loading group directory") }}</div>
    <label v-for="group in groups" :key="group.id" class="switch-row">
      <span>{{ group.name }} · {{ group.id }}</span>
      <input v-model="form.enabled_groups" type="checkbox" :value="group.id" />
    </label>
    <label v-for="id in form.enabled_groups.filter((id: string) => !groups.some(g => g.id === id))" :key="id" class="switch-row">
      <span>{{ t("已保存群", "Saved group") }} · {{ id }}</span>
      <input v-model="form.enabled_groups" type="checkbox" :value="id" />
    </label>
    <h2>{{ t("主动发送", "Proactive sending") }}</h2>
    <label class="switch-row">
      <span>{{ t("启用主动发送", "Enable proactive sending") }}</span>
      <input v-model="form.proactive_send.enabled" type="checkbox" role="switch" class="switch" />
    </label>
    <label v-for="target in targets" :key="target.value" class="switch-row">
      <span>{{ target.name }}</span><input v-model="form.proactive_send.targets" type="checkbox" :value="target.value" />
    </label>
    <label v-for="value in form.proactive_send.targets.filter((value: string) => !targets.some(t => t.value === value))" :key="value" class="switch-row">
      <span>{{ t("已保存目标（名称未核验）", "Saved destination (name unverified)") }} · {{ value }}</span>
      <input v-model="form.proactive_send.targets" type="checkbox" :value="value" />
    </label>
    <h2>{{ t("名片任务", "Card tasks") }}</h2>
    <div class="header-actions" role="radiogroup" :aria-label="t('任务类型', 'Task type')">
      <label><input v-model="mode" type="radio" value="rename" />{{ t("名片规范", "Normalize cards") }}</label>
      <label><input v-model="mode" type="radio" value="cleanup" />{{ t("封禁／注销清理", "Banned / closed account cleanup") }}</label>
    </div>
    <div class="header-actions">
      <button class="action-button" type="button" :disabled="taskBusy || !groupId" @click="task('card_preview')"><ListChecks :size="17" />{{ t("生成预览", "Preview") }}</button>
      <button v-if="job" class="action-button" type="button" :disabled="taskBusy || job.state !== 'preview' || (job.kind === 'cleanup' && !confirmed)" @click="task('card_execute')"><Play :size="17" />{{ t("确认执行", "Confirm execution") }}</button>
      <button v-if="job" class="icon-button" type="button" :disabled="taskBusy" :title="t('刷新任务', 'Refresh task')" @click="task('card_status')"><RefreshCw :size="17" /></button>
      <button v-if="job" class="action-button" type="button" :disabled="taskBusy" @click="task('card_stop')"><Square :size="17" />{{ t("停止", "Stop") }}</button>
    </div>
    <div v-if="job" class="task-detail">
      <label v-if="job.kind === 'cleanup' && job.state === 'preview'" class="switch-row"><span>{{ t("确认移出预览中的封禁／注销成员", "Confirm removal of the previewed banned / closed accounts") }}</span><input v-model="confirmed" type="checkbox" /></label>
      <p>{{ t("任务状态", "Task status") }}：{{ job.state }} · {{ job.items?.length || 0 }} {{ t("人", "members") }}</p>
      <div v-for="item in job.items || []" :key="item.member" class="switch-row">
        <span>{{ item.original || t("未设置名片", "No card") }} → {{ item.name || t("移除", "Remove") }} · UID {{ item.member }}</span>
        <span>{{ item.state }}</span>
      </div>
    </div>
    <h2>{{ t("测试窗口", "Test window") }}</h2>
    <div class="field-grid">
      <label class="wide">{{ t("来源机器人", "Source bot") }}
        <select v-model="form.developer_test.sender_instance"><option value="">{{ t("请选择", "Select") }}</option><option v-for="source in sources" :key="source.id" :value="source.id">{{ source.id }}</option></select>
      </label>
      <label>{{ t("有效秒数", "Seconds") }}<input v-model.number="form.developer_test.seconds" type="number" min="1" max="300" /></label>
      <label>{{ t("入站次数", "Inbound budget") }}<input v-model.number="form.developer_test.budget" type="number" min="1" max="10" /></label>
    </div>
    <label v-for="scope in ['private_commands', 'private_ai', 'group_commands', 'group_ai', 'group_rules']" :key="scope" class="switch-row">
      <span>{{ ({ private_commands: t("私聊命令", "Private commands"), private_ai: t("私聊 AI", "Private AI"), group_commands: t("群内命令", "Group commands"), group_ai: t("群内 AI", "Group AI"), group_rules: t("违规规则", "Violation rules") } as Record<string, string>)[scope] }}</span><input v-model="form.developer_test.scopes" type="checkbox" :value="scope" />
    </label>
    <label>{{ t("测试群", "Test groups") }}<select v-model="form.developer_test.groups" multiple><option v-for="id in form.enabled_groups" :key="id" :value="id">{{ bot.group_names?.[id] || id }}</option></select></label>
    <label>{{ t("测试关键词（WSL_TEST_ 开头，每行一个）", "Test keywords (WSL_TEST_ prefix, one per line)") }}<textarea :value="form.developer_test.keywords.join('\n')" rows="3" @input="form.developer_test.keywords = ($event.target as HTMLTextAreaElement).value.split('\n').map(v => v.trim()).filter(Boolean)" /></label>
    <div class="header-actions">
      <button class="action-button" type="button" :disabled="taskBusy || windowState.active" @click="test('test_open')"><Play :size="17" />{{ t("开启测试", "Open test window") }}</button>
      <button class="action-button" type="button" :disabled="taskBusy || !windowState.active" @click="test('test_close')"><Square :size="17" />{{ t("关闭测试", "Close test window") }}</button>
      <span>{{ windowState.active ? t("已开启", "Active") : t("未开启", "Closed") }} · {{ windowState.remaining || 0 }}</span>
    </div>
  </section>
</template>

<style scoped>
.d-flex { display: flex; align-items: center; justify-content: space-between; }
.task-detail { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 320px; overflow: auto; font-size: 13px; }
</style>
