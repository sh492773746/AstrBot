<script setup lang="ts">
import { computed, onMounted, ref, toRaw, watch } from "vue";
import { ArrowUpCircle, KeyRound, RefreshCw, Save, ShieldCheck } from "@lucide/vue";
import { getPlatformInstanceName } from "../../src/utils/platformInstanceName.mjs";
import AIProfileEditor from "./AIProfileEditor.vue";
import AIPlansPanel from "./AIPlansPanel.vue";
import FinanceGrantsPanel from "./FinanceGrantsPanel.vue";
import GroupRulesEditor from "./GroupRulesEditor.vue";
import MemberBlacklistPanel from "./MemberBlacklistPanel.vue";
import MemberWhitelistPanel from "./MemberWhitelistPanel.vue";
import RuleAutomationPanel from "./RuleAutomationPanel.vue";
import SupportSettingsPanel from "./SupportSettingsPanel.vue";
import ComponentRuntimePanel from "./ComponentRuntimePanel.vue";
import type { MemberBlacklistApi } from "./memberBlacklistControl";

const props = defineProps<{ bridge: { apiGet: Function; apiPost: Function }; view?: string }>();
const viewNames: Record<string, string> = {
  groups: "开通", rules: "规则", consultation: "客服AI咨询", "admin-guidance": "管理员指引AI",
  "group-ai": "群聊与热群AI",
  plans: "套餐与额度", model: "模型与预算", finance: "结算授权",
  runtime: "服务总控", audit: "群管审计",
};
const localView = ref("groups");
const view = computed(() => props.view && props.view in viewNames ? props.view : localView.value);
const visited = ref(new Set<string>());
watch(view, value => visited.value.add(value), { immediate: true });
const state = ref<any>(null);
const busy = ref(false);
const error = ref("");
const notice = ref("");
const selected = ref("");
const tenant = ref<any>(null);
const keywordText = ref("");
const members = ref<Array<{ id: string; name: string; role?: string }>>([]);
const memberSearch = ref("");
const selectedManagers = ref<string[]>([]);
const managerCandidates = computed(() => members.value.filter(m =>
  ["GROUP_ROLE_OWNER", "GROUP_ROLE_ADMIN"].includes(m.role || "")));
const invalidManagers = computed(() => membersReady.value ? selectedManagers.value.filter(
  uid => !managerCandidates.value.some(m => m.id === uid),
) : []);
const visibleManagers = computed(() => managerCandidates.value.filter(m =>
  selectedManagers.value.includes(m.id)
    || `${m.name} ${m.id}`.toLowerCase().includes(memberSearch.value.toLowerCase())));
const membersReady = ref(false);
let selectionSequence = 0;
const adminText = ref("");
const code = ref("");
const pendingTenant = ref<any>(null);
const pendingAdmission = ref<any>(null);
const model = ref({ provider_id: "" });
const profileDefaults = {
  config_id: "", persona_id: "", provider_id: "", temperature: null,
  top_p: null, max_tokens: null, history_turns: null,
};
const privateProfile = ref<Record<string, any>>({ ...profileDefaults });
const supportAccount = ref("");
const adminProfile = ref<Record<string, any>>({ ...profileDefaults });
const defaultGroupProfile = ref<Record<string, any>>({ ...profileDefaults });
const tenantProfile = ref<Record<string, any>>({ ...profileDefaults });
const useDefaultGroupProfile = ref(true);
const profileChoices = computed(() => state.value?.ai_choices || { configs: [], personas: [] });
const budgetProvider = ref("");
function savedProfile(kind: string, account = "", group = "") {
  return state.value?.ai_profiles?.find((r: any) =>
    r.kind === kind && r.account === account && r.group_id === group);
}
async function saveProfile(kind: string, forGroup = false) {
  const account = forGroup ? tenant.value.account : kind === "consultation" ? supportAccount.value : "";
  const group = forGroup ? tenant.value.group_id : "";
  const selection = forGroup ? (useDefaultGroupProfile.value ? null : tenantProfile.value)
    : kind === "consultation" ? privateProfile.value
      : kind === "admin_guidance" ? adminProfile.value : defaultGroupProfile.value;
  await api("ai_profile", {
    kind, account, group_id: group, version: savedProfile(kind, account, group)?.version || 0,
    selection, ...(forGroup ? { tenant_version: tenant.value.version } : {}),
  });
  await reload();
  notice.value = kind === "admin_guidance"
    ? "管理员指引配置已保存，权限分流已启用" : "AI对话配置已保存";
}
const budgetEvidence = ref("");
const budgetConfirmed = ref(false);
const aiDefaults = {
  enabled: false, mentions: true, autonomous: false, idle: false, interval_minutes: 15,
  daily_limit: 0, start_hour: 9, end_hour: 23,
  topic: "友善、轻松的日常交流，不发布广告。",
};
const groupAI = ref({ ...aiDefaults });
const allDay = computed({
  get: () => groupAI.value.start_hour === groupAI.value.end_hour,
  set: value => {
    groupAI.value.start_hour = value ? 0 : 9;
    groupAI.value.end_hour = value ? 0 : 23;
  },
});
const groupAIRow = ref<any>(null);
const resumeGroupAI = ref(false);
const endpoint = "open-service";

async function api(action: string, payload: any = {}) {
  return await props.bridge.apiPost(endpoint, {
    action, payload: JSON.parse(JSON.stringify(payload)),
  });
}
const blacklistApi: MemberBlacklistApi = api;
async function run(task: () => Promise<void>) {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  notice.value = "";
  try { await task(); } catch (e: any) { error.value = e?.message || "操作未确认，请刷新核对"; }
  finally { busy.value = false; }
}
async function reload() {
  const current = await api("status");
  if (current.mode !== "integrated") {
    state.value = null;
    tenant.value = null;
    throw new Error("独立开放服务已停用，请在 AstrBot 中启用合并模式后刷新。");
  }
  state.value = current;
  adminText.value = (state.value.admins || []).join("\n");
  model.value = { provider_id: state.value.model?.provider_id || "" };
  if (!(state.value.accounts || []).some((a: any) => a.account === supportAccount.value)) {
    supportAccount.value = state.value.accounts?.[0]?.account || "";
  }
  privateProfile.value = { ...(savedProfile("consultation", supportAccount.value)?.selection || profileDefaults) };
  adminProfile.value = { ...(savedProfile("admin_guidance")?.selection || profileDefaults) };
  defaultGroupProfile.value = { ...(savedProfile("group")?.selection || profileDefaults) };
  budgetProvider.value = state.value.ai_budget?.provider || model.value.provider_id;
  budgetConfirmed.value = Boolean(state.value.ai_budget?.confirmed);
  budgetEvidence.value = "";
  await choose();
}
watch(supportAccount, () => {
  privateProfile.value = { ...(savedProfile("consultation", supportAccount.value)?.selection || profileDefaults) };
});
async function provisionProfile(account: string) {
  await api("provision_profiles", { account });
  await reload();
  notice.value = "独立配置与人格已核验";
}
async function choose() {
  const sequence = ++selectionSequence;
  code.value = "";
  pendingTenant.value = null;
  pendingAdmission.value = null;
  const row = state.value?.groups.find((g: any) => `${g.account}/${g.group_id}` === selected.value);
  tenant.value = row ? structuredClone(toRaw(row)) : null;
  keywordText.value = row?.rules.keywords.join("\n") || "";
  selectedManagers.value = [...(row?.managers || [])];
  groupAIRow.value = state.value?.group_ai?.find((item: any) =>
    item.account === row?.account && item.group_id === row?.group_id) || null;
  groupAI.value = { ...aiDefaults, ...(groupAIRow.value?.config || {}) };
  resumeGroupAI.value = false;
  const profile = savedProfile("group", row?.account, row?.group_id);
  useDefaultGroupProfile.value = !profile?.selection;
  tenantProfile.value = { ...(profile?.selection || defaultGroupProfile.value) };
  members.value = [];
  membersReady.value = false;
  memberSearch.value = "";
  if (row?.present && !row.membership_pending
      && ["pending", "legacy", "verified"].includes(row.admission || "legacy")
      && (row.cleanup_epoch || 0) <= (row.cleanup_done || 0)) {
    const response = await api("tenant_members", {account: row.account, group_id: row.group_id});
    if (sequence !== selectionSequence) return;
    members.value = response.members;
    tenant.value.native_capability = response.native_capability || tenant.value.native_capability;
    membersReady.value = true;
  }
}
async function saveGroupAI() {
  const row = tenant.value;
  if (!row) return;
  await api("group_ai", {
    account: row.account, group_id: row.group_id, tenant_version: row.version,
    version: groupAIRow.value?.version || 0,
    config: { ...groupAI.value }, resume: resumeGroupAI.value,
  });
  await reload();
  notice.value = state.value.ai_budget?.confirmed
    ? "本群AI设置已保存" : "本群AI设置已保存，预算核验前不调用模型";
}
async function saveTenant() {
  if (!tenant.value) return;
  const row = tenant.value;
  const rules = { ...row.rules, keywords: keywordText.value.split("\n").map(w => w.trim()).filter(Boolean) };
  const saved = state.value.groups.find((g: any) => g.account === row.account && g.group_id === row.group_id);
  if (!rules.custom_rules?.length && !("custom_rules" in saved.rules)) delete rules.custom_rules;
  const payload = {
    account: row.account, group_id: row.group_id, version: row.version,
    paused: Boolean(view.value === "groups" ? row.paused : saved.paused),
    managers: view.value === "groups" ? [...selectedManagers.value] : [...saved.managers],
    rules: view.value === "rules" ? rules : structuredClone(toRaw(saved.rules)),
  };
  if (JSON.stringify(saved.managers) !== JSON.stringify(payload.managers)) {
    const preview = await api("tenant_preview", payload);
    pendingTenant.value = { payload, confirmation: preview.confirmation, previous: saved.managers };
    return;
  }
  await api("tenant", payload);
  await reload();
  notice.value = "本群配置已保存";
}
async function previewRulesProvision() {
  const row = tenant.value;
  if (!row || selectedManagers.value.length !== 1) return;
  const payload = {
    account: row.account, group_id: row.group_id, version: row.version,
    manager: selectedManagers.value[0],
  };
  const preview = await api("rules_provision_preview", payload);
  pendingTenant.value = {
    payload, confirmation: preview.confirmation, previous: [],
    action: "rules_provision",
  };
}
async function previewAIAdmission() {
  const row = tenant.value;
  if (!row?.active || !row.rules_only) return;
  const payload = { account: row.account, group_id: row.group_id, version: row.version };
  const preview = await api("ai_admission_preview", payload);
  pendingAdmission.value = { ...payload, confirmation: preview.confirmation };
}
async function confirmAIAdmission() {
  if (!pendingAdmission.value) return;
  await api("ai_admission", pendingAdmission.value);
  await reload();
  notice.value = "正式AI准入已确认；AI仍关闭，承担人确认与预算校验保持独立";
}
async function resumeReview() {
  const row = tenant.value;
  if (!row || !window.confirm("确认已复核丢失的审核证据，并恢复本群后续自动审核？")) return;
  await api("review_resume", {account: row.account, group_id: row.group_id, version: row.version});
  await reload();
  notice.value = "后续自动审核已恢复，历史未决操作未重放";
}
async function saveLimits() {
  const s = state.value.settings;
  await api("limits", {
    version: s.version, paused: Boolean(s.paused),
    personal_limit: 20, global_limit: Number(s.global_limit),
    admins: adminText.value.split(/\s+/).filter(Boolean),
  });
  await reload();
  notice.value = "额度及服务状态已保存";
}
function membershipStatus(row: any): string {
  if (row.cleanup_epoch > row.cleanup_done) return "离群清理中";
  if (row.admission === "departed") return "已离群 · 工作数据已清理";
  if (row.admission === "rejected") {
    return row.leave_state === "accepted" ? "邀请者非群管理 · 已申请退群"
      : row.leave_state === "rejected" ? "邀请者非群管理 · 退群被拒绝"
      : "邀请者非群管理 · 退群结果待核对";
  }
  if (row.membership_pending) return "成员身份待核验 · 已停用";
  if (!row.present) return "目录暂未确认成员身份 · 已停用";
  if (row.admission === "pending") return "真实邀请来源及群管理身份待核验";
  return row.active ? (row.paused ? "本群已暂停" : row.rules_only ? "已开通 · 仅规则" : "已开通") : "待开通";
}
onMounted(() => run(reload));
</script>

<template>
  <section class="open-service" aria-label="开放服务">
    <header>
      <h2><ShieldCheck :size="20" />{{ viewNames[view] }}</h2>
      <button type="button" title="刷新开放服务" :disabled="busy" @click="run(reload)"><RefreshCw :size="18" /></button>
    </header>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <template v-if="state">
      <nav v-if="!props.view" class="local-views" aria-label="群管理功能">
        <button v-for="(name, id) in viewNames" :key="id" type="button"
          :aria-pressed="view === id" @click="localView = id">{{ name }}</button>
      </nav>
      <p v-if="state.settings.paused && view !== 'runtime'" class="capability" role="status">开放服务已暂停</p>
      <div v-show="view === 'runtime'">
      <ComponentRuntimePanel v-if="view === 'runtime'" :api="api" />
      <div class="runtime" role="status">
        <strong>{{ state.settings.paused ? "开放服务已暂停" : "开放服务运行中" }}</strong>
        <span>占用开通名额 {{ state.allocated_count }} / {{ state.group_limit }} 群</span>
        <span>正在运行 {{ state.operating_count }} 群</span>
        <span>{{ state.ai_budget?.confirmed ? "AI硬预算已核验" : "AI未开放 · 硬预算待核验" }}</span>
        <span v-if="state.health">磁盘余量 {{ (state.health.disk_free_bytes / 1024 ** 3).toFixed(1) }} GiB</span>
        <span v-if="state.health?.capacity_paused" class="error">存储异常，自动暂停新增操作</span>
        <span v-else-if="state.health?.disk_warning" class="error">磁盘余量不足 2 GiB</span>
        <span v-for="q in state.queue_health" :key="q.account">
          消息待处理 {{ q.pending }} · 拒绝积压 {{ q.rejected }}
          <template v-if="q.review_groups.length"> · {{ q.review_groups.length }} 群审核已暂停待复核</template>
        </span>
        <span v-if="state.usage">今日AI {{ state.usage.used }} / {{ state.settings.global_limit }} · 超管 {{ state.usage.exempt_used }} 次</span>
        <span v-for="a in state.accounts" :key="a.instance_id">{{ getPlatformInstanceName({ type: "wangshangliao", id: a.instance_id, account_id: a.account, nickname: a.nickname }) }}<template v-if="a.nickname"> · {{ a.account }}</template> · {{ a.state }} · 已启用 {{ a.enabled_groups }} 群</span>
      </div>
      <div class="fields">
        <label>试用每日共享次数<input :value="20" type="number" readonly /></label>
        <label>全站每日调用安全上限<input v-model.number="state.settings.global_limit" type="number" min="1" max="100000" /></label>
        <label class="check"><input v-model="state.settings.paused" type="checkbox" :true-value="1" :false-value="0" />暂停开放服务（含私聊AI和命令）</label>
        <label>AI免每日次数账号（仍受费用预算保护）<textarea v-model="adminText" rows="2" /></label>
      </div>
      <button type="button" :disabled="busy" @click="run(saveLimits)"><Save :size="16" />保存额度与状态</button>
      </div>

      <div v-show="['groups', 'rules', 'group-ai', 'plans'].includes(view)" class="tenant-scope">
      <label>群聊<select v-model="selected" aria-label="群聊" :disabled="busy" @change="run(choose)">
        <option value="">选择群聊</option>
        <option v-for="g in state.groups" :key="`${g.account}/${g.group_id}`" :value="`${g.account}/${g.group_id}`">
          {{ g.name }} · {{ state.accounts.find((a: any) => a.account === g.account)?.nickname || g.account }} · {{ membershipStatus(g) }}
        </option>
      </select></label>
      <template v-if="tenant">
        <p class="capability" role="status">{{ membershipStatus(tenant) }}</p>
        <p class="capability" :class="{ error: tenant.native_capability?.state === 'missing' }">
          原生群管理权限：{{ tenant.native_role === 'GROUP_ROLE_MEMBER' || tenant.native_capability?.state === 'missing' ? '缺失，配置保留，处罚已暂停' : tenant.native_capability?.state === 'available' ? '已核验' : '待核验' }}
        </p>
      </template>
      </div>
      <div v-if="['groups', 'group-ai'].includes(view) && tenant?.active && tenant.rules_only"
        class="ai-admission">
        <button type="button"
          :disabled="busy || Boolean(pendingTenant) || Boolean(pendingAdmission) || !membersReady || Boolean(tenant.paused) || Boolean(state.settings.paused) || tenant.native_capability?.state !== 'available'"
          @click="run(previewAIAdmission)"><ArrowUpCircle :size="16" />预览升级正式AI</button>
        <div v-if="pendingAdmission" role="alertdialog" aria-label="确认正式AI准入" class="confirmation">
          <strong>升级正式AI准入 · {{ tenant.name }}</strong>
          <p>机器人 {{ pendingAdmission.account }} · 群 {{ pendingAdmission.group_id }}</p>
          <p>保留本群审核规则与管理员；AI仍关闭，不授予套餐、客服编辑或财务权限。</p>
          <p>AI发话仍须承担人本人确认、有效额度及预算核验。</p>
          <div class="actions">
            <button type="button" :disabled="busy" @click="run(confirmAIAdmission)">
              <ShieldCheck :size="16" />确认升级
            </button>
            <button type="button" :disabled="busy" @click="pendingAdmission = null">取消</button>
          </div>
        </div>
      </div>
      <div v-show="view === 'groups'">
      <template v-if="tenant">
        <label v-if="tenant.active" class="check"><input v-model="tenant.paused" type="checkbox" :true-value="1" :false-value="0" />暂停本群</label>
        <fieldset v-if="tenant.active || membersReady" class="member-grants">
          <legend>群管理员 · {{ selectedManagers.length }} / 20</legend>
          <label>查找群主或管理员<input v-model="memberSearch" type="search" :disabled="!membersReady || busy" /></label>
          <div v-if="invalidManagers.length" class="confirmation" role="alert">
            <strong>以下旧授权已不符合群管理身份，请移除后保存：</strong>
            <label v-for="uid in invalidManagers" :key="uid" class="check">
              <input v-model="selectedManagers" type="checkbox" :value="uid" :disabled="busy" />
              <span>{{ members.find(m => m.id === uid)?.name || uid }} · 仅可移除</span>
            </label>
          </div>
          <div class="member-list">
            <label v-for="member in visibleManagers" :key="member.id" class="check">
              <input v-model="selectedManagers" type="checkbox" :value="member.id" :disabled="busy || !membersReady || (!selectedManagers.includes(member.id) && selectedManagers.length >= 20)" />
              <span>{{ member.name }} · {{ member.role === 'GROUP_ROLE_OWNER' ? '群主' : '管理员' }}<small>{{ member.id }}</small></span>
            </label>
            <p v-if="membersReady && !visibleManagers.length">没有符合条件的群主或管理员。</p>
          </div>
        </fieldset>
        <button v-if="tenant.active" type="button" :disabled="busy || Boolean(pendingTenant) || Boolean(pendingAdmission) || !membersReady || Boolean(invalidManagers.length)" @click="run(saveTenant)"><Save :size="16" />保存本群</button>
        <button v-else-if="membersReady" type="button"
          :disabled="busy || Boolean(pendingTenant) || selectedManagers.length !== 1 || Boolean(invalidManagers.length)"
          @click="run(previewRulesProvision)"><ShieldCheck :size="16" />预览规则开通</button>
        <div v-if="pendingTenant" role="alertdialog" aria-label="确认管理员变更" class="confirmation">
          <strong>{{ pendingTenant.action ? '确认规则开通' : '确认管理员变更' }} · {{ tenant.name }}</strong>
          <p>原管理员：{{ pendingTenant.previous.join('、') }}</p>
          <p>新管理员：{{ (pendingTenant.payload.managers || [pendingTenant.payload.manager]).join('、') || '无' }}</p>
          <p v-if="pendingTenant.action">仅规则 · AI关闭 · 不开通套餐或财务权限</p>
          <div class="actions">
            <button type="button" :disabled="busy" @click="run(async () => {
              await api(pendingTenant.action || 'tenant', { ...pendingTenant.payload, confirmation: pendingTenant.confirmation });
              await reload(); notice = '群授权已确认';
            })"><ShieldCheck :size="16" />确认变更</button>
            <button type="button" :disabled="busy" @click="pendingTenant = null">取消</button>
          </div>
        </div>
        <button v-if="!tenant.active && tenant.admission === 'verified'" type="button" :disabled="busy || !tenant.present || tenant.membership_pending" @click="run(async () => {
          const result = await api('activation_code', { account: tenant.account, group_id: tenant.group_id });
          code = `认领 ${tenant.group_id} ${result.code}`;
        })"><KeyRound :size="16" />生成一次性开通码</button>
        <output v-if="code" class="activation">{{ code }}</output>
      </template>
      <p v-else class="capability">请选择要管理的群聊。</p>
      </div>
      <div v-show="view === 'rules'">
      <template v-if="tenant?.active">
        <button v-if="state.queue_health?.some((q: any) => q.account === tenant.account && q.review_groups.includes(tenant.group_id))"
          type="button" :disabled="busy" @click="run(resumeReview)"><ShieldCheck :size="16" />复核并恢复审核</button>
        <div class="fields">
          <label class="check"><input v-model="tenant.rules.advertising" type="checkbox" />广告审核与递进禁言</label>
          <label class="check"><input v-model="tenant.rules.flood" type="checkbox" />防刷屏</label>
          <label class="check"><input v-model="tenant.rules.video_whitelist_only" type="checkbox" />仅白名单成员可发视频</label>
          <label>广告审核关键词<textarea v-model="keywordText" rows="4" /></label>
        </div>
        <GroupRulesEditor :model-value="tenant.rules.custom_rules || []" :disabled="busy"
          @update:model-value="tenant.rules.custom_rules = $event" />
        <button type="button" :disabled="busy || !membersReady" @click="run(saveTenant)"><Save :size="16" />保存规则</button>
        <MemberWhitelistPanel v-if="view === 'rules'"
          :key="`${tenant.account}/${tenant.group_id}/${tenant.version}`"
          :api="api" :account="tenant.account" :group="tenant.group_id"
          :tenant-version="tenant.version" :disabled="busy" />
        <MemberBlacklistPanel v-if="view === 'rules'"
          :key="`${tenant.account}/${tenant.group_id}/${tenant.version}`"
          :api="blacklistApi" :account="tenant.account" :group="tenant.group_id"
          :tenant-version="tenant.version" :disabled="busy" />
        <RuleAutomationPanel v-if="view === 'rules'"
          :key="`${tenant.account}/${tenant.group_id}/${tenant.version}`"
          :api="api" :account="tenant.account" :group="tenant.group_id"
          :tenant-version="tenant.version" :disabled="busy" />
      </template>
      <p v-else class="capability">请选择已开通的群聊。</p>
      </div>
      <div v-show="view === 'group-ai'">
        <p v-if="tenant?.rules_only" class="capability" role="status">本群仅开放规则功能，AI不可启用。</p>
        <template v-else-if="tenant?.active && state.group_ai">
          <div v-if="groupAIRow?.readiness" class="capability" role="status">
            <strong>{{ groupAIRow.readiness.ready ? '基础条件已满足' : '设置已保存，当前不能发话' }}</strong>
            <ul v-if="groupAIRow.readiness.reasons.length">
              <li v-for="reason in groupAIRow.readiness.reasons" :key="reason">{{ reason }}</li>
            </ul>
          </div>
          <div class="fields">
            <label class="check"><input v-model="groupAI.enabled" type="checkbox" />启用本群AI</label>
            <label class="check"><input v-model="groupAI.mentions" type="checkbox" />@机器人热群对话</label>
            <label class="check"><input v-model="groupAI.autonomous" type="checkbox" />普通聊天自主接话</label>
            <label class="check"><input v-model="groupAI.idle" type="checkbox" />冷场时主动发起话题</label>
            <label>冷场间隔（分钟）<input v-model.number="groupAI.interval_minutes" type="number" min="5" max="1440" /></label>
            <label>本群额外每日限额（0为仅用共享池）<input v-model.number="groupAI.daily_limit" type="number" min="0" max="5000" /></label>
            <label class="check"><input v-model="allDay" type="checkbox" />全天可接话</label>
            <template v-if="!allDay">
              <label>开始时间（北京时间，小时）<input v-model.number="groupAI.start_hour" type="number" min="0" max="23" /></label>
              <label>结束时间（北京时间，小时）<input v-model.number="groupAI.end_hour" type="number" min="0" max="23" /></label>
            </template>
            <label>聊天主题<textarea v-model="groupAI.topic" maxlength="500" rows="3" /></label>
          </div>
          <div v-if="groupAIRow?.blocked" role="alert" class="confirmation">
            <strong>上次AI调用或消息发送结果待核对，自动发话已暂停。</strong>
            <label class="check"><input v-model="resumeGroupAI" type="checkbox" />已核对结果，恢复本群AI</label>
          </div>
          <button type="button" :disabled="busy || !membersReady" @click="run(saveGroupAI)"><Save :size="16" />保存本群AI</button>
          <label class="check"><input v-model="useDefaultGroupProfile" type="checkbox" />使用群聊AI默认配置</label>
          <AIProfileEditor v-if="!useDefaultGroupProfile" v-model="tenantProfile"
            :choices="profileChoices" :providers="state.providers" label="本群AI" />
          <button type="button" :disabled="busy || !membersReady"
            @click="run(() => saveProfile('group', true))"><Save :size="16" />保存本群AI配置</button>
        </template>
        <p v-else class="capability">请选择已开通的群聊。</p>
      </div>

      <div v-show="view === 'plans'">
        <AIPlansPanel v-if="visited.has('plans') && state.billing" :billing="state.billing" :tenant="tenant"
          :members="members" :providers="state.providers" :api="api" :disabled="busy"
          @saved="run(reload)" />
        <p v-else-if="!state.billing" class="capability">套餐状态暂不可用，请刷新核对。</p>
      </div>
      <FinanceGrantsPanel v-if="visited.has('finance')" v-show="view === 'finance'" :bridge="bridge" />
      <div v-show="view === 'consultation'">
      <label>机器人账号<select v-model="supportAccount" :disabled="busy">
        <option v-for="account in state.accounts" :key="account.account" :value="account.account">
          {{ account.nickname || account.account }} · {{ account.account }}
        </option>
      </select></label>
      <SupportSettingsPanel v-if="supportAccount && visited.has('consultation')"
        :key="supportAccount" :api="api" :account="supportAccount" :disabled="busy"
        :profiles="state.ai_profiles" :choices="profileChoices" :providers="state.providers"
        @saved="run(reload)" />
      </div>
      <div v-show="view === 'admin-guidance'">
      <p class="capability" role="status">{{ savedProfile('admin_guidance') ? '独立配置已保存' : '独立配置尚未保存' }}</p>
      <p v-if="state.guidance_status" class="capability" role="status">
        {{ state.guidance_status.profile_ready ? '配置可解析' : '配置待核实' }} ·
        {{ state.guidance_status.manual_current ? '操作手册已同步' : '操作手册待同步' }} ·
        {{ state.guidance_status.manual_entries }}/{{ state.guidance_status.manual_expected }}项 · 只读指引
      </p>
      <button type="button" :disabled="busy" @click="run(() => provisionProfile(''))"><ShieldCheck :size="16" />创建服务端专用配置与人格</button>
      <AIProfileEditor v-model="adminProfile" :choices="profileChoices"
        :providers="state.providers" label="管理员指引" private-chat />
      <button type="button" :disabled="busy" @click="run(() => saveProfile('admin_guidance'))"><Save :size="16" />保存管理员指引配置</button>
      </div>
      <div v-show="view === 'group-ai'">
      <h3>群聊 / 热群AI默认配置</h3>
      <AIProfileEditor v-model="defaultGroupProfile" :choices="profileChoices"
        :providers="state.providers" label="群聊默认" />
      <button type="button" :disabled="busy" @click="run(() => saveProfile('group'))"><Save :size="16" />保存群聊默认配置</button>
      </div>
      <div v-show="view === 'model'">
      <h3>共用备用模型与预算</h3>
      <div class="fields">
        <label>模型<select v-model="model.provider_id" aria-label="AI咨询模型">
          <option value="">选择模型</option>
          <option v-for="p in state.providers" :key="p.id" :value="p.id">{{ p.id }} · {{ p.model }}</option>
        </select></label>
      </div>
      <button type="button" :disabled="busy" @click="run(async () => {
        await api('model', {provider_id: model.provider_id});
        await reload(); notice = '模型配置已保存';
      })"><Save :size="16" />保存备用模型</button>
      <div v-if="state.ai_budget" class="fields">
        <label>预算核验模型<select v-model="budgetProvider" aria-label="预算核验模型">
          <option v-for="p in state.providers" :key="p.id" :value="p.id">{{ p.id }} · {{ p.model }}</option>
        </select></label>
        <label class="check"><input v-model="budgetConfirmed" type="checkbox" />供应商已设置可阻止超额调用的硬限额</label>
        <label>硬限额核验记录<input v-model="budgetEvidence" maxlength="500" autocomplete="off" /></label>
      </div>
      <button v-if="state.ai_budget" type="button"
        :disabled="busy || !budgetProvider || (budgetConfirmed && !budgetEvidence.trim())"
        @click="run(async () => {
          await api('ai_budget', {
            version: state.ai_budget.version, confirmed: budgetConfirmed,
            provider: budgetProvider,
            evidence: budgetEvidence,
          });
          await reload(); notice = 'AI预算门禁已保存';
        })"><ShieldCheck :size="16" />保存预算核验</button>
      </div>

      <div v-show="view === 'audit'">
      <ul class="audit"><li v-for="a in state.audit" :key="a.id">{{ new Date(a.created * 1000).toLocaleString() }} · {{ a.actor }} · {{ a.action }}</li></ul>
      <p v-if="!state.audit.length" class="capability">暂无群管操作记录。</p>
      </div>
    </template>
  </section>
</template>

<style scoped>
.open-service { min-width: 0; }
header, h2, .actions, .runtime { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; }
header { justify-content: space-between; }
h2 { font-size: 16px; margin: 0; }
h3 { font-size: 16px; margin: 28px 0 12px; border-top: 1px solid #dce2e2; padding-top: 20px; }
.runtime { margin: 16px 0; padding: 12px 0; border-bottom: 1px solid #dce2e2; }
.runtime span { min-width: 0; max-width: 100%; overflow-wrap: anywhere; }
.tenant-scope { margin: 20px 0; padding-bottom: 16px; border-bottom: 1px solid #dce2e2; }
.capability { margin-top: 10px; font-size: 13px; }
.capability ul { padding-left: 20px; overflow-wrap: anywhere; }
.rule-outcomes { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 8px 12px; font-size: 13px; }
.rule-outcomes dt { color: #657676; }
.rule-outcomes dd { margin: 0; overflow-wrap: anywhere; }
.local-views { display: flex; gap: 8px; overflow-x: auto; margin: 16px 0; }
.local-views button { flex-shrink: 0; }
.local-views button[aria-pressed="true"] { border-color: #12786b; }
.fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; margin: 16px 0; }
label { display: flex; flex-direction: column; gap: 7px; min-width: 0; font-size: 14px; }
input, select, textarea { width: 100%; box-sizing: border-box; min-width: 0; font: inherit; padding: 9px; border: 1px solid #9daaaa; border-radius: 4px; background: #fff; color: #202828; }
.check { flex-direction: row; align-items: center; }
.check input { width: 18px; height: 18px; flex-shrink: 0; }
button { display: inline-flex; align-items: center; justify-content: center; gap: 7px; min-height: 36px; border: 1px solid #becaca; border-radius: 4px; padding: 7px 12px; color: #164d46; background: #eef5f3; cursor: pointer; white-space: normal; }
button:disabled { opacity: .5; cursor: default; }
.activation { display: block; overflow-wrap: anywhere; padding: 12px 0; user-select: all; }
.confirmation { border-left: 3px solid #12786b; padding: 12px; margin: 12px 0; overflow-wrap: anywhere; }
.member-grants { border: 0; padding: 12px 0; min-width: 0; }
.member-list { max-height: 240px; overflow: auto; padding: 8px 0; }
.member-list label { min-height: 40px; padding: 4px 0; }
.member-list span { overflow-wrap: anywhere; }
.member-list small { display: block; color: #657676; font-size: 12px; }
.error { color: #b52b35; overflow-wrap: anywhere; }
.audit { padding-left: 20px; font-size: 13px; overflow-wrap: anywhere; max-height: 280px; overflow: auto; }
* { letter-spacing: 0; }
@media (max-width: 600px) { .fields { grid-template-columns: minmax(0, 1fr); } }
</style>
