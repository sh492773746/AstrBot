<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { Check, Eye, LoaderCircle, RefreshCw, Send, Video, X } from "@lucide/vue";

type Config = {
  cms_url: string;
  category_id: number;
  enabled: boolean;
  interval_minutes: number;
  max_video_mb: number;
  min_free_gb: number;
  follow_group_id?: string;
};
type State = {
  version: number; revision: string; config: Config; updated_at: number | null;
  destination: { platform: string; account: string; group_id: string };
  runtime: {
    state: string; next_at?: number; error?: string; video_sender?: string;
    can_preview_video?: boolean; tasks: Array<VideoJob> | null;
  };
};
type VideoJob = {
  id: string; state: string; error?: string; cleanup_state?: string;
  metadata?: {
    title: string; vod_id: number; size: number; duration: number; width: number; height: number;
    category_id?: number; category_name?: string;
  };
};
type Category = { id: number; name: string };
const props = defineProps<{
  botId: string; groupId: string; groupName?: string; locale: string;
  bridge: {
    apiGet(endpoint: string, params: Record<string, string>): Promise<any>;
    apiPost(endpoint: string, body: unknown): Promise<any>;
  };
}>();
const emit = defineEmits<{ (event: "dirty", value: boolean): void; (event: "busy", value: boolean): void }>();
const t = (zh: string, en: string) => props.locale.startsWith("zh") ? zh : en;
const state = ref<State | null>(null);
const config = ref<Config | null>(null);
const original = ref("");
const pending = ref("");
const error = ref("");
const notice = ref("");
const preview = ref<{ token: string; version: number; config: Config } | null>(null);
const confirmation = ref<HTMLDialogElement | null>(null);
const previewButton = ref<HTMLButtonElement | null>(null);
const videoPreview = ref<(VideoJob & { token: string }) | null>(null);
const videoConfirmation = ref<HTMLDialogElement | null>(null);
const categories = ref<Category[]>([]);
const categoriesLoading = ref(false);
const categoryError = ref("");
const categoryQuery = ref("");
const selectedCategory = ref<Category | null>(null);
const dirty = computed(() => Boolean(config.value && original.value
  && JSON.stringify(config.value) !== original.value));
const locked = computed(() => Boolean(pending.value || preview.value || videoPreview.value) || !state.value);
const destination = computed(() => `${props.groupName || props.groupId} · ${props.groupId}`);
const valid = computed(() => {
  const c = config.value;
  if (!c) return false;
  let sourceValid = !c.cms_url.trim();
  try {
    const url = new URL(c.cms_url.trim());
    sourceValid = url.protocol === "https:" && !url.username && !url.password
      && !url.hash && (!url.port || url.port === "443");
  } catch { /* Server validation remains authoritative. */ }
  return sourceValid && (!c.enabled || Boolean(c.cms_url.trim()))
    && [
      [c.category_id, 0, 2147483647], [c.interval_minutes, 1, 1440],
      [c.max_video_mb, 1, 112], [c.min_free_gb, 1, 20],
    ].every(([value, min, max]) => Number.isInteger(value) && value >= min && value <= max);
});
const limitError = computed(() => {
  const c = config.value;
  if (!c) return "";
  if (!Number.isInteger(c.max_video_mb) || c.max_video_mb < 1 || c.max_video_mb > 112)
    return t("单视频上限只能是 1—112 MiB。", "Video limit must be 1-112 MiB.");
  if (!Number.isInteger(c.min_free_gb) || c.min_free_gb < 1 || c.min_free_gb > 20)
    return t("最低剩余空间只能是 1—20 GiB。", "Minimum free space must be 1-20 GiB.");
  if (!Number.isInteger(c.interval_minutes) || c.interval_minutes < 1 || c.interval_minutes > 1440)
    return t("发送间隔只能是 1—1440 分钟。", "Send interval must be 1-1440 minutes.");
  if (!Number.isInteger(c.category_id) || c.category_id < 0 || c.category_id > 2147483647)
    return t("分类 ID 必须是非负整数。", "Category ID must be a non-negative integer.");
  return "";
});
const filteredCategories = computed(() => {
  const query = categoryQuery.value.trim().toLowerCase();
  if (!query) return categories.value;
  return categories.value.filter(category =>
    category.name.toLowerCase().includes(query) || String(category.id).includes(query));
});
let generation = 0;
let alive = true;
const videoStatus = (value: string) => ({
  stopped: t("已停用", "Stopped"), scheduled: t("等待下次发送", "Scheduled"),
  running: t("执行中", "Running"), blocked: t("已暂停，待核对", "Paused; Review Required"),
  preparing: t("准备中", "Preparing"), prepared: t("待确认", "Awaiting Confirmation"),
  uploading: t("上传中", "Uploading"), sending: t("发送中", "Sending"),
  accepted: t("平台已受理，待收件确认", "Accepted; Receipt Check Pending"),
  rejected: t("发送被拒绝", "Rejected"), unknown: t("结果未知，请核对，勿重发", "Unknown; Verify Before Sending Again"),
  not_sent: t("未发送", "Not Sent"),
  failed: t("未发送", "Not Sent"), cancelled: t("已取消", "Cancelled"), expired: t("预览已过期", "Preview Expired"),
}[value] || value);

async function loadCategories() {
  if (!props.botId || !props.groupId || categoriesLoading.value) return;
  categoriesLoading.value = true;
  categoryError.value = "";
  try {
    const result = await props.bridge.apiGet("video-relay", {
      bot_id: props.botId, group_id: props.groupId, operation: "categories",
    });
    categories.value = Array.isArray(result?.categories) ? result.categories : [];
    if (!categories.value.length) categoryError.value = t("CMS 未返回分类目录。", "The CMS returned no category catalog.");
  } catch (cause) {
    categories.value = [];
    categoryError.value = cause instanceof Error && cause.message.length <= 400
      ? cause.message : t("分类读取失败，请先保存 CMS 接口后重试。", "Category loading failed. Save the CMS URL and retry.");
  } finally {
    categoriesLoading.value = false;
  }
}

async function previewCategory(category: Category) {
  if (pending.value || videoPreview.value || preview.value) return;
  if (!state.value?.runtime.can_preview_video) return;
  if (limitError.value) {
    error.value = limitError.value;
    return;
  }
  if (dirty.value) {
    notice.value = t("表单有未保存修改；本次分类预览按已保存配置执行，不会写入配置。",
      "The form has unsaved changes; this category preview uses the saved configuration and does not write it.");
  }
  selectedCategory.value = category;
  const c = config.value;
  const limit = c && Number.isInteger(c.max_video_mb) && c.max_video_mb >= 1 && c.max_video_mb <= 112
    ? c.max_video_mb : undefined;
  await executeVideo("preview_video", category.id, limit);
  if (!videoPreview.value) selectedCategory.value = null;
}

async function cancelActive() {
  if (!state.value || !props.botId || !props.groupId) return;
  pending.value = "cancel_active";
  error.value = "";
  notice.value = "";
  try {
    await props.bridge.apiPost("video-relay", {
      bot_id: props.botId, group_id: props.groupId, operation: "cancel_active",
      version: state.value.version, revision: state.value.revision,
    });
    videoPreview.value = null;
    selectedCategory.value = null;
    videoConfirmation.value?.close();
    notice.value = t("已取消未提交的视频预览。", "Unsubmitted video preview cancelled.");
    await execute("read");
  } catch (cause) {
    error.value = cause instanceof Error && cause.message.length <= 400
      ? cause.message : t("取消失败，请刷新记录核对。", "Cancel failed; refresh the records.");
  } finally {
    pending.value = "";
  }
}

async function executeVideo(operation: "preview_video" | "send_video" | "cancel_video", categoryId?: number, limitMb?: number) {
  if (pending.value || !state.value) return;
  if (operation !== "preview_video" && dirty.value) {
    error.value = t("配置有未保存修改；请先保存配置后再发送。", "Save the configuration before sending.");
    return;
  }
  if (operation === "preview_video"
    && (preview.value || videoPreview.value || !state.value.runtime.can_preview_video)) return;
  if (operation !== "preview_video" && !videoPreview.value) return;
  const current = ++generation;
  const saved = state.value;
  const reviewed = videoPreview.value;
  const scope = { bot_id: props.botId, group_id: props.groupId };
  pending.value = operation;
  error.value = "";
  notice.value = "";
  if (operation === "send_video") {
    videoPreview.value = null;
    videoConfirmation.value?.close();
  }
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const result = await Promise.race([
      props.bridge.apiPost("video-relay", {
        ...scope, operation, version: saved.version, revision: saved.revision,
        ...(operation === "preview_video"
          ? { ...(categoryId === undefined ? {} : { category_id: categoryId }),
              ...(limitMb === undefined ? {} : { max_video_mb: limitMb }) }
          : { token: reviewed!.token }),
      }),
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(new Error("request_timeout")), 180000);
      }),
    ]);
    if (!alive || current !== generation) return;
    if (operation === "preview_video") {
      videoPreview.value = result;
      await nextTick();
      if (alive && current === generation) videoConfirmation.value?.showModal();
    } else {
      videoPreview.value = null;
      selectedCategory.value = null;
      videoConfirmation.value?.close();
      if (["accepted", "cancelled"].includes(result.state)) notice.value = videoStatus(result.state);
      else error.value = videoStatus(result.state);
      if (state.value) state.value.runtime.tasks = [{
        ...result, metadata: reviewed?.metadata,
      }];
      previewButton.value?.focus();
    }
  } catch (cause) {
    if (!alive || current !== generation) return;
    videoPreview.value = null;
    selectedCategory.value = null;
    videoConfirmation.value?.close();
    const message = cause instanceof Error ? cause.message : "";
    error.value = operation === "send_video"
      ? t("发送结果未确认，请刷新记录并核对目标群；不会自动重发。", "Delivery unconfirmed. Refresh records and check the destination; no automatic resend.")
      : message && message !== "request_timeout" && message.length <= 400 ? message
        : t("视频未发送，请刷新查看处理记录或重新预览。", "Video not sent. Refresh the processing record or prepare a new preview.");
  } finally {
    clearTimeout(timer);
    if (alive && current === generation) pending.value = "";
  }
}

function cancelPreview() {
  if (pending.value) return;
  preview.value = null;
  confirmation.value?.close();
  previewButton.value?.focus();
}

async function execute(operation: "read" | "preview" | "commit") {
  if (pending.value || videoPreview.value || !props.botId || !props.groupId) return;
  if (operation === "preview" && (!valid.value || !dirty.value || !state.value)) return;
  if (operation === "commit" && (!preview.value || !state.value)) return;
  const scope = { bot_id: props.botId, group_id: props.groupId };
  const saved = state.value;
  const draft = preview.value;
  const current = ++generation;
  pending.value = operation;
  error.value = "";
  notice.value = "";
  if (operation === "read") {
    state.value = null;
    config.value = null;
    original.value = "";
  }
  // Never reuse a confirmation after a possibly committed request.
  if (operation === "commit") {
    preview.value = null;
    confirmation.value?.close();
  }
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const request = operation === "read" ? props.bridge.apiGet("video-relay", scope)
      : props.bridge.apiPost("video-relay", {
        ...scope, operation, version: saved!.version, revision: saved!.revision,
        ...(operation === "preview" ? { config: JSON.parse(JSON.stringify(config.value)) } : { token: draft!.token }),
      });
    const result = await Promise.race([request, new Promise<never>((_, reject) => {
      timer = setTimeout(() => reject(new Error("request_timeout")), 15000);
    })]);
    if (!alive || current !== generation) return;
    if (operation === "read") {
      if (typeof result?.config?.cms_url !== "string") {
        throw new Error(t("CMS新版后端尚未加载，请管理员部署后刷新。", "The CMS backend update is not loaded. Deploy it, then refresh."));
      }
      state.value = result;
      config.value = structuredClone(result.config);
      original.value = JSON.stringify(config.value);
      selectedCategory.value = null;
      if (result.config.cms_url && !result.config.follow_group_id) void loadCategories();
    } else if (operation === "preview") {
      preview.value = result;
      await nextTick();
      if (alive && current === generation) confirmation.value?.showModal();
    } else {
      state.value = { ...saved!, version: result.version, config: draft!.config,
        runtime: { ...saved!.runtime, can_preview_video: Boolean(draft!.config.cms_url
          && !draft!.config.follow_group_id
          && saved!.runtime.video_sender === "single_video_ready"),
          state: result.started ? "scheduled" : "stopped", error: "",
          next_at: result.started ? Date.now() / 1000 + draft!.config.interval_minutes * 60 : undefined } };
      config.value = JSON.parse(JSON.stringify(draft!.config));
      original.value = JSON.stringify(config.value);
      notice.value = result.started
        ? t("配置已保存，自动发送已启用。", "Configuration saved; automatic sending enabled.")
        : t("配置已保存，自动发送已停用。", "Configuration saved; automatic sending disabled.");
      previewButton.value?.focus();
      if (draft!.config.cms_url && !draft!.config.follow_group_id) void loadCategories();
    }
  } catch (cause) {
    if (!alive || current !== generation) return;
    preview.value = null;
    confirmation.value?.close();
    if (operation === "commit") {
      state.value = null;
      original.value = "";
      config.value = null;
      error.value = t("保存结果未确认，请刷新核对；不会自动重试。", "Save outcome unconfirmed. Refresh to verify; no automatic retry.");
    } else {
      const message = cause instanceof Error ? cause.message : "";
      error.value = message && message !== "request_timeout" ? message
        : t("读取或预览超时，请稍后重试。", "Read or preview timed out. Try again later.");
    }
  } finally {
    clearTimeout(timer);
    if (alive && current === generation) pending.value = "";
  }
}

watch(() => [props.botId, props.groupId], () => {
  generation++;
  pending.value = "";
  preview.value = null;
  videoPreview.value = null;
  videoConfirmation.value?.close();
  confirmation.value?.close();
  selectedCategory.value = null;
  categories.value = [];
  categoryError.value = "";
  categoryQuery.value = "";
  state.value = null;
  config.value = null;
  original.value = "";
  error.value = "";
  notice.value = "";
  void execute("read");
}, { immediate: true });
watch(dirty, value => emit("dirty", value), { immediate: true });
watch(pending, value => emit("busy", Boolean(value)), { immediate: true });
onBeforeUnmount(() => {
  alive = false;
  generation++;
  confirmation.value?.close();
  videoConfirmation.value?.close();
  emit("dirty", false);
  emit("busy", false);
});
</script>

<template>
  <section class="video-relay" :aria-label="t('视频转发', 'Video Relay')">
    <header class="section-heading">
      <h2><Video :size="18" />{{ t("视频转发", "Video Relay") }}</h2>
      <button type="button" class="icon-button" :disabled="Boolean(pending) || dirty || Boolean(preview)"
        :title="t('刷新视频配置', 'Refresh relay configuration')" :aria-label="t('刷新视频配置', 'Refresh relay configuration')"
        @click="execute('read')"><RefreshCw :size="17" :class="{ spin: pending === 'read' }" /></button>
    </header>
    <p v-if="!groupId" class="empty">{{ t("没有已启用群", "No enabled groups") }}</p>
    <p v-if="pending" role="status" class="relay-progress"><LoaderCircle :size="16" class="spin" />
      {{ pending === 'read' ? t("读取配置中", "Loading configuration")
        : pending === 'preview_video' ? t("下载并准备视频预览中", "Downloading and Preparing Video")
        : pending === 'send_video' ? t("上传并发送视频中", "Uploading and Sending Video")
        : pending === 'cancel_active' ? t("正在取消视频预览", "Cancelling Video Preview")
        : t("处理配置中", "Processing configuration") }}</p>
    <p v-if="error" role="alert" class="message error">{{ error }}</p>
    <p v-if="notice" role="status" class="message success">{{ notice }}</p>
    <template v-if="state && config">
      <dl class="relay-status">
        <div v-if="config.follow_group_id"><dt>{{ t("同步来源群", "Follow Source Group") }}</dt><dd>{{ config.follow_group_id }}</dd></div>
        <div><dt>{{ t("自动发送", "Automatic Sending") }}</dt><dd>{{ videoStatus(state.runtime.state) }}</dd></div>
        <div v-if="config.follow_group_id"><dt>{{ t("发送计划", "Sending Schedule") }}</dt><dd>{{ t("跟随来源群新一轮", "Follow New Source Rounds") }}</dd></div>
        <div v-else><dt>{{ t("下次发送（北京时间）", "Next Send (Beijing Time)") }}</dt><dd>{{ config.enabled && state.runtime.next_at
          ? new Date(state.runtime.next_at * 1000).toLocaleString(props.locale, { timeZone: 'Asia/Shanghai', hour12: false }) : t("未安排", "Not Scheduled") }}</dd></div>
        <div><dt>{{ t("视频发送", "Video Sender") }}</dt><dd>{{ state.runtime.video_sender === 'single_video_ready'
          ? t("单条链路已接入", "Single-Video Path Connected")
          : state.runtime.video_sender === 'tools_missing' ? t("缺少媒体工具", "Media Tools Missing") : t("未接通", "Not Available") }}</dd></div>
        <div><dt>{{ t("配置版本", "Configuration Version") }}</dt><dd>{{ state.version ? `v${state.version}` : t("未保存", "Unsaved") }}</dd></div>
      </dl>
      <p v-if="state.runtime.error" role="alert" class="message error">{{ state.runtime.error }}</p>
      <h3>{{ t("目标群", "Destination") }}</h3>
      <p class="relay-destination">{{ destination }}<small>{{ t("机器人账号", "Bot Account") }} {{ state.destination.account }} · Wangshangliao</small></p>
      <fieldset class="relay-fields" :disabled="locked">
        <h3>{{ t("CMS 来源", "CMS Source") }}</h3>
        <label class="cms-source">{{ t("CMS 接口地址", "CMS API URL") }}
          <input v-model="config.cms_url" type="url" maxlength="4096" autocomplete="off" spellcheck="false" />
        </label>
        <h3>{{ t("发送计划", "Sending Schedule") }}</h3>
        <div class="field-grid">
          <label>{{ t("分类 ID（0 为全部）", "Category ID (0 for All)") }}<input v-model.number="config.category_id" type="number" min="0" max="2147483647" /></label>
          <label>{{ t("发送间隔（分钟）", "Send Interval (Minutes)") }}<input v-model.number="config.interval_minutes" type="number" min="1" max="1440" /></label>
          <label class="check"><input v-model="config.enabled" type="checkbox" />{{ t("自动发送", "Automatic Sending") }}</label>
        </div>
        <h3>{{ t("资源限制", "Resource Limits") }}</h3>
        <div class="field-grid">
          <label>{{ t("单视频上限（MiB）", "Video Limit (MiB)") }}<input v-model.number="config.max_video_mb" type="number" min="1" max="112" step="1" /></label>
          <label>{{ t("最低剩余空间（GiB）", "Minimum Free Space (GiB)") }}<input v-model.number="config.min_free_gb" type="number" min="1" max="20" step="1" /></label>
        </div>
        <p class="field-hint">{{ t("单条视频上限 1—112 MiB；上传越大越慢，超时或回执未知时不会自动重发。",
          "Video limit 1-112 MiB; larger uploads are slower and unknown receipts are never auto-retried.") }}</p>
        <p v-if="limitError" role="alert" class="message error">{{ limitError }}</p>
      </fieldset>
      <div class="relay-actions">
        <button ref="previewButton" type="button" class="primary" :disabled="locked || !dirty || !valid" @click="execute('preview')">
          <Eye :size="16" />{{ t("预览配置", "Preview Configuration") }}</button>
        <button type="button" :disabled="locked || dirty || !state.runtime.can_preview_video" @click="executeVideo('preview_video')">
          <Eye :size="16" />{{ t("预览一条视频", "Preview One Video") }}</button>
      </div>
      <section v-if="!config.follow_group_id" class="relay-categories" :aria-label="t('按分类转发', 'Relay by Category')">
        <header class="category-heading">
          <h3>{{ t("按分类转发", "Relay by Category") }}</h3>
          <div class="category-actions">
            <button v-if="pending === 'preview_video'" type="button" class="danger-button"
              :disabled="pending !== 'preview_video'" @click="cancelActive">
              <X :size="14" />{{ t("取消本次准备", "Cancel This Preview") }}</button>
            <button type="button" class="icon-button" :disabled="categoriesLoading || locked"
              :title="t('刷新分类', 'Refresh categories')" :aria-label="t('刷新分类', 'Refresh categories')"
              @click="loadCategories"><RefreshCw :size="15" :class="{ spin: categoriesLoading }" /></button>
          </div>
        </header>
        <p class="category-hint">{{ t("点击一个分类，只预览该分类中的一条视频；确认后才会上传并发送。",
          "Click a category to preview one video from it; upload and delivery happen only after confirmation.") }}</p>
        <label class="category-search">{{ t("搜索分类", "Search categories") }}
          <input v-model="categoryQuery" type="search" maxlength="64" autocomplete="off"
            :placeholder="t('分类名称或ID', 'Category name or ID')" :disabled="categoriesLoading || locked" />
        </label>
        <p v-if="categoriesLoading" role="status" class="relay-progress">
          <LoaderCircle :size="15" class="spin" />{{ t("读取分类中", "Loading categories") }}</p>
        <p v-if="categoryError" role="alert" class="message error">{{ categoryError }}</p>
        <p v-if="!categoriesLoading && !categoryError && !categories.length" class="empty">
          {{ t("CMS 暂无分类；仍可按已保存的分类 ID 预览。", "The CMS returned no categories; the saved category ID still works.") }}</p>
        <ul v-if="filteredCategories.length" class="category-list">
          <li v-for="category in filteredCategories" :key="category.id">
            <button type="button" :disabled="locked || !state.runtime.can_preview_video"
              :aria-label="`${t('预览分类', 'Preview category')} ${category.name}`" @click="previewCategory(category)">
              <span>{{ category.name }}</span><small>ID {{ category.id }}</small>
            </button>
          </li>
        </ul>
        <p v-else-if="categories.length && categoryQuery.trim()" class="empty">
          {{ t("没有匹配的分类。", "No matching categories.") }}</p>
      </section>
      <h3>{{ t("任务与发布记录", "Tasks & Publication Records") }}</h3>
      <p v-if="!state.runtime.tasks?.length" class="empty">{{ t("暂无单条发送记录", "No Single-Video Records") }}</p>
      <dl v-for="job in state.runtime.tasks || []" :key="job.id" class="relay-review" aria-live="polite">
        <div><dt>{{ t("视频", "Video") }}</dt><dd>{{ job.metadata?.title || job.id }}</dd></div>
        <div><dt>{{ t("状态", "Status") }}</dt><dd>{{ videoStatus(job.state) }}</dd></div>
        <div v-if="job.error"><dt>{{ t("错误代码", "Error Code") }}</dt><dd>{{ job.error }}</dd></div>
        <div v-if="job.cleanup_state"><dt>{{ t("本地文件", "Local Files") }}</dt><dd>{{ job.cleanup_state === 'deleted'
          ? t("已删除", "Deleted") : t("清理待重试", "Cleanup Pending") }}</dd></div>
      </dl>
    </template>
    <dialog ref="confirmation" class="relay-confirmation" :aria-label="t('确认视频配置', 'Confirm Relay Configuration')" @cancel.prevent="cancelPreview">
      <template v-if="preview">
        <header class="section-heading"><h2>{{ t("确认视频配置", "Confirm Relay Configuration") }}</h2>
          <button type="button" class="icon-button" :disabled="Boolean(pending)" :title="t('关闭预览', 'Close Preview')"
            :aria-label="t('关闭预览', 'Close Preview')" @click="cancelPreview"><X :size="18" /></button></header>
        <p class="relay-destination">{{ destination }}<small>{{ t("机器人账号", "Bot Account") }} {{ state?.destination.account }}</small></p>
        <dl class="relay-review">
          <div><dt>{{ t("CMS 来源", "CMS Source") }}</dt><dd>{{ preview.config.cms_url || t("无", "None") }}</dd></div>
          <div><dt>{{ t("分类 ID", "Category ID") }}</dt><dd>{{ preview.config.category_id }}</dd></div>
          <div v-if="preview.config.follow_group_id"><dt>{{ t("同步来源群", "Follow Source Group") }}</dt><dd>{{ preview.config.follow_group_id }}</dd></div>
          <div><dt>{{ t("自动发送／间隔", "Automatic Sending / Interval") }}</dt><dd>{{ preview.config.enabled ? t("启用", "Enabled") : t("停用", "Disabled") }} / {{ preview.config.interval_minutes }} min</dd></div>
          <div><dt>{{ t("视频／剩余空间", "Video / Free Space") }}</dt><dd>{{ preview.config.max_video_mb }} MiB / {{ preview.config.min_free_gb }} GiB</dd></div>
        </dl>
        <p role="status">{{ preview.config.enabled && preview.config.follow_group_id
          ? t("确认后跟随来源群的新一轮发送，不补发历史。", "Confirmation follows new source rounds without historical replay.")
          : preview.config.enabled
          ? t("确认后按上述间隔自动发送到当前群。", "Confirmation schedules automatic delivery to this group.")
          : t("确认后停用自动发送，保留手动单条入口。", "Confirmation disables automatic delivery and keeps manual single-video sending.") }}</p>
        <div class="relay-actions">
          <button type="button" :disabled="Boolean(pending)" @click="cancelPreview">{{ t("取消", "Cancel") }}</button>
          <button type="button" class="primary" :disabled="Boolean(pending)" @click="execute('commit')"><Check :size="16" />{{ t("确认保存", "Confirm Save") }}</button>
        </div>
      </template>
    </dialog>
    <dialog ref="videoConfirmation" class="relay-confirmation" :aria-label="t('确认发送一条视频', 'Confirm One Video')" @cancel.prevent="executeVideo('cancel_video')">
      <template v-if="videoPreview">
        <header class="section-heading"><h2>{{ t("确认发送一条视频", "Confirm One Video") }}</h2>
          <button type="button" class="icon-button" :disabled="Boolean(pending)" :title="t('取消视频', 'Cancel Video')"
            :aria-label="t('取消视频', 'Cancel Video')" @click="executeVideo('cancel_video')"><X :size="18" /></button></header>
        <p class="relay-destination">{{ destination }}<small>{{ t("机器人账号", "Bot Account") }} {{ state?.destination.account }}</small></p>
        <dl class="relay-review">
          <div><dt>{{ t("标题", "Title") }}</dt><dd>{{ videoPreview.metadata?.title }}</dd></div>
          <div><dt>{{ t("来源编号", "Source ID") }}</dt><dd>{{ videoPreview.metadata?.vod_id }}</dd></div>
          <div v-if="videoPreview.metadata?.category_name || selectedCategory"><dt>{{ t("分类", "Category") }}</dt><dd>
            {{ videoPreview.metadata?.category_name || selectedCategory?.name }}
            <template v-if="videoPreview.metadata?.category_id"> · ID {{ videoPreview.metadata.category_id }}</template>
          </dd></div>
          <div><dt>{{ t("时长／大小", "Duration / Size") }}</dt><dd>{{ videoPreview.metadata?.duration }} s /
            {{ (videoPreview.metadata?.size || 0) < 1024 * 1024
              ? `${Math.ceil((videoPreview.metadata?.size || 0) / 1024)} KiB`
              : `${((videoPreview.metadata?.size || 0) / 1024 / 1024).toFixed(2)} MiB` }}</dd></div>
          <div><dt>{{ t("分辨率", "Resolution") }}</dt><dd>{{ videoPreview.metadata?.width }} × {{ videoPreview.metadata?.height }}</dd></div>
        </dl>
        <div class="relay-actions">
          <button type="button" :disabled="Boolean(pending)" @click="executeVideo('cancel_video')"><X :size="16" />{{ t("取消", "Cancel") }}</button>
          <button type="button" class="primary" :disabled="Boolean(pending)" @click="executeVideo('send_video')"><Send :size="16" />{{ t("确认发送", "Confirm Send") }}</button>
        </div>
      </template>
    </dialog>
  </section>
</template>

<style scoped>
.video-relay { min-width: 0; }
.section-heading { justify-content: space-between; flex-wrap: wrap; }
.section-heading h2 { display: flex; gap: 8px; align-items: center; margin: 0; }
h3 { font-size: 14px; margin: 24px 0 12px; }
.relay-status { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 18px; padding: 20px 0; margin: 0; border-bottom: 1px solid var(--border); }
dt, small { font-size: 12px; color: var(--muted); }
dd { margin: 4px 0 0; overflow-wrap: anywhere; }
.relay-destination { overflow-wrap: anywhere; }
.relay-destination small { display: block; margin-top: 4px; }
.relay-fields { border: 0; padding: 0; margin: 0; min-width: 0; }
.cms-source { display: grid; min-width: 0; gap: 6px; }
.cms-source { max-width: 620px; }
.cms-source input { width: 100%; min-width: 0; min-height: 38px; border: 1px solid var(--border); border-radius: 5px; padding: 8px 10px; background: var(--surface); color: inherit; font: inherit; }
.relay-actions, .relay-progress { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin: 18px 0; }
.relay-actions button { max-width: 100%; white-space: normal; overflow-wrap: anywhere; }
.relay-confirmation { width: min(520px, calc(100% - 32px)); max-height: calc(100dvh - 32px); overflow: auto; background: var(--surface); color: inherit; padding: 20px; border: 1px solid var(--border); border-radius: 6px; }
.relay-confirmation::backdrop { background: #10121588; }
.relay-review > div { padding: 8px 0; border-bottom: 1px solid var(--border); }
.relay-categories { margin-top: 8px; padding-top: 4px; }
.category-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.category-actions { display: flex; align-items: center; gap: 8px; }
.field-hint { font-size: 12px; color: var(--muted); margin: 8px 0 0; }
.danger-button { display: inline-flex; align-items: center; gap: 5px; min-height: 30px; padding: 5px 9px; border: 1px solid #b4534b; border-radius: 4px; background: transparent; color: #b4534b; font: inherit; font-size: 12px; cursor: pointer; }
.danger-button:disabled { opacity: .5; cursor: default; }
.category-heading h3 { margin: 12px 0 6px; }
.category-hint { font-size: 12px; color: var(--muted); margin: 0 0 12px; }
.category-search { display: grid; gap: 5px; max-width: 320px; font-size: 12px; color: var(--muted); margin-bottom: 12px; }
.category-search input { min-height: 36px; border: 1px solid var(--border); border-radius: 5px; padding: 7px 10px; background: var(--surface); color: inherit; font: inherit; }
.category-list { list-style: none; display: flex; flex-wrap: wrap; gap: 8px; padding: 0; margin: 0; }
.category-list button { display: inline-flex; align-items: baseline; gap: 8px; max-width: 100%; min-height: 34px; padding: 6px 10px; border: 1px solid var(--border); border-radius: 5px; background: var(--surface); color: inherit; cursor: pointer; white-space: normal; overflow-wrap: anywhere; text-align: left; }
.category-list button:disabled { opacity: .5; cursor: default; }
.category-list small { color: var(--muted); font-size: 11px; white-space: nowrap; }
@media (max-width: 480px) {
  .relay-status { grid-template-columns: minmax(0, 1fr); gap: 12px; }
  .category-list button { width: 100%; justify-content: space-between; }
}
</style>
