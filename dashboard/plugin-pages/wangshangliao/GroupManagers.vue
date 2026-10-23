<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { LoaderCircle, RefreshCw, Save } from "@lucide/vue";

const props = defineProps<{
  bridge: {
    apiGet(endpoint: string, params?: Record<string, string>): Promise<any>;
    apiPost(endpoint: string, body: unknown): Promise<any>;
  };
  botId: string;
  groupId: string;
  locale: string;
  disabled: boolean;
}>();
const emit = defineEmits<{ (event: "dirty", value: boolean): void }>();
const t = (zh: string, en: string) => props.locale.startsWith("zh") ? zh : en;
const members = ref<Array<{ id: string; name: string }>>([]);
const selected = ref<string[]>([]);
const original = ref("[]");
const search = ref("");
const version = ref(0);
const revision = ref("");
const busy = ref(false);
const ready = ref(false);
const error = ref("");
const notice = ref("");
let sequence = 0;
const dirty = computed(() => JSON.stringify([...selected.value].sort()) !== original.value);
const visible = computed(() => members.value.filter(m =>
  selected.value.includes(m.id) || `${m.name} ${m.id}`.toLowerCase().includes(search.value.toLowerCase())
));
const invalid = computed(() => ready.value ? selected.value.filter(
  uid => !members.value.some(m => m.id === uid),
) : []);
watch(dirty, value => emit("dirty", value));

async function load() {
  const current = ++sequence;
  const scope = { bot_id: props.botId, group_id: props.groupId };
  ready.value = false;
  busy.value = true;
  error.value = "";
  notice.value = "";
  selected.value = [];
  original.value = "[]";
  members.value = [];
  try {
    if (!scope.bot_id || !scope.group_id) return;
    const data = await props.bridge.apiGet("group-managers", scope);
    if (current !== sequence) return;
    members.value = data.members;
    selected.value = [...data.users];
    original.value = JSON.stringify([...data.users].sort());
    version.value = data.version;
    revision.value = data.revision;
    ready.value = true;
  } catch (err) {
    if (current === sequence) error.value = err instanceof Error ? err.message : t("群管理员读取失败", "Group managers unavailable");
  } finally {
    if (current === sequence) busy.value = false;
  }
}

async function save() {
  if (busy.value || !ready.value) return;
  const current = sequence;
  const users = [...selected.value];
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    const data = await props.bridge.apiPost("group-managers", {
      bot_id: props.botId, group_id: props.groupId, users,
      version: version.value, revision: revision.value,
    });
    if (current !== sequence) return;
    version.value = data.version;
    original.value = JSON.stringify([...users].sort());
    notice.value = t("群管理员已保存", "Group managers saved");
  } catch (err) {
    if (current === sequence) {
      ready.value = false;
      error.value = err instanceof Error ? err.message : t("保存失败，请刷新核对", "Save failed; refresh to verify");
    }
  } finally {
    if (current === sequence) busy.value = false;
  }
}
watch(() => [props.botId, props.groupId], load, { immediate: true });
onBeforeUnmount(() => { sequence++; emit("dirty", false); });
</script>

<template>
  <div class="group-managers">
    <div class="manager-heading">
      <h2>{{ t("群管理员", "Group Managers") }}</h2>
      <button type="button" :disabled="busy || disabled" :title="t('刷新群管理员', 'Refresh group managers')" :aria-label="t('刷新群管理员', 'Refresh group managers')" @click="load">
        <RefreshCw :size="17" />
      </button>
    </div>
    <p v-if="error" role="alert" class="message error">{{ error }}</p>
    <p v-if="notice" role="status" class="message success">{{ notice }}</p>
    <div v-if="invalid.length" class="message error" role="alert">
      <p>{{ t("旧授权不再符合群管理身份，请移除后保存。", "Remove outdated grants before saving.") }}</p>
      <label v-for="uid in invalid" :key="uid">
        <input v-model="selected" type="checkbox" :value="uid" :disabled="busy || disabled" />{{ uid }}
      </label>
    </div>
    <label class="manager-search">
      {{ t("查找群主或管理员", "Find Owner or Administrator") }}
      <input v-model="search" type="search" :disabled="busy || disabled || !ready" />
    </label>
    <div class="manager-members" :aria-busy="busy">
      <label v-for="member in visible" :key="member.id">
        <input v-model="selected" type="checkbox" :value="member.id" :disabled="busy || disabled || !ready || (!selected.includes(member.id) && selected.length >= 20)" />
        <span>{{ member.name }}<small>{{ member.id }}</small></span>
      </label>
      <p v-if="!visible.length && !busy">{{ t("暂无匹配成员", "No Matching Members") }}</p>
    </div>
    <div class="manager-footer">
      <span>{{ selected.length }} / 20</span>
      <button type="button" :disabled="busy || disabled || !ready || !dirty || Boolean(invalid.length)" @click="save">
        <LoaderCircle v-if="busy" :size="17" /><Save v-else :size="17" />
        {{ t("保存群管理员", "Save Group Managers") }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.group-managers { padding: 0 0 24px; border-bottom: 1px solid var(--border); min-width: 0; }
.manager-heading, .manager-footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.manager-heading h2 { margin: 0; }
.manager-search { display: grid; gap: 6px; margin: 16px 0 8px; }
.manager-search input { width: 100%; min-height: 38px; padding: 8px 10px; border: 1px solid var(--border); border-radius: 5px; color: inherit; background: var(--surface); }
.manager-members { max-height: 260px; overflow: auto; min-height: 64px; margin-bottom: 12px; }
.manager-members label { display: flex; gap: 12px; align-items: center; padding: 8px 0; min-height: 48px; }
.manager-members input { width: 17px; height: 17px; flex-shrink: 0; accent-color: var(--accent); }
.manager-members span { min-width: 0; overflow-wrap: anywhere; }
.manager-members small { display: block; color: var(--muted); font-size: 12px; }
.manager-footer { flex-wrap: wrap; font-size: 12px; }
</style>
