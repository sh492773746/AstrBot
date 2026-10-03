<template>
  <section class="d-flex flex-column ga-3">
    <v-divider />
    <h3 class="text-subtitle-1">
      {{ t("开发测试门禁", "Developer test window") }}
    </h3>
    <v-select
      v-model="sender"
      :items="instances"
      item-title="id"
      item-value="id"
      :label="t('测试来源机器人', 'Source bot')"
      variant="outlined"
    />
    <v-select
      v-model="groups"
      :items="groupOptions"
      item-title="name"
      item-value="id"
      multiple
      chips
      :label="t('测试群', 'Test groups')"
      variant="outlined"
    />
    <v-select
      v-model="scopes"
      :items="scopeOptions"
      multiple
      chips
      :label="t('测试范围', 'Scopes')"
      variant="outlined"
    />
    <v-combobox
      v-if="scopes.includes('group_rules')"
      v-model="keywords"
      multiple
      chips
      :label="t('WSL_TEST_ 专用关键词', 'WSL_TEST_ keywords')"
      variant="outlined"
    />
    <v-text-field
      v-model.number="seconds"
      type="number"
      :min="1"
      :max="300"
      :label="t('有效秒数', 'Lifetime seconds')"
      variant="outlined"
    />
    <v-text-field
      v-model.number="budget"
      type="number"
      :min="1"
      :max="10"
      :label="t('入站次数', 'Inbound budget')"
      variant="outlined"
    />
    <div class="d-flex flex-wrap ga-2">
      <v-btn
        variant="tonal"
        prepend-icon="mdi-play"
        :disabled="busy || !sender || window.active"
        @click="call('test_open')"
        >{{ t("开启测试", "Open test") }}</v-btn
      >
      <v-btn
        variant="text"
        prepend-icon="mdi-stop"
        :disabled="busy"
        @click="call('test_close')"
        >{{ t("立即关闭", "Close now") }}</v-btn
      >
    </div>
    <div role="status">
      {{ window.active ? t("开启", "Open") : t("关闭", "Closed") }} ·
      {{ window.seconds || 0 }} s · {{ window.remaining || 0 }}
    </div>
    <v-alert v-if="error" type="error" variant="tonal" density="compact">{{
      error
    }}</v-alert>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { botApi } from "@/api/v1";
import { useI18n } from "@/i18n/composables";
const props = defineProps<{
  instance: string;
  enabledGroups: string[];
  groupOptions: Array<{ id: string; name: string }>;
  draft?: Record<string, any>;
}>();
const emit = defineEmits(["update:draft"]);
const { locale } = useI18n();
const t = (zh: string, en: string) => (locale.value.startsWith("zh") ? zh : en);
type TestScope =
  | "private_ai"
  | "private_commands"
  | "group_commands"
  | "group_ai"
  | "group_rules";
const sender = ref(""),
  groups = ref<string[]>([]),
  scopes = ref<TestScope[]>(["private_commands"]);
const keywords = ref<string[]>([]),
  seconds = ref(300),
  budget = ref(10);
const instances = ref<Array<{ id: string }>>([]),
  window = ref<Record<string, any>>({});
const error = ref(""),
  busy = ref(false);
const scopeOptions = computed(() => [
  { title: t("私聊 AI 客服", "Private AI support"), value: "private_ai" },
  { title: t("私聊管理命令", "Private commands"), value: "private_commands" },
  { title: t("群内管理命令", "Group commands"), value: "group_commands" },
  { title: t("群内 @ AI 客服", "Group mentioned AI"), value: "group_ai" },
  { title: t("群内违规规则", "Group rules"), value: "group_rules" },
]);
async function call(action: "test_status" | "test_open" | "test_close") {
  if (busy.value) return;
  const instance = props.instance;
  busy.value = true;
  try {
    const response = await botApi.registration("wangshangliao", {
      action,
      instance_id: instance,
      ...(action === "test_open"
        ? {
            sender_instance: sender.value,
            groups: groups.value,
            scopes: scopes.value,
            keywords: keywords.value,
            seconds: seconds.value,
            budget: budget.value,
          }
        : {}),
    });
    if (instance !== props.instance) return;
    if (response.data.status !== "ok")
      throw new Error(response.data.message || "Request failed");
    const data = response.data.data as any;
    window.value = data.window;
    instances.value = data.instances;
    error.value = "";
  } catch (e: any) {
    if (instance === props.instance) error.value = e.message;
  } finally {
    busy.value = false;
  }
}
watch(
  () => [props.instance, props.draft] as const,
  () => {
    const d = props.draft || {};
    sender.value = d.sender_instance || "";
    groups.value = d.groups || [];
    scopes.value = d.scopes || ["private_commands"];
    keywords.value = d.keywords || [];
    seconds.value = d.seconds ?? 300;
    budget.value = d.budget ?? 10;
  },
  { immediate: true, deep: true },
);
watch(
  [sender, groups, scopes, keywords, seconds, budget],
  () => {
    const draft = {
      sender_instance: sender.value,
      groups: groups.value,
      scopes: scopes.value,
      keywords: keywords.value,
      seconds: seconds.value,
      budget: budget.value,
    };
    const current = props.draft || {
      sender_instance: "",
      groups: [],
      scopes: ["private_commands"],
      keywords: [],
      seconds: 300,
      budget: 10,
    };
    if (JSON.stringify(draft) !== JSON.stringify(current))
      emit("update:draft", draft);
  },
  { deep: true },
);
watch(
  () => props.instance,
  () => {
    window.value = {};
    void call("test_status");
  },
  { immediate: true },
);
const timer = setInterval(() => {
  void call("test_status");
}, 3000);
onBeforeUnmount(() => clearInterval(timer));
</script>
