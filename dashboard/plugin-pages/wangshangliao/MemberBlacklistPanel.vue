<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { Check, LoaderCircle, RefreshCw, Search, UserMinus, X } from "@lucide/vue";
import type {
  BlacklistEntry, BlacklistMember, BlacklistMode, BlacklistPreview, MemberBlacklistApi,
} from "./memberBlacklistControl";

type Operation = "read" | "search" | "preview" | "settings_preview" | "commit";
const props = defineProps<{
  api: MemberBlacklistApi;
  account: string;
  group: string;
  tenantVersion: number;
  disabled?: boolean;
}>();
const members = ref<BlacklistEntry[]>([]);
const results = ref<BlacklistMember[]>([]);
const query = ref("");
const mode = ref<BlacklistMode>("add");
const selection = ref("");
const chosen = ref(0);
const searched = ref(false);
const loaded = ref(false);
const muteMinutes = ref(41760);
const duration = ref<number | string>(29);
const durationUnit = ref(1440);
const minutes = computed(() => {
  const text = String(duration.value);
  if (!/^(?:\d+(?:\.\d*)?|\.\d+)$/.test(text)) return NaN;
  const [whole, fraction = ""] = text.split(".");
  // Multiply decimal digits before dividing to avoid fractional-minute rounding.
  const numerator = Number(`${whole}${fraction}`) * durationUnit.value;
  return Number.isSafeInteger(numerator) ? numerator / 10 ** fraction.length : NaN;
});
const validDuration = computed(() => Number.isInteger(minutes.value)
  && minutes.value >= 1 && minutes.value <= 41760);
const preview = ref<BlacklistPreview | null>(null);
const pending = ref<Operation | null>(null);
const error = ref("");
const notice = ref("");
const locked = computed(() => Boolean(pending.value) || props.disabled || !props.account || !props.group);
let generation = 0;
const errors: Record<string, string> = {
  blacklist_changed: "成员、名单、设置或权限已变化，请刷新后重试。",
  blacklist_permission: "当前群黑名单不可管理，请核对授权。",
  blacklist_narrow: "匹配超过20位，请输入更完整的名称。",
  blacklist_query: "请输入有效成员名称。",
  blacklist_selection: "本次选择已失效，请重新搜索。",
  blacklist_confirmation: "本次确认已失效，请刷新并重新预览。",
  blacklist_protected: "群主、管理员、机器人或身份已变化的成员不可加入黑名单。",
  blacklist_minutes: "禁言时长须为1至41760整分钟。",
  member_list_conflict: "该成员已在白名单，请先移出白名单，再加入黑名单。",
  service_busy: "服务繁忙，请稍后刷新核对。",
};
const phases: Record<string, string> = {
  mute: "重入禁言",
};
const statuses: Record<string, string> = {
  idle: "等待成员重新入群",
  active: "名单有效 · 执行状态未提供",
  pending: "处理中 · 不重复提交",
  unknown: "结果未知 · 待核对，不自动重试",
  error: "执行出错 · 待核对",
  failed: "执行失败 · 待核对",
  rejected: "执行被拒绝 · 待核对",
  accepted: "已受理 · 待核验",
  verified: "已核验",
};

function clearSearch() {
  generation++;
  pending.value = null;
  results.value = [];
  selection.value = "";
  chosen.value = 0;
  searched.value = false;
  preview.value = null;
  notice.value = "";
}

async function execute(operation: Operation, removal?: BlacklistMember) {
  if (locked.value) return;
  if (operation !== "read" && !loaded.value) return;
  if (removal && (operation !== "search" || !members.value.some(member => member.id === removal.id))) return;
  if (operation === "search" && ((!removal && !query.value.trim()) || preview.value)) return;
  if (operation === "preview" && (!selection.value || preview.value
      || !Number.isInteger(chosen.value) || chosen.value < 1 || chosen.value > results.value.length)) return;
  if (operation === "settings_preview" && (!validDuration.value || preview.value)) return;
  const draft = preview.value;
  if (operation === "commit" && !draft?.token) return;
  if (removal) {
    mode.value = "remove";
    query.value = removal.id;
  }
  if (operation === "read" || operation === "search" || operation === "settings_preview") clearSearch();
  const current = ++generation;
  const target = {
    account: props.account, group_id: props.group, tenant_version: props.tenantVersion,
  };
  pending.value = operation;
  error.value = "";
  notice.value = "";
  let committed = false;
  if (operation === "read") {
    loaded.value = false;
    members.value = [];
  }
  // A confirmation is single use, including when its response is lost.
  if (operation === "commit") preview.value = null;
  try {
    if (operation === "read") {
      const row = await props.api("member_blacklist", { ...target, operation });
      if (current !== generation) return;
      members.value = row.members;
      muteMinutes.value = row.mute_minutes;
      durationUnit.value = row.mute_minutes % 1440 === 0 ? 1440 : row.mute_minutes % 60 === 0 ? 60 : 1;
      duration.value = row.mute_minutes / durationUnit.value;
      loaded.value = true;
    } else if (operation === "search") {
      const row = await props.api("member_blacklist", {
        ...target, operation, mode: mode.value, query: query.value.trim(),
      });
      if (current !== generation) return;
      if (removal) {
        const index = row.members.findIndex(member => member.id === removal.id);
        if (!row.selection || index < 0 || index >= 20
            || row.members.filter(member => member.id === removal.id).length !== 1) {
          throw new Error("blacklist_changed");
        }
        pending.value = "preview";
        const confirmation = await props.api("member_blacklist", {
          ...target, operation: "preview", selection: row.selection, index: index + 1,
        });
        if (current !== generation) return;
        if (!confirmation.token || confirmation.mode !== "remove" || confirmation.member.id !== removal.id) {
          throw new Error("blacklist_changed");
        }
        preview.value = confirmation;
      } else {
        results.value = row.members;
        selection.value = row.selection;
        searched.value = true;
      }
    } else if (operation === "preview") {
      const row = await props.api("member_blacklist", {
        ...target, operation, selection: selection.value, index: chosen.value,
      });
      if (current !== generation) return;
      preview.value = row;
    } else if (operation === "settings_preview") {
      const row = await props.api("member_blacklist", {
        ...target, operation, minutes: minutes.value,
      });
      if (current !== generation) return;
      preview.value = row;
    } else if (draft) {
      const row = await props.api("member_blacklist", { ...target, operation, token: draft.token });
      if (current !== generation) return;
      committed = true;
      results.value = [];
      selection.value = "";
      chosen.value = 0;
      searched.value = false;
      notice.value = row.mode === "settings" ? `已保存禁言时长：${row.mute_minutes} 分钟`
        : `已${row.mode === "add" ? "添加" : "移除"}：${row.member || "已离群成员"}`;
      loaded.value = false;
      members.value = [];
      const latest = await props.api("member_blacklist", { ...target, operation: "read" });
      if (current !== generation) return;
      members.value = latest.members;
      muteMinutes.value = latest.mute_minutes;
      durationUnit.value = latest.mute_minutes % 1440 === 0 ? 1440 : latest.mute_minutes % 60 === 0 ? 60 : 1;
      duration.value = latest.mute_minutes / durationUnit.value;
      loaded.value = true;
    }
  } catch (e: unknown) {
    if (current !== generation) return;
    const message = e instanceof Error ? e.message : "";
    const code = message.startsWith("名单未修改：该成员已在本群另一份名单中，请先移除另一名单，再重新添加。")
      ? "member_list_conflict" : message;
    const knownError = Object.prototype.hasOwnProperty.call(errors, code) ? errors[code] : "";
    error.value = committed ? "变更已保存，但名单读取失败，请刷新核对。"
      : knownError || (operation === "commit"
        ? "变更结果未确认，请刷新名单核对；不会自动重试。"
        : "读取或预览失败，请刷新后重试。");
    results.value = [];
    selection.value = "";
    chosen.value = 0;
    searched.value = false;
    preview.value = null;
    if (operation === "commit") {
      loaded.value = false;
      members.value = [];
    }
  } finally {
    if (current === generation) pending.value = null;
  }
}

watch([mode, query], clearSearch, { flush: "sync" });
watch([duration, durationUnit], () => {
  if (preview.value?.mode === "settings" || pending.value === "settings_preview") clearSearch();
}, { flush: "sync" });
watch(() => [props.account, props.group, props.tenantVersion, props.disabled], () => {
  clearSearch();
  members.value = [];
  loaded.value = false;
  muteMinutes.value = 41760;
  durationUnit.value = 1440;
  duration.value = 29;
  query.value = "";
  error.value = "";
  if (!props.disabled && props.account && props.group) void execute("read");
}, { immediate: true, flush: "sync" });
onBeforeUnmount(() => { generation++; });
</script>

<template>
  <section class="blacklist" aria-label="本群成员黑名单">
    <header>
      <h3>本群成员黑名单<small v-if="loaded">{{ members.length }}</small></h3>
      <button type="button" class="icon-button" title="刷新黑名单" aria-label="刷新黑名单"
        :disabled="locked" @click="execute('read')"><RefreshCw :size="16" /></button>
    </header>
    <p class="muted">仅当前机器人 {{ account }} · 群 {{ group }}，包含已不在群内的黑名单成员。</p>
    <p class="muted">加入名单不立即处罚；每次真实重新入群直接禁言。时长仅影响以后重入，不重设已有禁言；移除不自动解禁。</p>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="pending" class="loading" role="status"><LoaderCircle :size="16" class="spin" />
      {{ pending === 'commit' ? '正在保存并核对' : pending === 'preview' || pending === 'settings_preview' ? '正在生成预览' : '正在读取成员' }}
    </p>
    <fieldset :disabled="locked || Boolean(preview) || !loaded">
      <legend class="visually-hidden">重入禁言设置</legend>
      <form class="duration" @submit.prevent="execute('settings_preview')">
        <label>重入禁言时长<input v-model="duration" type="number" :min="1 / durationUnit"
          :max="41760 / durationUnit" step="any" :aria-invalid="!validDuration"
          aria-describedby="blacklist-duration-range" /></label>
        <label>单位<select v-model="durationUnit" aria-label="禁言时长单位">
          <option :value="1">分钟</option><option :value="60">小时</option><option :value="1440">天</option>
        </select></label>
        <button type="submit" :disabled="!validDuration"><Check :size="16" />预览时长</button>
      </form>
      <p id="blacklist-duration-range" class="muted">
        <template v-if="loaded">当前 {{ muteMinutes }} 分钟；</template>范围1至41760整分钟。
      </p>
    </fieldset>
    <ul v-if="loaded && members.length" class="members" aria-label="已加入黑名单的成员">
      <li v-for="member in members" :key="`${member.id}/${member.peer}`">
        <div class="identity"><strong>{{ member.name || '已离群成员' }}</strong><small>UID {{ member.id }}</small>
          <small v-if="member.conflict" class="warning">同时在白名单</small>
        </div>
        <dl>
          <div><dt>阶段</dt><dd>{{ phases[member.phase] || `待核对（${member.phase || '未提供'}）` }}</dd></div>
          <div><dt>状态</dt><dd :class="{
            warning: ['pending', 'unknown', 'accepted'].includes(member.status),
            error: ['error', 'failed', 'rejected'].includes(member.status),
          }">{{ statuses[member.status] || `待核对（${member.status || '未提供'}）` }}</dd></div>
        </dl>
        <button type="button" class="icon-button remove-member" title="移出黑名单"
          :aria-label="`将${member.name || '已离群成员'}（UID ${member.id}）移出黑名单`"
          :disabled="locked || Boolean(preview) || !loaded" @click="execute('search', member)">
          <UserMinus :size="16" />
        </button>
      </li>
    </ul>
    <p v-else-if="loaded" class="muted">暂无黑名单成员</p>
    <fieldset :disabled="locked || Boolean(preview) || !loaded">
      <legend class="visually-hidden">按名称搜索黑名单成员</legend>
      <div class="modes" role="group" aria-label="黑名单操作">
        <label><input v-model="mode" type="radio" name="blacklist-mode" value="add" />添加成员</label>
        <label><input v-model="mode" type="radio" name="blacklist-mode" value="remove" />移除成员</label>
      </div>
      <form class="search" @submit.prevent="execute('search')">
        <label>成员名称<input v-model="query" type="search" maxlength="64" autocomplete="off"
          aria-label="搜索黑名单成员名称" /></label>
        <button type="submit" :disabled="!query.trim()"><Search :size="16" />搜索</button>
      </form>
      <div v-if="results.length" class="results" role="radiogroup" aria-label="黑名单成员搜索结果">
        <label v-for="(member, index) in results" :key="member.id" class="candidate">
          <input v-model="chosen" type="radio" name="blacklist-member" :value="index + 1" />
          <span>{{ member.name }}<small>UID {{ member.id }}</small></span>
        </label>
      </div>
      <p v-else-if="searched" class="muted">{{ mode === 'add' ? '未找到可添加的本群成员' : '未找到符合条件的黑名单成员' }}</p>
      <button v-if="results.length" type="button" :disabled="!chosen || !selection"
        @click="execute('preview')"><Check :size="16" />选择并预览</button>
    </fieldset>
    <button v-if="pending === 'search' || pending === 'preview' || pending === 'settings_preview'" type="button"
      @click="clearSearch"><X :size="16" />取消</button>
    <div v-if="preview" class="confirmation" role="alertdialog" aria-label="确认本群黑名单变更">
      <template v-if="preview.mode === 'settings'">
        <h4>确认禁言时长</h4>
        <p>{{ preview.mute_minutes }} 分钟</p>
      </template>
      <template v-else>
        <h4>确认{{ preview.mode === 'add' ? '加入' : '移出' }}黑名单</h4>
        <p>{{ preview.member.name || '已离群成员' }} · UID {{ preview.member.id }}</p>
        <p v-if="preview.summary">{{ preview.summary }}</p>
      </template>
      <p class="muted">机器人 {{ account }} · 群 {{ group }}</p>
      <p v-if="preview.mode === 'settings'">仅影响以后重入，不立即处罚、不重设已有禁言。</p>
      <p v-else>{{ preview.mode === 'add' ? `加入名单不立即处罚；以后每次重新入群直接禁言${preview.mute_minutes ?? muteMinutes}分钟。` : '移除后不再按本群黑名单处理，不自动解禁。' }}</p>
      <div class="actions">
        <button type="button" :disabled="locked" @click="execute('commit')"><Check :size="16" />确认保存</button>
        <button type="button" :disabled="locked" @click="clearSearch"><X :size="16" />取消</button>
      </div>
    </div>
  </section>
</template>

<style scoped>
.blacklist { margin-top: 24px; border-top: 1px solid var(--border); padding-top: 16px; min-width: 0; }
header, h3, .actions, .modes, .loading { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; }
header { justify-content: space-between; }
h3, h4 { font-size: 16px; margin: 8px 0; }
fieldset { border: 0; padding: 12px 0; margin: 0; min-width: 0; }
button { max-width: 100%; }
button svg, input[type="radio"] { flex-shrink: 0; }
.search, .duration { display: flex; align-items: end; gap: 8px; margin: 12px 0; }
.search label, .duration label { display: flex; flex: 1; flex-direction: column; gap: 4px; min-width: 0; }
.duration label:first-child { flex: 2; }
.duration button { flex-shrink: 0; }
input[type="search"] { width: 100%; min-width: 0; padding: 8px; border: 1px solid var(--border); border-radius: 4px; color: inherit; background: var(--surface); font: inherit; }
.modes label, .candidate { display: flex; align-items: center; gap: 8px; font-size: 14px; }
.candidate { padding: 10px 4px; border-bottom: 1px solid var(--border); }
.candidate span, .identity { min-width: 0; overflow-wrap: anywhere; }
.members { list-style: none; padding: 0; margin: 8px 0; }
.members li { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 2fr) 36px; gap: 12px; padding: 12px 0; border-bottom: 1px solid var(--border); }
.remove-member { align-self: start; }
dl { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 2fr); gap: 8px; margin: 0; font-size: 13px; }
dt, small { color: var(--muted); font-size: 12px; }
dd { margin: 4px 0 0; overflow-wrap: anywhere; }
small { display: block; }
.results { max-height: 300px; overflow: auto; margin-bottom: 12px; }
.confirmation { padding: 12px; margin-top: 12px; border-left: 3px solid var(--accent); overflow-wrap: anywhere; }
p { margin: 8px 0; font-size: 13px; overflow-wrap: anywhere; }
.muted { color: var(--muted); font-size: 13px; }
.warning { color: var(--warning); }
.error { color: #c44450; }
.visually-hidden { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
@media (max-width: 600px) {
  .duration { flex-wrap: wrap; }
  .duration label { flex: 0 0 88px; }
  .duration label:first-child { flex: 1 1 100px; }
  .members li { grid-template-columns: minmax(0, 1fr) 36px; gap: 8px; }
  .members li dl { grid-column: 1; grid-row: 2; }
  .remove-member { grid-column: 2; grid-row: 1; }
  dl { grid-template-columns: minmax(0, 1fr); gap: 8px; }
  dl > div { display: grid; grid-template-columns: 64px minmax(0, 1fr); gap: 8px; }
  dd { margin: 0; }
}
</style>
