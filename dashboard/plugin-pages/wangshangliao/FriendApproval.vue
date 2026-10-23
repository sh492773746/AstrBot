<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from "vue";
import { RefreshCw, ShieldCheck } from "@lucide/vue";

type Approval = {
  enabled: boolean;
  version: number;
  active: boolean;
  paused_reason: string;
  pending: number;
  accepted: number;
  retry_after: number;
  read_failures: number;
};
const props = defineProps<{
  bot: { id: string; revision: string; account_id?: string };
  bridge: {
    apiGet(endpoint: string, params?: Record<string, string>): Promise<any>;
    apiPost(endpoint: string, body: unknown): Promise<any>;
  };
  t: (zh: string, en: string) => string;
}>();
const approval = ref<Approval | null>(null);
const loading = ref(false),
  saving = ref(false),
  error = ref("");
let generation = 0;

async function load() {
  const current = ++generation,
    bot = props.bot.id,
    revision = props.bot.revision;
  loading.value = true;
  error.value = "";
  approval.value = null;
  try {
    const result = await props.bridge.apiGet("friend-approval", {
      bot_id: bot,
    });
    if (
      current === generation &&
      bot === props.bot.id &&
      revision === props.bot.revision
    )
      approval.value = result;
  } catch (e) {
    if (current === generation)
      error.value = e instanceof Error ? e.message : String(e);
  } finally {
    if (current === generation) loading.value = false;
  }
}

async function save(checked: boolean) {
  if (!approval.value || loading.value || saving.value) return;
  const current = ++generation,
    bot = props.bot.id,
    revision = props.bot.revision;
  saving.value = true;
  error.value = "";
  try {
    const result = await props.bridge.apiPost("friend-approval", {
      bot_id: bot,
      revision,
      version: approval.value.version,
      enabled: checked,
    });
    if (
      current === generation &&
      bot === props.bot.id &&
      revision === props.bot.revision
    )
      approval.value = result;
  } catch (e) {
    if (current === generation) {
      await load();
      if (bot === props.bot.id && revision === props.bot.revision)
        error.value = e instanceof Error ? e.message : String(e);
    }
  } finally {
    if (bot === props.bot.id && revision === props.bot.revision)
      saving.value = false;
  }
}

async function reconcile() {
  if (!approval.value || loading.value || saving.value) return;
  const current = ++generation, bot = props.bot.id, revision = props.bot.revision;
  saving.value = true;
  error.value = "";
  try {
    const result = await props.bridge.apiPost("friend-approval", {
      bot_id: bot, revision, version: approval.value.version, action: "reconcile",
    });
    if (current === generation && bot === props.bot.id && revision === props.bot.revision)
      approval.value = result;
  } catch (e) {
    if (current === generation) {
      await load();
      if (bot === props.bot.id && revision === props.bot.revision)
        error.value = e instanceof Error ? e.message : String(e);
    }
  } finally {
    if (bot === props.bot.id && revision === props.bot.revision)
      saving.value = false;
  }
}

watch(
  () => [props.bot.id, props.bot.revision],
  () => {
    approval.value = null;
    saving.value = false;
    void load();
  },
  { immediate: true },
);
onBeforeUnmount(() => { generation++; });
</script>

<template>
  <section class="friend-approval">
    <header>
      <h2>{{ t("好友申请", "Friend Requests") }}</h2>
      <button
        type="button"
        :disabled="loading || saving"
        :title="t('刷新申请状态', 'Refresh Request Status')"
        :aria-label="t('刷新申请状态', 'Refresh Request Status')"
        @click="load"
      >
        <RefreshCw :size="18" :class="{ spin: loading }" />
      </button>
    </header>
    <p class="muted">
      {{ t("机器人账号", "Bot Account") }} {{ bot.account_id }}
    </p>
    <label class="toggle">
      <input
        type="checkbox"
        :checked="approval?.enabled === true"
        :disabled="!approval || loading || saving"
        @change="save(($event.target as HTMLInputElement).checked)"
      />
      {{ t("自动同意好友申请", "Automatically Accept Friend Requests") }}
    </label>
    <dl v-if="approval">
      <div>
        <dt>{{ t("待处理", "Pending") }}</dt>
        <dd>{{ approval.pending }}</dd>
      </div>
      <div>
        <dt>{{ t("已自动同意", "Accepted Automatically") }}</dt>
        <dd>{{ approval.accepted }}</dd>
      </div>
      <div>
        <dt>{{ t("动作冷却", "Action Cooldown") }}</dt>
        <dd>{{ approval.retry_after }} {{ t("秒", "s") }}</dd>
      </div>
    </dl>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="approval?.paused_reason" class="error" role="alert">
      {{
        t(
          "自动同意已暂停：申请结果或账号状态待核对，请核对官方客户端与操作日志。",
          "Automatic acceptance is paused. Review the official client and operation logs.",
        )
      }}
    </p>
    <button v-if="approval?.paused_reason" class="resume" type="button"
      :disabled="loading || saving" @click="reconcile">
      <ShieldCheck :size="18" />{{ t("核对并恢复", "Verify And Resume") }}
    </button>
    <p v-if="approval?.read_failures" class="muted" role="status">
      {{ t("读取暂不可用，退避重试中", "Read unavailable; retrying with backoff") }}
    </p>
    <p
      v-else-if="approval?.enabled && !approval.active && !approval.paused_reason"
      class="muted"
      role="status"
    >
      {{
        t(
          "自动同意等待服务在线",
          "Automatic acceptance is waiting for the service",
        )
      }}
    </p>
    <p v-else-if="loading" class="muted" role="status">
      {{ t("读取中...", "Loading...") }}
    </p>
  </section>
</template>

<style scoped>
.friend-approval {
  min-width: 0;
}
header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
h2 {
  font-size: 18px;
  margin: 0;
}
button {
  width: 36px;
  height: 36px;
  display: grid;
  place-items: center;
  border: 1px solid #cbd5e1;
  background: transparent;
  border-radius: 4px;
  cursor: pointer;
  flex-shrink: 0;
}
button:disabled {
  opacity: 0.5;
  cursor: default;
}
.resume {
  width: auto;
  min-height: 36px;
  height: auto;
  display: inline-flex;
  gap: 8px;
  padding: 8px 12px;
}
.toggle {
  display: flex;
  align-items: center;
  gap: 10px;
  overflow-wrap: anywhere;
}
input {
  width: 18px;
  height: 18px;
  flex-shrink: 0;
  accent-color: #0f766e;
}
dl {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px;
  margin: 24px 0;
  padding: 18px 0;
  border-block: 1px solid #cbd5e1;
}
dt,
.muted {
  color: #64748b;
  font-size: 14px;
  overflow-wrap: anywhere;
}
dd {
  margin: 8px 0 0;
  font-size: 20px;
}
.error {
  color: #b91c1c;
  overflow-wrap: anywhere;
}
.spin {
  animation: spin 1s linear infinite;
}
@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
@media (max-width: 360px) {
  dl {
    grid-template-columns: 1fr;
    gap: 14px;
  }
  dl div {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  dd {
    margin: 0;
  }
}
</style>
