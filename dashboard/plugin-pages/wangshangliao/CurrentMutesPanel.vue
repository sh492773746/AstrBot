<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
import { LoaderCircle, RefreshCw, ShieldCheck, Volume2, X } from "@lucide/vue";

const props = defineProps<{
  bridge: { apiPost: (endpoint: string, body: unknown) => Promise<any> };
  account: string;
  group: string;
  locale: string;
}>();
const t = (zh: string, en: string) => props.locale.startsWith("zh") ? zh : en;
type Member = { id: string; name: string; can_unmute: boolean };
const members = ref<Member[]>([]);
const version = ref(0);
const checkedAt = ref(0);
const busy = ref(false);
const error = ref("");
const notice = ref("");
const loaded = ref(false);
const pending = ref<{ member: Member; confirmation: string; version: number } | null>(null);
let active = true;
onBeforeUnmount(() => { active = false; });
async function api(payload: Record<string, unknown>) {
  return props.bridge.apiPost("open-service", {
    action: "member_mutes",
    payload: { account: props.account, group_id: props.group, ...payload },
  });
}
async function read() {
  const result = await api({ operation: "read" });
  if (!active) return;
  members.value = result.members;
  version.value = result.version;
  checkedAt.value = result.checked_at;
  loaded.value = true;
}
async function run(task: () => Promise<void>) {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  notice.value = "";
  try { await task(); }
  catch (e: any) {
    if (active) error.value = e?.message || t("平台状态未确认，请刷新核对", "Platform state unconfirmed; refresh to check");
  } finally { if (active) busy.value = false; }
}
async function refresh() {
  pending.value = null;
  loaded.value = false;
  members.value = [];
  await read();
}
async function preview(member: Member) {
  pending.value = null;
  const result = await api({ operation: "preview", version: version.value, member: member.id });
  if (active) pending.value = { member, confirmation: result.confirmation, version: version.value };
}
async function commit() {
  const selected = pending.value;
  if (!selected) return;
  pending.value = null;
  const result = await api({
    operation: "commit", version: selected.version, member: selected.member.id,
    confirmation: selected.confirmation,
  });
  if (!active) return;
  notice.value = result.verified
    ? t("已解除禁言，平台名单已核验", "Unmuted and verified against the platform list")
    : result.status === "accepted" || result.status === "verified"
      ? t("解禁已受理，平台状态待核验", "Unmute accepted; platform verification pending")
      : result.status === "unknown"
        ? t("解禁回执未知，未自动重试", "Unmute receipt unknown; not retried")
        : t("解禁未受理", "Unmute not accepted");
  await read();
}
onMounted(() => run(refresh));
</script>

<template>
  <section class="current-mutes" :aria-label="t('当前禁言成员', 'Currently muted members')">
    <header>
      <h2>{{ t("当前禁言成员", "Currently muted members") }}<small v-if="loaded">{{ members.length }}</small></h2>
      <button type="button" :disabled="busy" :title="t('刷新当前禁言名单', 'Refresh current mutes')"
        :aria-label="t('刷新当前禁言名单', 'Refresh current mutes')" @click="run(refresh)"><RefreshCw :size="18" /></button>
    </header>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="busy" role="status"><LoaderCircle :size="16" class="spin" />{{ t("核验中", "Verifying") }}</p>
    <p v-if="loaded" class="checked">{{ t("平台核验 · 北京时间", "Platform checked · Asia/Shanghai") }} {{ new Date(checkedAt * 1000).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }) }}</p>
    <ul v-if="loaded && members.length" class="members">
      <li v-for="member in members" :key="member.id">
        <div><strong>{{ member.name }}</strong><small>{{ member.id }}</small></div>
        <button type="button" :disabled="busy || !member.can_unmute || Boolean(pending)" @click="run(() => preview(member))">
          <Volume2 :size="16" />{{ t("解除禁言", "Unmute") }}
        </button>
      </li>
    </ul>
    <p v-else-if="loaded && !busy">{{ t("当前没有禁言成员", "No members are currently muted") }}</p>
    <div v-if="pending" role="alertdialog" :aria-label="t('确认解除禁言', 'Confirm unmute')" class="confirmation">
      <strong>{{ t("确认解除禁言", "Confirm unmute") }}</strong>
      <p>{{ pending.member.name }} · {{ pending.member.id }}</p>
      <p>{{ t("机器人", "Bot") }} {{ account }} · {{ t("群", "Group") }} {{ group }}</p>
      <div class="actions">
        <button type="button" :disabled="busy" @click="run(commit)"><ShieldCheck :size="16" />{{ t("确认解禁", "Confirm") }}</button>
        <button type="button" :disabled="busy" @click="pending = null"><X :size="16" />{{ t("取消", "Cancel") }}</button>
      </div>
    </div>
  </section>
</template>

<style scoped>
.current-mutes { padding-bottom: 24px; margin-bottom: 24px; border-bottom: 1px solid #dce2e2; min-width: 0; }
header, h2, .actions { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; }
header { justify-content: space-between; }
h2 { font-size: 16px; margin: 0; }
small, .checked { color: #657676; font-size: 12px; }
.members { margin: 12px 0; padding: 0; list-style: none; }
li { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 12px 0; border-bottom: 1px solid #e5eaea; }
li div { min-width: 0; overflow-wrap: anywhere; }
li small { display: block; margin-top: 4px; }
button { display: inline-flex; align-items: center; justify-content: center; gap: 7px; min-height: 36px; padding: 7px 10px; border: 1px solid #becaca; border-radius: 4px; background: #eef5f3; color: #164d46; font: inherit; cursor: pointer; flex-shrink: 0; }
button:disabled { opacity: .5; cursor: default; }
p { overflow-wrap: anywhere; font-size: 13px; }
.error { color: #b52b35; }
.confirmation { margin: 12px 0; padding: 12px; border-left: 3px solid #12786b; overflow-wrap: anywhere; }
.spin { animation: spin 1s linear infinite; vertical-align: middle; margin-right: 6px; }
@keyframes spin { to { transform: rotate(360deg); } }
* { letter-spacing: 0; }
</style>
