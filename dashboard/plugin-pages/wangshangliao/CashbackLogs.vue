<script setup lang="ts">
import { ref, watch } from "vue";
import { RefreshCw, Search } from "@lucide/vue";
const props = defineProps<{ botId: string; groupId: string; bridge: {apiGet(endpoint: string, params?: Record<string,string>): Promise<any>} | undefined }>();
const rows = ref<any[]>([]), busy = ref(false), error = ref(""), level = ref(""), trace = ref("");
const includeGlobal = ref(true);
let sequence = 0;
const names: Record<string,string> = {
  contact_changed: "修改群内联系人", access_cashier_admitted: "全群结账员首次登录",
  pay: "登记已结账", correct: "撤回结账登记", exception: "异常付款登记",
  invitation_scan: "邀请采集核验", invitation_source_failed: "邀请来源读取失败",
  invitation_source_recovered: "邀请来源已恢复", invitation_source_deferred: "邀请来源查询等待",
  member_left: "成员退群", repeated_join: "重复入群", invitation_verified: "邀请归属核验",
  attribution_conflict: "归属冲突", identity_conflict: "身份冲突", rules_changed: "群设置变更",
  "/api/payments": "结账请求", "/api/entries": "查询账本", "/api/audit": "查询日志",
  "/api/admin/groups": "群设置请求", dashboard_log_query: "插件日志查询",
};
const time = (value: number) => new Date(value * 1000).toLocaleString("zh-CN", {timeZone:"Asia/Shanghai", hour12:false});
async function load(before = 0) {
  const request = ++sequence;
  if (!before) rows.value = [];
  error.value = "";
  if (!props.bridge || !props.groupId || props.botId !== "wangshangliao_3cf3b038") return;
  busy.value = true;
  try {
    const result = await props.bridge.apiGet("cashback-logs", {bot_id:props.botId, group_id:props.groupId, before:String(before), level:level.value, trace:trace.value.trim(), include_global:includeGlobal.value ? "1" : "0"});
    if (request === sequence) rows.value = before ? [...rows.value, ...result.items] : result.items;
  } catch(e) { if (request === sequence) error.value = e instanceof Error ? e.message : "操作日志读取失败"; }
  finally { if (request === sequence) busy.value = false; }
}
watch(() => [props.botId, props.groupId], () => load(), {immediate:true});
</script>
<template>
  <section class="cashback-logs">
    <header><h2>邀请记账／结账</h2><button :disabled="busy" title="刷新操作日志" aria-label="刷新操作日志" @click="load()"><RefreshCw :size="18" /></button></header>
    <p v-if="botId !== 'wangshangliao_3cf3b038'">请选择 wangshangliao_3cf3b038 及其已勾选群。</p>
    <template v-else>
      <h3>操作日志</h3>
      <div class="log-filters">
        <label>日志级别<select v-model="level" @change="load()"><option value="">全部级别</option><option value="info">正常操作</option><option value="warning">警告／拒绝</option><option value="error">错误</option></select></label>
        <label>请求编号<input v-model="trace" @keydown.enter="load()" /></label>
        <label class="global-filter"><input v-model="includeGlobal" type="checkbox" @change="load()" />包含全局事件</label>
        <button :disabled="busy" @click="load()"><Search :size="16" />查询</button>
      </div>
      <p v-if="error" role="alert">{{ error }}</p>
      <p v-if="busy" role="status">加载中…</p>
      <p v-else-if="!rows.length">暂无操作日志</p>
      <details v-for="row in rows" :key="row.id">
        <summary><strong :class="row.level">{{ row.level === 'error' ? '错误' : row.level === 'warning' ? '警告' : '正常' }} · {{ names[row.action] || row.action }}</strong><time>{{ time(row.created) }}</time></summary>
        <p v-if="row.resolved_at" class="info">已恢复 · {{ time(row.resolved_at) }}（北京时间）</p>
        <p>{{ row.actor }}<span v-if="row.actor_uid"> · UID {{ row.actor_uid }}</span><span v-if="!row.group_id"> · 全局事件</span><span v-if="row.reason"> · {{ row.reason }}</span></p>
        <p v-if="row.code">错误码：{{ row.code }}</p><p v-if="row.trace">请求编号：{{ row.trace }}</p>
        <div class="log-values"><div><small>修改前</small><pre>{{ JSON.stringify(JSON.parse(row.before_json),null,2) }}</pre></div><div><small>结果／修改后</small><pre>{{ JSON.stringify(JSON.parse(row.after_json),null,2) }}</pre></div></div>
      </details>
      <button v-if="rows.length && rows.length % 100 === 0" :disabled="busy" @click="load(rows[rows.length - 1].id)">更早日志</button>
    </template>
  </section>
</template>
<style scoped>
.cashback-logs {padding:24px 0; min-width:0}
header,.log-filters,summary {display:flex;align-items:center;gap:16px;flex-wrap:wrap}
header {justify-content:space-between} h2{font-size:20px} h3{font-size:16px;margin:20px 0}
.log-filters{margin:18px 0} label{display:grid;gap:6px;font-size:13px;min-width:0}
.global-filter{display:flex;align-items:center;gap:8px}
input,select{padding:9px;border:1px solid #ccd3d8;border-radius:4px;max-width:100%;background:transparent;color:inherit}
button{display:inline-flex;gap:6px;align-items:center;padding:9px;border:1px solid #ccd3d8;border-radius:4px;cursor:pointer;background:transparent;color:inherit}
details{padding:16px 0;border-bottom:1px solid #ddd} summary{justify-content:space-between;cursor:pointer}
p{margin:12px 0;overflow-wrap:anywhere}time,small{font-size:12px;color:#687580}
.error{color:#b44337}.warning{color:#915615}.info{color:#087f75}
.log-values{display:grid;grid-template-columns:1fr 1fr;gap:16px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;padding:12px;background:#f3f5f6;color:#27343b}
@media(max-width:600px){.log-values{grid-template-columns:1fr}.log-filters>label{width:100%}}
</style>
