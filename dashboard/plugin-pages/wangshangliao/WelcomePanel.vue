<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from "vue";
import { Check, Eye, RefreshCw, X } from "@lucide/vue";

const props = defineProps<{
  api: Function; account: string; group: string; tenantVersion: number; disabled?: boolean;
}>();
const config = ref({ enabled: false, text: "欢迎加入本群！" });
const version = ref(0);
const busy = ref(false);
const ready = ref(false);
const error = ref("");
const notice = ref("");
const preview = ref<any>(null);
const last = ref<any>(null);
const authorizationRequired = ref(false);
let generation = 0;
const scope = () => ({
  account: props.account, group_id: props.group, tenant_version: props.tenantVersion,
});
async function run(operation: "read" | "preview" | "commit") {
  if (busy.value || (props.disabled && operation !== "read")) return;
  const current = generation;
  busy.value = true; error.value = ""; notice.value = "";
  const target = scope();
  try {
    const result = await props.api("group_welcome", {
      ...target, operation,
      ...(operation === "preview" ? { version: version.value, config: { ...config.value } } : {}),
      ...(operation === "commit" ? { token: preview.value.token } : {}),
    });
    if (current !== generation) return;
    if (operation === "preview") preview.value = result;
    else {
      const saved = operation === "read" ? result
        : await props.api("group_welcome", { ...target, operation: "read" });
      if (current !== generation) return;
      config.value = saved.config; version.value = saved.version;
      last.value = saved.last; ready.value = true; preview.value = null;
      authorizationRequired.value = Boolean(saved.authorization_required);
      if (operation === "commit") notice.value = "欢迎语已保存";
    }
  } catch (e: any) {
    if (current === generation) error.value = e?.message || "欢迎语操作未确认，请刷新核对";
  } finally {
    if (current === generation) busy.value = false;
  }
}
watch(() => [props.account, props.group, props.tenantVersion], () => {
  generation++; busy.value = false; ready.value = false; preview.value = null;
  last.value = null; error.value = ""; notice.value = "";
  authorizationRequired.value = false;
  config.value = { enabled: false, text: "欢迎加入本群！" };
  if (props.account && props.group) void run("read");
}, { immediate: true });
onBeforeUnmount(() => { generation++; });
</script>

<template>
  <section class="welcome" aria-label="入群欢迎">
    <header>
      <h3>入群欢迎</h3>
      <button type="button" title="刷新欢迎语" :disabled="busy || disabled"
        @click="preview = null; run('read')"><RefreshCw :size="16" /></button>
    </header>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="authorizationRequired" class="error" role="status">入群授权已变化，欢迎语暂不执行；请重新预览确认保存。</p>
    <fieldset :disabled="busy || disabled || !ready || Boolean(preview)">
      <label class="check"><input v-model="config.enabled" type="checkbox" />启用入群欢迎</label>
      <label>欢迎语<textarea v-model="config.text" maxlength="300" rows="3" /></label>
      <dl><div><dt>撤回倒计时</dt><dd>20秒</dd></div>
        <div><dt>入群合并窗口</dt><dd>2秒</dd></div>
        <div><dt>旧欢迎语</dt><dd>新消息发送成功后撤回</dd></div>
      </dl>
      <button type="button" :disabled="config.enabled && !config.text.trim()"
        @click="run('preview')"><Eye :size="16" />预览修改</button>
    </fieldset>
    <p v-if="last && ['unknown', 'rejected'].includes(last.state)" class="error" role="status">
      {{ last.state === 'unknown' ? '最近一次欢迎语发送结果未知，未自动重发，请核对群消息。'
        : '最近一次欢迎语未发送成功，请核对机器人在线状态和群权限。' }}
    </p>
    <div v-if="preview" class="confirmation" role="alertdialog" aria-label="确认入群欢迎">
      <strong>{{ preview.config.enabled ? '启用入群欢迎' : '关闭入群欢迎' }}</strong>
      <p>@新成员 {{ preview.config.text }}</p>
      <dl><div><dt>撤回倒计时</dt><dd>20秒</dd></div></dl>
      <div class="actions">
        <button type="button" :disabled="busy || disabled" @click="run('commit')"><Check :size="16" />确认保存</button>
        <button type="button" :disabled="busy" @click="preview = null"><X :size="16" />取消</button>
      </div>
    </div>
  </section>
</template>

<style scoped>
.welcome { margin-top: 24px; border-top: 1px solid var(--border, #d9dde1); padding-top: 16px; }
header, .actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
header { justify-content: space-between; }
h3 { font-size: 16px; margin: 8px 0; }
fieldset { display: grid; gap: 12px; border: 0; padding: 12px 0; min-width: 0; }
label { display: flex; flex-direction: column; gap: 4px; min-width: 0; font-size: 14px; }
.check { flex-direction: row; align-items: center; }
textarea { width: 100%; min-width: 0; box-sizing: border-box; padding: 8px; border: 1px solid #b9c2ca; border-radius: 4px; color: inherit; background: transparent; font: inherit; }
button { display: inline-flex; align-items: center; gap: 6px; min-height: 34px; justify-self: start; }
dl { margin: 0; font-size: 13px; }
dl > div { display: flex; gap: 12px; flex-wrap: wrap; padding: 3px 0; }
dt { color: #5b6770; min-width: 7em; }
dd { margin: 0; }
p, dd { overflow-wrap: anywhere; white-space: pre-wrap; }
.error { color: #b42318; }
.confirmation { padding: 12px; border: 1px solid #829b88; border-radius: 6px; }
</style>
