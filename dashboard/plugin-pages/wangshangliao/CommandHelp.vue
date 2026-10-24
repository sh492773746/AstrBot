<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { Settings2 } from "@lucide/vue";
const props = defineProps<{ bridge: { apiPost: Function }; locale: string }>();
const emit = defineEmits<{ navigate: [tab: string] }>();
const all = ref(false);
const common = new Set(["开通", "规则", "我的权限", "群列表", "选择群", "禁言名单", "解禁", "踢出", "参加抽奖", "抽奖状态", "我的邀请", "我的邀请明细", "我的套餐", "确认本群扣费", "结账", "/chat", "/exit"]);
const place = ref("group");
const rows = ref<any[]>([]);
const error = ref("");
const english = computed(() => props.locale.startsWith("en"));
const places = computed(() => english.value
  ? { group: "Group commands", private: "Private commands", server: "Server-only settings" }
  : { group: "群内规则命令", private: "私信规则命令", server: "服务端专属设置" });
const roles = computed<Record<string, string>>(() => english.value ? {
  public: "Everyone", group_manager: "Authorized group manager",
  support_editor: "Dedicated owner / legacy account editor", finance: "Independent finance role", server: "Server",
} : {
  public: "所有人", group_manager: "本群授权管理", support_editor: "专属客服所有者／旧账号指定负责人",
  finance: "独立财务角色", server: "服务端",
});
const commands = computed(() => rows.value.filter(row => (all.value || common.has(row.command)) && (place.value === "server"
  ? row.role === "server" : row.role !== "server" && row.locations.includes(place.value))));
const settings = computed(() => english.value ? [
  ["open", "Activation"], ["rules", "Moderation & Auto Replies"], ["group-ai", "Group AI"],
  ["activities", "Player Lottery"], ["invite-rewards", "Invitation Rewards"], ["cashback", "Settlement"],
] : [
  ["open", "开通"], ["rules", "规则、关键词回复、定时广告"], ["group-ai", "群聊与热群"],
  ["activities", "玩家福利抽奖"], ["invite-rewards", "邀请奖励"], ["cashback", "邀请记账／结账"],
]);
onMounted(async () => {
  try {
    const data = await props.bridge.apiPost("open-service", { action: "command_capabilities", payload: {} });
    rows.value = data.commands;
  } catch (e: any) { error.value = e?.message || "命令目录暂不可用"; }
});
</script>
<template>
  <section aria-label="命令能力目录">
    <div class="setting-links">
      <button v-for="[id, name] in settings" :key="id" type="button" @click="emit('navigate', id)"><Settings2 :size="15" />{{ name }}</button>
    </div>
    <nav role="tablist" :aria-label="english ? 'Command location' : '命令执行位置'">
      <button v-for="(name, id) in places" :key="id" type="button" role="tab"
        :aria-selected="place === id" @click="place = id">{{ name }}</button>
    </nav>
    <p v-if="error" role="alert">{{ error }}</p>
    <label class="all-commands"><input v-model="all" type="checkbox" />{{ english ? "All compatible commands" : "全部兼容命令" }}</label>
    <div class="help-table">
      <table>
        <thead><tr><th>{{ english ? 'Command' : '命令' }}</th><th>{{ english ? 'Required role' : '所需角色' }}</th><th>{{ english ? 'Confirmation' : '预览确认' }}</th></tr></thead>
        <tbody><tr v-for="row in commands" :key="row.command">
          <td><code>{{ row.command }}</code><span v-if="row.retired"> · {{ english ? 'Retired' : '已停用' }}</span></td><td>{{ roles[row.role] }}</td>
          <td>{{ row.preview ? (english ? 'Required' : '需要') : (english ? 'No' : '否') }}</td>
        </tr></tbody>
      </table>
    </div>
    <p v-if="place === 'server'">{{ english
      ? "Administrator guidance; group AI models, personas, tools, topic, hours and knowledge; support infrastructure; editor grants and budgets."
      : "管理员指引配置、人格及权限手册；群AI模型、人格、工具、主题、时段、模式及群知识；客服基础配置、负责人授权及预算。" }}</p>
  </section>
</template>
<style scoped>
nav { display: flex; gap: 4px; flex-wrap: wrap; border-bottom: 1px solid #d9dde1; }
.setting-links { display:flex;flex-wrap:wrap;gap:8px;margin-bottom:20px; }
.setting-links button { display:inline-flex;align-items:center;gap:6px;padding:8px;border:1px solid #d9dde1;border-radius:4px;background:transparent;color:inherit;cursor:pointer; }
.all-commands { display:flex;align-items:center;gap:8px;padding:14px 0;font-size:13px; }
nav button { padding: 8px 10px; border: 0; background: transparent; color: inherit; }
nav button[aria-selected=true] { border-bottom: 2px solid #398477; font-weight: 600; }
.help-table { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 14px; table-layout: fixed; }
th, td { padding: 10px 8px; text-align: left; border-bottom: 1px solid #d9dde1; overflow-wrap: anywhere; }
p { overflow-wrap: anywhere; }
</style>
