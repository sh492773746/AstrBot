<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { Check, LoaderCircle, RefreshCw, Search, UserMinus, X } from "@lucide/vue";

type Member = { id: string; name: string; conflict?: boolean };
type Mode = "add" | "remove";
type Preview = { token: string; mode: Mode; member: Member };
type Operation = "read" | "search" | "preview" | "commit";
type Scope = { account: string; group_id: string; tenant_version: number };
interface MemberWhitelistApi {
  (action: "member_whitelist", payload: Scope & { operation: "read" }):
    Promise<{ version: number; members: Member[] }>;
  (action: "member_whitelist", payload: Scope & { operation: "search"; mode: Mode; query: string }):
    Promise<{ version: number; members: Member[]; selection: string }>;
  (action: "member_whitelist", payload: Scope & { operation: "preview"; selection: string; index: number }):
    Promise<Preview>;
  (action: "member_whitelist", payload: Scope & { operation: "commit"; token: string }):
    Promise<{ saved: true; mode: Mode; member: string }>;
}
const props = defineProps<{
  api: MemberWhitelistApi; account: string; group: string; tenantVersion: number; disabled?: boolean;
}>();
const members = ref<Member[]>([]);
const results = ref<Member[]>([]);
const query = ref("");
const mode = ref<Mode>("add");
const selection = ref("");
const chosen = ref(0);
const searched = ref(false);
const preview = ref<Preview | null>(null);
const pending = ref<Operation | null>(null);
const loaded = ref(false);
const error = ref("");
const notice = ref("");
const locked = computed(() => Boolean(pending.value) || props.disabled || !props.account || !props.group);
let generation = 0;
const errors: Record<string, string> = {
  member_whitelist_changed: "成员、名单或权限已变化，请重新搜索。",
  member_whitelist_permission: "当前群白名单不可管理，请核对授权。",
  member_whitelist_narrow: "匹配超过20位，请输入更完整的名称。",
  member_whitelist_query: "请输入有效成员名称。",
  member_whitelist_selection: "本次选择已失效，请重新搜索。",
  member_whitelist_confirmation: "本次确认已失效，请刷新并重新预览。",
  member_list_conflict: "该成员已在黑名单，请先移出黑名单，再加入白名单。",
  service_busy: "服务繁忙，请稍后刷新核对。",
};
function clearSearch() {
  generation++;
  pending.value = null;
  results.value = []; selection.value = ""; chosen.value = 0;
  preview.value = null; searched.value = false; notice.value = "";
}
async function execute(operation: Operation, removal?: Member) {
  if (locked.value) return;
  if (operation !== "read" && !loaded.value) return;
  if (removal && (operation !== "search" || !members.value.some(member => member.id === removal.id))) return;
  if (operation === "search" && ((!removal && !query.value.trim()) || preview.value)) return;
  if (operation === "preview" && (!selection.value || preview.value
      || !Number.isInteger(chosen.value) || chosen.value < 1 || chosen.value > results.value.length)) return;
  const draft = preview.value;
  if (operation === "commit" && !draft?.token) return;
  if (removal) {
    mode.value = "remove";
    query.value = removal.id;
  }
  if (operation === "read" || operation === "search") clearSearch();
  const current = ++generation;
  const target = {
    account: props.account, group_id: props.group, tenant_version: props.tenantVersion,
  };
  pending.value = operation;
  error.value = ""; notice.value = "";
  let committed = false;
  if (operation === "read") {
    loaded.value = false;
    members.value = [];
  }
  // A confirmation is single use, including when its response is lost.
  if (operation === "commit") preview.value = null;
  try {
    if (operation === "read") {
      const row = await props.api("member_whitelist", { ...target, operation });
      if (current !== generation) return;
      members.value = row.members;
      loaded.value = true;
    } else if (operation === "search") {
      const row = await props.api("member_whitelist", {
        ...target, operation, mode: mode.value, query: query.value.trim(),
      });
      if (current !== generation) return;
      if (removal) {
        const index = row.members.findIndex(member => member.id === removal.id);
        if (!row.selection || index < 0 || index >= 20
            || row.members.filter(member => member.id === removal.id).length !== 1) {
          throw new Error("member_whitelist_changed");
        }
        pending.value = "preview";
        const confirmation = await props.api("member_whitelist", {
          ...target, operation: "preview", selection: row.selection, index: index + 1,
        });
        if (current !== generation) return;
        if (!confirmation.token || confirmation.mode !== "remove" || confirmation.member.id !== removal.id) {
          throw new Error("member_whitelist_changed");
        }
        preview.value = confirmation;
      } else {
        results.value = row.members; selection.value = row.selection;
        chosen.value = 0; searched.value = true;
      }
    } else if (operation === "preview") {
      const row = await props.api("member_whitelist", {
        ...target, operation, selection: selection.value, index: chosen.value,
      });
      if (current !== generation) return;
      preview.value = row;
    } else if (draft) {
      const row = await props.api("member_whitelist", { ...target, operation, token: draft.token });
      if (current !== generation) return;
      committed = true;
      results.value = []; selection.value = ""; chosen.value = 0; searched.value = false;
      notice.value = `已${row.mode === "add" ? "添加" : "移除"}：${row.member || "已离群成员"}`;
      loaded.value = false;
      members.value = [];
      const latest = await props.api("member_whitelist", { ...target, operation: "read" });
      if (current !== generation) return;
      members.value = latest.members;
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
    results.value = []; selection.value = ""; chosen.value = 0; searched.value = false;
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
watch(() => [props.account, props.group, props.tenantVersion, props.disabled], () => {
  clearSearch(); members.value = []; loaded.value = false; query.value = "";
  error.value = "";
  if (!props.disabled && props.account && props.group) void execute("read");
}, { immediate: true, flush: "sync" });
onBeforeUnmount(() => { generation++; });
</script>

<template>
  <section class="whitelist" aria-label="本群发言白名单">
    <header><h3>本群发言白名单</h3>
      <button type="button" class="icon-button" title="刷新白名单" aria-label="刷新白名单" :disabled="locked"
        @click="execute('read')"><RefreshCw :size="16" /></button>
    </header>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="pending" class="loading" role="status"><LoaderCircle :size="16" class="spin" />
      {{ pending === 'commit' ? '正在保存并核对' : pending === 'preview' ? '正在生成预览' : '正在读取成员' }}
    </p>
    <ul v-if="loaded && members.length" class="members" aria-label="已加入白名单的成员">
      <li v-for="member in members" :key="member.id">
        <div class="identity"><strong>{{ member.name || '已离群成员' }}</strong><small>UID {{ member.id }}</small>
          <small v-if="member.conflict" class="warning">同时在黑名单</small>
        </div>
        <button type="button" class="icon-button" title="移出白名单"
          :aria-label="`将${member.name || '已离群成员'}（UID ${member.id}）移出白名单`"
          :disabled="locked || Boolean(preview) || !loaded" @click="execute('search', member)">
          <UserMinus :size="16" />
        </button>
      </li>
    </ul>
    <p v-else-if="loaded" class="muted">暂无白名单成员</p>
    <fieldset :disabled="locked || Boolean(preview) || !loaded">
      <legend class="visually-hidden">搜索当前群成员</legend>
      <div class="modes" role="group" aria-label="白名单操作">
        <label><input v-model="mode" type="radio" name="whitelist-mode" value="add" />添加成员</label>
        <label><input v-model="mode" type="radio" name="whitelist-mode" value="remove" />移除成员</label>
      </div>
      <form class="search" @submit.prevent="execute('search')">
        <label>成员名称<input v-model="query" type="search" maxlength="64"
          autocomplete="off" aria-label="搜索本群成员名称" /></label>
        <button type="submit" :disabled="!query.trim()"><Search :size="16" />搜索</button>
      </form>
      <div v-if="results.length" class="results" role="radiogroup" aria-label="本群成员搜索结果">
        <label v-for="(member, index) in results" :key="member.id" class="candidate">
          <input v-model="chosen" type="radio" name="whitelist-member" :value="index + 1" />
          <span>{{ member.name }}<small>UID {{ member.id }}</small></span>
        </label>
      </div>
      <p v-else-if="searched" class="muted">未找到符合条件的本群成员</p>
      <button v-if="results.length" type="button" :disabled="!chosen || !selection"
        @click="execute('preview')"><Check :size="16" />选择并预览</button>
    </fieldset>
    <button v-if="pending === 'search' || pending === 'preview'" type="button"
      @click="clearSearch"><X :size="16" />取消</button>
    <div v-if="preview" class="confirmation" role="alertdialog" aria-label="确认本群白名单变更">
      <h4>确认{{ preview.mode === 'add' ? '加入' : '移出' }}白名单</h4>
      <p>{{ preview.member.name || '已离群成员' }} · UID {{ preview.member.id }}</p>
      <p class="muted">机器人 {{ account }} · 群 {{ group }} · 不自动解除已有禁言</p>
      <p>{{ preview.mode === 'add' ? '加入后豁免自动处罚，不会解除已有禁言。' : '移除后不再豁免自动处罚，不会解除已有禁言。' }}</p>
      <div class="actions">
        <button type="button" :disabled="locked" @click="execute('commit')"><Check :size="16" />确认保存</button>
        <button type="button" :disabled="locked" @click="clearSearch"><X :size="16" />取消</button>
      </div>
    </div>
  </section>
</template>

<style scoped>
.whitelist { margin-top: 24px; border-top: 1px solid var(--border, #d9dde1); padding-top: 16px; min-width: 0; }
header, .actions, .modes, .loading { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; }
header { justify-content: space-between; }
h3, h4 { font-size: 16px; margin: 8px 0; }
fieldset { border: 0; padding: 12px 0; margin: 0; min-width: 0; }
button { display: inline-flex; align-items: center; justify-content: center; gap: 6px; min-height: 36px; }
.search { display: flex; align-items: end; gap: 8px; margin: 12px 0; }
.search label { display: flex; flex: 1; flex-direction: column; gap: 4px; min-width: 0; font-size: 14px; }
input[type="search"] { width: 100%; min-width: 0; box-sizing: border-box; padding: 8px; border: 1px solid #b9c2ca; border-radius: 4px; color: inherit; background: transparent; font: inherit; }
.modes label, .candidate { display: flex; align-items: center; gap: 8px; font-size: 14px; }
.candidate { padding: 10px 4px; border-bottom: 1px solid var(--border, #d9dde1); }
.candidate span, .identity { min-width: 0; overflow-wrap: anywhere; }
.members { list-style: none; padding: 0; margin: 8px 0; }
.members li { display: grid; grid-template-columns: minmax(0, 1fr) 36px; align-items: start; gap: 12px; padding: 10px 0; border-bottom: 1px solid var(--border); }
small { display: block; color: var(--muted); font-size: 12px; }
.results { max-height: 300px; overflow: auto; margin-bottom: 12px; }
.confirmation { padding: 12px; margin-top: 12px; border-left: 3px solid var(--accent); }
p { overflow-wrap: anywhere; }
.muted { color: var(--muted); font-size: 13px; }
.warning { color: var(--warning); }
.error { color: #b42318; }
.visually-hidden { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
</style>
