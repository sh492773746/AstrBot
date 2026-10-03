<script setup lang="ts">
import reference from "./reference-tables.json";

const props = defineProps<{
  view: "architecture" | "matrix";
  locale: string;
  groupId: string;
  permissions: string[];
  contentMode: boolean;
}>();
const t = (zh: string, en: string) => (props.locale.startsWith("zh") ? zh : en);
const text = (value: string[]) => t(value[0], value[1]);
const access = (value: string) => {
  if (value === "kick-mode")
    return props.contentMode
      ? t("群内静默", "Silent in groups")
      : t("允许", "Allowed");
  const labels: Record<string, string[]> = {
    allowed: ["允许", "Allowed"],
    denied: ["无权限", "Denied"],
    "private-only": ["群内静默", "Silent in groups"],
    "group-only": ["仅群内", "Group only"],
    unavailable: ["未提供", "Not available"],
    tool: ["需 AI 工具", "AI tool required"],
  };
  return text(labels[value]);
};
</script>

<template>
  <section
    :id="`panel-${view}`"
    role="tabpanel"
    :aria-labelledby="`tab-${view}`"
    class="reference-panel"
  >
    <template v-if="view === 'architecture'">
      <h2>
        {{ t("模块职责与数据边界", "Module responsibilities and boundaries") }}
      </h2>
      <div
        class="table-scroll"
        tabindex="0"
        :aria-label="t('架构表', 'Architecture table')"
      >
        <table class="reference-table architecture-table">
          <thead>
            <tr>
              <th scope="col">{{ t("模块", "Module") }}</th>
              <th scope="col">{{ t("职责", "Responsibility") }}</th>
              <th scope="col">{{ t("边界", "Boundary") }}</th>
              <th scope="col">{{ t("实现文件", "Implementation") }}</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="row in reference.architecture"
              :key="row.id"
              :data-module="row.id"
            >
              <th scope="row">{{ text(row.name) }}</th>
              <td>{{ text(row.responsibility) }}</td>
              <td>{{ text(row.boundary) }}</td>
              <td>
                <code v-for="file in row.files" :key="file">{{ file }}</code>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
    <template v-else>
      <h2>{{ t("调用权限对照", "Caller permission matrix") }}</h2>
      <div
        class="table-scroll"
        tabindex="0"
        :aria-label="t('权限表', 'Permission matrix')"
      >
        <table class="reference-table permission-matrix">
          <thead>
            <tr>
              <th scope="col">{{ t("功能／命令", "Feature / command") }}</th>
              <th scope="col">{{ t("普通群员 · 群内", "Member · group") }}</th>
              <th scope="col">
                {{ t("授权管理员 · 群内", "Authorized admin · group") }}
              </th>
              <th scope="col">
                {{ t("普通用户 · 私聊", "Member · private") }}
              </th>
              <th scope="col">
                {{ t("授权管理员 · 私聊", "Authorized admin · private") }}
              </th>
              <th scope="col">
                {{ t("动作所需已保存授权", "Required saved action grants") }}
              </th>
              <th scope="col">{{ t("执行条件", "Execution conditions") }}</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="row in reference.permissions"
              :key="row.id"
              :data-feature="row.id"
            >
              <th scope="row">
                {{ text(row.name) }}
                <code v-for="example in row.examples" :key="example">{{
                  example
                }}</code>
              </th>
              <td v-for="(value, index) in row.access" :key="index">
                <span
                  :class="{
                    enabled: value === 'allowed',
                    warning: value === 'denied',
                  }"
                  >{{ access(value) }}</span
                >
              </td>
              <td>
                <span
                  v-for="grant in row.grants"
                  :key="grant"
                  class="grant-status"
                >
                  <code>{{ grant }}</code>
                  <span
                    class="badge"
                    :class="{ allow: groupId && permissions.includes(grant) }"
                  >
                    {{
                      !groupId
                        ? t("未选群", "No group selected")
                        : permissions.includes(grant)
                        ? t("已授权", "Granted")
                        : t("未授权", "Not granted")
                    }}
                  </span>
                </span>
                <span v-if="'proactive' in row && row.proactive">
                  {{
                    t(
                      "主动发送：机器人页核验",
                      "Proactive sending: verify in bot editor",
                    )
                  }}
                </span>
                <span v-else-if="!row.grants.length">{{
                  t("无处罚动作要求", "No sanction grant required")
                }}</span>
              </td>
              <td>{{ text(row.rule) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <h2>{{ t("授权来源与隔离", "Authority sources and isolation") }}</h2>
      <div
        class="table-scroll"
        tabindex="0"
        :aria-label="t('授权来源', 'Authority sources')"
      >
        <table class="reference-table authority-table">
          <thead>
            <tr>
              <th scope="col">{{ t("权限层", "Authority layer") }}</th>
              <th scope="col">{{ t("来源", "Source") }}</th>
              <th scope="col">
                {{ t("不能替代的检查", "Non-substitutable checks") }}
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in reference.layers" :key="row.name[1]">
              <th scope="row">{{ text(row.name) }}</th>
              <td>{{ text(row.source) }}</td>
              <td>{{ text(row.boundary) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </section>
</template>
