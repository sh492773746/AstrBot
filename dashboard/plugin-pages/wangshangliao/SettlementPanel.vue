<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { RefreshCw, ReceiptText, Search, Check, ChevronDown, ChevronLeft, ChevronRight, Settings, History, Undo2, UserRoundCheck, X } from "@lucide/vue";

const props = defineProps<{ bridge: { apiPost: Function } }>();
const emit = defineEmits<{ dirty: [value: boolean] }>();
type User = { uid: string; name: string; role?: string; active?: number; version?: number };
type Group = { id: string; name: string; bot_id: string; contact: string; rate: number | null; agent_rate: number | null; configured: boolean; enabled: number; version: number; error: string; scanned: number; require_speech?: number; agent_only?: number; source_error?: string; source_scanned?: number };
type Entry = { id: string; member: string; member_name: string; inviter: string; inviter_name: string; amount: number; version: number; state: string; verification: string; present: number; ever_left: number; first_seen: number; first_spoke_at: number; can_correct: boolean; can_assign: boolean; qualification_started: number; qualification_reason: string; attribution_source: string; inviter_class: string; inviter_name_snapshot: string; name_digit_count: number; member_class: string; member_avatar_status: string; member_avatar_digit_count: number; pricing_status: string; hold_until: number; risk_blocked?: boolean; self_join?: number; review_reason?: string; membership_left_at?: number; payment_kind?: "pay" | "exception" | "" };
type PriceConfirmation = { group: string; category: "ordinary" | "agent"; rule_version: number; items: { id: string; version: number; original_amount: number; target_amount: number; original_category: "ordinary" | "agent"; price_rule_version: number }[]; digest: string };
type SectionSummary = { inviter: string; inviter_name: string; count: number; amount: number; historical: boolean; risk_blocked?: boolean; page: number; page_size: number; page_count: number };
type RiskCase = { id: string; inviter: string; inviter_name: string; detected: number; member_count: number; matching_count: number; name_length: number; status: string; notice_status: string; blocked_at: number | null; resolution: string };
const me = ref<User | null>(null), groups = ref<Group[]>([]);
const catalog = ref<{id: string; name: string; bot_id: string}[]>([]);
const tab = ref("ledger"), group = ref(""), state = ref("pending"), period = ref("all"), search = ref(""), page = ref(1);
const customStart = ref(""), customEnd = ref("");
const rows = ref<Entry[]>([]), total = ref(0), amount = ref(0), payable = ref(0);
const sectionSummaries = ref<SectionSummary[]>([]), pageCount = ref(1), appliedSearch = ref("");
const busy = ref(false), saving = ref(false), error = ref(""), notice = ref(""), fatal = ref(false);
const readOnly = ref(true);
const scopeAvailable = ref(false);
const selected = ref<string[]>([]), audits = ref<any[]>([]);
const riskCases = ref<RiskCase[]>([]);
const riskReleaseDialog = ref(false), riskReleaseTarget = ref<RiskCase | null>(null), riskReleaseReason = ref("");
const riskReleaseGroup = ref(""), riskGroup = ref("");
let riskSequence = 0;
const clock = ref(Date.now() / 1000);
const stateNames: Record<string, string> = { verifying: "核验中", pending: "待结账", paid: "已结账", void: "已作废", unassigned: "未归属", no_reward: "普通成员不返现" };
const logLevel = ref(""), logTrace = ref("");
const groupMenuOpen = ref(false);
const groupStorageKey = "wangshangliao-cashier-group";
const current = computed(() => groups.value.find(g => g.id === group.value));
const admin = computed(() => me.value?.role === "admin");
const sections = computed(() => {
  const map = new Map<string, Entry[]>();
  for (const row of rows.value) {
    if (!map.has(row.inviter)) map.set(row.inviter, []);
    map.get(row.inviter)!.push(row);
  }
  return sectionSummaries.value.map(summary => ({
    ...summary, key: summary.inviter, id: summary.inviter,
    name: summary.inviter ? summary.inviter_name || "名称待核验" : state.value === "unassigned" ? "未归属" : state.value === "paid" ? "已结账 · 归属待核对" : "邀请来源待核对",
    items: map.get(summary.inviter) || [],
  }));
});
const selectedRows = computed(() => rows.value.filter(r => selected.value.includes(r.id)));
const selectedAmount = computed(() => selectedRows.value.reduce((n, r) => n + r.amount, 0));
const dialog = ref(false), operation = ref("pay"), reason = ref(""), requestId = ref(""), priceCategory = ref<"ordinary" | "agent">("agent");
const priceConfirmation = ref<PriceConfirmation | null>(null), actualPaymentConfirmed = ref(false);
const inviterOptions = ref<User[]>([]), assignedInviter = ref<string | null>(null);
const selectionSnapshot = ref<Entry[]>([]);
const selectionGroup = ref<Group | null>(null);
const rule = ref({ id: "", bot_id: "", contact: "", money: "", agentMoney: "", qualification_seconds: 10800, require_speech: false, agent_only: false, enabled: false, version: 0 });
const savedRule = ref("");
const ruleDirty = computed(() => !!savedRule.value && JSON.stringify(rule.value) !== savedRule.value);
watch(ruleDirty, value => emit("dirty", value));
const money = (cents: number) => (cents / 100).toFixed(2);
const dateFormat = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
const deadlineFormat = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
const time = (stamp: number) => stamp ? dateFormat.format(new Date(stamp * 1000)) : "尚未核验";
function beijingTimestamp(value: string) {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value);
  if (!match) return null;
  const [year, month, day, hour, minute] = match.slice(1).map(Number);
  if (month < 1 || month > 12 || day < 1 || day > 31 || hour > 23 || minute > 59) return null;
  const utc = Date.UTC(year, month - 1, day, hour, minute);
  const check = new Date(utc);
  if (check.getUTCFullYear() !== year || check.getUTCMonth() !== month - 1 || check.getUTCDate() !== day || check.getUTCHours() !== hour || check.getUTCMinutes() !== minute) return null;
  return utc / 1000 - 8 * 60 * 60;
}
const customRange = computed(() => {
  if (period.value !== "custom") return { start: null as number | null, end: null as number | null, error: "" };
  if (!customStart.value || !customEnd.value) return { start: null, end: null, error: "请选择开始和结束时间。" };
  const start = beijingTimestamp(customStart.value), end = beijingTimestamp(customEnd.value);
  if (start === null || end === null) return { start, end, error: "时间格式无效，请重新选择或输入。" };
  if (end <= start) return { start, end, error: "结束时间必须晚于开始时间。" };
  return { start, end, error: "" };
});
const customRangeError = computed(() => customRange.value.error);
const entryFilters = computed(() => {
  const params = new URLSearchParams({ group: group.value, state: state.value, period: period.value, search: appliedSearch.value, pagination: "inviters" });
  if (period.value === "custom" && !customRangeError.value) {
    params.set("start_at", String(customRange.value.start));
    params.set("end_at", String(customRange.value.end));
  }
  return params;
});
const verification = (r: Entry) => r.risk_blocked ? "风控暂停结账，等待复核" : r.self_join ? r.verification === "conflict" ? "主动入群归属待复核" : "主动入群，不参与返现" : r.verification === "conflict" ? r.state === "paid" ? "原收款归属待复核" : "归属冲突" : r.verification === "blocked" ? "不符合资格" : r.verification !== "verified" ? "归属待核对" : r.ever_left ? "曾退群" : !r.present ? "已退群" : r.pricing_status === "no_reward" ? "普通成员不返现，代理商待核验" : r.member_class === "unclassified" || r.member_class === "legacy" ? "价格待确认" : r.pricing_status === "unconfigured" ? "价格待确认" : r.state !== "paid" && current.value?.require_speech && !r.first_spoke_at ? r.hold_until <= clock.value ? "三小时已满，待发言" : "未发言，核验中" : r.state === "verifying" ? "在群时间核验中" : "已核验";
const canPay = (r: Entry) => !r.review_reason && !r.membership_left_at && !r.risk_blocked && !r.self_join && r.state === "pending" && r.verification === "verified" && !!r.present && !r.ever_left && ["ordinary", "agent"].includes(r.member_class) && r.pricing_status === "priced" && r.hold_until <= clock.value && (!current.value?.require_speech || r.first_spoke_at > 0);
const canReprice = (r: Entry) => !r.review_reason && !r.membership_left_at && !r.self_join && !readOnly.value && ["verifying", "pending"].includes(r.state) && r.verification === "verified" && !!r.inviter && !!r.present && !r.ever_left && ["ordinary", "agent"].includes(r.member_class);
const canException = (r: Entry) => !r.review_reason && !r.risk_blocked && !r.self_join && admin.value && r.state !== "paid" && r.verification === "verified" && !!r.inviter && ["ordinary", "agent"].includes(r.member_class) && r.pricing_status === "priced";
const remaining = (r: Entry) => {
  const seconds = Math.max(0, Math.ceil(r.hold_until - clock.value));
  return seconds ? `剩余 ${Math.floor(seconds / 3600)}时${Math.floor(seconds % 3600 / 60)}分${seconds % 60}秒` : current.value?.require_speech && !r.first_spoke_at ? "三小时已满，等待发言" : "等待名单复核";
};
const canCorrect = (r: Entry) => !r.self_join && r.state === "paid" && r.can_correct;
const canAssign = (r: Entry) => !r.self_join && !r.inviter && r.can_assign;
const canSelect = (r: Entry) => !readOnly.value && state.value !== "unassigned" && (state.value === "paid" ? canCorrect(r) : canPay(r) || canException(r));

async function api(path: string, body?: unknown) {
  const url = new URL(path, "https://cashier.invalid/");
  try {
    return await props.bridge.apiPost("cashier", {
      action: url.pathname.slice(1),
      payload: body === undefined ? Object.fromEntries(url.searchParams) : body,
    });
  } catch (e: any) {
    throw Object.assign(new Error(e?.message || "结账请求未确认，请刷新核对。"), {
      status: e?.status, code: e?.code,
    });
  }
}
async function guard(fn: () => Promise<void>) {
  error.value = ""; activeLoads++; busy.value = true;
  try { await fn(); } catch (e: any) { error.value = e.message; } finally { busy.value = --activeLoads > 0; }
}
let activeLoads = 0;
let loadVersion = 0;
let loadedFilterKey = "";
let rememberedGroup = "";
async function loadEntries() {
  const version = ++loadVersion;
  selected.value = [];
  if (!group.value) { rows.value = []; sectionSummaries.value = []; total.value = amount.value = payable.value = 0; pageCount.value = 1; return; }
  if (period.value === "custom" && customRangeError.value) {
    rows.value = []; sectionSummaries.value = []; total.value = amount.value = payable.value = 0; pageCount.value = 1;
    return;
  }
  const params = new URLSearchParams(entryFilters.value);
  params.set("page", String(page.value));
  const filterKey = params.toString();
  const rememberedPages = loadedFilterKey === filterKey ? sectionSummaries.value.filter(section => section.page > 1) : [];
  try {
    const data = await api(`entries?${params}`);
    if (version !== loadVersion) return;
    if (data.pagination !== "inviters" || !Array.isArray(data.sections)) throw new Error("账本页面已更新，请刷新后重试。");
    for (const previous of rememberedPages) {
      if (!data.sections.some((section: SectionSummary) => section.inviter === previous.inviter)) continue;
      const memberParams = new URLSearchParams(params);
      memberParams.set("inviter", previous.inviter);
      memberParams.set("member_page", String(previous.page));
      const members = await api(`entries?${memberParams}`);
      if (version !== loadVersion) return;
      const summary = members.sections.find((section: SectionSummary) => section.inviter === previous.inviter);
      if (!summary) continue;
      data.items = [...data.items.filter((row: Entry) => row.inviter !== previous.inviter), ...members.items];
      data.sections = data.sections.map((section: SectionSummary) => section.inviter === previous.inviter ? summary : section);
    }
    if (version !== loadVersion) return;
    rows.value = data.items; sectionSummaries.value = data.sections;
    total.value = data.total; amount.value = data.amount; payable.value = data.payable_amount;
    pageCount.value = data.page_count; loadedFilterKey = filterKey;
    if (page.value !== data.page) page.value = data.page;
  } catch (e) {
    if (version !== loadVersion) return;
    rows.value = []; sectionSummaries.value = []; total.value = amount.value = payable.value = 0; pageCount.value = 1;
    throw e;
  }
}
async function loadSectionEntries(section: SectionSummary, targetPage: number) {
  const version = loadVersion;
  selected.value = [];
  const params = new URLSearchParams(entryFilters.value);
  params.set("page", String(page.value));
  params.set("inviter", section.inviter);
  params.set("member_page", String(targetPage));
  await guard(async () => {
    const data = await api(`entries?${params}`);
    if (version !== loadVersion) return;
    const summary = data.sections.find((item: SectionSummary) => item.inviter === section.inviter);
    if (!summary) { await loadEntries(); return; }
    rows.value = [...rows.value.filter(row => row.inviter !== section.inviter), ...data.items];
    sectionSummaries.value = sectionSummaries.value.map(item => item.inviter === section.inviter ? summary : item);
    total.value = data.total; amount.value = data.amount; payable.value = data.payable_amount;
  });
}
function applySearch() {
  appliedSearch.value = search.value || "";
  page.value = 1;
  void guard(loadEntries);
}
async function applyCustomRange() {
  if (customRangeError.value) return;
  page.value = 1;
  await guard(loadEntries);
}
async function refresh() {
  await guard(async () => {
    try {
      groups.value = await api("groups");
      scopeAvailable.value = true;
    } catch (e) {
      scopeAvailable.value = false;
      readOnly.value = true;
      selected.value = []; dialog.value = false;
      throw e;
    }
    await loadStatus();
    if (!group.value && groups.value.some(g => g.id === rememberedGroup)) group.value = rememberedGroup;
    rememberedGroup = "";
    if (!groups.value.some(g => g.id === group.value)) group.value = groups.value[0]?.id || "";
    if (admin.value) {
      catalog.value = await api("admin/catalog");
    }
    await loadEntries();
    if (tab.value === "audit") await loadAudit();
    if (tab.value === "rules") {
      editGroup(rule.value.id || group.value);
    }
  });
}
async function loadStatus() {
  try {
    readOnly.value = (await api("status")).read_only !== false || !scopeAvailable.value;
  } catch {
    readOnly.value = true;
  }
  if (readOnly.value && !saving.value) {
    selected.value = [];
    dialog.value = false;
  }
}
async function loadAudit(before = 0) {
  if (!group.value) { audits.value = []; return; }
  const targetGroup = group.value;
  const data = await api(`audit?${new URLSearchParams({ group: group.value, before: String(before), level: logLevel.value, trace: logTrace.value.trim() })}`);
  if (targetGroup !== group.value) return;
  audits.value = before ? [...audits.value, ...data] : data;
}
function toggle(row: Entry) {
  if (selected.value.includes(row.id)) selected.value = selected.value.filter(id => id !== row.id);
  else {
    if (selectedRows.value.some(r => r.inviter !== row.inviter)) selected.value = [];
    selected.value.push(row.id);
  }
}
function selectSection(items: Entry[]) {
  if (readOnly.value) return;
  const allowed = items.filter(r => canPay(r));
  selected.value = allowed.every(r => selected.value.includes(r.id)) ? [] : allowed.map(r => r.id);
}
async function confirm(op: string, row?: Entry) {
  if (readOnly.value || saving.value || busy.value || !current.value) return;
  const items = row ? [{ ...row }] : selectedRows.value.map(r => ({ ...r }));
  if (!items.length || (op === "correct" && !items.every(canCorrect))) return;
  error.value = "";
  operation.value = op; reason.value = ""; requestId.value = crypto.randomUUID();
  priceConfirmation.value = null; actualPaymentConfirmed.value = false;
  if (op === "reprice" && row) priceCategory.value = row.member_class === "agent" ? "ordinary" : "agent";
  selectionSnapshot.value = items;
  selectionGroup.value = { ...current.value };
  if (op === "reprice") {
    const targetGroup = group.value;
    await guard(async () => {
      const preview: PriceConfirmation = await api("price-preview", {
        group: targetGroup, category: priceCategory.value,
        items: items.map(item => ({ id: item.id, version: item.version })),
      });
      if (targetGroup !== group.value || readOnly.value) return;
      priceConfirmation.value = preview;
      dialog.value = true;
    });
    return;
  }
  dialog.value = true;
}
async function confirmAssignment(row: Entry) {
  if (!canAssign(row) || readOnly.value || saving.value || busy.value || !current.value) return;
  const targetGroup = group.value;
  await guard(async () => {
    const options = await api(`inviters?${new URLSearchParams({ group: targetGroup })}`);
    if (group.value !== targetGroup || !rows.value.some(r => r.id === row.id && r.version === row.version)) return;
    inviterOptions.value = options;
    if (!options.length) throw new Error("暂无可选的已确认邀请人，请先核对邀请来源。");
    assignedInviter.value = null;
    operation.value = "assign"; reason.value = ""; requestId.value = crypto.randomUUID();
    selectionSnapshot.value = [{ ...row }]; selectionGroup.value = { ...current.value! };
    dialog.value = true;
  });
}
async function submit() {
  if (saving.value || readOnly.value || !selectionGroup.value) return;
  if (operation.value === "exception" && !actualPaymentConfirmed.value) return;
  if (operation.value === "reprice" && !priceConfirmation.value) return;
  error.value = ""; saving.value = true;
  try {
    const result = await api("payments", { group: selectionGroup.value.id, operation: operation.value, reason: reason.value,
      category: operation.value === "reprice" ? priceCategory.value : undefined,
      inviter: operation.value === "assign" ? assignedInviter.value : undefined,
      price_confirmation: operation.value === "reprice" ? priceConfirmation.value : undefined,
      actual_payment_confirmed: operation.value === "exception" ? actualPaymentConfirmed.value : undefined,
      request_id: requestId.value, items: selectionSnapshot.value.map(r => ({ id: r.id, version: r.version })) });
    dialog.value = false;
    if (operation.value === "assign") {
      notice.value = result.state === "paid" ? "归属已补充，原已结账登记和金额保持不变。" : result.state === "no_reward" ? "归属已确认，该普通成员不参与返现。" : result.state === "pending" ? "归属已确认，记录已转入待结账。" : "归属已确认，记录仍需完成连续在群核验。";
      selected.value = []; group.value = selectionGroup.value.id; state.value = result.state; page.value = 1;
    } else if (operation.value === "correct") {
      notice.value = `已撤回 ${result.count} 笔结账登记，共 ¥${money(result.amount)}。符合核验条件的记录转回待结账，未完成核验的继续核验，退群记录已作废。`;
      selected.value = []; group.value = selectionGroup.value.id; state.value = "pending"; page.value = 1;
    } else if (operation.value === "reprice") {
      notice.value = `已切换 ${result.count} 笔成员价格，共 ¥${money(result.amount)}。`;
      if (state.value === "no_reward" && priceCategory.value === "agent") state.value = "verifying";
    } else if (operation.value === "exception") {
      notice.value = `已补记 ${result.count} 笔已发生的外部付款，共 ¥${money(result.amount)}。`;
    } else {
      notice.value = `已登记 ${result.count} 笔，共 ¥${money(result.amount)}`;
    }
    await refresh();
  } catch (e: any) {
    error.value = e.message;
    if ([403, 409].includes(e.status)) { dialog.value = false; await loadEntries(); }
  } finally { saving.value = false; }
}
function editGroup(id: string) {
  if (id !== rule.value.id && ruleDirty.value && !window.confirm("返现设置尚未保存，放弃修改并切换群？")) return;
  riskSequence++;
  riskCases.value = [];
  riskGroup.value = "";
  riskReleaseTarget.value = null;
  riskReleaseDialog.value = false;
  riskReleaseGroup.value = "";
  const saved = groups.value.find(g => g.id === id);
  const option = catalog.value.find(g => g.id === id);
  rule.value = { id, bot_id: saved?.bot_id || option?.bot_id || "", contact: saved?.contact || "", money: saved?.rate != null ? money(saved.rate) : "", agentMoney: saved?.agent_rate != null ? money(saved.agent_rate) : "", qualification_seconds: 10800, require_speech: !!saved?.require_speech, agent_only: !!saved?.agent_only, enabled: !!saved?.enabled, version: saved?.version || 0 };
  savedRule.value = JSON.stringify(rule.value);
  void loadRiskCases(id).catch((e: any) => { error.value = e.message; });
}
async function loadRiskCases(id: string) {
  const sequence = ++riskSequence;
  riskCases.value = [];
  riskGroup.value = "";
  if (!admin.value || !id) { riskCases.value = []; return; }
  const result = await api(`admin/risk?${new URLSearchParams({ group: id })}`);
  if (sequence === riskSequence && rule.value.id === id) {
    riskCases.value = result;
    riskGroup.value = id;
  }
}
function openRiskRelease(item: RiskCase) {
  if (busy.value || readOnly.value || riskGroup.value !== rule.value.id
      || !riskCases.value.some(row => row.id === item.id && row.inviter === item.inviter)) return;
  riskReleaseTarget.value = item;
  riskReleaseGroup.value = riskGroup.value;
  riskReleaseReason.value = "";
  riskReleaseDialog.value = true;
}
async function releaseRisk() {
  const target = riskReleaseTarget.value;
  const targetGroup = riskReleaseGroup.value;
  if (!target || !targetGroup || targetGroup !== rule.value.id
      || targetGroup !== riskGroup.value || riskReleaseReason.value.trim().length < 2 || readOnly.value) return;
  await guard(async () => {
    await api("admin/risk/release", {
      group: targetGroup,
      inviter: target.inviter,
      reason: riskReleaseReason.value.trim(),
    });
    riskReleaseDialog.value = false;
    notice.value = `已解除 ${target.inviter_name} 的本群返现冻结，原账本金额和记录未改写。`;
    await loadRiskCases(rule.value.id);
    await loadEntries();
  });
}
async function saveRule() {
  if (busy.value || readOnly.value) return;
  await guard(async () => {
    if (!/^(?:0|[1-9]\d{0,5})(?:\.\d{1,2})?$/.test(rule.value.money)) throw new Error("请输入最多两位小数的每人金额。");
    const [yuan, fraction = ""] = rule.value.money.split(".");
    const rate = Number(yuan) * 100 + Number(fraction.padEnd(2, "0"));
    let agentRate: number | null = null;
    if (rule.value.agentMoney) {
      if (!/^(?:0|[1-9]\d{0,5})(?:\.\d{1,2})?$/.test(rule.value.agentMoney)) throw new Error("代理商金额最多两位小数。");
      const [agentYuan, agentFraction = ""] = rule.value.agentMoney.split(".");
      agentRate = Number(agentYuan) * 100 + Number(agentFraction.padEnd(2, "0"));
      if (agentRate < 1) throw new Error("代理商金额不能小于0.01元。");
    }
    const { money: _money, agentMoney: _agentMoney, ...body } = rule.value;
    try {
      await api("admin/groups", { ...body, rate, agent_rate: agentRate });
    } catch (e: any) {
      if (e.code === "conflict") {
        groups.value = await api("groups");
        editGroup(body.id);
        throw new Error(`群设置已被更新，已载入最新设置，请核对后重新保存。${e.message}`);
      }
      throw e;
    }
    notice.value = "群设置已保存"; group.value = body.id; await refresh(); editGroup(body.id);
  });
}
watch(group, value => {
  try {
    if (value) sessionStorage.setItem(groupStorageKey, value);
    else sessionStorage.removeItem(groupStorageKey);
  } catch {}
});
watch([group, state, period], () => { rows.value = []; sectionSummaries.value = []; page.value = 1; guard(loadEntries); if (tab.value === "audit") guard(() => loadAudit()); });
watch(page, () => guard(loadEntries));
watch(tab, value => {
  if (value === "rules") void refresh();
  if (value === "audit" && (group.value || admin.value)) guard(() => loadAudit());
});
onMounted(async () => {
  try {
    me.value = await api("me");
    try { rememberedGroup = sessionStorage.getItem(groupStorageKey) || ""; } catch {}
    await refresh();
  } catch (e: any) { error.value = e.message; fatal.value = true; }
});
const actionNames: Record<string, string> = { pay: "登记已结账", correct: "撤回结账登记", exception: "异常付款登记", rules_changed: "修改群设置", cashier_changed: "修改结账员", member_left: "成员退群", repeated_join: "重复入群", invitation_verified: "邀请归属核验", attribution_conflict: "归属冲突", identity_conflict: "身份冲突", invitation_scan: "邀请采集核验", invitation_source_failed: "邀请来源读取失败", cashback_risk_warning_detected: "返现风控预警", cashback_risk_notice: "发送返现风控通知", cashback_risk_blocked: "返现风控冻结", cashback_risk_released: "管理员解除返现冻结", "/api/payments": "结账请求", "/api/admin/groups": "群设置请求", "/api/admin/risk": "读取返现风控记录", "/api/admin/risk/release": "复核并解除返现冻结", "/api/admin/users": "结账员管理", "/api/admin/members": "读取群成员", "/api/audit": "查询操作日志", "/api/entries": "查询账本", "/api/groups": "查询群列表", "/api/me": "核验登录身份" };
actionNames.contact_changed = "修改群内联系人";
actionNames.amount_corrected = "更正单条返现金额";
actionNames.access_cashier_admitted = "全群结账员首次登录";
actionNames.qualification_rules_activated = "启用新邀请三小时核验";
actionNames.qualification_restarted = "中断监测后重新核验";
actionNames.qualification_completed = "三小时核验通过";
actionNames.member_speech_verified = "确认成员入群后发言";
actionNames.agent_price_confirmed = "确认代理商单价";
actionNames.price_class_changed = "人工切换成员价格";
actionNames.automatic_avatar_pricing_removed = "移除自动头像定价";
actionNames.default_price_applied = "按普通成员默认定价";
actionNames.member_avatar_classified = "按被邀请者头像分类";
actionNames.member_avatar_classification_recovered = "补核验被邀请者头像分类";
actionNames.join_operator_attribution_removed = "撤销入群操作人的错误归属";
actionNames.paid_attribution_review_required = "已结账收款归属待复核";
actionNames.migration_invitation_recovered = "补回迁移期间的真实邀请";
actionNames.unverified_paid_inviter_detached = "移除历史错误邀请归属";
actionNames.paid_invitation_history_verified = "复核原已结账邀请归属";
actionNames.inviter_identity_conflict = "邀请人身份冲突";
actionNames.invitation_evidence_conflict = "邀请证据时间冲突";
actionNames.attribution_revalidation_required = "邀请归属重新核验";
actionNames.inviter_assigned = "结账员确认邀请归属";
actionNames["/api/inviters"] = "读取已有邀请人";
actionNames.self_join_confirmed = "管理员确认主动入群，无邀请返现";
actionNames.self_join_attribution_conflict = "主动入群与邀请证据冲突";
actionNames.invitation_scheduler = "邀请采集调度";
actionNames.invitation_source_recovered = "邀请来源已恢复";
actionNames.invitation_source_deferred = "邀请来源查询等待";
actionNames.invitation_source_row_quarantined = "邀请来源单行隔离";
actionNames["/api/admin/users"] = "群内联系人管理";
let lastNetworkReport = 0;
function reportClientError(kind: string) {
  if (kind === "network_error") {
    const now = Date.now();
    if (now - lastNetworkReport < 60000) return;
    lastNetworkReport = now;
  }
  if (me.value) void props.bridge.apiPost("cashier", {action:"client-error",payload:{kind}}).catch(() => {});
}
const onScriptError = () => reportClientError("script_error");
const onRejection = () => reportClientError("unhandled_rejection");
window.addEventListener("error", onScriptError);
window.addEventListener("unhandledrejection", onRejection);
const refreshTimer = window.setInterval(() => {
  if (me.value && !fatal.value && tab.value === "ledger" && !document.hidden
      && !busy.value && !saving.value && !dialog.value && !groupMenuOpen.value && !selected.value.length) refresh();
}, 30000);
const statusTimer = window.setInterval(() => {
  if (me.value && !fatal.value && !document.hidden) void loadStatus();
}, 10000);
const clockTimer = window.setInterval(() => { clock.value = Date.now() / 1000; }, 1000);
onUnmounted(() => {
  emit("dirty", false);
  window.clearInterval(refreshTimer);
  window.clearInterval(statusTimer);
  window.clearInterval(clockTimer);
  window.removeEventListener("error", onScriptError);
  window.removeEventListener("unhandledrejection", onRejection);
});
</script>

<template>
  <v-app class="cashier-shell embedded">
    <header class="topbar">
      <div class="brand"><ReceiptText :size="25" /><div><h1>邀请返现</h1><span>结账台</span></div></div>
      <div class="identity" v-if="me"><strong>{{ me.name }}</strong><span>{{ admin ? "管理员" : "全群结账员" }}</span></div>
      <v-btn variant="text" icon title="刷新" aria-label="刷新" :loading="busy" @click="refresh"><RefreshCw :size="20" /></v-btn>
    </header>
    <main>
      <v-alert v-if="error" type="error" variant="tonal" class="message" role="alert">{{ error }}</v-alert>
      <v-alert v-if="me && readOnly" type="warning" variant="tonal" class="message" role="status">当前只读：群管服务暂不可用，付款登记、撤回结账与规则保存已暂停。</v-alert>
      <v-alert v-if="notice" type="success" variant="tonal" class="message" role="status" closable @click:close="notice = ''">{{ notice }}</v-alert>
      <template v-if="!fatal && me">
        <nav class="tabs" aria-label="页面">
          <v-btn :variant="tab === 'ledger' ? 'tonal' : 'text'" @click="tab = 'ledger'"><ReceiptText :size="17" />账本</v-btn>
          <v-btn :variant="tab === 'audit' ? 'tonal' : 'text'" @click="tab = 'audit'"><History :size="17" />操作日志</v-btn>
          <v-btn v-if="admin" :variant="tab === 'rules' ? 'tonal' : 'text'" @click="tab = 'rules'"><Settings :size="17" />群设置</v-btn>
        </nav>
        <template v-if="tab === 'ledger' || tab === 'audit'">
          <div class="filters" :class="{ 'ledger-filters': tab === 'ledger' }">
            <v-select v-model="group" v-model:menu="groupMenuOpen" :items="groups" item-title="name" item-value="id" label="群聊" :disabled="busy || saving || dialog" />
            <v-select v-if="tab === 'ledger'" v-model="state" label="结账状态" :items="[{title:'未归属',value:'unassigned'},{title:'核验中',value:'verifying'},{title:'待结账',value:'pending'},{title:'普通成员不返现',value:'no_reward'},{title:'已结账',value:'paid'},{title:'已作废',value:'void'}]" />
            <v-select v-if="tab === 'ledger'" v-model="period" label="时间范围" :items="[{title:'全部时间',value:'all'},{title:'本月',value:'month'},{title:'今日',value:'day'},{title:'本小时',value:'hour'},{title:'自定义',value:'custom'}]" />
            <v-text-field v-if="tab === 'ledger'" v-model="search" label="邀请人或被邀请人名称" clearable @click:clear="search = ''; applySearch()" @keydown.enter="applySearch">
              <template #append-inner><button aria-label="搜索" title="搜索" @click="applySearch"><Search :size="19" /></button></template>
            </v-text-field>
          </div>
          <div v-if="tab === 'ledger' && period === 'custom'" class="custom-range">
            <v-text-field v-model="customStart" type="datetime-local" step="60" label="开始时间（北京时间）" :disabled="busy" @keydown.enter="applyCustomRange" />
            <v-text-field v-model="customEnd" type="datetime-local" step="60" label="结束时间（北京时间）" :disabled="busy" @keydown.enter="applyCustomRange" />
            <v-btn variant="tonal" :loading="busy" :disabled="busy || !!customRangeError" @click="applyCustomRange"><Search :size="17" />查询</v-btn>
            <p v-if="customRangeError" class="custom-range-error" role="alert">{{ customRangeError }}</p>
          </div>
          <div v-if="tab === 'audit'" class="filters">
            <v-select v-model="logLevel" label="日志级别" :items="[{title:'全部级别',value:''},{title:'正常操作',value:'info'},{title:'警告 / 拒绝',value:'warning'},{title:'错误',value:'error'}]" @update:model-value="guard(() => loadAudit())" />
            <v-text-field v-model="logTrace" label="请求编号" @keydown.enter="guard(() => loadAudit())" />
            <v-btn variant="tonal" :loading="busy" @click="guard(() => loadAudit())"><Search :size="17" />查询日志</v-btn>
          </div>
          <p v-if="!groups.length" class="empty">{{ busy ? "读取群配置中" : error ? "群配置暂不可用" : "暂无已配置返现群" }}</p>
        </template>
        <section v-if="tab === 'ledger' && group" aria-label="邀请账本">
          <div class="summary">
            <div><span>{{ stateNames[state] }}金额</span><strong>¥{{ money(amount) }}</strong></div>
            <div><span>邀请记录</span><strong>{{ total }}<small> 笔</small></strong></div>
            <div v-if="state === 'pending'"><span>符合付款条件</span><strong>¥{{ money(payable) }}</strong></div>
          </div>
          <div class="ledger-heading"><h2>{{ current?.name }}</h2><span>{{ current?.enabled ? '返现进行中' : '返现未开启' }} · {{ current?.contact }}</span></div>
          <v-alert v-if="current?.error" type="warning" variant="tonal">采集核验暂不可用。普通付款登记已暂停，历史记录不受影响。</v-alert>
          <p class="muted">名单核验：{{ time(current?.scanned || 0) }}（北京时间）</p>
          <p v-if="current?.source_error" class="warning" role="status">邀请来源暂不可用，新邀请入账可能延迟 · {{ current.source_error }}</p>
          <div v-if="!rows.length" class="empty"><ReceiptText :size="36" /><h3>暂无{{ stateNames[state] }}记录</h3></div>
          <details v-for="section in sections" :key="section.key" class="inviter" open>
            <summary><ChevronDown :size="17" /><div><small>{{ !section.id ? '归属状态' : section.historical ? '历史登记收款人' : '邀请人' }}</small><strong>{{ section.name }}</strong><small>{{ section.id ? `账号 ${section.id}` : state === 'paid' ? '原结账登记保留' : '来源不明' }}</small><small v-if="section.risk_blocked" class="warning">风控冻结 · 待管理员复核</small></div><div class="section-totals"><span class="section-count">{{ section.count }} 人</span><b>¥{{ money(section.amount) }}</b></div></summary>
            <v-alert v-if="section.risk_blocked" type="warning" variant="tonal">该邀请人的未结返现已冻结，不会登记结账；已结账历史保持不变。</v-alert>
            <div v-if="state === 'pending' && section.items.some(canPay)" class="section-action"><v-btn size="small" variant="text" :disabled="readOnly" @click="selectSection(section.items)"><Check :size="16" />选择本页可结账记录</v-btn></div>
            <div v-for="row in section.items" :key="row.id" class="entry">
              <input type="checkbox" :aria-label="`选择${row.member_name}`" :checked="selected.includes(row.id)" :disabled="!canSelect(row) || saving" @change="toggle(row)" />
              <div class="member">
                <small>{{ row.self_join ? '成员' : '被邀请人' }}</small><strong>{{ row.member_name }}</strong><small>账号 {{ row.member }}</small>
                <small class="entry-inviter">{{ row.self_join ? '入群方式：主动入群，无邀请人' : `${row.state === 'paid' && row.verification === 'conflict' ? '原登记收款人' : '邀请人'}：${row.inviter ? row.inviter_name || '名称待核验' : '来源待核对'}` }}<span v-if="row.inviter">（账号 {{ row.inviter }}）</span></small>
                <small v-if="row.member_class === 'agent' || row.member_class === 'ordinary'">{{ row.member_class === 'agent' ? '代理商价格' : '普通成员价格' }} · 私信核验后人工切换</small>
                <small v-if="row.attribution_source === 'cashier_confirmation'">归属：结账员人工确认</small>
                <small v-if="row.payment_kind === 'exception'" class="warning">异常付款补记</small>
                <small v-if="state === 'unassigned'">原结账状态：{{ stateNames[row.state] }}</small>
              </div>
              <div class="entry-date"><span>{{ time(row.first_seen) }}</span><small>首次发现入群</small><template v-if="row.state === 'verifying' && row.qualification_started"><small>核验开始：{{ time(row.qualification_started) }}</small><small v-if="row.qualification_reason === 'observation_gap'">采集中断后重新核验</small><small v-else-if="row.qualification_reason === 'migration_recovery'">补录后重新核验</small><small v-else-if="row.qualification_reason === 'manual_confirmation'">人工归属后重新核验</small></template></div>
              <div class="entry-status"><span :class="['status', canPay(row) ? 'good' : 'warning']">{{ row.review_reason ? '来源或原邀请周期待复核' : row.membership_left_at ? '邀请周期内曾退群' : verification(row) }}</span><small>{{ row.present ? '当前在群' : '已退群' }}</small><small v-if="current?.require_speech">{{ row.first_spoke_at ? `已发言 · ${time(row.first_spoke_at)}` : row.state === 'paid' ? '发言未记录，原已结账登记保留' : '未发言，暂不符合结账条件' }}</small><template v-if="row.state === 'verifying' && row.inviter && row.pricing_status !== 'no_reward'"><small>{{ remaining(row) }}</small><small :title="time(row.hold_until)">预计 {{ deadlineFormat.format(new Date(row.hold_until * 1000)) }}</small></template></div>
              <b class="entry-money">{{ row.self_join || row.pricing_status === 'no_reward' ? '无返现' : ['unconfigured', 'unclassified'].includes(row.pricing_status) ? '待定价' : `¥${money(row.amount)}` }}</b>
              <div v-if="canPay(row) || canCorrect(row) || canException(row) || canReprice(row) || canAssign(row)" class="entry-actions" role="group" :aria-label="`${row.member_name}的账本操作`">
                <v-btn v-if="canAssign(row) && state === 'unassigned'" size="small" variant="text" :disabled="readOnly || saving || busy" @click="confirmAssignment(row)"><UserRoundCheck :size="16" />确认归属</v-btn>
                <v-btn v-else-if="canPay(row)" size="small" variant="text" :disabled="readOnly || saving || busy" @click="confirm('pay', row)">登记已结账</v-btn>
                <v-btn v-else-if="canCorrect(row)" size="small" variant="text" :disabled="readOnly || saving || busy" title="撤回已结账登记" @click="confirm('correct', row)"><Undo2 :size="16" />撤回结账</v-btn>
                <v-btn v-else-if="canException(row)" size="small" variant="text" :disabled="readOnly || saving || busy" @click="confirm('exception', row)">异常付款</v-btn>
                <v-btn v-if="canReprice(row)" size="small" variant="text" :disabled="readOnly || saving || busy" @click="confirm('reprice', row)">{{ row.member_class === 'agent' ? '改普通成员价' : '改代理商价' }}</v-btn>
              </div>
            </div>
            <nav v-if="section.page_count > 1" class="pagination member-pagination" :aria-label="`${section.name}的成员分页`">
              <v-btn icon size="small" variant="text" title="上一页成员" aria-label="上一页成员" :disabled="section.page <= 1 || busy || saving || dialog" @click="loadSectionEntries(section, section.page - 1)"><ChevronLeft :size="19" /></v-btn>
              <span>成员 {{ section.page }} / {{ section.page_count }}</span>
              <v-btn icon size="small" variant="text" title="下一页成员" aria-label="下一页成员" :disabled="section.page >= section.page_count || busy || saving || dialog" @click="loadSectionEntries(section, section.page + 1)"><ChevronRight :size="19" /></v-btn>
            </nav>
          </details>
          <nav class="pagination" v-if="pageCount > 1" aria-label="邀请人分页"><v-btn icon size="small" variant="text" title="上一页邀请人" aria-label="上一页邀请人" :disabled="page <= 1 || busy || saving || dialog" @click="page--"><ChevronLeft :size="19" /></v-btn><span>邀请人 {{ page }} / {{ pageCount }}</span><v-btn icon size="small" variant="text" title="下一页邀请人" aria-label="下一页邀请人" :disabled="page >= pageCount || busy || saving || dialog" @click="page++"><ChevronRight :size="19" /></v-btn></nav>
          <div class="selection" v-if="selectedRows.length">
            <div><strong>已选 {{ selectedRows.length }} 笔 · ¥{{ money(selectedAmount) }}</strong><small>{{ selectedRows[0].inviter_name }}</small></div>
            <v-btn v-if="selectedRows.every(canPay)" color="primary" @click="confirm('pay')">登记已结账</v-btn>
            <v-btn v-else-if="state === 'paid'" color="primary" :disabled="readOnly || saving || busy" @click="confirm('correct')"><Undo2 :size="16" />撤回结账</v-btn>
            <v-btn v-else-if="admin" color="error" @click="confirm('exception')">异常付款登记</v-btn>
            <v-btn icon size="small" variant="text" title="取消选择" aria-label="取消选择" @click="selected = []"><X :size="18" /></v-btn>
          </div>
        </section>
        <section v-if="tab === 'rules' && admin" class="settings">
          <h2>群返现设置</h2>
          <v-select :model-value="rule.id" :items="[...new Map([...groups, ...catalog].map(g => [g.id, g])).values()]" item-title="name" item-value="id" label="目标群" :disabled="busy || readOnly" @update:model-value="editGroup" />
          <v-text-field v-model="rule.contact" label="结账联系方式" maxlength="200" :disabled="busy || readOnly" />
          <v-text-field v-model="rule.money" label="每位有效新成员金额（元）" inputmode="decimal" :disabled="busy || readOnly" />
          <v-text-field v-model="rule.agentMoney" label="代理商每位有效新成员金额（元）" inputmode="decimal" :disabled="busy || readOnly" />
          <p class="muted">连续在群核验：3小时 · 采集机器人：{{ rule.bot_id }}</p>
          <v-checkbox v-model="rule.require_speech" label="按照进群发言计算" color="primary" hide-details :disabled="busy || readOnly" />
          <v-checkbox v-model="rule.agent_only" label="仅代理商返现（新增普通成员不返现）" color="primary" hide-details :disabled="busy || readOnly" />
          <v-switch v-model="rule.enabled" label="启用新邀请返现" color="primary" hide-details :disabled="busy || readOnly" />
          <v-btn color="primary" :loading="busy" :disabled="busy || readOnly || !rule.id || !rule.contact.trim() || !rule.money" @click="saveRule">保存群设置</v-btn>
          <h2>返现风控复核</h2>
          <p class="muted">同一邀请者在 5 分钟内邀请至少 10 名不同成员，且至少 90% 的入群名称长度相同，会收到一次群内 @ 提醒；下一次独立命中后冻结本群未结返现。不会踢群或禁言。</p>
          <p v-if="!riskCases.length" class="empty">当前群暂无风控记录</p>
          <article v-for="item in riskCases" :key="item.id" class="audit-row">
            <strong>{{ item.inviter_name }} · {{ item.status === 'blocked' && item.blocked_at ? '风控冻结' : item.status === 'warned' ? '已提醒' : item.status === 'warning_pending' ? '通知待发送' : item.status === 'warning_failed' ? '通知发送未确认' : item.status === 'released' ? '已解除' : '风控记录' }}</strong>
            <p>{{ time(item.detected) }} · {{ item.member_count }} 名成员中 {{ item.matching_count }} 名名称长度为 {{ item.name_length }}</p>
            <p v-if="item.notice_status">群通知：{{ item.notice_status === 'accepted' || item.notice_status === 'verified' ? '已受理' : item.notice_status === 'pending' ? '待发送' : item.notice_status }}</p>
            <p v-if="item.resolution">复核说明：{{ item.resolution }}</p>
            <v-btn v-if="item.blocked_at" size="small" variant="tonal" :disabled="readOnly || busy" @click="openRiskRelease(item)">复核并解除本群冻结</v-btn>
          </article>
        </section>
        <section v-if="tab === 'audit'">
          <h2>操作日志</h2>
          <p v-if="!audits.length" class="empty">暂无操作日志</p>
          <details v-for="item in audits" :key="item.id" class="audit-row">
            <summary><strong><span :class="item.level === 'error' ? 'log-error' : item.level === 'warning' ? 'warning' : 'good'">{{ item.level === 'error' ? '错误' : item.level === 'warning' ? '警告' : '正常' }}</span> · {{ actionNames[item.action] || item.action }}</strong><span>{{ time(item.created) }}</span></summary>
            <p v-if="item.resolved_at" class="good">已恢复 · {{ time(item.resolved_at) }}（北京时间）</p>
            <p>{{ item.actor }}<span v-if="item.actor_uid"> · UID {{ item.actor_uid }}</span><span v-if="item.reason"> · {{ item.reason }}</span></p>
            <p v-if="item.code">错误码：{{ item.code }}</p><small v-if="item.trace">请求编号：{{ item.trace }}</small><small v-if="item.bot_id">机器人：{{ item.bot_id }}</small>
            <div class="audit-values"><div><small>修改前</small><pre>{{ JSON.stringify(JSON.parse(item.before_json), null, 2) }}</pre></div><div><small>修改后</small><pre>{{ JSON.stringify(JSON.parse(item.after_json), null, 2) }}</pre></div></div>
          </details>
          <v-btn v-if="audits.length && audits.length % 100 === 0" :loading="busy" @click="guard(() => loadAudit(audits[audits.length - 1].id))">更早记录</v-btn>
        </section>
      </template>
      <v-progress-linear v-if="busy" indeterminate color="primary" aria-label="加载中" />
    </main>
    <v-dialog v-model="dialog" max-width="560" :persistent="saving">
      <v-card>
        <v-card-title class="text-h3 pa-4 pb-0 pl-6">{{ operation === 'assign' ? '确认邀请归属' : operation === 'pay' ? '确认已人工付款' : operation === 'correct' ? '撤回结账登记' : operation === 'reprice' ? `切换为${priceCategory === 'agent' ? '代理商' : '普通成员'}价格` : '登记异常付款' }}</v-card-title>
        <v-card-text>
          <p v-if="operation !== 'assign'" class="payment-recipient"><small>{{ operation === 'correct' ? '原结账登记' : '收款邀请人' }}</small><strong>{{ selectionSnapshot[0]?.inviter_name || (operation === 'correct' ? '归属待核对' : '名称待核验') }}</strong><small v-if="selectionSnapshot[0]?.inviter">账号 {{ selectionSnapshot[0]?.inviter }}</small></p>
          <v-autocomplete v-else v-model="assignedInviter" :items="inviterOptions.map(item => ({ ...item, title: `${item.name}（账号 ${item.uid}）` }))" item-title="title" item-value="uid" label="已有邀请人（必选）" :disabled="saving" />
          <p>{{ selectionGroup?.name }}</p>
          <p class="confirm-total">¥{{ money(selectionSnapshot.reduce((n, r) => n + r.amount, 0)) }} <small>共 {{ selectionSnapshot.length }} 笔</small></p>
          <p v-if="operation === 'reprice' && priceConfirmation" class="confirm-total">目标金额 ¥{{ money(priceConfirmation.items.reduce((n, item) => n + item.target_amount, 0)) }} <small>规则版本 {{ priceConfirmation.rule_version }}</small></p>
          <ul class="confirm-list"><li v-for="item in selectionSnapshot" :key="item.id"><span><small>被邀请人</small>{{ item.member_name }}<small>账号 {{ item.member }}</small></span><b>¥{{ money(item.amount) }}</b></li></ul>
          <ul v-if="operation === 'reprice' && priceConfirmation" class="confirm-list"><li v-for="price in priceConfirmation.items" :key="price.id"><span>{{ selectionSnapshot.find(item => item.id === price.id)?.member_name }}<small>原价 ¥{{ money(price.original_amount) }} · 记录版本 {{ price.version }}</small></span><b>目标 ¥{{ money(price.target_amount) }}</b></li></ul>
          <v-alert v-if="operation === 'correct'" type="warning" variant="tonal">撤回后，符合核验条件的记录转回待结账；核验时间未到、发言条件未满足或价格未配置的继续核验；已退群或曾退群的记录改为已作废。仅撤回登记，不会自动退回已支付款项，原结账记录保留在操作日志中。</v-alert>
          <v-alert v-if="operation === 'reprice'" type="info" variant="tonal">{{ selectionSnapshot[0]?.pricing_status === 'no_reward' ? '该普通成员不参与返现。' : '当前金额已保留在账本。' }}请确认私信证明后，再切换为{{ priceCategory === 'agent' ? '代理商' : '普通成员' }}价格。</v-alert>
          <v-alert v-if="operation === 'exception'" type="warning" variant="tonal">仅补记管理员已实际完成的外部付款，不会转账。可补记未满三小时、未发言或已退群的记录；收款身份、邀请归属、价格及风控仍须核验。原资格证据和异常原因将保留。</v-alert>
          <v-checkbox v-if="operation === 'exception'" v-model="actualPaymentConfirmed" label="我确认已向上述收款人实际完成外部付款" :disabled="saving" color="primary" hide-details />
          <v-alert v-if="operation === 'assign'" type="info" variant="tonal">{{ selectionSnapshot[0]?.state === 'paid' ? '仅补充归属，保留原付款金额、时间和已结账状态，不会再次转入待结账。' : '确认归属后，核验满三小时的记录转入待结账；未满期限的记录继续核验。' }}</v-alert>
          <v-textarea v-if="operation !== 'pay'" v-model="reason" :label="operation === 'correct' ? '撤回原因（必填）' : '原因（必填）'" variant="outlined" rows="2" maxlength="300" class="reason" />
          <v-alert v-if="error" type="error" variant="tonal">{{ error }}</v-alert>
        </v-card-text>
        <v-card-actions><v-spacer /><v-btn variant="text" :disabled="saving" @click="dialog = false">取消</v-btn><v-btn color="primary" variant="tonal" :loading="saving" :disabled="saving || readOnly || (operation !== 'pay' && reason.trim().length < 2) || (operation === 'assign' && !assignedInviter) || (operation === 'exception' && !actualPaymentConfirmed) || (operation === 'reprice' && !priceConfirmation)" @click="submit">{{ operation === 'assign' ? '确认归属' : operation === 'correct' ? '确认撤回' : operation === 'reprice' ? '确认切换价格' : '已人工付款，登记' }}</v-btn></v-card-actions>
      </v-card>
    </v-dialog>
    <v-dialog v-model="riskReleaseDialog" max-width="520" :persistent="busy">
      <v-card>
        <v-card-title class="text-h3 pa-4 pb-0 pl-6">复核并解除返现冻结</v-card-title>
        <v-card-text>
          <p>{{ riskReleaseTarget?.inviter_name }} · {{ groups.find(item => item.id === riskReleaseGroup)?.name }}（{{ riskReleaseGroup }}）</p>
          <v-alert type="warning" variant="tonal">仅解除该邀请人在本群的返现冻结，不修改金额或已结账历史；解除后新记录仍继续接受风控检测。</v-alert>
          <v-textarea v-model="riskReleaseReason" label="复核依据（必填）" maxlength="300" rows="3" />
          <v-alert v-if="error" type="error" variant="tonal">{{ error }}</v-alert>
        </v-card-text>
        <v-card-actions><v-spacer /><v-btn variant="text" :disabled="busy" @click="riskReleaseDialog = false">取消</v-btn><v-btn color="primary" :loading="busy" :disabled="busy || readOnly || riskReleaseReason.trim().length < 2" @click="releaseRisk">确认解除</v-btn></v-card-actions>
      </v-card>
    </v-dialog>
  </v-app>
</template>

<style scoped src="./settlement.css"></style>
