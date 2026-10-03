<template>
  <section class="d-flex flex-column ga-3">
    <v-divider />
    <h3 class="text-subtitle-1">{{ t('成员批量操作', 'Batch member actions') }}</h3>
    <v-btn-toggle v-model="mode" mandatory divided variant="outlined" density="compact">
      <v-btn value="rename">{{ t('名片规范', 'Cards') }}</v-btn>
      <v-btn value="cleanup">{{ t('封禁／注销清理', 'Account cleanup') }}</v-btn>
    </v-btn-toggle>
    <p class="text-caption text-medium-emphasis">{{ t('使用已保存的逐群授权。先预览，再执行；排除机器人、群主和管理员。未知结果停止，不自动重发。', 'Uses saved per-group grants. Preview before execution. Bots, owners and administrators are excluded. Unknown results stop the job.') }}</p>
    <v-select v-model="group" :items="groups" item-title="name" item-value="id" :label="t('目标群', 'Group')" variant="outlined" density="compact" />
    <v-text-field v-model="jobId" :label="t('任务 ID（刷新后可查询）', 'Job ID (query after refresh)')" variant="outlined" density="compact" />
    <div class="d-flex flex-wrap ga-2">
      <v-btn variant="tonal" :disabled="!group || busy" @click="call('card_preview')">{{ t('生成预览', 'Preview') }}</v-btn>
      <v-btn variant="tonal" color="warning" :disabled="job?.state !== 'preview' || busy || (job?.kind === 'cleanup' && !confirmed)" @click="call('card_execute')">{{ t('执行此预览', 'Execute preview') }}</v-btn>
      <v-btn variant="text" :disabled="!jobId || busy" @click="call('card_status')">{{ t('刷新进度', 'Refresh progress') }}</v-btn>
      <v-btn variant="text" :disabled="!job || busy" @click="call('card_stop')">{{ t('停止待执行项', 'Stop pending items') }}</v-btn>
    </div>
    <p v-if="error" role="alert" class="text-error">{{ error }}</p>
    <template v-if="job">
      <v-checkbox v-if="job.kind === 'cleanup' && job.state === 'preview'" v-model="confirmed" :label="t('确认将预览中的封禁／注销普通成员移出此群；不会注销平台账号。', 'Remove the previewed banned / cancelled ordinary members from this group; their platform accounts are not deleted.')" hide-details />
      <p v-if="job.excluded_accounts" class="text-caption">{{ t('已排除封禁、注销或结果待核对的账号：', 'Excluded banned, cancelled or quarantined accounts: ') }}{{ job.excluded_accounts }}</p>
      <p role="status" class="text-body-2">{{ labels[job.state] || job.state }} · {{ job.items.filter((i: any) => i.state === 'verified' || i.state === 'unchanged').length }} / {{ job.items.length }}</p>
      <p class="text-caption" style="overflow-wrap: anywhere">{{ t('任务 ID：', 'Job ID: ') }}{{ job.id }}</p>
      <v-list density="compact" class="rounded border" max-height="280" style="overflow-y: auto">
        <v-list-item v-for="item in job.items" :key="item.member">
          <div class="text-body-2" style="overflow-wrap: anywhere; white-space: pre-wrap">{{ item.original || t('空白名片', 'Empty card') }} → {{ job.kind === 'cleanup' ? t('移出群聊', 'Remove from group') : item.name }}</div>
          <div v-if="item.account_state" class="text-caption">{{ item.account_state === 'ACCOUNT_STATE_BAN' ? t('平台封禁', 'Banned') : t('账号注销', 'Cancelled') }}</div>
          <div class="text-caption">{{ item.member }} · {{ labels[item.state] || item.state }} {{ item.error || '' }}</div>
        </v-list-item>
      </v-list>
    </template>
  </section>
</template>
<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { botApi } from '@/api/v1';
import { useI18n } from '@/i18n/composables';
const props = defineProps<{ instance: string; groups: Array<{ id: string; name: string }> }>();
const { locale } = useI18n();
const t = (zh: string, en: string) => locale.value.startsWith('zh') ? zh : en;
const group = ref('');
const mode = ref('rename');
const confirmed = ref(false);
const jobId = ref('');
const job = ref<any>(null);
const error = ref('');
const busy = ref(false);
const labels = computed<Record<string, string>>(() => ({preview: t('待确认预览', 'Preview'), queued: t('排队中', 'Queued'), running: t('执行中', 'Running'), completed: t('已完成', 'Completed'), partial: t('部分完成', 'Partial'), stopped: t('已停止', 'Stopped'), needs_review: t('需核对平台状态', 'Needs review'), pending: t('待执行', 'Pending'), accepted: t('已接受，未确认', 'Accepted, unconfirmed'), verified: t('已回读确认', 'Verified'), unknown: t('未知，不重发', 'Unknown, no retry'), rejected: t('拒绝', 'Rejected'), unchanged: t('无需修改', 'Unchanged')}));
watch(() => [props.instance, group.value, mode.value], () => { job.value = null; jobId.value = ''; error.value = ''; confirmed.value = false; });
async function call(action: 'card_preview' | 'card_execute' | 'card_status' | 'card_stop') {
  busy.value = true; error.value = '';
  const instance = props.instance;
  try {
    const response = await botApi.registration('wangshangliao', {action: action === 'card_preview' && mode.value === 'cleanup' ? 'cleanup_preview' : action, instance_id: instance, group: group.value, card_job_id: jobId.value || undefined});
    if (action === 'card_preview') confirmed.value = false;
    if (response.data.status !== 'ok') throw new Error(response.data.message || 'Request failed');
    if (instance === props.instance) { job.value = response.data.data; jobId.value = job.value.id; }
  } catch (e: any) { error.value = e.message; }
  finally { busy.value = false; }
}
</script>
