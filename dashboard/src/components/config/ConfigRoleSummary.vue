<template>
  <section class="config-role-summary" aria-label="配置档角色">
    <header>
      <h2>{{ role.title }}</h2>
      <v-btn
        :to="`/plugin-page/${role.plugin}/accounts`"
        prepend-icon="mdi-account-cog-outline"
        variant="text"
        size="small"
      >账号服务</v-btn>
    </header>
    <dl>
      <div><dt>消息路由</dt><dd><code v-for="route in role.routes" :key="route">{{ route }}</code></dd></div>
      <div><dt>自动 AI 对话</dt><dd>{{ configData.provider_settings?.enable ? '已启用' : '已禁用' }}</dd></div>
      <div><dt>知识库</dt><dd>{{ configData.kb_names?.length ? '隔离状态异常' : '未接入' }}</dd></div>
      <div><dt>插件范围</dt><dd>{{ (configData.plugin_set || []).join('、') }}</dd></div>
      <div><dt>原生管理命令</dt><dd>{{ configData.disable_builtin_commands ? '已禁用' : '隔离状态异常' }}</dd></div>
      <div><dt>配置权限</dt><dd>{{ role.role === 'conflict' ? '路由冲突 · 禁止编辑' : (role.editable ? 'AI 参数可编辑 · 隔离设置锁定' : '业务入口专用 · 原生配置锁定') }}</dd></div>
    </dl>
  </section>
</template>

<script setup>
defineProps({
  role: { type: Object, required: true },
  configData: { type: Object, required: true }
});
</script>

<style scoped>
.config-role-summary {
  padding: 4px 0 24px;
  margin-bottom: 24px;
  border-bottom: 1px solid rgba(var(--v-theme-on-surface), 0.12);
}
header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
h2 { margin: 0; font-size: 1.2rem; line-height: 1.4; letter-spacing: 0; }
dl { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px 24px; margin: 20px 0 0; }
dt { color: rgba(var(--v-theme-on-surface), 0.6); font-size: 0.82rem; }
dd { margin: 4px 0 0; font-size: 0.9rem; overflow-wrap: anywhere; }
code { display: block; }
@media (max-width: 600px) { dl { grid-template-columns: minmax(0, 1fr); gap: 12px; } }
</style>
