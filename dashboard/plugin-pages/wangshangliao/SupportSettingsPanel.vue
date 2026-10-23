<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { Check, Eye, Plus, RefreshCw, Save, ShieldCheck, Trash2, X } from "@lucide/vue";
import AIProfileEditor from "./AIProfileEditor.vue";
import { componentConstraints, componentField, type ComponentPage } from "./componentSchema";

const props = defineProps<{
  api: Function; account: string; disabled?: boolean;
  profiles?: any[]; choices?: { configs: any[]; personas: any[] }; providers?: any[];
}>();
const emit = defineEmits<{ saved: [] }>();
const settings = ref<any>({ wording: "", style: "简洁", length: 300, links: [], knowledge: [] });
const editors = ref<any[]>([]);
const version = ref(0);
const busy = ref(false);
const error = ref("");
const preview = ref<any>(null);
const uid = ref("");
const peer = ref("");
const owners = ref<any[]>([]);
const target = ref("legacy");
const heading = computed(() => target.value === "legacy" ? `账号 ${props.account} 的旧客服`
  : target.value === "template" ? "新开通者默认模板"
  : `${owners.value.find(row => row.uid === target.value)?.name || target.value} · 专属客服`);
const selectedOwner = computed(() => owners.value.find(row => row.uid === target.value));
const profileScope = computed(() => target.value === "legacy" ? props.account : `owner:${target.value}`);
const profile = computed(() => props.profiles?.find(row => row.kind === "consultation"
  && row.account === profileScope.value && !row.group_id));
const profileDraft = ref<Record<string, any>>({});
watch(profile, row => { profileDraft.value = { ...(row?.selection || {}) }; }, { immediate: true });
const callSettings = (operation: string, payload: Record<string, unknown> = {}) => target.value === "legacy"
  ? props.api("support_settings", { account: props.account, operation, ...payload })
  : props.api("owner_support", { uid: target.value === "template" ? "" : target.value, operation, ...payload });
const schema = ref<ComponentPage | null>(null);
const field = (name: string) => componentField(schema.value, name);
const constraints = (name: string) => componentConstraints(schema.value, name);
let generation = 0;
let pendingLoad = false;
async function run(task: () => Promise<void>) {
  if (busy.value) return;
  busy.value = true; error.value = "";
  try { await task(); } catch (e: any) { error.value = e?.message || "操作未确认，请刷新核对"; }
  finally {
    busy.value = false;
    if (pendingLoad) { pendingLoad = false; void run(load); }
  }
}
async function load() {
  const current = ++generation;
  preview.value = null;
  schema.value = null;
  const businesses = await props.api("owner_support", { operation: "list", account: props.account });
  if (current !== generation) return;
  owners.value = businesses.owners;
  if (target.value !== "legacy" && target.value !== "template"
      && !owners.value.some(owner => owner.uid === target.value)) {
    target.value = "legacy";
    return;
  }
  const [row, grants, declaration] = await Promise.all([
    callSettings("read"),
    target.value === "legacy" ? props.api("support_editor", { account: props.account, operation: "read" }) : Promise.resolve({ editors: [] }),
    props.api("page_schema", { name: "support_settings" }),
  ]);
  if (current !== generation) return;
  settings.value = row.config; version.value = row.version; editors.value = grants.editors;
  schema.value = declaration;
}
async function inspect() {
  const current = generation;
  const result = await callSettings("preview", {
    version: version.value, config: settings.value,
  });
  if (current === generation) preview.value = result;
}
async function commit() {
  const current = generation;
  await callSettings("commit", { token: preview.value.token });
  if (current === generation) await load();
}
async function grant(id: string, transport: string, active: boolean) {
  if (!window.confirm(`${active ? '授予' : '撤销'}账号${props.account}的客服编辑权：UID ${id}？`)) return;
  await props.api("support_editor", {
    account: props.account, operation: "save", uid: id, peer: transport, active,
    version: editors.value.find(row => row.uid === id)?.version || 0,
  });
  uid.value = ""; peer.value = ""; await load();
}
watch(() => props.account, () => {
  generation++; preview.value = null; editors.value = [];
  target.value = "legacy";
  if (props.account) {
    if (busy.value) pendingLoad = true;
    else void run(load);
  }
}, { immediate: true });
watch(target, () => {
  generation++; preview.value = null; schema.value = null;
  if (busy.value) pendingLoad = true;
  else void run(load);
});
async function changeState() {
  const owner = selectedOwner.value;
  if (!owner || !window.confirm(`${owner.active ? "停用" : "恢复"} ${owner.name} 的专属客服？`)) return;
  await props.api("owner_support", { operation: "state", uid: owner.uid, version: owner.version, active: !owner.active });
  await load();
}
async function saveProfile() {
  if (!profile.value?.selection || target.value === "template") return;
  const current = generation;
  const payload = { version: profile.value.version, selection: profileDraft.value };
  if (target.value === "legacy") {
    await props.api("ai_profile", { kind: "consultation", account: props.account, group_id: "", ...payload });
  } else {
    await callSettings("profile", payload);
  }
  if (current === generation) emit("saved");
}
async function provisionOwner() {
  const current = generation;
  await callSettings("provision");
  if (current !== generation) return;
  emit("saved");
  await load();
}
</script>

<template>
  <section aria-label="专属与账号业务客服设置" class="support-settings">
    <header><h3>{{ heading }}</h3>
      <button type="button" title="刷新客服设置" :disabled="busy || disabled" @click="run(load)"><RefreshCw :size="16" /></button>
    </header>
    <label>业务归属<select v-model="target" aria-label="业务归属" :disabled="busy || disabled">
      <option value="legacy">旧账号客服 · {{ account }}</option>
      <option value="template">新开通者默认模板</option>
      <option v-for="owner in owners" :key="owner.uid" :value="owner.uid">{{ owner.name }} · UID {{ owner.uid }}{{ owner.active ? "" : " · 已停用" }}</option>
    </select></label>
    <div v-if="selectedOwner" class="editor-row">
      <span>UID {{ selectedOwner.uid }} · {{ selectedOwner.active ? "有效" : "已停用" }}</span>
      <button type="button" :disabled="busy || disabled || !selectedOwner.active" @click="run(provisionOwner)"><Plus :size="16" />创建或核实专属配置</button>
      <label><input type="checkbox" :checked="Boolean(selectedOwner.active)" :disabled="busy || disabled" @change="run(changeState)" />有效</label>
    </div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <button v-if="target === 'legacy'" type="button" :disabled="busy || disabled"
      @click="run(async () => { await api('provision_profiles', { account }); emit('saved'); })">
      <ShieldCheck :size="16" />创建或核实账号客服配置
    </button>
    <fieldset v-if="target !== 'template' && profile?.selection" :disabled="busy || disabled">
      <legend>{{ heading }} · AI配置</legend>
      <AIProfileEditor v-model="profileDraft" :choices="choices || { configs: [], personas: [] }"
        :providers="providers || []" label="客服咨询" private-chat />
      <button type="button" @click="run(saveProfile)"><Save :size="16" />保存专属客服AI参数</button>
    </fieldset>
    <fieldset :disabled="busy || disabled || Boolean(preview) || !schema">
      <legend>业务客服 · 版本 {{ version }}</legend>
      <div class="support-fields">
        <label class="wide">业务话术<textarea v-model="settings.wording" rows="4" v-bind="constraints('wording')" /></label>
        <label>风格<select v-model="settings.style"><option v-for="style in field('style').enum" :key="style">{{ style }}</option></select></label>
        <label>回复长度（字）<input v-model.number="settings.length" type="number" v-bind="constraints('length')" /></label>
        <label class="wide">公开链接（每行一个）<textarea :value="settings.links.join('\n')" rows="3"
          @input="settings.links = ($event.target as HTMLTextAreaElement).value.split('\n').filter(v => v.trim())" /></label>
      </div>
      <div v-for="(_entry, index) in settings.knowledge" :key="index" class="knowledge-row">
        <label>业务知识 {{ index + 1 }}<textarea v-model="settings.knowledge[index]" rows="3" :maxlength="field('knowledge').items?.maxLength" /></label>
        <button type="button" title="删除客服知识" @click="settings.knowledge.splice(index, 1)"><Trash2 :size="16" /></button>
      </div>
      <button type="button" :disabled="!schema || settings.knowledge.length >= field('knowledge').maxItems" @click="settings.knowledge.push('')"><Plus :size="16" />添加业务知识</button>
    </fieldset>
    <button type="button" :disabled="busy || disabled || Boolean(preview) || !schema" @click="run(inspect)"><Eye :size="16" />预览客服修改</button>
    <div v-if="preview" role="alertdialog" aria-label="确认客服业务资料" class="confirmation">
      <h4>确认 {{ heading }}</h4>
      <p>{{ preview.config.wording }}</p>
      <p>{{ preview.config.style }} · {{ preview.config.length }}字</p>
      <p v-for="link in preview.config.links" :key="link">{{ link }}</p>
      <p v-for="(entry, index) in preview.config.knowledge" :key="index">{{ index + 1 }}. {{ entry }}</p>
      <button type="button" :disabled="busy || disabled" @click="run(commit)"><Check :size="16" />确认保存</button>
      <button type="button" :disabled="busy" @click="preview = null"><X :size="16" />取消</button>
    </div>
    <fieldset v-if="target === 'legacy'" :disabled="busy || disabled">
      <legend>旧账号客服负责人</legend>
      <div v-for="editor in editors.filter(row => row.active)" :key="editor.uid" class="editor-row">
        <span>UID {{ editor.uid }} · {{ editor.peer }}</span>
        <button type="button" title="撤销客服编辑权" @click="run(() => grant(editor.uid, editor.peer, false))"><Trash2 :size="16" /></button>
      </div>
      <div class="support-fields">
        <label>真实 UID<input v-model="uid" inputmode="numeric" /></label>
        <label>已核验的可信发送者身份<input v-model="peer" maxlength="256" /></label>
      </div>
      <button type="button" :disabled="!uid || !peer" @click="run(() => grant(uid, peer, true))"><ShieldCheck :size="16" />指定客服负责人</button>
    </fieldset>
  </section>
</template>

<style scoped>
header, .knowledge-row, .editor-row { display: flex; gap: 8px; align-items: center; justify-content: space-between; }
header, .editor-row { flex-wrap: wrap; }
h3, h4 { font-size: 16px; overflow-wrap: anywhere; min-width: 0; }
fieldset { border: 0; padding: 16px 0; min-width: 0; }
legend { font-weight: 600; }
.support-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-bottom: 12px; }
label { display: flex; flex-direction: column; gap: 4px; font-size: 14px; min-width: 0; }
.wide { grid-column: 1 / -1; }
.knowledge-row label { flex: 1; margin-bottom: 12px; }
input, select, textarea { min-width: 0; max-width: 100%; box-sizing: border-box; padding: 8px; border: 1px solid #b9c2ca; border-radius: 4px; background: transparent; color: inherit; font: inherit; }
button { display: inline-flex; align-items: center; gap: 6px; min-height: 34px; }
p, .editor-row span { white-space: pre-wrap; overflow-wrap: anywhere; }
.confirmation { margin: 12px 0; padding: 12px; border: 1px solid #829b88; border-radius: 6px; }
.error { color: #b42318; }
@media (max-width: 600px) { .support-fields { grid-template-columns: minmax(0, 1fr); } }
</style>
