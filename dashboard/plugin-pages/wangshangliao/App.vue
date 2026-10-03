<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import {
  CalendarClock,
  ClipboardList,
  Gift,
  LoaderCircle,
  MessageSquareText,
  Network,
  Pause,
  RefreshCw,
  Save,
  Settings2,
  ShieldCheck,
  TableProperties,
  Terminal,
  X,
} from "@lucide/vue";
import ConversationExamples from "./ConversationExamples.vue";
import ReferenceTables from "./ReferenceTables.vue";

type Policy = Record<string, any>;
type Bot = {
  id: string;
  nickname?: string;
  name?: string;
  account_id?: string;
  enabled_groups?: string[];
  reply_private?: boolean;
  reply_groups?: Record<string, boolean>;
  ai_routes?: {
    admin_provider_id?: string;
    groups?: Record<string, string>;
  };
  moderation?: Policy;
  revision: string;
  state: string;
  enable?: boolean;
};
type Audit = {
  reviews: Array<Record<string, any>>;
  sanctions: Array<Record<string, any>>;
  counts: Array<Record<string, any>>;
  rules: Array<Record<string, any>>;
};
type ActivityState = {
  lottery: {
    prize: string;
    winners: number;
    duration_minutes: number;
    capacity: number;
    invite_gate: number;
    contact: string;
  };
  invite_rate: string;
  invitation: {
    rate: string;
    enabled: boolean;
    last_scan: number;
    error: string;
    credited_count: number;
    credited_total: string;
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
  invite_rate: "0.00",
});
const activityOriginal = ref("");
const scheduleState = ref<ScheduleState>({
  schedule: null,
  executions: [],
  history: [],
});
const confirmSchedulePause = ref(false);
const pending = ref<(() => void) | null>(null);
const emptyAudit = (): Audit => ({
  reviews: [],
  sanctions: [],
  counts: [],
  rules: [],
});
const audit = ref<Audit>(emptyAudit());
const bot = computed(() => bots.value.find((item) => item.id === botId.value));
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
const busy = computed(() => loading.value || saving.value);
let auditSequence = 0;
let activitySequence = 0;
let scheduleSequence = 0;
let unsubscribe: (() => void) | undefined;

const tabs = computed(() => [
  { id: "settings", name: t("审核配置", "Moderation"), icon: Settings2 },
  { id: "permissions", name: t("群授权", "Group Grants"), icon: ShieldCheck },
  { id: "activities", name: t("活动控制", "Activities"), icon: Gift },
  { id: "schedule", name: t("定时管理", "Schedules"), icon: CalendarClock },
  { id: "audit", name: t("处罚记录", "Audit"), icon: ClipboardList },
  { id: "commands", name: t("群内命令", "Commands"), icon: Terminal },
  {
    id: "examples",
    name: t("对话示例", "Chat examples"),
    icon: MessageSquareText,
  },
  { id: "architecture", name: t("架构表", "Architecture"), icon: Network },
  { id: "matrix", name: t("权限表", "Permissions"), icon: TableProperties },
]);
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
  { command: "禁言 @成员", action: "mute" },
  { command: "解禁 @成员", action: "unmute" },
  { command: "公告 公告内容", action: "announce" },
  { command: "全员禁言", action: "mute_all" },
  { command: "解除全员禁言", action: "unmute_all" },
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
    unknown: ["未知", "Unknown"],
    rejected: ["已拒绝", "Rejected"],
    allow: ["正常", "Allowed"],
    violation: ["违规", "Violation"],
    review: ["证据不足", "Review"],
    fallback: ["规则兜底", "Rule fallback"],
    mute: ["禁言", "Mute"],
    kick: ["踢出", "Remove"],
    external_promotion: ["外部引流", "External promotion"],
    adult_promotion: ["成人推广", "Adult promotion"],
    mute_keyword: ["禁言关键词", "Mute keyword"],
    kick_keyword: ["踢出关键词", "Removal keyword"],
    none: ["无", "None"],
    warned: ["已警告", "Warned"],
    muted: ["已禁言", "Muted"],
    kicked: ["已提交踢出", "Removal submitted"],
    history: ["历史消息", "Historical"],
    needs_review: ["待核验", "Needs review"],
    announcing: ["通知发送中", "Announcing"],
    open: ["报名中", "Open"],
    blocked: ["暂停待核查", "Blocked for review"],
    drawing: ["开奖中", "Drawing"],
    finished: ["已开奖", "Finished"],
    cancelled: ["已取消", "Cancelled"],
    notice_unknown: ["开场通知结果未知", "Announcement outcome unknown"],
    result_unknown: ["开奖通知结果未知", "Result notification unknown"],
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
          reply: bot.value?.reply_groups?.[groupId.value] !== false,
          customer_provider_id:
            bot.value?.ai_routes?.groups?.[groupId.value] || "",
        }
      : {},
    reply_private: bot.value?.reply_private !== false,
    admin_provider_id: bot.value?.ai_routes?.admin_provider_id || "",
  };
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
    invite_rate: settings?.invite_rate ?? "0.00",
  };
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
    if (sequence === auditSequence) audit.value = data;
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
  activityError.value = "";
  if (!bridge || !botId.value || !groupId.value) {
    activityState.value = null;
    activityOriginal.value = "";
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
  if (!bridge || !botId.value || !groupId.value) {
    scheduleState.value = { schedule: null, executions: [], history: [] };
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
    bots.value = data.bots;
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
  if (dirty.value || activityDirty.value) pending.value = action;
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
    await bridge.apiPost(
      "settings",
      JSON.parse(
        JSON.stringify({
          bot_id: botId.value,
          revision: bot.value.revision,
          group_id: groupId.value,
          ...form.value,
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
  if (!activityState.value || !bridge || busy.value || !activityDirty.value)
    return;
  activitySaving.value = true;
  activityError.value = "";
  notice.value = "";
  try {
    const saved = await bridge.apiPost("activities", {
      bot_id: botId.value,
      group_id: groupId.value,
      revision: activityState.value.revision,
      lottery: {
        prize: activityForm.value.prize,
        winners: activityForm.value.winners,
        duration_minutes: activityForm.value.duration_minutes,
        capacity: activityForm.value.capacity,
        invite_gate: activityForm.value.invite_gate,
        contact: activityForm.value.contact,
      },
      invite_rate: activityForm.value.invite_rate,
    });
    activityState.value = saved;
    fillActivityForm();
    notice.value = t("活动参数已保存", "Activity settings saved");
  } catch (err) {
    activityError.value =
      err instanceof Error
        ? err.message
        : t("活动参数保存失败", "Activity settings could not be saved");
  } finally {
    activitySaving.value = false;
  }
}
async function pauseSchedule() {
  const schedule = scheduleState.value.schedule;
  if (!schedule || !bridge || scheduleSaving.value) return;
  scheduleSaving.value = true;
  scheduleError.value = "";
  try {
    await bridge.apiPost("schedule", {
      bot_id: botId.value,
      group_id: groupId.value,
      version: schedule.version,
      action: "pause",
    });
    confirmSchedulePause.value = false;
    await loadSchedule();
    notice.value = t(
      "后续定时操作已暂停，当前群状态未改变",
      "Future scheduled actions paused; current group state unchanged",
    );
  } catch (err) {
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
          <p>
            {{ bot?.nickname || bot?.name || "Wangshangliao"
            }}<span v-if="bot?.account_id"> · {{ bot.account_id }}</span>
          </p>
        </div>
      </div>
      <div class="header-actions">
        <span v-if="bot" class="badge" :class="bot.state"
          ><span class="status-dot" />{{ label(bot.state) }}</span
        ><button
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
    <div v-else-if="!bot" class="empty">
      <ShieldCheck :size="32" /><strong>{{
        t("暂无旺商聊实例", "No Wangshangliao instances")
      }}</strong>
    </div>
    <template v-else>
      <div class="scope-bar">
        <label
          >{{ t("机器人实例", "Bot instance")
          }}<select
            :value="botId"
            :disabled="busy"
            @change="select($event, 'bot')"
          >
            <option v-for="item in bots" :key="item.id" :value="item.id">
              {{ item.nickname || item.name || item.id }}
            </option>
          </select></label
        >
        <label
          >{{ t("当前群", "Current group")
          }}<select
            :value="groupId"
            :disabled="busy || !bot.enabled_groups?.length"
            @change="select($event, 'group')"
          >
            <option v-if="!bot.enabled_groups?.length" value="">
              {{ t("没有已启用群", "No enabled groups") }}
            </option>
            <option v-for="id in bot.enabled_groups" :key="id" :value="id">
              {{ t("群", "Group") }} {{ id }}
            </option>
          </select></label
        >
      </div>
      <div class="overview">
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
      <nav
        class="tabs"
        role="tablist"
        :aria-label="t('管理视图', 'Management views')"
      >
        <button
          v-for="item in tabs"
          :id="`tab-${item.id}`"
          :key="item.id"
          type="button"
          role="tab"
          :aria-controls="`panel-${item.id}`"
          :aria-selected="tab === item.id"
          :class="{ selected: tab === item.id }"
          @click="tab = item.id"
        >
          <component :is="item.icon" :size="17" />{{ item.name }}
        </button>
      </nav>
      <form @submit.prevent="save">
        <fieldset :disabled="busy" class="form-body">
          <section
            v-if="tab === 'settings'"
            id="panel-settings"
            role="tabpanel"
            aria-labelledby="tab-settings"
          >
            <h2>{{ t("AI 对话模型路由", "AI conversation routing") }}</h2>
            <div class="field-grid">
              <label class="wide"
                >{{ t("管理员私聊群管模型", "Admin private-management model")
                }}<select v-model="form.admin_provider_id">
                  <option value="">
                    {{
                      t("沿用当前会话模型", "Use current conversation model")
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
              <label class="wide"
                >{{
                  t("本群 @ 客服模型", "Customer-service model for this group")
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
                  "客服模型仍使用本群现有的人格与知识库；管理员群管模型只在管理员私聊生效。违规审核模型在下方单独设置。",
                  "Customer service keeps this group's persona and knowledge base. The admin model applies only in admin private chats; moderation has a separate model below.",
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
              <label for="progressive-mute">{{
                t("递进禁言＋撤回（5分钟 / 15分钟 / 1小时）", "Progressive mute + recall (5 / 15 / 60 minutes)")
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
                >{{ t("踢出关键词", "Removal keywords")
                }}<textarea
                  :value="form.policy.kick_keywords.join('\n')"
                  rows="4"
                  @input="keywords('kick_keywords', $event)"
                ></textarea>
              </label>
            </div>
            <h2>{{ t("回复设置", "Replies") }}</h2>
            <div class="switch-row">
              <label for="private-reply">{{
                t("私聊回复", "Private replies")
              }}</label
              ><input
                id="private-reply"
                v-model="form.reply_private"
                type="checkbox"
                role="switch"
                class="switch"
              />
            </div>
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
                  type="checkbox"
                  role="switch"
                  class="switch"
                />
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
        <section
          v-if="tab === 'activities'"
          id="panel-activities"
          role="tabpanel"
          aria-labelledby="tab-activities"
        >
          <div class="section-heading">
            <h2>{{ t("当前群活动", "Group activities") }}</h2>
            <button
              type="button"
              class="icon-button"
              :disabled="activityLoading || activitySaving || !groupId"
              :title="t('刷新活动状态', 'Refresh activity state')"
              :aria-label="t('刷新活动状态', 'Refresh activity state')"
              @click="loadActivities"
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
              <div>
                <span>{{ t("邀请奖励", "Invite rewards") }}</span>
                <strong
                  :class="{ enabled: activityState.invitation.enabled }"
                  >{{
                    activityState.invitation.enabled
                      ? t("已开启", "Enabled")
                      : t("未开启", "Off")
                  }}</strong
                >
              </div>
              <div>
                <span>{{ t("已记账奖励", "Credits recorded") }}</span>
                <strong
                  >{{ activityState.invitation.credited_count }} ·
                  {{ activityState.invitation.credited_total }}</strong
                >
              </div>
              <div>
                <span>{{ t("待核验邀请", "Pending attribution") }}</span>
                <strong>{{ activityState.invitation.pending_count }}</strong>
              </div>
            </div>
            <dl
              v-if="activityState.current_lottery"
              class="policy-details schedule-details"
            >
              <div>
                <dt>{{ t("本期奖品", "Current prize") }}</dt>
                <dd>{{ activityState.current_lottery.prize }}</dd>
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
                <dt>{{ t("开奖时间", "Draw time") }}</dt>
                <dd>{{ date(activityState.current_lottery.ends) }}</dd>
              </div>
              <div>
                <dt>{{ t("领奖联系人", "Claim contact") }}</dt>
                <dd>{{ activityState.current_lottery.contact }}</dd>
              </div>
              <div v-if="activityState.current_lottery.error">
                <dt>{{ t("活动异常", "Activity issue") }}</dt>
                <dd>{{ activityState.current_lottery.error }}</dd>
              </div>
            </dl>
            <p
              v-if="activityState.invitation.error"
              role="alert"
              class="message warning"
            >
              {{ t("邀请核验异常", "Invitation verification issue") }}:
              {{ activityState.invitation.error }}
            </p>
            <h2>{{ t("下一期抽奖参数", "Next lottery settings") }}</h2>
            <div class="field-grid">
              <label
                >{{ t("奖品", "Prize")
                }}<input
                  v-model="activityForm.prize"
                  type="text"
                  maxlength="128"
                  required
              /></label>
              <label
                >{{ t("领奖联系人", "Claim contact")
                }}<input
                  v-model="activityForm.contact"
                  type="text"
                  maxlength="128"
                  required
              /></label>
              <label
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
                >{{ t("参与上限（0为不限）", "Entry limit (0 = unlimited)")
                }}<input
                  v-model.number="activityForm.capacity"
                  type="number"
                  min="0"
                  max="10000"
                  step="1"
                  required
              /></label>
              <label
                >{{ t("有效邀请门槛", "Verified invitation threshold")
                }}<input
                  v-model.number="activityForm.invite_gate"
                  type="number"
                  min="0"
                  max="10000"
                  step="1"
                  required
              /></label>
            </div>
            <h2>{{ t("统一邀请奖励", "Uniform invitation reward") }}</h2>
            <div class="field-grid">
              <label
                >{{
                  t("每位有效邀请奖励（积分）", "Per verified invite (points)")
                }}<input
                  v-model="activityForm.invite_rate"
                  type="number"
                  min="0"
                  max="999999.99"
                  step="0.01"
                  required
              /></label>
              <p class="field-note">
                {{
                  t(
                    "修改金额不会重算已记账奖励。启用时建立成员基线，需由管理员私聊发送“开启邀请奖励”。",
                    "Changing the rate does not reprice credits already recorded. Enabling creates a member baseline and remains an admin private-chat action.",
                  )
                }}
              </p>
            </div>
            <p class="message">
              {{
                t(
                  "保存只修改下一期抽奖和统一邀请奖励金额，不会自动开启活动或付款。开启/开奖/取消请在已授权管理员私聊中操作；抽奖期间本期参数冻结。",
                  "Saving changes only the next draw and the uniform reward rate. It does not start activities or pay out. Start/draw/cancel in an authorized admin DM; settings are frozen during a draw.",
                )
              }}
            </p>
            <div class="save-bar">
              <span :class="{ warning: activityDirty }">{{
                activityDirty
                  ? t("活动参数有未保存更改", "Unsaved activity changes")
                  : t("活动参数已同步", "Activity settings are current")
              }}</span>
              <div>
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
                      : t("保存活动参数", "Save activity settings")
                  }}
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
                  "暂停只阻止后续计划，不会解除当前禁言。恢复或改时段仍需管理员私聊确认。",
                  "Pausing only stops future scheduled actions; it does not unmute the group. Resume or change the time in admin private chat with confirmation.",
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
          <h2>{{ t("管理员私聊操作", "Admin private-chat controls") }}</h2>
          <dl class="policy-details">
            <div>
              <dt>{{ t("新建或改时段", "Create or change") }}</dt>
              <dd><code>定时禁言 23:00 08:00</code></dd>
            </div>
            <div>
              <dt>{{ t("恢复已暂停计划", "Resume paused schedule") }}</dt>
              <dd><code>恢复定时</code></dd>
            </div>
            <div>
              <dt>{{ t("确认预览", "Confirm preview") }}</dt>
              <dd><code>确认定时 &lt;确认码&gt;</code></dd>
            </div>
          </dl>
          <p class="field-note">
            {{
              t(
                "需当前会话授权管理员身份，并为本群保存“全员禁言”和“解除全员禁言”动作授权。时间使用北京时间。",
                "Requires an authorized admin conversation and both mute-all and unmute-all grants for this group. Times use China Standard Time.",
              )
            }}
          </p>
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
                  <th>{{ t("成员 ID", "Member ID") }}</th>
                  <th>{{ t("本轮受理", "Accepted in cycle") }}</th>
                  <th>{{ t("未知", "Unknown") }}</th>
                  <th>
                    {{ t("最近记录 · 北京时间", "Latest · Asia/Shanghai") }}
                  </th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in audit.counts" :key="row.member">
                  <td class="mono">{{ row.member }}</td>
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
                  <td class="mono">{{ row.member }}</td>
                  <td>{{ label(row.action) }}</td>
                  <td>{{ row.minutes ? `${row.minutes} ${t("分钟", "min")}` : "-" }}</td>
                  <td>{{ row.recall_status ? label(row.recall_status) : "-" }}</td>
                  <td>
                    <span class="badge" :class="row.status">{{
                      label(row.status)
                    }}</span>
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
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in audit.rules" :key="row.message">
                  <td>{{ date(row.observed) }}</td>
                  <td class="mono">{{ row.sender }}</td>
                  <td>{{ label(row.category) }}</td>
                  <td>{{ label(row.status) }}</td>
                </tr>
                <tr v-if="!audit.rules.length">
                  <td colspan="4" class="table-empty">
                    {{ t("暂无内容规则记录", "No content rule audit") }}
                  </td>
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
          :content-mode="Boolean(activePolicy.content_rules_since)"
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
                        : t("管理员 ID", "Administrator ID")
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
                    "管理员私聊预览与确认",
                    "Private administrator preview and confirmation",
                  )
                }}
              </dd>
            </div>
            <div>
              <dt>{{ t("违规撤回", "Violation recall") }}</dt>
              <dd>{{ t("自动违规处理", "Automatic moderation") }}</dd>
            </div>
            <div>
              <dt>{{ t("帮助", "Help") }}</dt>
              <dd><code>帮助</code> · {{ t("私聊", "Private chat") }}</dd>
            </div>
          </dl>
        </section>
        <footer
          v-if="tab === 'settings' || tab === 'permissions'"
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
