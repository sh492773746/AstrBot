<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { KeyRound, RefreshCw, Save, UserMinus } from "@lucide/vue";

type Role = "cashier" | "finance_admin";
type Scope = { account: string; group_id: string; name: string; configured: boolean; bot_id: string };
type Grant = {
  account: string; group_id: string; uid: string; peer: string; name: string;
  role: Role; active: boolean; version: number;
};
type FinanceStatus = { version: 1; grants: Grant[]; groups: Scope[]; test_active: boolean };
type GrantRequest = Pick<Grant, "account" | "group_id" | "uid" | "role" | "active" | "version"> & {
  request_id: string;
};

const props = defineProps<{ bridge: { apiGet: Function; apiPost: Function } }>();
const status = ref<FinanceStatus | null>(null);
const loading = ref(false);
const saving = ref(false);
const error = ref("");
const notice = ref("");
const scopeKey = ref("");
const uid = ref("");
const role = ref<Role | "">("");
const members = ref<Array<{ uid: string; name: string }>>([]);
const membersLoading = ref(false);
const membersReady = ref(false);
const pending = ref<GrantRequest | null>(null);
let memberSequence = 0;
const scope = computed(() => status.value?.groups.find(
  group => JSON.stringify([group.account, group.group_id]) === scopeKey.value,
));
const member = computed(() => members.value.find(item => item.uid === uid.value));
const locked = computed(() => loading.value || saving.value || Boolean(pending.value));
const canSave = computed(() => Boolean(
  status.value && !locked.value && scope.value && membersReady.value
  && !membersLoading.value && member.value && ["cashier", "finance_admin"].includes(role.value),
));

async function loadMembers() {
  const sequence = ++memberSequence;
  const selected = scope.value;
  members.value = [];
  uid.value = "";
  role.value = "";
  membersReady.value = false;
  membersLoading.value = Boolean(selected);
  error.value = "";
  notice.value = "";
  if (!selected) return;
  try {
    const result = await props.bridge.apiPost("open-service", {
      action: "finance_members", payload: { account: selected.account, group_id: selected.group_id },
    });
    if (sequence !== memberSequence) return;
    if (!Array.isArray(result.members)) throw new Error("群成员读取失败，请刷新");
    members.value = result.members;
    membersReady.value = true;
  } catch (e) {
    if (sequence === memberSequence) error.value = e instanceof Error ? e.message : "群成员读取失败，请刷新";
  } finally {
    if (sequence === memberSequence) membersLoading.value = false;
  }
}

async function load() {
  if (loading.value) return;
  loading.value = true;
  error.value = "";
  notice.value = "";
  memberSequence++;
  members.value = [];
  uid.value = "";
  role.value = "";
  membersReady.value = false;
  membersLoading.value = false;
  status.value = null;
  try {
    const result = await props.bridge.apiPost("open-service", { action: "finance_status", payload: {} });
    if (result.version !== 1 || !Array.isArray(result.grants) || !Array.isArray(result.groups)) {
      throw new Error("结算授权状态不可用，请刷新");
    }
    status.value = result;
    pending.value = null;
    if (!scope.value) scopeKey.value = "";
    await loadMembers();
  } catch (e) {
    error.value = e instanceof Error ? e.message : "结算授权读取失败，请刷新";
  } finally {
    loading.value = false;
  }
}

async function submit() {
  if (!pending.value || saving.value || loading.value) return;
  const request = { ...pending.value };
  saving.value = true;
  error.value = "";
  notice.value = "";
  try {
    const result = await props.bridge.apiPost("open-service", { action: "finance_grant", payload: request });
    if (result.status !== "saved") throw new Error("操作未确认，请重试或刷新核对");
    pending.value = null;
    await load();
    if (status.value) notice.value = request.active ? "结算授权已保存" : "结算授权已撤销";
  } catch (e) {
    error.value = e instanceof Error ? e.message : "操作未确认，请重试或刷新核对";
  } finally {
    saving.value = false;
  }
}

async function changeGrant(row?: Grant) {
  if (!status.value || locked.value) return;
  let target: Omit<GrantRequest, "request_id">;
  if (row) {
    const current = status.value.grants.find(item =>
      item.account === row.account && item.group_id === row.group_id && item.uid === row.uid
      && item.role === row.role && item.version === row.version && item.active,
    );
    if (!current || !window.confirm(
      `撤销${current.name || current.uid}的${current.role === "cashier" ? "结账员" : "财务管理员"}授权？\n账号 ${current.account} · 群 ${current.group_id} · UID ${current.uid}`,
    )) return;
    target = {
      account: current.account, group_id: current.group_id, uid: current.uid,
      role: current.role, active: false, version: current.version,
    };
  } else {
    if (!canSave.value || !scope.value || !member.value || !role.value) return;
    const existing = status.value.grants.find(item =>
      item.account === scope.value!.account && item.group_id === scope.value!.group_id
      && item.uid === member.value!.uid,
    );
    target = {
      account: scope.value.account, group_id: scope.value.group_id, uid: member.value.uid,
      role: role.value, active: true, version: existing?.version ?? 0,
    };
  }
  try {
    pending.value = { ...target, request_id: crypto.randomUUID() };
  } catch {
    error.value = "无法生成请求标识，请使用安全连接后重试";
    return;
  }
  await submit();
}

watch(scopeKey, () => { void loadMembers(); }, { flush: "sync" });
watch(uid, () => { role.value = ""; }, { flush: "sync" });
onMounted(() => { void load(); });
onBeforeUnmount(() => { memberSequence++; });
</script>

<template>
  <section class="finance-grants" aria-label="结算授权">
    <header>
      <h3><KeyRound :size="18" />结算授权</h3>
      <button type="button" class="icon-button" title="刷新结算授权" aria-label="刷新结算授权"
        :disabled="loading || saving" @click="load"><RefreshCw :size="17" /></button>
    </header>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="loading" role="status" class="muted">读取授权中...</p>
    <form v-if="status" @submit.prevent="changeGrant()">
      <div class="grant-fields">
        <label>账号 / 群聊
          <select v-model="scopeKey" aria-label="结算授权范围" :disabled="locked">
            <option value="">选择账号 / 群聊</option>
            <option v-for="group in status.groups" :key="JSON.stringify([group.account, group.group_id])"
              :value="JSON.stringify([group.account, group.group_id])">
              {{ group.name || group.group_id }} · {{ group.account }} / {{ group.group_id }}{{ group.configured ? "" : " · 未配置" }}
            </option>
          </select>
        </label>
        <label>群成员
          <select v-model="uid" aria-label="结算授权成员" :disabled="locked || !membersReady || membersLoading">
            <option value="">{{ membersLoading ? "读取成员中..." : "选择群成员" }}</option>
            <option v-for="item in members" :key="item.uid" :value="item.uid">{{ item.name }} · {{ item.uid }}</option>
          </select>
        </label>
        <label>角色
          <select v-model="role" aria-label="结算授权角色" :disabled="locked || !member">
            <option value="">选择角色</option>
            <option value="cashier">结账员</option>
            <option value="finance_admin">财务管理员</option>
          </select>
        </label>
      </div>
      <p v-if="scope && membersReady && !members.length" role="status" class="muted">暂无可授权成员</p>
      <button type="submit" :disabled="!canSave"><Save :size="16" />保存结算授权</button>
    </form>
    <button v-if="pending" type="button" :disabled="loading || saving" @click="submit">
      <RefreshCw :size="16" />重试{{ pending.active ? "授权" : "撤销" }}
    </button>
    <ul v-if="status?.grants.length" class="grant-list" aria-label="结算授权记录">
      <li v-for="row in status.grants" :key="JSON.stringify([row.account, row.group_id, row.uid])">
        <div>
          <strong>{{ status.groups.find(group => group.account === row.account && group.group_id === row.group_id)?.name || row.group_id }}</strong>
          <small>账号 {{ row.account }} · 群 {{ row.group_id }}</small>
        </div>
        <div><strong>{{ row.name || row.uid }}</strong><small>UID {{ row.uid }}</small></div>
        <div>
          <span>{{ row.role === "cashier" ? "结账员" : "财务管理员" }}</span>
          <small :class="{ active: row.active }">{{ row.active ? "已授权" : "已撤销" }} · 版本 {{ row.version }}</small>
        </div>
        <button type="button" class="icon-button revoke"
          :title="`撤销${row.name || row.uid}的${row.role === 'cashier' ? '结账员' : '财务管理员'}授权`"
          :aria-label="`撤销授权：${row.account}/${row.group_id}/${row.uid}/${row.role}`"
          :disabled="locked || !row.active" @click="changeGrant(row)"><UserMinus :size="17" /></button>
      </li>
    </ul>
    <p v-else-if="status && !loading" class="muted">暂无结算授权</p>
  </section>
</template>

<style scoped>
.finance-grants { min-width: 0; margin-top: 28px; padding-top: 20px; border-top: 1px solid #dce2e2; }
header, h3 { display: flex; align-items: center; gap: 10px; }
header { justify-content: space-between; }
h3 { margin: 0; padding: 0; border: 0; font-size: 16px; }
.grant-fields { display: grid; grid-template-columns: minmax(0, 1.5fr) minmax(0, 1fr) minmax(0, .8fr); gap: 12px; margin: 16px 0 12px; }
label { display: grid; gap: 7px; min-width: 0; font-size: 14px; }
select { width: 100%; min-width: 0; min-height: 38px; padding: 8px; font: inherit; color: #202828; background: #fff; border: 1px solid #9daaaa; border-radius: 4px; text-overflow: ellipsis; }
button { display: inline-flex; align-items: center; justify-content: center; gap: 7px; min-height: 36px; padding: 7px 12px; border: 1px solid #becaca; border-radius: 4px; color: #164d46; background: #eef5f3; cursor: pointer; white-space: normal; }
button:disabled { opacity: .5; cursor: default; }
button svg { flex-shrink: 0; }
.icon-button { width: 36px; height: 36px; padding: 0; flex-shrink: 0; }
.revoke, .error { color: #b52b35; }
p { margin: 12px 0; font-size: 14px; overflow-wrap: anywhere; }
.muted, small { color: #657676; }
.grant-list { list-style: none; margin: 16px 0 0; padding: 0; }
.grant-list li { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr) minmax(0, 1fr) 36px; align-items: center; gap: 12px; padding: 12px 0; border-top: 1px solid #dce2e2; font-size: 14px; }
.grant-list li > div { min-width: 0; overflow-wrap: anywhere; }
strong { font-weight: 600; }
small { display: block; margin-top: 4px; font-size: 12px; }
.active { color: #164d46; }
* { letter-spacing: 0; box-sizing: border-box; }
@media (max-width: 700px) {
  .grant-fields { grid-template-columns: minmax(0, 1fr); }
  .grant-list li { grid-template-columns: minmax(0, 1fr) 36px; gap: 8px 12px; }
  .grant-list li > div { grid-column: 1; }
  .revoke { grid-column: 2; grid-row: 1 / 4; }
}
</style>
