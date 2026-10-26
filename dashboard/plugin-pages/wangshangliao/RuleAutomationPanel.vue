<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { ArrowDown, ArrowUp, Check, Eye, ImagePlus, LoaderCircle, Plus, RefreshCw, Trash2, X } from "@lucide/vue";
import { componentConstraints, componentField, type ComponentPage } from "./componentSchema";

const props = defineProps<{
  api: Function; account: string; group: string; tenantVersion: number; disabled?: boolean;
}>();
const config = ref<any>({ keywords: [], ads: [] });
const version = ref(0);
const busy = ref(false);
const error = ref("");
const preview = ref<any>(null);
const schedules = ref<any[]>([]);
const authorizationRequired = ref(false);
const images = ref<Record<string, string>>({});
const uploadStatus = ref("");
const schema = ref<ComponentPage | null>(null);
const staggerIds = ref(["", "", ""]);
const staggerMinutes = ref([0, 20, 40]);
const staggerStatus = ref("");
const staggerGaps = computed(() => staggerMinutes.value.map((minute, index, items) =>
  (items[(index + 1) % 3] - minute + 60) % 60,
));
const field = (name: string, definition?: string) => componentField(schema.value, name, definition);
const constraints = (name: string, definition?: string) => componentConstraints(schema.value, name, definition);
let generation = 0;
let operation = 0;
const scope = () => ({
  account: props.account, group_id: props.group, tenant_version: props.tenantVersion,
});
async function run(task: () => Promise<void>, replace = false) {
  if (busy.value && !replace) return;
  const current = ++operation;
  busy.value = true; error.value = "";
  try { await task(); } catch (e: any) { if (current === operation) error.value = e?.message || "操作未确认，请刷新核对"; }
  finally { if (current === operation) busy.value = false; }
}
async function load() {
  const current = ++generation;
  preview.value = null;
  uploadStatus.value = "";
  schema.value = null;
  const [row, declaration] = await Promise.all([
    props.api("business_rules", { ...scope(), operation: "read" }),
    props.api("page_schema", { name: "business_rules" }),
  ]);
  if (current !== generation) return;
  config.value = row.config; version.value = row.version;
  authorizationRequired.value = row.version > 0 && row.tenant_version !== props.tenantVersion;
  schema.value = declaration;
  schedules.value = row.schedules || [];
  images.value = row.images || {};
  for (const rule of [...config.value.keywords, ...config.value.ads]) rule.images ||= [];
  staggerIds.value = [0, 1, 2].map(index =>
    config.value.ads.some((ad: any) => ad.id === staggerIds.value[index])
      ? staggerIds.value[index] : config.value.ads[index]?.id || "",
  );
  const selected = staggerIds.value.map(id => config.value.ads.find((ad: any) => ad.id === id));
  if (selected.every(ad => ad?.mode === "daily" && ad.times.length === 24
    && ad.times.every((clock: string, hour: number) =>
      clock === `${String(hour).padStart(2, "0")}:${ad.times[0].slice(3)}`,
    ))) {
    staggerMinutes.value = selected.map(ad => Number(ad.times[0].slice(3)));
  }
  staggerStatus.value = "";
}
function add(kind: "keywords" | "ads") {
  const rule = kind === "keywords" ? {
    id: crypto.randomUUID(), name: "", enabled: false, match: "exact",
    terms: [], text: "", images: [], cooldown_seconds: 30,
  } : {
    id: crypto.randomUUID(), name: "", enabled: false, text: "", mode: "daily",
    times: [], start: "09:00", end: "22:00", interval_minutes: 60, images: [],
  };
  config.value[kind].push(rule);
  return rule;
}
function applyStagger() {
  error.value = ""; staggerStatus.value = "";
  const minutes = staggerMinutes.value;
  const selected = staggerIds.value.map(id => config.value.ads.find((ad: any) => ad.id === id));
  if (selected.some(ad => !ad) || new Set(staggerIds.value).size !== 3) {
    error.value = "请选择三个不同的发言计划"; return;
  }
  if (minutes.some(minute => !Number.isInteger(minute) || minute < 0 || minute > 59)
    || staggerGaps.value.some(gap => gap === 0)
    || staggerGaps.value.reduce((sum, gap) => sum + gap, 0) !== 60) {
    error.value = "分钟须为0至59的整数，三条按顺序错开且一轮合计60分钟"; return;
  }
  selected.forEach((ad, index) => {
    ad.mode = "daily";
    ad.times = Array.from({ length: 24 }, (_, hour) =>
      `${String(hour).padStart(2, "0")}:${String(minutes[index]).padStart(2, "0")}`,
    );
  });
  staggerStatus.value = "三条时间已填入草稿，启用状态保持不变；预览确认保存后生效";
}
function addStagger() {
  if (!schema.value || config.value.ads.length + 3 > field("ads").maxItems) return;
  staggerMinutes.value = [0, 20, 40];
  staggerIds.value = [1, 2, 3].map(index => {
    const ad = add("ads");
    ad.name = `定时内容${index}`;
    return ad.id;
  });
  applyStagger();
}
function move(items: any[], index: number, direction: number) {
  [items[index], items[index + direction]] = [items[index + direction], items[index]];
}
async function inspect() {
  const current = generation;
  const row = await props.api("business_rules", {
    ...scope(), operation: "preview", version: version.value, config: config.value,
  });
  if (current === generation) preview.value = row;
}
async function commit() {
  const current = generation;
  await props.api("business_rules", { ...scope(), operation: "commit", token: preview.value.token });
  if (current === generation) await load();
}
async function upload(ad: any, files: File[], index?: number, kind: "keywords" | "ads" = "ads") {
  if (busy.value || props.disabled || preview.value || !schema.value || !files.length) return;
  const limit = kind === "keywords" ? 1 : 3;
  if (files.length + (index === undefined ? ad.images.length : ad.images.length - 1) > limit) {
    error.value = kind === "keywords" ? "每条关键词回复最多上传1张配图" : "每个计划最多上传3张图片"; return;
  }
  const current = generation;
  const target = scope();
  await run(async () => {
    for (const [fileIndex, file] of files.entries()) {
      if (!["image/jpeg", "image/png", "image/webp"].includes(file.type) || file.size > 5 * 1024 * 1024) {
        throw new Error("请选择5MB以内的JPG、PNG或WebP静态图片");
      }
      uploadStatus.value = `正在读取第${fileIndex + 1}/${files.length}张图片`;
      const data = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        const timer = window.setTimeout(() => {
          reject(new Error("图片读取超时，请重新选择图片"));
          reader.abort();
        }, 15000);
        reader.onload = () => { clearTimeout(timer); resolve(String(reader.result).split(",", 2)[1]); };
        reader.onerror = reader.onabort = () => { clearTimeout(timer); reject(new Error("图片读取失败或已取消")); };
        reader.readAsDataURL(file);
      });
      if (current !== generation) return;
      uploadStatus.value = `正在上传第${fileIndex + 1}/${files.length}张图片`;
      let timer: ReturnType<typeof setTimeout> | undefined;
      let result: any;
      try {
        result = await Promise.race([
          props.api("business_rules", { ...target, operation: "image_upload", data }),
          new Promise((_, reject) => {
            timer = setTimeout(() => reject(new Error("图片上传超时，本次未加入计划；请刷新核对后重试")), 45000);
          }),
        ]);
      } finally { clearTimeout(timer); }
      if (current !== generation || !config.value[kind].includes(ad)) return;
      images.value[result.id] = result.thumbnail;
      if (index === undefined) ad.images.push(result.id);
      else ad.images.splice(index, 1, result.id);
      preview.value = null;
    }
    uploadStatus.value = `已添加${files.length}张图片，尚未保存`;
  });
  if (current === generation && error.value) uploadStatus.value = "";
}
function selectImages(ad: any, event: Event, index?: number, kind: "keywords" | "ads" = "ads") {
  const input = event.target as HTMLInputElement;
  const files = Array.from(input.files || []);
  input.value = "";
  void upload(ad, files, index, kind);
}
function receiveImages(ad: any, event: ClipboardEvent | DragEvent, kind: "keywords" | "ads" = "ads") {
  const transfer = "clipboardData" in event ? event.clipboardData : event.dataTransfer;
  const files = Array.from(transfer?.files || []);
  if (!files.length) return;
  event.preventDefault();
  void upload(ad, files, undefined, kind);
}
function deliveryError(id: string) {
  const receipt = schedules.value.find(item => item.rule_id === id);
  if (!receipt || receipt.blocked) return "";
  if (["rejected", "not_sent"].includes(receipt.last_state)) {
    return "最近一轮发送被拒绝或未提交，未自动重试。请核对机器人连接及发送权限。";
  }
  if (receipt.last_state !== "skipped") return "";
  if (receipt.last_error === "advertisement_slot_expired") {
    return "最近一轮超过发送等待时限，已跳过，不会补发。";
  }
  if (receipt.last_error === "advertisement_before_runtime") {
    return "最近一轮早于本次服务启动，已跳过，不会补发。";
  }
  return "最近一轮发送前校验未通过，未发送。请刷新核对群权限、配置授权和机器人连接。";
}
watch(() => [props.account, props.group, props.tenantVersion], () => {
  generation++; preview.value = null; config.value = { keywords: [], ads: [] };
  schedules.value = []; images.value = {}; uploadStatus.value = "";
  authorizationRequired.value = false;
  staggerIds.value = ["", "", ""]; staggerMinutes.value = [0, 20, 40]; staggerStatus.value = "";
  if (props.account && props.group) void run(load, true);
}, { immediate: true });
onBeforeUnmount(() => { generation++; operation++; });
</script>

<template>
  <section class="automation" aria-label="群关键词回复与定时发言">
    <header><h3>关键词回复与定时发言</h3>
      <button type="button" title="刷新业务规则" :disabled="busy || disabled" @click="run(load)"><RefreshCw :size="16" /></button>
    </header>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="authorizationRequired" class="error" role="status">群配置授权已变化，已保存的关键词回复与定时发言暂不执行。请核对后重新预览确认保存。</p>
    <p v-if="uploadStatus" class="upload-status" role="status" aria-live="polite"><LoaderCircle v-if="busy" :size="16" class="spin" />{{ uploadStatus }}</p>
    <fieldset :disabled="busy || disabled || Boolean(preview) || !schema">
      <legend>群关键词自动回复</legend>
      <article v-for="(rule, index) in config.keywords" :key="rule.id" class="rule-row">
        <div class="rule-head">
          <strong>{{ index + 1 }}</strong>
          <label>名称<input v-model="rule.name" v-bind="constraints('name', 'KeywordReply')" /></label>
          <label class="check"><input v-model="rule.enabled" type="checkbox" />启用</label>
          <button type="button" title="上移关键词规则" :disabled="index === 0" @click="move(config.keywords, index, -1)"><ArrowUp :size="16" /></button>
          <button type="button" title="下移关键词规则" :disabled="index === config.keywords.length - 1" @click="move(config.keywords, index, 1)"><ArrowDown :size="16" /></button>
          <button type="button" title="删除关键词规则" @click="config.keywords.splice(index, 1)"><Trash2 :size="16" /></button>
        </div>
        <div class="rule-fields">
          <label>匹配<select v-model="rule.match"><option v-for="mode in field('match', 'KeywordReply').enum" :key="mode" :value="mode">{{ mode === 'exact' ? '整句精确' : '包含' }}</option></select></label>
          <label>同义词（每行一个）<textarea :value="rule.terms.join('\n')" rows="3"
            @input="rule.terms = ($event.target as HTMLTextAreaElement).value.split('\n').filter(v => v.trim())" /></label>
          <label>冷却（秒）<input v-model.number="rule.cooldown_seconds" type="number" v-bind="constraints('cooldown_seconds', 'KeywordReply')" /></label>
          <label class="wide">回复正文<textarea v-model="rule.text" rows="3" v-bind="constraints('text', 'KeywordReply')" /></label>
          <div class="wide media-editor" role="group" aria-label="关键词回复配图" tabindex="0"
            title="上传、粘贴或拖入图片" @paste="receiveImages(rule, $event, 'keywords')"
            @dragover.prevent @drop="receiveImages(rule, $event, 'keywords')">
            <div v-if="rule.images.length" class="media-list">
              <div class="media-item">
                <img :src="images[rule.images[0]]" alt="关键词回复配图" />
                <div class="actions">
                  <button type="button" title="替换关键词配图"
                    @click="(($event.currentTarget as HTMLButtonElement).nextElementSibling as HTMLInputElement).click()"><ImagePlus :size="16" /></button>
                  <input class="file-input" type="file" accept="image/jpeg,image/png,image/webp" aria-label="替换关键词配图"
                    @change="selectImages(rule, $event, 0, 'keywords')" />
                  <button type="button" title="移除关键词配图" @click="rule.images.splice(0, 1)"><Trash2 :size="16" /></button>
                </div>
              </div>
            </div>
            <template v-else>
              <button type="button" class="upload-button"
                @click="(($event.currentTarget as HTMLButtonElement).nextElementSibling as HTMLInputElement).click()"><ImagePlus :size="16" />上传配图</button>
              <input class="file-input" type="file" accept="image/jpeg,image/png,image/webp" aria-label="上传关键词配图"
                @change="selectImages(rule, $event, undefined, 'keywords')" />
            </template>
            <p class="media-note">最多1张配图，先图后文。单张不超过5MB，正文可留空。</p>
          </div>
        </div>
      </article>
      <button type="button" :disabled="!schema || config.keywords.length >= field('keywords').maxItems" @click="add('keywords')"><Plus :size="16" />添加关键词回复</button>
    </fieldset>
    <fieldset :disabled="busy || disabled || Boolean(preview) || !schema">
      <legend>定时发言 · 北京时间</legend>
      <div class="stagger-editor" role="group" aria-label="三条消息错峰排程">
        <h4>三条消息 · 每小时循环</h4>
        <div class="stagger-slots">
          <div v-for="(_, index) in staggerIds" :key="index" class="stagger-slot">
            <label>第{{ index + 1 }}条计划<select v-model="staggerIds[index]">
              <option value="">选择计划</option>
              <option v-for="(ad, adIndex) in config.ads" :key="ad.id" :value="ad.id">{{ ad.name || `未命名计划${adIndex + 1}` }}</option>
            </select></label>
            <label>第{{ index + 1 }}条 · 每小时第几分<input v-model.number="staggerMinutes[index]" type="number" min="0" max="59" step="1" /></label>
          </div>
        </div>
        <p class="media-note">相邻间隔：{{ staggerGaps.join(' / ') }} 分钟；一轮60分钟，每条每小时一次，全天循环。</p>
        <div class="actions">
          <button type="button" @click="applyStagger"><Check :size="16" />应用三条时间</button>
          <button type="button" :disabled="!schema || config.ads.length + 3 > field('ads').maxItems" @click="addStagger"><Plus :size="16" />新增三条计划</button>
        </div>
        <p v-if="staggerStatus" role="status" class="media-note">{{ staggerStatus }}</p>
      </div>
      <article v-for="(ad, index) in config.ads" :key="ad.id" class="rule-row">
        <div class="rule-head">
          <label>名称<input v-model="ad.name" v-bind="constraints('name', 'Advertisement')" /></label>
          <label class="check"><input v-model="ad.enabled" type="checkbox" />启用</label>
          <button type="button" title="删除广告计划" @click="config.ads.splice(index, 1)"><Trash2 :size="16" /></button>
        </div>
        <div class="rule-fields">
          <label>发送计划<select v-model="ad.mode"><option value="daily">每日固定时刻</option><option value="interval">每日时段内按间隔</option></select></label>
          <label v-if="ad.mode === 'daily'">时刻（逗号分隔）<input :value="ad.times.join(',')" placeholder="09:00,18:00"
            @input="ad.times = ($event.target as HTMLInputElement).value.split(',').map(v => v.trim()).filter(Boolean)" /></label>
          <template v-else>
            <label>开始<input v-model="ad.start" type="time" /></label>
            <label>结束<input v-model="ad.end" type="time" /></label>
            <label>间隔（分钟）<input v-model.number="ad.interval_minutes" type="number" v-bind="constraints('interval_minutes', 'Advertisement')" /></label>
          </template>
          <label class="wide">文字与链接<textarea v-model="ad.text" rows="3" v-bind="constraints('text', 'Advertisement')" /></label>
          <div class="wide media-editor" role="group" aria-label="定时发言图片" tabindex="0"
            title="上传、粘贴或拖入图片" @paste="receiveImages(ad, $event)"
            @dragover.prevent @drop="receiveImages(ad, $event)">
            <div class="media-list">
              <div v-for="(image, imageIndex) in ad.images" :key="image" class="media-item">
                <img :src="images[image]" :alt="`轮换图片 ${imageIndex + 1}`" />
                <div class="actions">
                  <button type="button" :title="`替换第${imageIndex + 1}张图片`"
                    @click="(($event.currentTarget as HTMLButtonElement).nextElementSibling as HTMLInputElement).click()">
                    <ImagePlus :size="16" />
                  </button><input class="file-input" type="file" accept="image/jpeg,image/png,image/webp"
                      :aria-label="`替换第${imageIndex + 1}张图片`"
                      @change="selectImages(ad, $event, imageIndex)" />
                  <button type="button" :title="`上移第${imageIndex + 1}张图片`" :disabled="imageIndex === 0" @click="move(ad.images, imageIndex, -1)"><ArrowUp :size="16" /></button>
                  <button type="button" :title="`移除第${imageIndex + 1}张图片`" @click="ad.images.splice(imageIndex, 1)"><Trash2 :size="16" /></button>
                </div>
              </div>
            </div>
            <template v-if="ad.images.length < 3">
              <button type="button" class="upload-button"
                @click="(($event.currentTarget as HTMLButtonElement).nextElementSibling as HTMLInputElement).click()">
                <ImagePlus :size="16" />上传图片（{{ ad.images.length }}/3）
              </button>
              <input class="file-input" type="file" aria-label="上传轮换图片" accept="image/jpeg,image/png,image/webp" multiple @change="selectImages(ad, $event)" />
            </template>
            <p class="media-note">每次发送一张，按排列顺序循环。图片可替换或删除；单张不超过5MB。文字可留空。</p>
          </div>
        </div>
        <p v-if="schedules.find(s => s.rule_id === ad.id)?.blocked" class="error" role="status">发送结果未知或未完整送达，已暂停。请先核对群内实际消息，再重新预览确认。</p>
        <p v-else-if="deliveryError(ad.id)" class="error" role="status">{{ deliveryError(ad.id) }}</p>
      </article>
      <button type="button" :disabled="!schema || config.ads.length >= field('ads').maxItems" @click="add('ads')"><Plus :size="16" />添加发言计划</button>
    </fieldset>
    <button type="button" :disabled="busy || disabled || Boolean(preview) || !schema" @click="run(inspect)"><Eye :size="16" />预览修改</button>
    <div v-if="preview" class="rule-confirmation" role="alertdialog" aria-label="确认业务规则">
      <h4>确认本群业务规则 · 版本 {{ preview.version }}</h4>
      <p v-for="rule in preview.config.keywords" :key="rule.id">
        {{ rule.enabled ? '开启' : '关闭' }} · {{ rule.name }} · {{ rule.match === 'exact' ? '整句精确' : '包含' }} · {{ rule.terms.join(' / ') }}<br />{{ rule.text }}
      </p>
      <p v-for="ad in preview.config.ads" :key="ad.id">
        {{ ad.enabled ? '开启' : '关闭' }} · {{ ad.name }} · {{ ad.mode === 'daily' ? ad.times.join('、') : `${ad.start}-${ad.end} / ${ad.interval_minutes}分钟` }}<br />{{ ad.text }}
      </p>
      <div v-for="ad in [...preview.config.keywords, ...preview.config.ads].filter((item: any) => item.images?.length)" :key="ad.id" class="media-list">
        <img v-for="(image, index) in ad.images" :key="image" :src="images[image]" :alt="`${ad.name}第${index + 1}张`" class="preview-image" />
      </div>
      <div class="actions">
        <button type="button" :disabled="busy || disabled" @click="run(commit)"><Check :size="16" />确认保存</button>
        <button type="button" :disabled="busy" @click="preview = null"><X :size="16" />取消</button>
      </div>
    </div>
  </section>
</template>

<style scoped>
.automation { margin-top: 24px; border-top: 1px solid var(--border, #d9dde1); padding-top: 16px; }
header, .rule-head, .actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
header { justify-content: space-between; }
h3, h4 { font-size: 16px; margin: 8px 0; }
fieldset { border: 0; padding: 12px 0; min-width: 0; }
legend { font-weight: 600; }
.rule-row { border-bottom: 1px solid var(--border, #d9dde1); padding: 12px 0; }
.rule-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin: 12px 0; }
label { display: flex; flex-direction: column; gap: 4px; min-width: 0; font-size: 14px; }
label.check { flex-direction: row; align-items: center; }
input, select, textarea { min-width: 0; max-width: 100%; box-sizing: border-box; padding: 8px; border: 1px solid #b9c2ca; border-radius: 4px; color: inherit; background: transparent; font: inherit; }
.wide { grid-column: 1 / -1; }
button { display: inline-flex; align-items: center; gap: 6px; min-height: 34px; }
.rule-confirmation { padding: 12px; margin-top: 12px; border: 1px solid #829b88; border-radius: 6px; }
p { white-space: pre-wrap; overflow-wrap: anywhere; }
.error { color: #b42318; }
.media-editor { min-width: 0; }
.media-list { display: flex; flex-wrap: wrap; gap: 12px; margin: 8px 0; }
.media-item { width: 128px; max-width: 100%; }
.media-item > img, .preview-image { width: 128px; height: 96px; object-fit: contain; background: #f3f5f6; border: 1px solid #d9dde1; border-radius: 4px; }
.upload-button { cursor: pointer; min-height: 34px; }
.file-input { display: none; }
.upload-status { display: flex; align-items: center; gap: 6px; font-size: 14px; }
.media-editor:focus-visible { outline: 2px solid #176f62; outline-offset: 4px; }
.spin { animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.media-note { font-size: 12px; color: #5b6770; }
.stagger-editor { padding: 0 0 16px; border-bottom: 1px solid var(--border, #d9dde1); }
.stagger-slots { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.stagger-slot { display: grid; gap: 8px; min-width: 0; }
@media (max-width: 600px) { .stagger-slots { grid-template-columns: minmax(0, 1fr); } }
@media (max-width: 600px) { .rule-fields { grid-template-columns: minmax(0, 1fr); } }
</style>
