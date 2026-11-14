<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import {
  CalendarClock,
  ClipboardList,
  Gift,
  LoaderCircle,
  MessageSquareText,
  Network,
  Pause,
  Plus,
  RefreshCw,
  Save,
  Settings2,
  ShieldCheck,
  TableProperties,
  Terminal,
  Trash2,
  Users,
  Video,
  Wallet,
  X,
} from "@lucide/vue";
import ConversationExamples from "./ConversationExamples.vue";
import ReferenceTables from "./ReferenceTables.vue";
import Cashier from "./SettlementPanel.vue";
import BusinessControls from "./BusinessControls.vue";
import OperationsLog from "./OperationsLog.vue";
import GroupManagers from "./GroupManagers.vue";
import OpenService from "./OpenService.vue";
import CommandHelp from "./CommandHelp.vue";
import CurrentMutesPanel from "./CurrentMutesPanel.vue";
import BulkRecallPanel from "./BulkRecallPanel.vue";
import SpeechActivityPanel from "./SpeechActivityPanel.vue";
import VideoRelayPanel from "./VideoRelayPanel.vue";
import { getPlatformInstanceName } from "../../src/utils/platformInstanceName.mjs";

type Policy = Record<string, any>;
type Bot = {
  id: string;
  nickname?: string;
  name?: string;
  account_id?: string;
  enabled_groups?: string[];
  reply_groups?: Record<string, boolean>;
  ai_routes?: {
    groups?: Record<string, string>;
  };
  moderation?: Policy;
  revision: string;
  state: string;
  current_error?: string;
  enable?: boolean;
  proactive_send?: { enabled: boolean; targets: string[] };
  developer_test?: Policy;
  business_active?: boolean;
  group_names?: Record<string, string>;
};
type Audit = {
  reviews: Array<Record<string, any>>;
  sanctions: Array<Record<string, any>>;
  counts: Array<Record<string, any>>;
  rules: Array<Record<string, any>>;
  recalls: Array<Record<string, any>>;
  recalls_available: boolean;
};
type ActivityState = {
  lottery: {
    prize: string;
    winners: number;
    duration_minutes: number;
    capacity: number;
    invite_gate: number;
    contact: string;
    total_amount: string;
    packet_mode: "prize" | "ordinary" | "lucky" | "tiered";
    by_invites: boolean;
    remarks: string;
    entry_phrase: string;
    prize_tiers: Array<{ amount: string; count: number }>;
    reserved_members: Array<{ member: string; peer: string; name: string; amount: string }>;
    start_at: string;
    luckiest_bonus: string;
    integer_packets: boolean;
    draw_on_full: boolean;
  };
  invite_rate: string;
  invitation: {
    rate: string;
    enabled: boolean;
    last_scan: number;
    presence_checked_at: number;
    error: string;
    credited_count: number;
    credited_total: string;
    active_count: number;
    left_count: number;
    pending_count: number;
    expired_count: number;
  };
  current_lottery: Record<string, any> | null;
  revision: string;
};
type ScheduleState = {
  schedule: Record<string, any> | null;
  executions: Array<Record<string, any>>;
  history: Array<Record<string, any>>;
  revision: string;
};
type Bridge = {
  ready(): Promise<unknown>;
  getLocale(): string;
  onContext(handler: (context: { locale?: string }) => void): () => void;
  apiGet(endpoint: string, params?: Record<string, string>): Promise<any>;
  apiPost(endpoint: string, body: unknown): Promise<any>;
};
const bridge = (window as unknown as { AstrBotPluginPage?: Bridge })
  .AstrBotPluginPage;
const locale = ref("zh-CN");
const t = (zh: string, en: string) => (locale.value.startsWith("zh") ? zh : en);
const bots = ref<Bot[]>([]);
const providers = ref<Array<{ id: string; model: string }>>([]);
const botId = ref("");
const groupId = ref("");
const tab = ref("settings");
const integrated = ref(false);
const loading = ref(true);
const saving = ref(false);
const auditLoading = ref(false);
const activityLoading = ref(false);
const activitySaving = ref(false);
const scheduleLoading = ref(false);
const scheduleSaving = ref(false);
const error = ref("");
const auditError = ref("");
const activityError = ref("");
const scheduleError = ref("");
const notice = ref("");
const managersDirty = ref(false);
const settlementDirty = ref(false);
const relayDirty = ref(false);
const relayBusy = ref(false);
const relayEpoch = ref(0);
const form = ref<Policy>({});
const original = ref("");
const activityState = ref<ActivityState | null>(null);
const activityForm = ref<ActivityState["lottery"] & { invite_rate: string }>({
  prize: "",
  winners: 3,
  duration_minutes: 10,
  capacity: 15,
  invite_gate: 0,
  contact: "",
  total_amount: "0.00",
  packet_mode: "ordinary",
  by_invites: false,
  remarks: "",
  entry_phrase: "参与抽奖",
  prize_tiers: [],
  reserved_members: [],
  start_at: "",
  luckiest_bonus: "0.00",
  integer_packets: false,
  draw_on_full: false,
  invite_rate: "0.00",
});
const activityOriginal = ref("");
const activityControlling = ref(false);
const activityControlRequest = ref("");
const activityControlScope = ref("");
const activityControlUnconfirmed = ref(false);
const reservedQuery = ref("");
const reservedTier = ref("");
const reservedResults = ref<Array<{ member: string; peer: string; name: string }>>([]);
const reservedChosen = ref("");
const reservedSearching = ref(false);
const reservedSearched = ref(false);
let reservedSequence = 0;
const activeLottery = computed(() => activityState.value?.current_lottery &&
  ["scheduled", "schedule_blocked", "announcing", "open", "blocked", "drawing", "notice_unknown", "result_unknown"]
    .includes(activityState.value.current_lottery.status));
const tierSummary = computed(() => {
  const tiers = activityForm.value.prize_tiers;
  const count = tiers.reduce((sum, tier) => sum + Number(tier.count || 0), 0);
  const cents = tiers.reduce(
    (sum, tier) => sum + Math.round(Number(tier.amount || 0) * 100) * Number(tier.count || 0), 0,
  );
  const remainingCount = activityForm.value.capacity - count;
  const remainingCents = Math.round(Number(activityForm.value.total_amount || 0) * 100) - cents;
  return {
    count, amount: (cents / 100).toFixed(2), remainingCount,
    remainingAmount: (remainingCents / 100).toFixed(2),
    invalid: count < 1 || remainingCount < 0
      || remainingCents < remainingCount * (activityForm.value.integer_packets ? 100 : 1)
      || (remainingCount === 0 && remainingCents !== 0),
  };
});
function formatTiers(tiers: Array<{ amount: string; count: number }>) {
  return tiers.map(tier => `${tier.count} × ${tier.amount}`).join(" / ");
}
function clearReservedSearch() {
  reservedSequence++;
  reservedResults.value = [];
  reservedChosen.value = "";
  reservedSearching.value = false;
  reservedSearched.value = false;
}
async function searchReservedMembers() {
  if (!bridge || !botId.value || !groupId.value || !reservedQuery.value.trim()
    || !reservedTier.value || reservedSearching.value || activitySaving.value
    || activityForm.value.packet_mode !== "tiered")
    return;
  activityError.value = "";
  clearReservedSearch();
  reservedSearching.value = true;
  const search = reservedSequence;
  const selectedBot = botId.value, selectedGroup = groupId.value, sequence = activitySequence;
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const data = await Promise.race([
      bridge.apiGet("lottery-members", {
        bot_id: selectedBot,
        group_id: selectedGroup,
        query: reservedQuery.value.trim(),
      }),
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(new Error(
          t("群成员查询超时，请稍后重试", "Member search timed out; please retry"),
        )), 18000);
      }),
    ]);
    if (search === reservedSequence && sequence === activitySequence
      && selectedBot === botId.value && selectedGroup === groupId.value) {
      reservedResults.value = data.members || [];
      reservedSearched.value = true;
    }
  } catch (err) {
    if (search === reservedSequence && sequence === activitySequence
      && selectedBot === botId.value && selectedGroup === groupId.value)
      activityError.value = err instanceof Error ? err.message : t("群成员搜索失败", "Member search failed");
  } finally {
    clearTimeout(timer);
    if (search === reservedSequence) reservedSearching.value = false;
  }
}
function addReservedMember() {
  const member = reservedResults.value.find(item => item.member === reservedChosen.value);
  const tier = activityForm.value.prize_tiers.find(item => item.amount === reservedTier.value);
  if (!member || !tier || !Number.isFinite(Number(tier.amount)) || Number(tier.amount) <= 0) return;
  if (activityForm.value.reserved_members.some(item => item.member === member.member)) {
    activityError.value = t("同一成员只能指定一个固定档位", "A member can only hold one fixed tier");
    return;
  }
  const used = activityForm.value.reserved_members.filter(item => item.amount === tier.amount).length;
  if (used >= Number(tier.count)) {
    activityError.value = t("该固定档位的指定人数已满", "This fixed tier has no reservation slots left");
    return;
  }
  activityForm.value.reserved_members.push({ ...member, amount: tier.amount });
  activityError.value = "";
  reservedQuery.value = "";
  clearReservedSearch();
}
function addPrizeTier() {
  let amount = 288;
  while (activityForm.value.prize_tiers.some(tier => Number(tier.amount) === amount)) amount++;
  const tier = { amount: amount.toFixed(2), count: 1 };
  activityForm.value.prize_tiers.push(tier);
  if (!reservedTier.value) reservedTier.value = tier.amount;
}
function updatePrizeTierAmount(index: number, event: Event) {
  const tier = activityForm.value.prize_tiers[index];
  if (!tier) return;
  const input = event.target as HTMLInputElement;
  if (input.value && activityForm.value.prize_tiers.some(
    (other, otherIndex) => otherIndex !== index && Number(other.amount) === Number(input.value),
  )) {
    input.value = tier.amount;
    activityError.value = t("相同金额请合并到同一个档位", "Merge equal amounts into one tier");
    return;
  }
  const previous = tier.amount;
  tier.amount = input.value;
  activityError.value = "";
  for (const member of activityForm.value.reserved_members)
    if (member.amount === previous) member.amount = tier.amount;
  if (reservedTier.value === previous) reservedTier.value = tier.amount;
}
function removePrizeTier(index: number) {
  const [removed] = activityForm.value.prize_tiers.splice(index, 1);
  if (!removed) return;
  activityError.value = "";
  activityForm.value.reserved_members = activityForm.value.reserved_members
    .filter(member => member.amount !== removed.amount);
  if (!activityForm.value.prize_tiers.some(tier => tier.amount === reservedTier.value))
    reservedTier.value = activityForm.value.prize_tiers[0]?.amount || "";
  clearReservedSearch();
}
const scheduleState = ref<ScheduleState>({
  schedule: null,
  executions: [],
  history: [],
  revision: "",
});
const confirmSchedulePause = ref(false);
const pending = ref<(() => void) | null>(null);
const emptyAudit = (): Audit => ({
  reviews: [],
  sanctions: [],
  counts: [],
  rules: [],
  recalls: [],
  recalls_available: false,
});
const audit = ref<Audit>(emptyAudit());
const bot = computed(() => bots.value.find((item) => item.id === botId.value));
const groupOptions = computed(() => [
  ...new Set([
    ...(bot.value?.enabled_groups || []),
    ...Object.keys(bot.value?.group_names || {}),
  ]),
]);
const dirty = computed(
  () => original.value !== "" && JSON.stringify(form.value) !== original.value,
);
const activityDirty = computed(
  () =>
    activityOriginal.value !== "" &&
    JSON.stringify(activityForm.value) !== activityOriginal.value,
);
const groupPermissions = computed<string[]>(
  () => form.value.group?.permissions || [],
);
const busy = computed(() => loading.value || saving.value || activityControlling.value || relayBusy.value);
let auditSequence = 0;
let activitySequence = 0;
let scheduleSequence = 0;
let unsubscribe: (() => void) | undefined;
watch([botId, groupId, reservedQuery, () => activityForm.value.packet_mode],
  clearReservedSearch, { flush: "sync" });

const tabs = computed(() => [
  { id: "open", name: integrated.value ? t("开通", "Activation") : t("开放服务", "Open Service"), icon: Network },
  { id: "rules", name: t("规则", "Rules"), icon: ShieldCheck },
  { id: "business", name: t("群聊与运行", "Groups & Runtime"), icon: Settings2 },
  { id: "speech-activity", name: t("发言统计", "Speech Activity"), icon: MessageSquareText },
  { id: "video-relay", name: t("视频转发", "Video Relay"), icon: Video },
  { id: "settings", name: t("审核配置", "Moderation"), icon: Settings2 },
  { id: "permissions", name: t("群授权", "Group Grants"), icon: ShieldCheck },
  { id: "activities", name: t("玩家福利抽奖", "Player Lottery"), icon: Gift },
  { id: "invite-rewards", name: t("邀请奖励", "Invitation Rewards"), icon: Users },
  { id: "cashback", name: t("邀请记账／结账", "Invitation Settlement"), icon: ClipboardList },
  { id: "schedule", name: t("定时管理", "Schedules"), icon: CalendarClock },
  { id: "audit", name: t("处罚记录", "Audit"), icon: ClipboardList },
  { id: "operations", name: t("操作日志", "Operation Logs"), icon: ClipboardList },
  { id: "commands", name: t("命令与权限", "Commands & Roles"), icon: Terminal },
  {
    id: "examples",
    name: t("对话示例", "Chat examples"),
    icon: MessageSquareText,
  },
  { id: "architecture", name: t("架构表", "Architecture"), icon: Network },
  { id: "matrix", name: t("权限表", "Permissions"), icon: TableProperties },
  { id: "group-ai", name: t("群聊与热群", "Group AI"), icon: MessageSquareText },
  { id: "plans", name: t("套餐与额度", "Plans & Quotas"), icon: Wallet },
  { id: "model", name: t("模型与预算", "Model & Budget"), icon: ShieldCheck },
  { id: "runtime", name: t("服务总控", "Service Controls"), icon: Settings2 },
  { id: "open-audit", name: t("群管审计", "Management Audit"), icon: ClipboardList },
].filter(item => integrated.value ? !["business", "settings"].includes(item.id) : item.id !== "rules"));
const sections = computed(() => [
  { id: "groups", name: t("群管理", "Groups"), icon: Network, tabs: ["open", "rules", "business", "settings", "permissions", "speech-activity", "video-relay"] },
  { id: "ai", name: t("AI服务", "AI"), icon: MessageSquareText, tabs: ["group-ai", "plans", "model"] },
  { id: "finance", name: t("活动与财务", "Activities & Finance"), icon: Wallet, tabs: ["activities", "invite-rewards", "cashback"] },
  { id: "runtime", name: t("运行与日志", "Runtime & Logs"), icon: CalendarClock, tabs: ["runtime", "schedule", "audit", "operations", "open-audit"] },
  { id: "help", name: t("使用帮助", "Help"), icon: Terminal, tabs: ["commands", "examples", "architecture", "matrix"] },
]);
const section = computed(() => sections.value.find(item => item.tabs.includes(tab.value))!);
const sectionTabs = computed(() => section.value.tabs
  .flatMap(id => tabs.value.filter(item => item.id === id)));
const lastTabs = ref<Record<string, string>>({});
const openViews: Record<string, string> = {
  open: "groups", rules: "rules", "group-ai": "group-ai",
  plans: "plans", model: "model", runtime: "runtime", "open-audit": "audit",
};
const openVisible = computed(() => tab.value in openViews);
const openView = ref("groups");
watch(tab, value => {
  lastTabs.value[section.value.id] = value;
  if (value in openViews) openView.value = openViews[value];
}, { immediate: true });
function selectTab(value: string) {
  if (!tabs.value.some(item => item.id === value)) return;
  guard(() => { fillActivityForm(); tab.value = value; });
}
function navigateTabs(event: KeyboardEvent) {
  const target = event.target as HTMLElement;
  if (target.getAttribute("role") !== "tab"
      || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  const buttons = [...target.closest('[role="tablist"]')!
    .querySelectorAll<HTMLButtonElement>('button[role="tab"]:not(:disabled)')];
  const index = buttons.indexOf(target as HTMLButtonElement);
  const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
    : (index + (event.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length;
  event.preventDefault();
  buttons[next].click();
  buttons[next].focus({ preventScroll: true });
  buttons[next].scrollIntoView({ block: "nearest", inline: "nearest" });
}
const actions = computed(() => [
  ["mute", t("成员禁言", "Mute member")],
  ["unmute", t("成员解禁", "Unmute member")],
  ["kick", t("移除成员", "Remove member")],
  ["recall", t("撤回违规消息", "Recall violation")],
  ["announce", t("发布公告", "Publish announcement")],
  ["mute_all", t("全员禁言", "Mute all")],
  ["unmute_all", t("解除全员禁言", "Unmute all")],
  ["rename", t("修改成员名片", "Rename member")],
  ["cleanup", t("清理封禁与注销账号", "Remove banned / cancelled accounts")],
]);
const commands = computed(() => [
  { command: "排名", action: "ranking", public: true },
  { command: "参加抽奖", action: "lottery_join", public: true },
  { command: "抽奖状态", action: "lottery_status", public: true },
  { command: "我的邀请", action: "own_invites", public: true },
  { command: "邀请奖励", action: "invite_rewards", public: true },
  { command: "公告 公告内容", action: "announce" },
  { command: "解禁 @成员（或：解除禁言 @成员）", action: "unmute" },
  { command: "禁言名单", action: "unmute" },
  { command: "定时禁言 23:00 08:00", action: "mute_all" },
]);
const activePolicy = computed(() => bot.value?.moderation || {});
const activePermissions = computed<string[]>(
  () => activePolicy.value.permissions?.[groupId.value] || [],
);
const active = computed(
  () => bot.value?.enable !== false && activePolicy.value.enabled === true,
);
const unknownCount = computed(() =>
  audit.value.counts.reduce(
    (total, row) => total + Number(row.unknown || 0),
    0,
  ),
);

function label(value: string): string {
  const labels: Record<string, [string, string]> = {
    online: ["在线", "Online"],
    reconnecting: ["重连中", "Reconnecting"],
    rate_limited: ["限流中", "Rate limited"],
    account_conflict: ["账号冲突", "Account conflict"],
    reauth_required: ["需要重新登录", "Sign-in required"],
    stopped: ["未连接", "Stopped"],
    error: ["故障", "Error"],
    accepted: ["已受理", "Accepted"],
    verified: ["已确认", "Verified"],
    unknown: ["结果待核对", "Outcome unverified"],
    rejected: ["已拒绝", "Rejected"],
    action_scope_changed: ["操作范围或授权已变化", "Scope or authorization changed"],
    action_automation_changed: ["成员身份或自动化授权已变化", "Member identity or automation grant changed"],
    recall_scope_changed: ["连接或成员身份已变化", "Connection or member identity changed"],
    recall_identity: ["原消息身份校验失败", "Original message identity check failed"],
    moderation_target: ["处罚对象已离群或角色变化", "Target left or changed role"],
    moderation_permission: ["原生角色校验未通过", "Native role check failed"],
    moderation_identity: ["成员身份未核实", "Member identity unverified"],
    content_policy_changed: ["审核策略已变化", "Moderation policy changed"],
    not_online: ["连接不可用，未执行", "Connection unavailable"],
    progressive_not_authorized: ["递进处罚授权已变化", "Progressive sanction grant changed"],
    operation_failed: ["执行异常", "Execution error"],
    lottery_bonus_invalid: ["人气王额外奖励无效，已暂停开奖", "Invalid luckiest bonus; draw blocked"],
    lottery_reserved_member_unavailable: ["指定成员未有效报名或身份已变化，已暂停开奖", "A reserved member did not enter or changed identity; draw blocked"],
    validation_failed: ["提交校验未通过", "Submission validation failed"],
    transport: ["平台连接异常", "Provider connection error"],
    failed: ["失败", "Failed"],
    disabled: ["已关闭，未执行", "Disabled; not executed"],
    pending: ["等待处理", "Pending"],
    sending: ["提交中，待核对", "Submitting; needs verification"],
    observed: ["已记录，处理结果待更新", "Recorded; outcome pending"],
    claimed: ["处理中，结果待更新", "Processing; outcome pending"],
    reserved: ["已预留，待核对", "Reserved; needs verification"],
    new: ["待提交", "Not submitted"],
    expired: ["已过期，未执行", "Expired; not executed"],
    business_suspended: ["业务暂停，未执行", "Service paused; not executed"],
    sent: ["已发送（平台受理）", "Sent (platform accepted)"],
    not_recorded: ["未查到发送回执", "No send receipt found"],
    not_applicable: ["本项无需警告", "No warning required"],
    audit_incomplete: ["原审计未完成，见撤回回执", "Audit incomplete; see recall receipt"],
    recall_observed: ["已收到撤回通知", "Recall notification received"],
    bot_rule_recall: ["机器人撤回（有回执）", "Bot recall (receipt matched)"],
    bot_command_cleanup: ["机器人清理群命令", "Bot command cleanup"],
    bot_reply_cleanup: ["机器人清理回复", "Bot reply cleanup"],
    self_recall: ["成员自己撤回", "Author recalled"],
    other_member_recall: ["其他成员撤回", "Another member recalled"],
    bot_account_unattributed: ["机器人账号撤回，执行来源待核对", "Bot account; process unverified"],
    unattributed: ["来源待核对", "Source unverified"],
    allow: ["正常", "Allowed"],
    violation: ["违规", "Violation"],
    review: ["证据不足", "Review"],
    fallback: ["规则兜底", "Rule fallback"],
    mute: ["禁言", "Mute"],
    kick: ["踢出", "Remove"],
    external_promotion: ["外部引流", "External promotion"],
    adult_promotion: ["成人推广", "Adult promotion"],
    mute_keyword: ["禁言关键词", "Mute keyword"],
    blacklist_keyword: ["关键词拉黑并禁言", "Keyword blacklist and mute"],
    consecutive_digits: ["连续6位以上数字（禁言1分钟）", "6+ consecutive digits (1-minute mute)"],
    kick_keyword: ["高风险关键词", "High-risk keyword"],
    none: ["无", "None"],
    warned: ["已警告", "Warned"],
    muted: ["已禁言", "Muted"],
    kicked: ["已提交踢出", "Removal submitted"],
    recalled: ["已撤回", "Recalled"],
    message_flood: ["刷屏", "Message flood"],
    prohibited_image: ["禁止图片", "Prohibited image"],
    prohibited_video: ["禁止视频", "Prohibited video"],
    prohibited_forward: ["禁止合并聊天记录", "Prohibited merged chat"],
    restricted_video: ["非白名单视频", "Restricted video"],
    restricted_forward: ["合并聊天记录限制", "Merged chat restriction"],
    image_qr: ["二维码图片", "QR code image"],
    image_digits: ["含数字图片", "Image with digits"],
    review_only: ["旧记录未确认执行结果", "Legacy outcome unconfirmed"],
    preflight_failed: ["执行前核验失败", "Preflight verification failed"],
    execution_rejected: ["执行被拒绝", "Execution rejected"],
    recall_pending: ["撤回清理排队中", "Recall cleanup queued"],
    business_rejected: ["平台拒绝请求", "Provider rejected the request"],
    identity_unverified: ["成员身份待核验", "Member identity unverified"],
    administrator_exempt: ["群管理免自动处罚", "Administrator exempt"],
    historical_or_unknown_time: ["历史消息或时间待核验", "Historical or unverified time"],
    history: ["历史消息", "Historical"],
    needs_review: ["待核验", "Needs review"],
    announcing: ["通知发送中", "Announcing"],
    open: ["报名中", "Open"],
    blocked: ["暂停待核查", "Blocked for review"],
    drawing: ["开奖中", "Drawing"],
    scheduled: ["待定时发布", "Scheduled"],
    schedule_blocked: ["定时发布已阻止", "Scheduled publication blocked"],
    scheduled_start_missed: ["错过预约时刻超过60秒；请取消后重新预约，不自动补发", "More than 60 seconds late; cancel and reschedule. No catch-up sends."],
    scheduled_start_verification_failed: ["到点成员权限核验失败；请核对后取消并重新预约", "Member verification failed; check permissions, cancel and reschedule."],
    activity_owner_revoked: ["创建者已失去管理授权；请取消后由当前管理员重新预约", "Creator authorization revoked; cancel and reschedule as a current administrator."],
    activity_proactive_not_authorized: ["本群未授权主动发送；请核对后重新预约", "Proactive group sending is not authorized; check grants and reschedule."],
    group_native_admin_required: ["机器人不是本群管理员；请先授予管理权限", "The bot is not a group administrator; grant the required role first."],
    activity_scope: ["群或机器人当前未启用；请核对后取消并重新预约", "Group or bot is not active; check the scope, cancel and reschedule."],
    finished: ["已开奖", "Finished"],
    cancelled: ["已取消", "Cancelled"],
    notice_unknown: ["开场通知结果未知", "Announcement outcome unknown"],
    result_unknown: ["平台回执待核对", "Provider receipt unverified"],
    active: ["运行中", "Active"],
    paused: ["已暂停", "Paused"],
    deleted: ["已删除", "Deleted"],
    mute_all: ["全员禁言", "Mute all"],
    unmute_all: ["解除全员禁言", "Unmute all"],
  };
  return labels[value] ? t(...labels[value]) : value || t("无", "None");
}
function date(value: number): string {
  return value
    ? new Date(value * 1000).toLocaleString(locale.value, {
        timeZone: "Asia/Shanghai",
        hour12: false,
      })
    : "-";
}
function fillForm() {
  const settings = bot.value?.moderation || {};
  form.value = {
    policy: {
      enabled: settings.enabled === true,
      automation_enabled: settings.automation_enabled === true,
      recall_enabled: settings.recall_enabled === true,
      progressive_mute: settings.progressive_mute === true,
      cooldown_seconds: settings.cooldown_seconds ?? 60,
      semantic: {
        enabled: settings.semantic?.enabled === true,
        provider_id: settings.semantic?.provider_id || "",
        timeout_seconds: settings.semantic?.timeout_seconds ?? 12,
        context_limit: settings.semantic?.context_limit ?? 20,
      },
      mute_keywords: [...(settings.mute_keywords ?? settings.keywords ?? [])],
      kick_keywords: [...(settings.kick_keywords || [])],
    },
    group: groupId.value
      ? {
          permissions: [...(settings.permissions?.[groupId.value] || [])],
          auto_kick: settings.auto_kick?.[groupId.value] === true,
          card_auto: settings.card_auto?.[groupId.value] === true,
          card_suffix: settings.card_suffix?.[groupId.value] || "",
          reply: bot.value?.reply_groups?.[groupId.value] !== false,
          customer_provider_id:
            bot.value?.ai_routes?.groups?.[groupId.value] || "",
        }
      : {},
    enabled_groups: [...(bot.value?.enabled_groups || [])],
    proactive_send: {
      enabled: bot.value?.proactive_send?.enabled === true,
      targets: (bot.value?.proactive_send?.targets || []).filter(target => !target.startsWith("private/")),
    },
    developer_test: {
      sender_instance: "",
      groups: [],
      scopes: ["group_commands", "group_ai", "group_rules"],
      keywords: [],
      seconds: 300,
      budget: 3,
      ...(bot.value?.developer_test || {}),
    },
  };
  form.value.developer_test.scopes = form.value.developer_test.scopes
    .filter((scope: string) => ["group_commands", "group_ai", "group_rules"].includes(scope));
  original.value = JSON.stringify(form.value);
}
function fillActivityForm() {
  const settings = activityState.value;
  activityForm.value = {
    prize: settings?.lottery.prize || "",
    winners: settings?.lottery.winners ?? 3,
    duration_minutes: settings?.lottery.duration_minutes ?? 10,
    capacity: settings?.lottery.capacity ?? 15,
    invite_gate: settings?.lottery.invite_gate ?? 0,
    contact: settings?.lottery.contact || "",
    total_amount: settings?.lottery.total_amount ?? "0.00",
    packet_mode: settings?.lottery.prize ? settings.lottery.packet_mode || "prize" : "ordinary",
    by_invites: settings?.lottery.by_invites ?? Boolean(settings?.lottery.invite_gate),
    remarks: settings?.lottery.remarks || "",
    entry_phrase: settings?.lottery.entry_phrase || "参与抽奖",
    prize_tiers: (settings?.lottery.prize_tiers || []).map(tier => ({ ...tier })),
    reserved_members: (settings?.lottery.reserved_members || []).map(member => ({ ...member })),
    start_at: settings?.lottery.start_at || "",
    luckiest_bonus: settings?.lottery.luckiest_bonus ?? "0.00",
    integer_packets: settings?.lottery.integer_packets ?? false,
    draw_on_full: settings?.lottery.draw_on_full ?? false,
    invite_rate: settings?.invite_rate ?? "0.00",
  };
  reservedQuery.value = "";
  reservedTier.value = activityForm.value.prize_tiers[0]?.amount || "";
  clearReservedSearch();
  activityOriginal.value = JSON.stringify(activityForm.value);
}
async function loadAudit() {
  const sequence = ++auditSequence;
  audit.value = emptyAudit();
  auditError.value = "";
  if (!botId.value || !groupId.value || !bridge) {
    auditLoading.value = false;
    return;
  }
  auditLoading.value = true;
  try {
    const data = await bridge.apiGet("audit", {
      bot_id: botId.value,
      group_id: groupId.value,
    });
    if (sequence === auditSequence) audit.value = { ...emptyAudit(), ...data };
  } catch (err) {
    if (sequence === auditSequence)
      auditError.value =
        err instanceof Error
          ? err.message
          : t("审计读取失败", "Audit unavailable");
  } finally {
    if (sequence === auditSequence) auditLoading.value = false;
  }
}
async function loadActivities() {
  const sequence = ++activitySequence;
  clearReservedSearch();
  activityError.value = "";
  activityState.value = null;
  activityOriginal.value = "";
  activityLoading.value = false;
  if (!bridge || !botId.value || !groupId.value) {
    return;
  }
  activityLoading.value = true;
  try {
    const data = await bridge.apiGet("activities", {
      bot_id: botId.value,
      group_id: groupId.value,
    });
    if (sequence === activitySequence) {
      activityState.value = data;
      activityControlUnconfirmed.value = false;
      fillActivityForm();
    }
  } catch (err) {
    if (sequence === activitySequence)
      activityError.value =
        err instanceof Error
          ? err.message
          : t("活动状态读取失败", "Activity state unavailable");
  } finally {
    if (sequence === activitySequence) activityLoading.value = false;
  }
}
async function loadSchedule() {
  const sequence = ++scheduleSequence;
  scheduleError.value = "";
  scheduleState.value = { schedule: null, executions: [], history: [], revision: "" };
  scheduleLoading.value = false;
  confirmSchedulePause.value = false;
  if (!bridge || !botId.value || !groupId.value) {
    return;
  }
  scheduleLoading.value = true;
  try {
    const data = await bridge.apiGet("schedule", {
      bot_id: botId.value,
      group_id: groupId.value,
    });
    if (sequence === scheduleSequence) scheduleState.value = data;
  } catch (err) {
    if (sequence === scheduleSequence)
      scheduleError.value =
        err instanceof Error
          ? err.message
          : t("定时状态读取失败", "Schedule state unavailable");
  } finally {
    if (sequence === scheduleSequence) scheduleLoading.value = false;
  }
}
async function reload() {
  if (!bridge) {
    error.value = t("管理面板连接不可用", "Dashboard bridge unavailable");
    loading.value = false;
    return;
  }
  loading.value = true;
  error.value = "";
  try {
    const data = await bridge.apiGet("settings");
    const wasIntegrated = integrated.value;
    integrated.value = data.open_mode === "integrated";
    if (integrated.value && !wasIntegrated) tab.value = "open";
    bots.value = data.bots.filter((item: Bot) => item.enable !== false);
    providers.value = data.providers;
    if (!bots.value.some((item) => item.id === botId.value))
      botId.value = bots.value[0]?.id || "";
    if (!bot.value?.enabled_groups?.includes(groupId.value))
      groupId.value = bot.value?.enabled_groups?.[0] || "";
    fillForm();
    await Promise.all([loadAudit(), loadActivities(), loadSchedule()]);
  } catch (err) {
    error.value =
      err instanceof Error
        ? err.message
        : t("配置读取失败", "Settings unavailable");
  } finally {
    loading.value = false;
  }
}
function guard(action: () => void) {
  notice.value = "";
  if (dirty.value || activityDirty.value || managersDirty.value || settlementDirty.value || relayDirty.value) {
    pending.value = () => {
      if (relayDirty.value) {
        relayEpoch.value++;
        relayDirty.value = false;
      }
      action();
    };
  }
  else action();
}
function select(event: Event, kind: "bot" | "group") {
  const input = event.target as HTMLSelectElement;
  const value = input.value;
  input.value = kind === "bot" ? botId.value : groupId.value;
  guard(() => {
    if (kind === "bot") {
      botId.value = value;
      groupId.value = bot.value?.enabled_groups?.[0] || "";
    } else groupId.value = value;
    fillForm();
    error.value = "";
    void Promise.all([loadAudit(), loadActivities(), loadSchedule()]);
  });
}
function togglePermission(action: string, event: Event) {
  const checked = (event.target as HTMLInputElement).checked;
  form.value.group.permissions = checked
    ? [...groupPermissions.value, action]
    : groupPermissions.value.filter((item) => item !== action);
}
function keywords(field: string, event: Event) {
  form.value.policy[field] = (event.target as HTMLTextAreaElement).value
    .split("\n")
    .map((value) => value.trim())
    .filter(Boolean);
}
async function save() {
  if (!bot.value || !bridge || busy.value || !dirty.value) return;
  saving.value = true;
  error.value = "";
  notice.value = "";
  try {
    const proposal = JSON.parse(JSON.stringify(form.value));
    if (integrated.value) {
      proposal.policy = {};
      delete proposal.enabled_groups;
      delete proposal.proactive_send;
      delete proposal.developer_test;
    }
    if (bot.value.business_active === undefined) {
      delete proposal.enabled_groups;
      delete proposal.proactive_send;
      delete proposal.developer_test;
    }
    await bridge.apiPost(
      "settings",
      JSON.parse(
        JSON.stringify({
          bot_id: botId.value,
          revision: bot.value.revision,
          group_id: form.value.enabled_groups.includes(groupId.value) ? groupId.value : "",
          ...proposal,
          group: form.value.enabled_groups.includes(groupId.value) ? form.value.group : {},
        }),
      ),
    );
    original.value = "";
    await reload();
    notice.value = t("配置已保存", "Settings saved");
  } catch (err) {
    error.value =
      err instanceof Error ? err.message : t("保存失败", "Save failed");
  } finally {
    saving.value = false;
  }
}
async function saveActivities() {
  if (
    !activityState.value || !bridge || busy.value || activityLoading.value
    || activitySaving.value || activityControlling.value || !activityDirty.value
  )
    return;
  const sequence = activitySequence, selectedBot = botId.value, selectedGroup = groupId.value;
  activitySaving.value = true;
  activityError.value = "";
  notice.value = "";
  try {
    const invitationOnly = tab.value === "invite-rewards";
    const saved = await bridge.apiPost("activities", {
      bot_id: selectedBot,
      group_id: selectedGroup,
      revision: activityState.value.revision,
      ...(invitationOnly ? { invite_rate: activityForm.value.invite_rate } : { lottery: {
        prize: activityForm.value.prize,
        winners: activityForm.value.packet_mode === "prize" ? activityForm.value.winners : activityForm.value.capacity,
        duration_minutes: activityForm.value.duration_minutes,
        capacity: activityForm.value.capacity,
        invite_gate: activityForm.value.by_invites ? activityForm.value.invite_gate : 0,
        contact: activityForm.value.contact,
        total_amount: activityForm.value.total_amount,
        packet_mode: activityForm.value.packet_mode,
        by_invites: activityForm.value.by_invites,
        remarks: activityForm.value.remarks,
        entry_phrase: activityForm.value.entry_phrase,
        start_at: activityForm.value.start_at,
        luckiest_bonus: activityForm.value.packet_mode === "lucky" ? activityForm.value.luckiest_bonus : "0.00",
        integer_packets: activityForm.value.packet_mode !== "prize" && activityForm.value.integer_packets,
        draw_on_full: activityForm.value.packet_mode !== "prize" && activityForm.value.draw_on_full,
        prize_tiers: activityForm.value.packet_mode === "tiered"
          ? activityForm.value.prize_tiers.map(tier => ({ amount: tier.amount, count: tier.count })) : [],
        reserved_members: activityForm.value.packet_mode === "tiered"
          ? activityForm.value.reserved_members.map(member => ({
              member: member.member, peer: member.peer, amount: member.amount,
            })) : [],
      } }),
    });
    if (sequence !== activitySequence || selectedBot !== botId.value || selectedGroup !== groupId.value)
      return;
    activityState.value = saved;
    fillActivityForm();
    notice.value = t("活动参数已保存", "Activity settings saved");
  } catch (err) {
    if (sequence === activitySequence && selectedBot === botId.value && selectedGroup === groupId.value)
      activityError.value =
        err instanceof Error
          ? err.message
          : t("活动参数保存失败", "Activity settings could not be saved");
  } finally {
    activitySaving.value = false;
  }
}

async function controlLottery(action: "start" | "cancel") {
  if (!bridge || !activityState.value || activityDirty.value || busy.value || activityLoading.value || activitySaving.value || activityControlUnconfirmed.value)
    return;
  const selectedBot = botId.value, selectedGroup = groupId.value, sequence = activitySequence;
  if (action === "cancel" && !window.confirm(t("取消当前活动？已生成的开奖结果不会重抽。", "Cancel this draw? Existing results are never rerolled.")))
    return;
  activityControlling.value = true;
  const scope = JSON.stringify([selectedBot, selectedGroup, action]);
  if (activityControlScope.value !== scope) {
    activityControlRequest.value = "";
    activityControlScope.value = scope;
  }
  activityControlRequest.value ||= crypto.randomUUID();
  activityError.value = "";
  try {
    const result = await bridge.apiPost("lottery-control", {
      bot_id: selectedBot, group_id: selectedGroup, revision: activityState.value.revision,
      operation_id: activityControlRequest.value, action,
      lottery_id: action === "cancel" ? activityState.value.current_lottery?.id || "" : "",
    });
    if (sequence !== activitySequence || selectedBot !== botId.value || selectedGroup !== groupId.value)
      return;
    activityState.value = result.state;
    fillActivityForm();
    notice.value = result.result;
    activityControlRequest.value = "";
    activityControlScope.value = "";
  } catch (err) {
    if (sequence === activitySequence && selectedBot === botId.value && selectedGroup === groupId.value) {
      activityControlUnconfirmed.value = true;
      activityError.value = (err instanceof Error ? err.message + " " : "") + t("请刷新核对当前活动；不会自动重试。", "Refresh the round state; no automatic retry.");
    }
  } finally {
    activityControlling.value = false;
  }
}
async function pauseSchedule() {
  const schedule = scheduleState.value.schedule;
  if (!schedule || !scheduleState.value.revision || !bridge || scheduleLoading.value || scheduleSaving.value) return;
  const sequence = scheduleSequence, selectedBot = botId.value, selectedGroup = groupId.value;
  scheduleSaving.value = true;
  scheduleError.value = "";
  try {
    await bridge.apiPost("schedule", {
      bot_id: selectedBot,
      group_id: selectedGroup,
      version: schedule.version,
      revision: scheduleState.value.revision,
      action: "pause",
    });
    if (sequence !== scheduleSequence || selectedBot !== botId.value || selectedGroup !== groupId.value)
      return;
    confirmSchedulePause.value = false;
    await loadSchedule();
    if (selectedBot === botId.value && selectedGroup === groupId.value)
      notice.value = t(
        "后续定时操作已暂停，当前群状态未改变",
        "Future scheduled actions paused; current group state unchanged",
      );
  } catch (err) {
    if (selectedBot === botId.value && selectedGroup === groupId.value)
      scheduleError.value =
        err instanceof Error
          ? err.message
          : t("定时规则暂停失败", "Could not pause schedule");
  } finally {
    scheduleSaving.value = false;
  }
}
onMounted(async () => {
  if (bridge) {
    await bridge.ready();
    locale.value = bridge.getLocale();
    document.documentElement.lang = locale.value;
    unsubscribe = bridge.onContext((context) => {
      if (context.locale) {
        locale.value = context.locale;
        document.documentElement.lang = locale.value;
      }
    });
  }
  await reload();
});
onBeforeUnmount(() => {
  unsubscribe?.();
  clearReservedSearch();
  auditSequence++;
  activitySequence++;
  scheduleSequence++;
});
</script>

<template>
  <main>
    <header class="page-header">
      <div class="heading">
        <ShieldCheck :size="26" />
        <div>
          <h1>{{ t("旺商聊群管", "Wangshangliao Moderation") }}</h1>
          <p v-if="!openVisible">
            {{ bot?.nickname || bot?.name || "Wangshangliao"
            }}<span v-if="bot?.account_id"> · {{ bot.account_id }}</span>
          </p>
        </div>
      </div>
      <div class="header-actions">
        <span v-if="bot && !openVisible" class="badge" :class="bot.state"
          ><span class="status-dot" />{{ label(bot.state) }}</span
        ><span v-if="bot && !openVisible && bot.business_active !== undefined" class="badge" :class="{ warning: !bot.business_active }">{{ bot.business_active ? t("业务运行中", "Business active") : t("业务暂停", "Business paused") }}</span><button
          type="button"
          class="icon-button"
          :disabled="busy"
          :title="t('刷新配置', 'Refresh settings')"
          :aria-label="t('刷新配置', 'Refresh settings')"
          @click="
            guard(() => {
              void reload();
            })
          "
        >
          <RefreshCw :size="18" :class="{ spin: loading }" />
        </button>
      </div>
    </header>
    <div v-if="error" role="alert" class="message error">{{ error }}</div>
    <div v-if="notice" role="status" class="message success">{{ notice }}</div>
    <div v-if="loading && !bot" class="empty">
      <LoaderCircle class="spin" :size="24" />{{ t("读取中", "Loading") }}
    </div>
    <section v-else-if="!bot">
      <p>{{ t("暂无生产旺商聊实例", "No production Wangshangliao instances") }}</p>
      <OpenService v-if="bridge" :bridge="bridge" />
    </section>
    <template v-else>
      <nav class="tabs primary-tabs" role="tablist"
        :aria-label="t('功能分类', 'Feature categories')" @keydown="navigateTabs">
        <button v-for="item in sections" :id="`section-${item.id}`" :key="item.id"
          type="button" role="tab" :aria-controls="`section-panel-${item.id}`"
          :aria-selected="section.id === item.id" :tabindex="section.id === item.id ? 0 : -1"
          :class="{ selected: section.id === item.id }"
          @click="selectTab(lastTabs[item.id] || item.tabs.find(id => tabs.some(view => view.id === id))!)">
          <component :is="item.icon" :size="17" />{{ item.name }}
        </button>
      </nav>
      <div :id="`section-panel-${section.id}`" class="section-panel" role="tabpanel"
        :aria-labelledby="`section-${section.id}`">
      <nav class="tabs secondary-tabs" role="tablist"
        :aria-label="t('管理视图', 'Management views')" @keydown="navigateTabs">
        <button v-for="item in sectionTabs" :id="`tab-${item.id}`" :key="item.id"
          type="button" role="tab" :aria-controls="`panel-${item.id}`"
          :aria-selected="tab === item.id" :tabindex="tab === item.id ? 0 : -1"
          :class="{ selected: tab === item.id }" @click="selectTab(item.id)">
          {{ item.name }}
        </button>
      </nav>
      <div v-if="!openVisible && !['examples', 'architecture', 'cashback'].includes(tab)" class="scope-bar">
        <label
          >{{ t("机器人实例", "Bot instance")
          }}<select
            :value="botId"
            :aria-label="t('机器人实例', 'Bot instance')"
            :disabled="busy"
            @change="select($event, 'bot')"
          >
            <option v-for="item in bots" :key="item.id" :value="item.id">
              {{ getPlatformInstanceName({ ...item, type: "wangshangliao" }) }}<template v-if="item.nickname && item.account_id"> · {{ item.account_id }}</template>
            </option>
          </select></label
        >
        <label
          >{{ t("当前群", "Current group")
          }}<select
            :value="groupId"
            :aria-label="t('当前群', 'Current group')"
            :disabled="busy || !groupOptions.length"
            @change="select($event, 'group')"
          >
            <option v-if="!bot.enabled_groups?.length" value="">
              {{ t("没有已启用群", "No enabled groups") }}
            </option>
            <option
              v-for="id in groupOptions"
              :key="id"
              :value="id"
              :disabled="!bot.enabled_groups?.includes(id)"
            >
              {{ bot.group_names?.[id] || t("群", "Group") }} · {{ id }}
              {{ bot.enabled_groups?.includes(id) ? "" : t("（未启用）", "(Not enabled)") }}
            </option>
          </select></label
        >
      </div>
      <div v-if="['settings', 'permissions', 'audit'].includes(tab)" class="overview">
        <div>
          <span>{{ t("自动审核", "Automatic moderation") }}</span
          ><strong
            :class="{ enabled: active && activePolicy.automation_enabled }"
            >{{
              active && activePolicy.automation_enabled
                ? t("已开启", "Enabled")
                : t("关闭", "Off")
            }}</strong
          >
        </div>
        <div>
          <span>{{ t("审核方式", "Review mode") }}</span
          ><strong>{{
            activePolicy.semantic?.enabled
              ? t("AI + 规则兜底", "AI + fallback")
              : t("确定性规则", "Rules")
          }}</strong>
        </div>
        <div>
          <span>{{ t("踢出阈值", "Removal threshold") }}</span
          ><strong>{{
            activePolicy.auto_kick?.[groupId]
              ? t("3 次禁言后再违规", "New violation after 3 mutes")
              : t("未开启", "Off")
          }}</strong>
        </div>
        <div>
          <span>{{ t("待核验禁言", "Unknown mutes") }}</span
          ><strong :class="{ warning: unknownCount }">{{
            auditLoading ? "-" : unknownCount
          }}</strong>
        </div>
      </div>
      <VideoRelayPanel v-if="tab === 'video-relay' && bridge" id="panel-video-relay"
        :key="`${botId}/${groupId}/${relayEpoch}`"
        role="tabpanel" aria-labelledby="tab-video-relay" :bot-id="botId" :group-id="groupId"
        :group-name="bot.group_names?.[groupId]" :locale="locale" :bridge="bridge"
        @dirty="relayDirty = $event" @busy="relayBusy = $event" />
      <form @submit.prevent="['settings', 'permissions', 'business'].includes(tab) && save()">
        <fieldset :disabled="busy" class="form-body">
          <OpenService v-if="bridge" v-show="openVisible" :id="openVisible ? `panel-${tab}` : 'panel-open-service-hidden'"
            role="tabpanel" :aria-labelledby="openVisible ? `tab-${tab}` : undefined" :bridge="bridge" :view="openView" />
          <p v-if="['business', 'operations'].includes(tab) && bot.business_active === undefined" role="status">{{ t("此实例尚未切换到插件配置服务。", "This instance has not switched to the plugin policy service.") }}</p>
          <BusinessControls v-if="tab === 'business' && bridge && bot.business_active !== undefined" id="panel-business" role="tabpanel" aria-labelledby="tab-business" :bot="bot" :form="form" :group-id="groupId" :bridge="bridge" :t="t" />
          <OperationsLog v-if="tab === 'operations' && bridge && bot.business_active !== undefined" id="panel-operations" role="tabpanel" aria-labelledby="tab-operations" :bot-id="botId" :bridge="bridge" :t="t" />
          <SpeechActivityPanel v-if="tab === 'speech-activity' && bridge" id="panel-speech-activity"
            role="tabpanel" aria-labelledby="tab-speech-activity" :bot-id="botId" :group-id="groupId"
            :group-name="bot.group_names?.[groupId]" :bridge="bridge" />
          <section
            v-if="tab === 'settings'"
            id="panel-settings"
            role="tabpanel"
            aria-labelledby="tab-settings"
          >
            <h2>{{ t("AI 对话模型路由", "AI conversation routing") }}</h2>
            <div class="field-grid">
              <label class="wide"
                >{{
                  t("本群 @ 对话模型", "Conversation model for this group")
                }}<select v-model="form.group.customer_provider_id">
                  <option value="">
                    {{
                      t(
                        "沿用当前群会话模型",
                        "Use current group conversation model",
                      )
                    }}
                  </option>
                  <option
                    v-for="item in providers"
                    :key="item.id"
                    :value="item.id"
                  >
                    {{ item.id }} · {{ item.model }}
                  </option>
                </select></label
              >
            </div>
            <p class="field-note">
              {{
                t(
                  "群聊模型使用本群现有的人格与知识库；违规审核模型在下方单独设置。",
                  "Group conversations use this group's persona and knowledge base; moderation has a separate model below.",
                )
              }}
            </p>
            <h2>{{ t("自动违规处理", "Automatic moderation") }}</h2>
            <div class="switch-row">
              <label for="moderation">{{
                t("启用群管", "Enable moderation")
              }}</label
              ><input
                id="moderation"
                v-model="form.policy.enabled"
                type="checkbox"
                role="switch"
                class="switch"
              />
            </div>
            <div class="switch-row">
              <label for="automation">{{
                t("自动违规处理", "Automatic sanctions")
              }}</label
              ><input
                id="automation"
                v-model="form.policy.automation_enabled"
                type="checkbox"
                role="switch"
                class="switch"
              />
            </div>
            <div class="switch-row">
              <label for="semantic">{{
                t("AI 上下文语义审核", "AI contextual review")
              }}</label
              ><input
                id="semantic"
                v-model="form.policy.semantic.enabled"
                type="checkbox"
                role="switch"
                class="switch"
              />
            </div>
            <div v-if="form.policy.semantic.enabled" class="field-grid">
              <label class="wide"
                >{{ t("违规审核模型", "Violation-review model")
                }}<select
                  v-model="form.policy.semantic.provider_id"
                  id="provider"
                >
                  <option value="">
                    {{ t("使用会话模型", "Use session model") }}
                  </option>
                  <option
                    v-if="
                      form.policy.semantic.provider_id &&
                      !providers.some(
                        (item) => item.id === form.policy.semantic.provider_id,
                      )
                    "
                    :value="form.policy.semantic.provider_id"
                  >
                    {{ form.policy.semantic.provider_id }} ·
                    {{ t("不可用", "Unavailable") }}
                  </option>
                  <option
                    v-for="item in providers"
                    :key="item.id"
                    :value="item.id"
                  >
                    {{ item.id }}
                  </option>
                </select></label
              >
              <label
                >{{ t("审核超时（秒）", "Timeout (seconds)")
                }}<input
                  v-model.number="form.policy.semantic.timeout_seconds"
                  id="timeout"
                  type="number"
                  min="1"
                  max="30"
                  step="1"
                  required
              /></label>
              <label
                >{{ t("上下文条数", "Context messages")
                }}<input
                  v-model.number="form.policy.semantic.context_limit"
                  id="context-limit"
                  type="number"
                  min="1"
                  max="50"
                  step="1"
                  required
              /></label>
            </div>
            <h2>{{ t("规则兜底", "Rule fallback") }}</h2>
            <div class="switch-row">
              <span>{{ t("刷屏阈值", "Flood thresholds") }}</span>
              <span>{{ t("3秒5条 / 10秒15条 / 30秒同文5条", "5 messages / 3s; 15 / 10s; 5 identical / 30s") }}</span>
            </div>
            <div class="switch-row">
              <label for="progressive-mute">{{
                t("每轮警告3次，再禁言1 / 3 / 10分钟；10分钟后重新累计", "3 warnings, then 1 / 3 / 10 minute mutes; restart after the final mute")
              }}</label>
              <input
                id="progressive-mute"
                v-model="form.policy.progressive_mute"
                @change="form.policy.progressive_mute && (form.policy.recall_enabled = true)"
                type="checkbox"
                role="switch"
                class="switch"
              />
            </div>
            <div class="switch-row">
              <label for="recall">{{
                t("撤回违规消息", "Recall violation messages")
              }}</label
              ><input
                id="recall"
                v-model="form.policy.recall_enabled"
                :disabled="form.policy.progressive_mute"
                type="checkbox"
                role="switch"
                class="switch"
              />
            </div>
            <div class="field-grid">
              <label class="wide"
                >{{ form.policy.progressive_mute
                  ? t("旧模式处罚冷却（秒）", "Legacy cooldown (seconds)")
                  : t("每群处罚冷却（秒）", "Per-group cooldown (seconds)")
                }}<input
                  v-model.number="form.policy.cooldown_seconds"
                  :disabled="form.policy.progressive_mute"
                  id="cooldown"
                  type="number"
                  min="10"
                  max="86400"
                  step="1"
                  required
              /></label>
              <label
                >{{ t("禁言关键词", "Mute keywords")
                }}<textarea
                  :value="form.policy.mute_keywords.join('\n')"
                  rows="4"
                  @input="keywords('mute_keywords', $event)"
                ></textarea>
              </label>
              <label
                >{{ t("高风险关键词（不自动踢人）", "High-risk keywords (no auto-kick)")
                }}<textarea
                  :value="form.policy.kick_keywords.join('\n')"
                  rows="4"
                  @input="keywords('kick_keywords', $event)"
                ></textarea>
              </label>
            </div>
            <h2>{{ t("回复设置", "Replies") }}</h2>
            <div v-if="groupId" class="switch-row">
              <label for="group-reply">{{
                t("当前群回复", "Current group replies")
              }}</label
              ><input
                id="group-reply"
                v-model="form.group.reply"
                type="checkbox"
                role="switch"
                class="switch"
              />
            </div>
          </section>
          <section
            v-if="tab === 'permissions'"
            id="panel-permissions"
            role="tabpanel"
            aria-labelledby="tab-permissions"
          >
            <h2>{{ t("当前群动作授权", "Current group capabilities") }}</h2>
            <div v-if="!groupId" class="empty">
              {{ t("没有已启用群", "No enabled groups") }}
            </div>
            <template v-else>
              <GroupManagers
                v-if="bridge && !integrated"
                :bridge="bridge"
                :bot-id="botId"
                :group-id="groupId"
                :locale="locale"
                :disabled="loading || saving"
                @dirty="managersDirty = $event"
              />
              <div class="permission-grid">
                <label
                  v-for="[action, name] in actions"
                  :key="action"
                  class="permission"
                  ><input
                    type="checkbox"
                    :value="action"
                    :checked="groupPermissions.includes(action)"
                    @change="togglePermission(action, $event)"
                  />{{ name }}</label
                >
              </div>
              <h2>{{ t("自动升级", "Automatic escalation") }}</h2>
              <div class="switch-row">
                <label for="auto-kick">{{
                  activePolicy.manual_kick_only
                  ? t("自动踢出已停用（仅手动确认）", "Automatic removal disabled (manual confirmation only)")
                  : t(
                    "先禁言三次，第四次违规踢出",
                    "Mute three times; remove on the fourth violation",
                  )
                }}</label
                ><input
                  id="auto-kick"
                  v-model="form.group.auto_kick"
                  :disabled="activePolicy.manual_kick_only"
                  type="checkbox"
                  role="switch"
                  class="switch"
                />
              </div>
              <p
                v-if="
                  form.group.auto_kick &&
                  !['mute', 'kick', 'recall'].every((action) =>
                    groupPermissions.includes(action),
                  )
                "
                role="alert"
                class="message warning"
              >
                {{
                  t(
                    "自动升级所需授权不完整：禁言、踢出、撤回",
                    "Escalation requires mute, removal and recall grants",
                  )
                }}
              </p>
              <div class="switch-row">
                <label for="card-auto">{{
                  t("自动规范成员名片", "Automatic member cards")
                }}</label
                ><input
                  id="card-auto"
                  v-model="form.group.card_auto"
                  :disabled="!form.group.card_auto && (!form.policy.enabled || !groupPermissions.includes('rename'))"
                  type="checkbox"
                  role="switch"
                  class="switch"
                />
              </div>
              <div class="field-grid">
                <label for="card-suffix" class="wide">
                  {{ t("本群名片后缀（可留空）", "Card suffix for this group (optional)") }}
                  <input id="card-suffix" v-model="form.group.card_suffix" type="text" />
                </label>
              </div>
              <p
                v-if="
                  form.group.card_auto && !groupPermissions.includes('rename')
                "
                role="alert"
                class="message warning"
              >
                {{
                  t("当前群尚未授权名片修改", "Card renaming is not authorized")
                }}
              </p>
            </template>
          </section>
        </fieldset>
        <Cashier v-if="tab === 'cashback' && bridge" id="panel-cashback" role="tabpanel" aria-labelledby="tab-cashback" :bridge="bridge" @dirty="settlementDirty = $event" />
        <section
          v-if="tab === 'activities'"
          id="panel-activities"
          role="tabpanel"
          aria-labelledby="tab-activities"
        >
          <div class="section-heading">
            <h2>{{ t("玩家福利抽奖", "Player lottery") }}</h2>
            <button
              type="button"
              class="icon-button"
              :disabled="activityLoading || activitySaving || activityControlling || !groupId"
              :title="t('刷新活动状态', 'Refresh activity state')"
              :aria-label="t('刷新活动状态', 'Refresh activity state')"
              @click="guard(() => loadActivities())"
            >
              <RefreshCw :size="18" :class="{ spin: activityLoading }" />
            </button>
          </div>
          <p v-if="activityError" role="alert" class="message error">
            {{ activityError }}
          </p>
          <p v-if="activityLoading" class="loading-line">
            <LoaderCircle :size="18" class="spin" />{{ t("读取中", "Loading") }}
          </p>
          <template v-if="activityState">
            <div class="control-overview">
              <div>
                <span>{{ t("当前抽奖", "Current lottery") }}</span>
                <strong>{{
                  activityState.current_lottery
                    ? label(activityState.current_lottery.status)
                    : t("无进行中活动", "No active draw")
                }}</strong>
              </div>
              <div><span>{{ t("参与口令", "Entry command") }}</span><strong>{{ activityState.current_lottery?.entry_phrase || activityState.lottery.entry_phrase || "参与抽奖" }}</strong></div>
            </div>
            <dl
              v-if="activityState.current_lottery"
              class="policy-details schedule-details"
            >
              <div>
                <dt>{{ t("本期奖品", "Current prize") }}</dt>
                <dd>{{ activityState.current_lottery.prize }}</dd>
              </div>
              <div v-if="activityState.current_lottery.packet_mode && activityState.current_lottery.packet_mode !== 'prize'">
                <dt>{{ t("红包总金额", "Packet total") }}</dt>
                <dd>{{ activityState.current_lottery.total_amount }} {{ t("元", "CNY") }} · {{ activityState.current_lottery.packet_mode === 'tiered' ? t("分档拼手气", "Fixed tiers + random") : activityState.current_lottery.packet_mode === 'lucky' ? t("拼手气", "Lucky split") : t("普通红包", "Equal split") }}</dd>
              </div>
              <div v-if="activityState.current_lottery.prize_tiers?.length">
                <dt>{{ t("本期固定档位", "Current fixed tiers") }}</dt>
                <dd>{{ formatTiers(activityState.current_lottery.prize_tiers) }}</dd>
              </div>
              <div v-if="Number(activityState.current_lottery.luckiest_bonus) > 0">
                <dt>{{ t("人气王额外奖励", "Luckiest winner bonus") }}</dt>
                <dd>{{ activityState.current_lottery.luckiest_bonus }} {{ t("元（原奖池外）", "CNY (outside packet pool)") }}</dd>
              </div>
              <div>
                <dt>{{ t("报名人数", "Entries") }}</dt>
                <dd>
                  {{ activityState.current_lottery.entries }} /
                  {{
                    activityState.current_lottery.capacity ||
                    t("不限", "unlimited")
                  }}
                </dd>
              </div>
              <div>
                <dt>{{ t("开始时间（北京时间）", "Start time (Beijing)") }}</dt>
                <dd>{{ activityState.current_lottery.starts ? date(activityState.current_lottery.starts) : t("立即发布", "Immediate publication") }}</dd>
              </div>
              <div>
                <dt>{{ t("开奖时间（北京时间）", "Draw time (Beijing)") }}</dt>
                <dd>{{ activityState.current_lottery.draw_on_full ? t("报满设定人数后公布", "Publish when full") : activityState.current_lottery.status === "schedule_blocked" ? t("尚未发布，无开奖时间", "Not published; no draw time") : date(activityState.current_lottery.ends) }}{{ activityState.current_lottery.status === "scheduled" && !activityState.current_lottery.draw_on_full ? t("（预计）", " (estimated)") : "" }}</dd>
              </div>
              <div>
                <dt>{{ t("领奖联系人", "Claim contact") }}</dt>
                <dd>{{ activityState.current_lottery.contact }}</dd>
              </div>
              <div v-if="activityState.current_lottery.remarks">
                <dt>{{ t("活动备注", "Activity remarks") }}</dt>
                <dd class="lottery-remarks">{{ activityState.current_lottery.remarks }}</dd>
              </div>
              <template v-if="activityState.current_lottery.fixed_packets && ['finished', 'result_unknown', 'cancelled', 'notice_unknown'].includes(activityState.current_lottery.status)">
                <div>
                  <dt>{{ t("本期分配（待发放）", "Allocated (pending payout)") }}</dt>
                  <dd>{{ activityState.current_lottery.allocated_count }} {{ t("份", "packets") }} · {{ activityState.current_lottery.allocated_amount }} {{ t("元", "CNY") }}</dd>
                </div>
                <div>
                  <dt>{{ t("未领取退回（未支出）", "Unclaimed (unspent)") }}</dt>
                  <dd>{{ activityState.current_lottery.capacity - activityState.current_lottery.allocated_count }} {{ t("份", "packets") }} · {{ activityState.current_lottery.unclaimed_amount }} {{ t("元", "CNY") }}</dd>
                </div>
                <div v-if="Number(activityState.current_lottery.luckiest_bonus) > 0">
                  <dt>{{ t("额外奖励待发放／本期待发放合计", "Bonus allocated / total payable") }}</dt>
                  <dd>{{ activityState.current_lottery.bonus_allocated_amount }} / {{ activityState.current_lottery.total_payable_amount }} {{ t("元", "CNY") }}</dd>
                </div>
              </template>
              <div v-if="activityState.current_lottery.error">
                <dt>{{ t("活动异常", "Activity issue") }}</dt>
                <dd>{{ label(activityState.current_lottery.error) }}</dd>
              </div>
            </dl>
            <h2>{{ t("下一期抽奖参数", "Next lottery settings") }}</h2>
            <div class="field-grid">
              <label
                >{{ t("活动名称", "Draw name")
                }}<input
                  v-model="activityForm.prize"
                  type="text"
                  maxlength="128"
                  required
              /></label>
              <label>{{ t("红包类型", "Packet type") }}
                <select v-model="activityForm.packet_mode" :aria-label="t('红包类型', 'Packet type')">
                  <option value="ordinary">{{ t("普通红包", "Equal split") }}</option>
                  <option value="lucky">{{ t("拼手气", "Lucky split") }}</option>
                  <option value="tiered">{{ t("分档拼手气", "Fixed tiers + random") }}</option>
                  <option v-if="activityState.lottery.packet_mode === 'prize'" value="prize">{{ t("原有奖品抽奖", "Legacy prize draw") }}</option>
                </select>
              </label>
              <label>{{ t("报名口令（整句）", "Entry phrase (exact)") }}
                <input v-model="activityForm.entry_phrase" type="text" minlength="1" maxlength="32" required />
              </label>
              <label v-if="activityForm.packet_mode !== 'prize'">{{ t("总金额（元）", "Total amount (CNY)") }}
                <input v-model="activityForm.total_amount" type="number" :min="activityForm.integer_packets ? 1 : 0.01" max="999999.99" :step="activityForm.integer_packets ? 1 : 0.01" required />
              </label>
              <label v-if="activityForm.packet_mode !== 'prize'" class="check">
                <input v-model="activityForm.integer_packets" type="checkbox" />
                {{ t("仅整数元（每份至少1元）", "Whole yuan only (minimum CNY 1)") }}
              </label>
              <label v-if="activityForm.packet_mode !== 'prize'">{{ t("开奖方式", "Draw trigger") }}
                <select v-model="activityForm.draw_on_full" :aria-label="t('开奖方式', 'Draw trigger')">
                  <option :value="false">{{ t("倒计时结束", "Countdown deadline") }}</option>
                  <option :value="true">{{ t("报满人数", "When full") }}</option>
                </select>
              </label>
              <label v-if="activityForm.packet_mode === 'lucky'">{{ t("人气王额外奖励（元，0为不加奖）", "Luckiest winner bonus (CNY, 0 = off)") }}
                <input v-model="activityForm.luckiest_bonus" type="number" min="0" max="999999.99" :step="activityForm.integer_packets ? 1 : 0.01" required />
              </label>
              <label
                >{{ t("领奖联系人", "Claim contact")
                }}<input
                  v-model="activityForm.contact"
                  type="text"
                  maxlength="128"
                  required
              /></label>
              <label v-if="activityForm.packet_mode === 'prize'"
                >{{ t("中奖名额", "Winner count")
                }}<input
                  v-model.number="activityForm.winners"
                  type="number"
                  min="1"
                  max="100"
                  step="1"
                  required
              /></label>
              <label
                >{{ t("开始时间（北京时间，留空立即发送）", "Start time (Beijing, blank = immediate)")
                }}<input
                  v-model="activityForm.start_at"
                  type="datetime-local"
                  step="60"
                />
              </label>
              <label v-if="!activityForm.draw_on_full || activityForm.packet_mode === 'prize'"
                >{{ t("倒计时（分钟）", "Countdown (minutes)")
                }}<input
                  v-model.number="activityForm.duration_minutes"
                  type="number"
                  min="1"
                  max="10080"
                  step="1"
                  required
              /></label>
              <label
                >{{ activityForm.packet_mode === 'prize' ? t("参与上限（0为不限）", "Entry limit (0 = unlimited)") : t("参加人数", "Participant count")
                }}<input
                  v-model.number="activityForm.capacity"
                  type="number"
                  :min="activityForm.packet_mode === 'prize' ? 0 : 1"
                  :max="activityForm.packet_mode === 'prize' ? 10000 : 100"
                  step="1"
                  required
              /></label>
              <label class="check">
                <input v-model="activityForm.by_invites" type="checkbox" />
                {{ t("是否按照邀请进行抽奖", "Require verified invitations") }}
              </label>
              <label v-if="activityForm.by_invites"
                >{{ t("拉几人可以抽（有效邀请）", "Verified invites required to enter")
                }}<input
                  v-model.number="activityForm.invite_gate"
                  type="number"
                  min="1"
                  max="10000"
                  step="1"
                  required
              /></label>
              <label class="wide">{{ t("抽奖备注／参与要求（选填）", "Lottery remarks / eligibility (optional)") }}
                <textarea v-model="activityForm.remarks" rows="3" maxlength="500"
                  :placeholder="t('例如：须使用指定头像，昵称包含活动标识；领奖时由管理员核验。', 'For example: use the required avatar and event name tag; verified by the claim administrator.')" />
              </label>
            </div>
            <dl v-if="activityForm.packet_mode === 'lucky' && Number(activityForm.luckiest_bonus) > 0"
              class="policy-details" aria-live="polite">
              <div><dt>{{ t("人气王", "Luckiest winner") }}</dt><dd>{{ t("本期最高红包者；并列随机选1人", "Highest packet; one randomly chosen if tied") }}</dd></div>
              <div><dt>{{ t("总预算（红包＋额外奖励）", "Total budget (packets + bonus)") }}</dt>
                <dd>{{ ((Math.round(Number(activityForm.total_amount || 0) * 100) + Math.round(Number(activityForm.luckiest_bonus || 0) * 100)) / 100).toFixed(2) }} {{ t("元", "CNY") }}</dd>
              </div>
            </dl>
            <p class="message">
              {{ t("活动开始后，群员发送报名口令即可参与。", "Members enter using the entry phrase after publication.") }}
            </p>
            <template v-if="activityForm.packet_mode === 'tiered'">
              <div class="section-heading">
                <h3>{{ t("固定金额档位", "Fixed amount tiers") }}</h3>
                <button type="button" :disabled="activityForm.prize_tiers.length >= 10"
                  @click="addPrizeTier">
                  <Plus :size="16" />{{ t("添加档位", "Add tier") }}
                </button>
              </div>
              <div v-for="(tier, index) in activityForm.prize_tiers" :key="index" class="lottery-tier-row">
                <label>{{ t("每份金额（元）", "Amount per packet (CNY)") }}
                  <input :value="tier.amount" type="number" :min="activityForm.integer_packets ? 1 : 0.01" max="999999.99" :step="activityForm.integer_packets ? 1 : 0.01" required @input="updatePrizeTierAmount(index, $event)" />
                </label>
                <label>{{ t("份数", "Packets") }}
                  <input v-model.number="tier.count" type="number" min="1" max="100" step="1" required />
                </label>
                <button type="button" class="icon-button"
                  :title="t('删除档位', 'Remove tier')" :aria-label="t('删除档位', 'Remove tier')"
                  @click="removePrizeTier(index)"><Trash2 :size="17" /></button>
              </div>
              <dl class="policy-details" :class="{ warning: tierSummary.invalid }" aria-live="polite">
                <div><dt>{{ t("固定份额", "Fixed allocation") }}</dt><dd>{{ tierSummary.count }} × {{ t("份", "packets") }} · {{ tierSummary.amount }} {{ t("元", "CNY") }}</dd></div>
                <div><dt>{{ t("剩余随机份额", "Remaining random allocation") }}</dt><dd>{{ tierSummary.remainingCount }} × {{ t("份", "packets") }} · {{ tierSummary.remainingAmount }} {{ t("元", "CNY") }}</dd></div>
              </dl>
              <div class="section-heading lottery-reservation-heading">
                <h3>{{ t("指定档位成员（选填）", "Reserved tier members (optional)") }}</h3>
              </div>
              <div class="lottery-reservation-search">
                <label>{{ t("对应固定档位", "Fixed tier") }}
                  <select v-model="reservedTier" :aria-label="t('指定成员对应固定档位', 'Reserved member fixed tier')">
                    <option v-for="tier in activityForm.prize_tiers" :key="tier.amount" :value="tier.amount">
                      {{ tier.amount }} {{ t("元", "CNY") }} · {{ tier.count }} {{ t("份", "packets") }}
                    </option>
                  </select>
                </label>
                <div class="lottery-member-query">
                  <label>{{ t("当前群成员", "Current group member") }}
                    <input v-model="reservedQuery" type="search" maxlength="64"
                      :placeholder="t('输入名称或UID', 'Name or UID')" autocomplete="off"
                      @keydown.enter.prevent="searchReservedMembers" />
                  </label>
                  <button type="button" :disabled="reservedSearching || !reservedQuery.trim() || !reservedTier" @click="searchReservedMembers">
                    <LoaderCircle v-if="reservedSearching" :size="16" class="spin" />
                    <Users v-else :size="16" />{{ t("搜索", "Search") }}
                  </button>
                </div>
              </div>
              <p v-if="reservedSearched && !reservedResults.length" class="message" role="status">
                {{ t("未找到可参与抽奖的当前群成员", "No eligible current group members found") }}
              </p>
              <div v-if="reservedResults.length" class="lottery-member-results" role="radiogroup"
                :aria-label="t('指定成员搜索结果', 'Reserved member search results')">
                <label v-for="member in reservedResults" :key="member.member">
                  <input v-model="reservedChosen" type="radio" name="reserved-lottery-member" :value="member.member" />
                  <span>{{ member.name }}<small>UID {{ member.member }}</small></span>
                </label>
                <button type="button" :disabled="!reservedChosen || !reservedTier" @click="addReservedMember">
                  <Plus :size="16" />{{ t("加入指定方案", "Add reservation") }}
                </button>
              </div>
              <ul v-if="activityForm.reserved_members.length" class="lottery-reserved-members">
                <li v-for="member in activityForm.reserved_members" :key="member.member">
                  <span><strong>{{ member.name }}</strong><small>UID {{ member.member }} · {{ member.amount }} {{ t("元", "CNY") }}</small></span>
                  <button type="button" class="icon-button" :title="t('移除指定成员', 'Remove reservation')"
                    :aria-label="t(`移除${member.name}的指定档位`, `Remove ${member.name}'s reserved tier`)"
                    @click="activityForm.reserved_members = activityForm.reserved_members.filter(item => item.member !== member.member)">
                    <Trash2 :size="17" />
                  </button>
                </li>
              </ul>
              <p v-else class="message">{{ t("未指定成员，全部档位按原规则随机分配。", "No reservations; all tiers use the default random allocation.") }}</p>
            </template>
            <div class="save-bar">
              <span :class="{ warning: activityDirty }">{{
                activityDirty
                  ? t("活动参数有未保存更改", "Unsaved activity changes")
                  : t("活动参数已同步", "Activity settings are current")
              }}</span>
              <div class="lottery-controls">
                <button v-if="!activeLottery" type="button" class="primary"
                  :disabled="activityDirty || activitySaving || activityControlling || activityControlUnconfirmed"
                  @click="controlLottery('start')"><CalendarClock :size="17" />
                  {{ activityForm.start_at ? t("预约发布", "Schedule draw") : t("开启活动", "Start draw") }}
                </button>
                <button v-else type="button"
                  :disabled="activityDirty || activitySaving || activityControlling || activityControlUnconfirmed || !['scheduled','schedule_blocked','open','blocked'].includes(activityState.current_lottery?.status)"
                  @click="controlLottery('cancel')"><Trash2 :size="17" />{{ t("取消活动", "Cancel draw") }}</button>
                <button
                  type="button"
                  :disabled="activitySaving || !activityDirty"
                  @click="fillActivityForm"
                >
                  {{ t("还原", "Reset") }}</button
                ><button
                  type="button"
                  class="primary"
                  :disabled="activitySaving || !activityDirty"
                  @click="saveActivities"
                >
                  <LoaderCircle
                    v-if="activitySaving"
                    :size="17"
                    class="spin"
                  /><Save v-else :size="17" />{{
                    activitySaving
                      ? t("保存中", "Saving")
                      : t("保存抽奖参数", "Save lottery settings")
                  }}
                </button>
              </div>
            </div>
          </template>
        </section>
        <section v-if="tab === 'invite-rewards'" id="panel-invite-rewards" role="tabpanel" aria-labelledby="tab-invite-rewards">
          <div class="section-heading">
            <h2>{{ t("邀请奖励", "Invitation rewards") }}</h2>
            <button type="button" class="icon-button" :disabled="activityLoading || activitySaving || activityControlling || !groupId"
              :title="t('刷新邀请奖励', 'Refresh invitation rewards')" @click="guard(() => loadActivities())">
              <RefreshCw :size="18" :class="{ spin: activityLoading }" />
            </button>
          </div>
          <p v-if="activityError" role="alert" class="message error">{{ activityError }}</p>
          <p v-if="activityLoading" class="loading-line"><LoaderCircle :size="18" class="spin" />{{ t("读取中", "Loading") }}</p>
          <template v-if="activityState">
            <div class="control-overview">
              <div><span>{{ t("奖励状态", "Reward status") }}</span><strong>{{ activityState.invitation.enabled ? t("已开启", "Enabled") : t("未开启", "Off") }}</strong></div>
              <div><span>{{ t("已记账奖励（积分）", "Recorded credits (points)") }}</span><strong>{{ activityState.invitation.credited_count }} · {{ activityState.invitation.credited_total }}</strong></div>
              <div><span>{{ t("当前仍在群", "Currently in group") }}</span><strong>{{ activityState.invitation.active_count }}</strong></div>
              <div><span>{{ t("已退群", "Left group") }}</span><strong>{{ activityState.invitation.left_count }}</strong></div>
              <div><span>{{ t("待核验邀请", "Pending attribution") }}</span><strong>{{ activityState.invitation.pending_count }}</strong></div>
            </div>
            <p v-if="activityState.invitation.error" role="alert" class="message warning">{{ activityState.invitation.error }}</p>
            <div class="field-grid">
              <label>{{ t("每位有效邀请奖励（积分）", "Per verified invite (points)") }}
                <input v-model="activityForm.invite_rate" type="number" min="0" max="999999.99" step="0.01" required />
              </label>
            </div>
            <div class="save-bar">
              <span>{{ activityDirty ? t("有未保存更改", "Unsaved changes") : t("已同步", "Current") }}</span>
              <div>
                <button type="button" :disabled="activitySaving || !activityDirty" @click="fillActivityForm"><X :size="17" />{{ t("还原", "Reset") }}</button>
                <button type="button" class="primary" :disabled="activitySaving || !activityDirty" @click="saveActivities">
                  <LoaderCircle v-if="activitySaving" :size="17" class="spin" /><Save v-else :size="17" />{{ t("保存邀请奖励", "Save invitation reward") }}
                </button>
              </div>
            </div>
          </template>
        </section>
        <section
          v-if="tab === 'schedule'"
          id="panel-schedule"
          role="tabpanel"
          aria-labelledby="tab-schedule"
        >
          <div class="section-heading">
            <h2>{{ t("每日全员禁言计划", "Daily group mute schedule") }}</h2>
            <button
              type="button"
              class="icon-button"
              :disabled="scheduleLoading || scheduleSaving || !groupId"
              :title="t('刷新定时状态', 'Refresh schedule')"
              :aria-label="t('刷新定时状态', 'Refresh schedule')"
              @click="loadSchedule"
            >
              <RefreshCw :size="18" :class="{ spin: scheduleLoading }" />
            </button>
          </div>
          <p v-if="scheduleError" role="alert" class="message error">
            {{ scheduleError }}
          </p>
          <p v-if="scheduleLoading" class="loading-line">
            <LoaderCircle :size="18" class="spin" />{{ t("读取中", "Loading") }}
          </p>
          <template v-if="scheduleState.schedule">
            <dl class="policy-details schedule-details">
              <div>
                <dt>{{ t("状态", "Status") }}</dt>
                <dd>
                  <span
                    class="badge"
                    :class="
                      scheduleState.schedule.status === 'active'
                        ? 'online'
                        : 'unknown'
                    "
                    >{{ label(scheduleState.schedule.status) }}</span
                  >
                </dd>
              </div>
              <div>
                <dt>{{ t("每日时段", "Daily window") }}</dt>
                <dd>
                  {{ scheduleState.schedule.start }} —
                  {{ scheduleState.schedule.end }} ({{
                    scheduleState.schedule.zone
                  }})
                </dd>
              </div>
              <div>
                <dt>{{ t("下次动作", "Next action") }}</dt>
                <dd>
                  {{ date(scheduleState.schedule.next_at) }}
                  · {{ label(scheduleState.schedule.next_action) }}
                </dd>
              </div>
              <div v-if="scheduleState.schedule.error">
                <dt>{{ t("暂停原因", "Pause reason") }}</dt>
                <dd>{{ scheduleState.schedule.error }}</dd>
              </div>
            </dl>
            <p class="message warning">
              {{
                t(
                  "暂停只阻止后续计划，不会解除当前禁言。",
                  "Pausing only stops future scheduled actions; it does not unmute the group.",
                )
              }}
            </p>
            <button
              v-if="scheduleState.schedule.status === 'active'"
              type="button"
              class="danger"
              :disabled="scheduleSaving"
              @click="confirmSchedulePause = true"
            >
              <Pause :size="16" />{{
                t("暂停后续计划", "Pause future schedule")
              }}
            </button>
          </template>
          <div v-else-if="!scheduleLoading" class="empty schedule-empty">
            {{ t("当前群没有定时规则", "No schedule for this group") }}
          </div>
          <h2>{{ t("最近执行", "Recent executions") }}</h2>
          <div class="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>{{ t("计划时间", "Planned time") }}</th>
                  <th>{{ t("动作", "Action") }}</th>
                  <th>{{ t("结果", "Result") }}</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="row in scheduleState.executions"
                  :key="row.operation"
                >
                  <td>{{ date(row.planned) }}</td>
                  <td>{{ label(row.action) }}</td>
                  <td>
                    <span class="badge" :class="row.status">{{
                      label(row.status)
                    }}</span>
                  </td>
                </tr>
                <tr v-if="!scheduleState.executions.length">
                  <td colspan="3" class="table-empty">
                    {{ t("暂无执行记录", "No execution records") }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <h2>{{ t("规则变更记录", "Rule change history") }}</h2>
          <div class="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>{{ t("记录时间", "Recorded") }}</th>
                  <th>{{ t("版本", "Version") }}</th>
                  <th>{{ t("规则", "Window") }}</th>
                  <th>{{ t("状态", "Status") }}</th>
                  <th>{{ t("说明", "Details") }}</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="(row, index) in scheduleState.history"
                  :key="`${row.version}-${index}`"
                >
                  <td>{{ row.recorded }}</td>
                  <td>{{ row.version }}</td>
                  <td>{{ row.start }} — {{ row.end }}</td>
                  <td>
                    <span class="badge" :class="row.status">{{
                      label(row.status)
                    }}</span>
                  </td>
                  <td>{{ row.error || "—" }}</td>
                </tr>
                <tr v-if="!scheduleState.history.length">
                  <td colspan="5" class="table-empty">
                    {{ t("暂无规则变更记录", "No rule changes recorded") }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>
        <section
          v-if="tab === 'audit'"
          id="panel-audit"
          role="tabpanel"
          aria-labelledby="tab-audit"
        >
          <CurrentMutesPanel v-if="integrated && bridge && bot?.account_id && groupId"
            :key="`${botId}/${groupId}`" :bridge="bridge" :account="bot.account_id" :group="groupId" :locale="locale" />
          <BulkRecallPanel v-if="integrated && bridge && bot?.account_id && groupId"
            :key="`recalls/${botId}/${groupId}`" :bridge="bridge" :account="bot.account_id" :group="groupId" :locale="locale" />
          <div class="section-heading">
            <h2>{{ t("自动禁言累计", "Automatic mute counts") }}</h2>
            <button
              type="button"
              class="icon-button"
              :disabled="auditLoading || !groupId"
              :title="t('刷新记录', 'Refresh audit')"
              :aria-label="t('刷新记录', 'Refresh audit')"
              @click="loadAudit"
            >
              <RefreshCw :size="18" :class="{ spin: auditLoading }" />
            </button>
          </div>
          <p v-if="auditError" role="alert" class="message error">
            {{ auditError }}
          </p>
          <p v-if="auditLoading" class="loading-line">
            <LoaderCircle :size="18" class="spin" />{{ t("读取中", "Loading") }}
          </p>
          <div class="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>{{ t("成员", "Member") }}</th>
                  <th>{{ t("本轮受理", "Accepted in cycle") }}</th>
                  <th>{{ t("未知", "Unknown") }}</th>
                  <th>
                    {{ t("最近记录 · 北京时间", "Latest · Asia/Shanghai") }}
                  </th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in audit.counts" :key="row.member">
                  <td class="audit-member">
                    <span>{{ row.member_name || t("名称未获取", "Name unavailable") }}</span>
                    <small v-if="row.member_name_source === 'message'">{{ t("历史名称", "Historical name") }}</small>
                    <small class="mono">{{ row.member }}</small>
                  </td>
                  <td>
                    <span class="count">{{ row.accepted }} / 3</span>
                  </td>
                  <td :class="{ warning: row.unknown }">{{ row.unknown }}</td>
                  <td>{{ date(row.created) }}</td>
                </tr>
                <tr v-if="!audit.counts.length">
                  <td colspan="4" class="table-empty">
                    {{ t("暂无累计记录", "No mute counts") }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <h2>{{ t("最近自动处罚", "Recent automatic sanctions") }}</h2>
          <div class="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>{{ t("时间", "Time") }}</th>
                  <th>{{ t("成员", "Member") }}</th>
                  <th>{{ t("动作", "Action") }}</th>
                  <th>{{ t("禁言时长", "Mute duration") }}</th>
                  <th>{{ t("原消息撤回", "Message recall") }}</th>
                  <th>{{ t("状态", "Status") }}</th>
                  <th>{{ t("操作 ID", "Operation ID") }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in audit.sanctions" :key="row.operation">
                  <td>{{ date(row.created) }}</td>
                  <td class="audit-member">
                    <span>{{ row.member_name || t("名称未获取", "Name unavailable") }}</span>
                    <small v-if="row.member_name_source === 'message'">{{ t("历史名称", "Historical name") }}</small>
                    <small class="mono">{{ row.member }}</small>
                  </td>
                  <td>{{ label(row.action) }}</td>
                  <td>{{ row.minutes ? `${row.minutes} ${t("分钟", "min")}` : "-" }}</td>
                  <td>{{ row.recall_status ? label(row.recall_status) : "-" }}</td>
                  <td>
                    <span class="badge" :class="row.status">{{
                      label(row.status)
                    }}</span>
                    <small v-if="row.action_submitted === 0" class="warning-cleanup">{{ t("未提交处罚", "Sanction not submitted") }}</small>
                    <small v-if="row.action_error" class="warning-cleanup">{{ label(row.action_error) }}</small>
                    <small v-if="row.provider_code != null" class="mono warning-cleanup">{{ row.provider_code }}</small>
                  </td>
                  <td class="mono wrap-id">{{ row.operation }}</td>
                </tr>
                <tr v-if="!audit.sanctions.length">
                  <td colspan="7" class="table-empty">
                    {{ t("暂无自动处罚", "No automatic sanctions") }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <h2>{{ t("最近 AI 审核", "Recent AI reviews") }}</h2>
          <div class="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>{{ t("时间", "Time") }}</th>
                  <th>{{ t("结论", "Decision") }}</th>
                  <th>{{ t("类别", "Category") }}</th>
                  <th>{{ t("原因", "Reason") }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in audit.reviews" :key="row.message">
                  <td>{{ date(row.created) }}</td>
                  <td>
                    <span class="badge" :class="row.decision">{{
                      label(row.decision)
                    }}</span>
                  </td>
                  <td>{{ label(row.category) }}</td>
                  <td class="reason">{{ row.reason }}</td>
                </tr>
                <tr v-if="!audit.reviews.length">
                  <td colspan="4" class="table-empty">
                    {{ t("暂无 AI 审核记录", "No AI reviews") }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <h2>{{ t("内容规则审计", "Content rule audit") }}</h2>
          <div class="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>{{ t("时间", "Time") }}</th>
                  <th>{{ t("成员", "Member") }}</th>
                  <th>{{ t("类别", "Category") }}</th>
                  <th>{{ t("状态", "Status") }}</th>
                  <th>{{ t("原消息撤回", "Message recall") }}</th>
                  <th>{{ t("警告通知", "Warning notification") }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in audit.rules" :key="row.message">
                  <td>{{ date(row.observed) }}</td>
                  <td class="audit-member">
                    <span>{{ row.sender_name || t("名称未获取", "Name unavailable") }}</span>
                    <small v-if="row.sender_name_source === 'message'">{{ t("历史名称", "Historical name") }}</small>
                    <small class="mono">{{ row.sender }}</small>
                  </td>
                  <td>{{ label(row.category) }}</td>
                  <td>
                    {{ label(row.status === "review_only" && ["accepted", "verified"].includes(row.recall_status) ? "audit_incomplete" : row.status) }}
                    <small v-if="row.processing_error" class="warning-cleanup">{{ label(row.processing_error) }}</small>
                    <small v-if="row.processing_provider_code != null" class="warning-cleanup">{{ t("平台错误码：", "Provider code: ") }}{{ row.processing_provider_code }}</small>
                  </td>
                  <td>
                    {{ row.recall_status ? label(row.recall_status) : t("未查到撤回回执", "No recall receipt found") }}
                    <small v-if="row.recall_submitted === 0" class="warning-cleanup">{{ t("未提交撤回", "Recall not submitted") }}</small>
                    <small v-if="row.recall_error" class="warning-cleanup">{{ label(row.recall_error) }}</small>
                  </td>
                  <td>
                    <span v-if="row.warning_number">{{ t(`第${row.warning_number}次警告 · `, `Warning ${row.warning_number} · `) }}</span>
                    {{ label(row.warning_status || "not_recorded") }}
                    <small v-if="['accepted', 'verified'].includes(row.warning_cleanup_status)" class="warning-cleanup">{{ t("警告曾被自动清理", "Warning was auto-removed") }}</small>
                    <small v-else-if="row.warning_cleanup_status === 'pending'" class="warning-cleanup">{{ t("警告已安排自动清理", "Warning cleanup is scheduled") }}</small>
                  </td>
                </tr>
                <tr v-if="!audit.rules.length">
                  <td colspan="6" class="table-empty">
                    {{ t("暂无内容规则记录", "No content rule audit") }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <h2>{{ t("撤回来源", "Recall sources") }}</h2>
          <p v-if="!audit.recalls_available" role="status" class="message">
            {{ t("撤回通知暂不可读取，不能据此判断是谁撤回。", "Recall notifications are unavailable; the actor cannot be determined.") }}
          </p>
          <div class="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>{{ t("撤回时间 · 北京时间", "Recall time · Asia/Shanghai") }}</th>
                  <th>{{ t("原消息成员", "Message author") }}</th>
                  <th>{{ t("撤回人", "Recall actor") }}</th>
                  <th>{{ t("来源", "Source") }}</th>
                  <th>{{ t("证据", "Evidence") }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in audit.recalls" :key="`${row.message}-${row.client}-${row.author_peer}-${row.operator_peer}`">
                  <td>
                    {{ date(row.recalled_at || row.received_at) }}
                    <small v-if="!row.recalled_at">{{ t("通知接收时间", "Notification received") }}</small>
                  </td>
                  <td class="audit-member">
                    <span>{{ row.author_name || t("名称未获取", "Name unavailable") }}</span>
                    <small>{{ row.author_id ? t(`账号 ${row.author_id}`, `Account ${row.author_id}`) : t("账号待核验", "Account unverified") }}</small>
                  </td>
                  <td class="audit-member">
                    <span>{{ row.operator_name || t("名称未获取", "Name unavailable") }}</span>
                    <small>{{ row.operator_id ? t(`账号 ${row.operator_id}`, `Account ${row.operator_id}`) : t("账号待核验", "Account unverified") }}</small>
                  </td>
                  <td>{{ label(row.source) }}</td>
                  <td>
                    {{ label(row.status) }}
                    <small v-if="row.receipt_status">{{ t("机器人回执：", "Bot receipt: ") }}{{ label(row.receipt_status) }}</small>
                  </td>
                </tr>
                <tr v-if="!audit.recalls.length && audit.recalls_available">
                  <td colspan="5" class="table-empty">{{ t("暂无留存的撤回通知", "No retained recall notifications") }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>
        <ReferenceTables
          v-if="tab === 'architecture' || tab === 'matrix'"
          :view="tab"
          :locale="locale"
          :group-id="groupId"
          :permissions="activePermissions"
          :integrated="integrated"
        />
        <ConversationExamples
          v-if="tab === 'examples'"
          id="panel-examples"
          :locale="locale"
        />
        <section
          v-if="tab === 'commands'"
          id="panel-commands"
          role="tabpanel"
          aria-labelledby="tab-commands"
        >
          <CommandHelp v-if="integrated && bridge" :bridge="bridge" :locale="locale" @navigate="selectTab" />
          <template v-else>
          <h2>
            {{ t("群内命令", "Group commands") }}
          </h2>
          <div class="table-scroll">
            <table class="commands">
              <thead>
                <tr>
                  <th>{{ t("命令", "Command") }}</th>
                  <th>{{ t("使用身份", "Caller") }}</th>
                  <th>{{ t("已保存授权", "Saved grant") }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="item in commands" :key="item.action">
                  <td>
                    <code>{{ item.command }}</code>
                  </td>
                  <td>
                    {{
                      item.public
                        ? t("普通群员", "Group members")
                        : t("本群授权管理", "Authorized group manager")
                    }}
                  </td>
                  <td>
                    <span v-if="item.public" class="badge">{{
                      bot?.enable !== false &&
                      bot?.reply_groups?.[groupId] !== false
                        ? t("开放", "Open")
                        : t("群回复已关闭", "Group replies disabled")
                    }}</span>
                    <span
                      v-else
                      class="badge"
                      :class="{
                        allow:
                          active && activePermissions.includes(item.action),
                      }"
                      >{{
                        active && activePermissions.includes(item.action)
                          ? t("已授权", "Granted")
                          : t("未授权", "Not granted")
                      }}</span
                    >
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <dl class="policy-details">
            <div>
              <dt>{{ t("手动踢出", "Manual removal") }}</dt>
              <dd>
                {{
                  t(
                    "本群授权管理员预览与确认",
                    "Authorized group administrator preview and confirmation",
                  )
                }}
              </dd>
            </div>
            <div>
              <dt>{{ t("违规撤回", "Violation recall") }}</dt>
              <dd>{{ t("自动违规处理", "Automatic moderation") }}</dd>
            </div>
          </dl>
          </template>
        </section>
        <footer
          v-if="tab === 'settings' || tab === 'permissions' || tab === 'business'"
          class="save-bar"
        >
          <span :class="{ warning: dirty }">{{
            dirty
              ? t("有未保存更改", "Unsaved changes")
              : t("与已保存配置一致", "Settings up to date")
          }}</span>
          <div>
            <button type="button" :disabled="busy || !dirty" @click="fillForm">
              {{ t("还原", "Reset") }}</button
            ><button type="submit" class="primary" :disabled="busy || !dirty">
              <LoaderCircle v-if="saving" :size="17" class="spin" /><Save
                v-else
                :size="17"
              />{{
                saving ? t("保存中", "Saving") : t("保存更改", "Save changes")
              }}
            </button>
          </div>
        </footer>
      </form>
      </div>
    </template>
    <div v-if="pending" class="modal-backdrop" @keydown.esc="pending = null">
      <section
        class="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="discard-title"
      >
        <div class="section-heading">
          <h2 id="discard-title">
            {{ t("放弃未保存更改？", "Discard unsaved changes?") }}
          </h2>
          <button
            type="button"
            class="icon-button"
            :title="t('关闭', 'Close')"
            :aria-label="t('关闭', 'Close')"
            @click="pending = null"
          >
            <X :size="18" />
          </button>
        </div>
        <div class="modal-actions">
          <button type="button" autofocus @click="pending = null">
            {{ t("继续编辑", "Keep editing") }}</button
          ><button
            type="button"
            class="danger"
            @click="
              () => {
                const action = pending;
                pending = null;
                action?.();
              }
            "
          >
            {{ t("放弃更改", "Discard") }}
          </button>
        </div>
      </section>
    </div>
    <div
      v-if="confirmSchedulePause"
      class="modal-backdrop"
      @keydown.esc="confirmSchedulePause = false"
    >
      <section
        class="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="pause-schedule-title"
      >
        <div class="section-heading">
          <h2 id="pause-schedule-title">
            {{ t("暂停本群定时计划？", "Pause this group schedule?") }}
          </h2>
          <button
            type="button"
            class="icon-button"
            :title="t('关闭', 'Close')"
            :aria-label="t('关闭', 'Close')"
            @click="confirmSchedulePause = false"
          >
            <X :size="18" />
          </button>
        </div>
        <p class="message warning">
          {{
            t(
              "只停止下一次及后续自动操作；不会改变群当前的禁言状态。",
              "This stops the next and later scheduled actions. It does not change the current mute state.",
            )
          }}
        </p>
        <div class="modal-actions">
          <button
            type="button"
            :disabled="scheduleSaving"
            @click="confirmSchedulePause = false"
          >
            {{ t("返回", "Back") }}</button
          ><button
            type="button"
            class="danger"
            :disabled="scheduleSaving"
            @click="pauseSchedule"
          >
            <LoaderCircle v-if="scheduleSaving" :size="16" class="spin" /><Pause
              v-else
              :size="16"
            />{{
              scheduleSaving
                ? t("暂停中", "Pausing")
                : t("确认暂停", "Confirm pause")
            }}
          </button>
        </div>
      </section>
    </div>
  </main>
</template>

<style scoped>
.lottery-remarks { white-space: pre-wrap; overflow-wrap: anywhere; }
.lottery-tier-row { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) 36px; gap: 12px; align-items: end; margin-bottom: 12px; }
.lottery-tier-row label { min-width: 0; display: grid; gap: 6px; }
.lottery-tier-row input { width: 100%; min-width: 0; box-sizing: border-box; }
.lottery-reservation-heading { margin-top: 20px; }
.lottery-reservation-search { display: grid; grid-template-columns: minmax(180px, .7fr) minmax(0, 1.3fr); gap: 12px; align-items: end; }
.lottery-reservation-search > label, .lottery-member-query label { display: grid; gap: 6px; min-width: 0; }
.lottery-member-query { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; align-items: end; min-width: 0; }
.lottery-reservation-search input, .lottery-reservation-search select { width: 100%; min-width: 0; box-sizing: border-box; }
.lottery-member-results { display: grid; gap: 0; margin: 12px 0; max-height: 280px; overflow: auto; }
.lottery-member-results label { display: flex; align-items: center; gap: 8px; padding: 10px 4px; border-bottom: 1px solid var(--border); }
.lottery-member-results span, .lottery-reserved-members span { min-width: 0; overflow-wrap: anywhere; }
.lottery-member-results small, .lottery-reserved-members small { display: block; color: var(--muted); font-size: 12px; }
.lottery-member-results button { justify-self: start; margin-top: 10px; }
.lottery-reserved-members { list-style: none; padding: 0; margin: 12px 0; }
.lottery-reserved-members li { display: grid; grid-template-columns: minmax(0, 1fr) 36px; gap: 12px; align-items: center; padding: 10px 0; border-bottom: 1px solid var(--border); }
.lottery-controls { flex-wrap: wrap; justify-content: flex-end; max-width: 100%; }
@media (max-width: 640px) {
  .lottery-reservation-search { grid-template-columns: minmax(0, 1fr); }
}
</style>
