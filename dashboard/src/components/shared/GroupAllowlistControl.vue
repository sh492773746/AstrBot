<script setup>
import axios from 'axios'
import { computed, ref, watch, onBeforeUnmount } from 'vue'

const props = defineProps({
  modelValue: { type: Object, default: () => ({}) },
  sessionRef: { type: String, default: '' },
  directoryApi: { type: String, default: '' },
})
const emit = defineEmits(['update:modelValue'])
const groups = ref([])
const manualId = ref('')
const search = ref('')
const loading = ref(false)
const error = ref('')
const nextPage = ref(1)
const hasMore = ref(false)
let generation = 0
const enabled = computed(() => props.modelValue?.group_mode === 'allowlist' && Array.isArray(props.modelValue?.group_ids)
  ? props.modelValue.group_ids.map(String) : [])
const rows = computed(() => {
  const known = new Map(groups.value.map(group => [group.id, group]))
  enabled.value.forEach(id => { if (!known.has(id)) known.set(id, { id, name: '群名待加载' }) })
  const query = (search.value || '').trim().toLowerCase()
  return [...known.values()].filter(row => `${row.name} ${row.id}`.toLowerCase().includes(query))
})
function update(ids, privateEnabled = props.modelValue?.private_enabled === true) {
  emit('update:modelValue', { ...props.modelValue, group_mode: 'allowlist',
    group_ids: [...new Set(ids)], private_enabled: privateEnabled })
}
function toggle(id, value) {
  if (!groups.value.some(row => row.id === id)) groups.value.push({ id, name: '群名待加载' })
  update(value ? [...enabled.value, id] : enabled.value.filter(group => group !== id))
}
function addGroup() {
  const id = manualId.value.trim()
  if (!id || id.length > 128) return
  if (!groups.value.some(row => row.id === id)) groups.value.push({ id, name: '手动添加' })
  toggle(id, true)
  manualId.value = ''
}
watch(() => props.sessionRef, () => {
  generation += 1
  groups.value = []; error.value = ''; nextPage.value = 1; hasMore.value = false; loading.value = false
})
onBeforeUnmount(() => { generation += 1 })
async function loadGroups(more = false) {
  if (loading.value || !props.sessionRef) return
  const current = ++generation
  const page = more ? nextPage.value : 1
  loading.value = true; error.value = ''
  try {
    const response = await axios.post(props.directoryApi, { session_ref: props.sessionRef, page })
    const body = response.data
    if (body?.status === 'error') throw new Error(body.message)
    const data = body?.data ?? body
    const list = data?.groups ?? data?.groupInfos ?? data?.groupList
    if (!Array.isArray(list)) throw new Error('invalid_directory_response')
    const loaded = list.map(row => {
      const id = String(row.groupID ?? row.id ?? '')
      if (!id) throw new Error('invalid_directory_response')
      return { id, name: String(row.groupName ?? row.name ?? '未命名群') }
    })
    if (current !== generation) return
    const merged = new Map((more ? groups.value : []).map(row => [row.id, row]))
    loaded.forEach(row => merged.set(row.id, row))
    groups.value = [...merged.values()]
    const total = Number(data.total ?? data.totalCount)
    hasMore.value = loaded.length >= 100 && (!Number.isFinite(total) || total > page * 100)
    nextPage.value = page + 1
  } catch (exc) {
    if (current !== generation) return
    const code = exc.response?.data?.message || exc.message
    error.value = ({
      session_access_denied: '没有此账号的访问权限。',
      session_binding_required: '请先完成账号登录。',
      session_unavailable: '账号会话不可用，请检查登录状态。',
      consumer_already_connected: '账号正在使用中，请稍后刷新。',
      directory_request_failed: '群目录查询失败，请检查登录有效期后重试。',
      invalid_directory_response: '群目录格式不兼容，未修改现有群设置。',
    })[code] || '群目录暂时无法读取，现有群设置保持不变。'
  } finally { if (current === generation) loading.value = false }
}
</script>

<template>
  <section class="group-allowlist" aria-label="群聊与私聊控制">
    <div class="policy-heading"><h3>消息控制</h3><span>群聊范围：白名单（固定）</span></div>
    <v-switch :model-value="modelValue?.private_enabled === true" label="允许私聊"
      color="primary" hide-details @update:model-value="update(enabled, $event === true)" />
    <div class="policy-toolbar">
      <v-text-field v-model="search" label="搜索群名称或群 ID" prepend-inner-icon="mdi-magnify"
        density="compact" variant="outlined" hide-details clearable @click:clear="search = ''" />
      <v-btn v-if="directoryApi" prepend-icon="mdi-refresh" variant="text"
        :loading="loading" :disabled="!sessionRef" @click="loadGroups(false)">刷新群列表</v-btn>
    </div>
    <v-alert v-if="error" type="error" variant="tonal" class="my-2">{{ error }}</v-alert>
    <p class="text-caption">已开启 {{ enabled.length }} 个群 · 未开启的群不处理消息</p>
    <div v-for="group in rows" :key="group.id" class="policy-group">
      <div><strong>{{ group.name }}</strong><div class="text-caption">{{ group.id }}</div></div>
      <v-switch :model-value="enabled.includes(group.id)" :label="`允许群 ${group.id}`"
        color="primary" hide-details density="compact" @update:model-value="toggle(group.id, $event === true)" />
    </div>
    <p v-if="!rows.length" class="text-caption">暂无匹配的群</p>
    <v-btn v-if="hasMore" variant="text" :loading="loading" @click="loadGroups(true)">加载更多群</v-btn>
    <div class="policy-toolbar">
      <v-text-field v-model="manualId" label="添加群 ID" maxlength="128" density="compact"
        variant="outlined" hide-details @keydown.enter.prevent="addGroup" />
      <v-btn prepend-icon="mdi-plus" variant="tonal" :disabled="!manualId.trim()" @click="addGroup">添加并开启</v-btn>
    </div>
  </section>
</template>

<style scoped>
.group-allowlist { width: 100%; padding: 12px 0; }
.policy-heading, .policy-toolbar, .policy-group { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.policy-heading { justify-content: space-between; }
.policy-heading h3 { font-size: 16px; }
.policy-heading span { font-size: 13px; }
.policy-toolbar { margin: 12px 0; }
.policy-toolbar .v-input { flex: 1 1 180px; min-width: 0; }
.policy-group { padding: 10px 0; border-bottom: 1px solid rgba(var(--v-theme-on-surface), .12); justify-content: space-between; }
.policy-group > div:first-child { flex: 1 1 160px; min-width: 0; overflow-wrap: anywhere; }
.policy-group > .v-input { flex: 0 1 auto; max-width: 100%; overflow-wrap: anywhere; }
</style>
