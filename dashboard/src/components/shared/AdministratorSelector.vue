<template>
  <div class="administrator-selector">
    <div class="administrator-summary" aria-live="polite">
      <span v-if="!modelValue.length" class="text-medium-emphasis">{{ tm('empty') }}</span>
      <v-tooltip v-for="account in selectedAccounts.slice(0, 3)" :key="account.id" :text="account.id">
        <template #activator="{ props: tooltipProps }">
          <v-chip v-bind="tooltipProps" size="small" label>
            <v-icon start size="16">mdi-account-outline</v-icon>
            <span class="administrator-name">{{ account.name || tm('unknownName') }}</span>
          </v-chip>
        </template>
      </v-tooltip>
      <span v-if="selectedAccounts.length > 3" class="text-caption">+{{ selectedAccounts.length - 3 }}</span>
    </div>
    <v-btn :disabled="disabled" size="small" variant="tonal" color="primary" prepend-icon="mdi-account-plus-outline" @click="openDialog">
      {{ tm('choose') }}
    </v-btn>
  </div>

  <v-dialog v-model="dialog" max-width="700">
    <v-card class="administrator-dialog">
      <v-card-title class="text-h3 pa-4 pb-0 pl-6">{{ tm('title') }}</v-card-title>
      <v-card-text class="administrator-body">
        <v-alert type="warning" variant="tonal" density="compact" class="mb-4">
          {{ tm('permissionWarning') }}
        </v-alert>
        <div v-if="draft.length" class="administrator-selected">
          <v-tooltip v-for="id in draft" :key="id" :text="id">
            <template #activator="{ props: tooltipProps }">
              <v-chip v-bind="tooltipProps" size="small" label closable :aria-label="tm('remove', { name: accountName(id) })" @click:close="toggle(id)">
                <span class="administrator-name">{{ accountName(id) }}</span>
              </v-chip>
            </template>
          </v-tooltip>
        </div>
        <div class="administrator-filters">
          <v-text-field v-model="search" :label="tm('search')" :aria-label="tm('search')" prepend-inner-icon="mdi-magnify" variant="outlined" density="compact" hide-details clearable />
          <v-select v-model="platform" :items="platformOptions" item-title="title" item-value="value" :label="tm('platform')" :aria-label="tm('platform')" variant="outlined" density="compact" hide-details />
          <v-tooltip :text="tm('refresh')">
            <template #activator="{ props: tooltipProps }">
              <v-btn v-bind="tooltipProps" icon="mdi-refresh" variant="text" size="small" :aria-label="tm('refresh')" :loading="loading" @click="loadAccounts" />
            </template>
          </v-tooltip>
        </div>
        <v-alert v-if="error" type="error" variant="tonal" density="compact" role="alert" class="mt-3">{{ error }}</v-alert>
        <div class="administrator-conversations" :aria-label="tm('conversations')">
          <v-progress-linear v-if="loading" indeterminate color="primary" :aria-label="tm('loading')" />
          <v-list v-if="filteredAccounts.length" density="compact">
            <v-list-item v-for="account in filteredAccounts" :key="account.id" class="administrator-conversation" :aria-label="account.name || tm('unknownName')" @click="toggle(account.id)">
              <template #prepend>
                <v-checkbox-btn :model-value="draft.includes(account.id)" :aria-label="account.name || tm('unknownName')" color="primary" @click.stop @update:model-value="toggle(account.id)" />
              </template>
              <v-list-item-title class="administrator-person-name">{{ account.name || tm('unknownName') }}</v-list-item-title>
              <v-list-item-subtitle class="administrator-person-source">{{ account.platforms.map(platformName).join(' · ') }}</v-list-item-subtitle>
              <template #append>
                <v-tooltip :text="`${tm('accountId')}: ${account.id}`">
                  <template #activator="{ props: tooltipProps }">
                    <span v-bind="tooltipProps" class="administrator-account-id">{{ tm('accountId') }}: {{ account.id }}</span>
                  </template>
                </v-tooltip>
              </template>
            </v-list-item>
          </v-list>
          <p v-else-if="!loading" class="administrator-empty">{{ search || platform ? tm('noMatches') : tm('noConversations') }}</p>
        </div>
        <v-btn size="small" variant="text" :aria-expanded="manual" prepend-icon="mdi-keyboard-outline" @click="manual = !manual">{{ tm('manual') }}</v-btn>
        <div v-if="manual" class="administrator-manual">
          <v-text-field v-model="manualId" :label="tm('accountId')" :aria-label="tm('accountId')" variant="outlined" density="compact" hide-details @keyup.enter="addManual" />
          <v-btn size="small" variant="tonal" :disabled="!validManual" @click="addManual">{{ t('core.common.list.addButton') }}</v-btn>
        </div>
      </v-card-text>
      <v-card-actions class="pa-4 administrator-actions">
        <span class="text-caption">{{ tm('selected', { count: draft.length }) }}</span>
        <v-spacer />
        <v-btn variant="text" @click="dialog = false">{{ t('core.common.cancel') }}</v-btn>
        <v-btn color="primary" variant="tonal" :disabled="disabled" @click="confirm">{{ t('core.common.confirm') }}</v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { sessionApi } from '@/api/v1'
import { useI18n, useModuleI18n } from '@/i18n/composables'
import { adminConversationCandidates } from '@/utils/adminConversationCandidates.mjs'

const props = defineProps({
  modelValue: { type: Array, default: () => [] },
  disabled: { type: Boolean, default: false }
})
const emit = defineEmits(['update:modelValue'])
const { t } = useI18n()
const { tm: sharedTm } = useModuleI18n('core/shared')
const tm = (key, params) => sharedTm(`administratorSelector.${key}`, params)
const accounts = ref([])
const loading = ref(false)
const error = ref('')
const dialog = ref(false)
const draft = ref([])
const search = ref('')
const platform = ref('')
const manual = ref(false)
const manualId = ref('')
const accountMap = computed(() => new Map(accounts.value.map(account => [account.id, account])))
const selectedAccounts = computed(() => props.modelValue.map(id => accountMap.value.get(id) || { id, name: '' }))
const validManual = computed(() => {
  const id = manualId.value.trim()
  return id.length > 0 && id.length <= 255 && !/[\s\p{C}]/u.test(id)
})
const platformOptions = computed(() => [
  { title: tm('allPlatforms'), value: '' },
  ...[...new Set(accounts.value.flatMap(account => account.platforms.map(platformName)))]
    .sort().map(name => ({ title: name, value: name }))
])
const filteredAccounts = computed(() => {
  const term = (search.value || '').trim().toLocaleLowerCase()
  return accounts.value.filter(account =>
    (!platform.value || account.platforms.some(source => platformName(source) === platform.value))
    && (!term || [account.id, ...account.names, ...account.platforms.map(platformName)]
      .some(value => value.toLocaleLowerCase().includes(term)))
  )
})

function platformName(source) {
  if (source.startsWith('wangshangliao_')) return tm('wangshangliao')
  if (source.startsWith('telegram')) return 'Telegram'
  if (source.startsWith('webchat')) return tm('webchat')
  return source
}

function accountName(id) {
  return accountMap.value.get(id)?.name || `${tm('unknownName')} (${id})`
}

function toggle(id) {
  draft.value = draft.value.includes(id)
    ? draft.value.filter(value => value !== id)
    : [...draft.value, id]
}

async function loadAccounts() {
  if (loading.value) return
  loading.value = true
  error.value = ''
  try {
    const response = await sessionApi.activeUmos()
    if (response.data.status !== 'ok' || !Array.isArray(response.data.data?.umo_infos)) {
      throw new Error('Conversation identities unavailable')
    }
    accounts.value = adminConversationCandidates(response.data.data.umo_infos)
  } catch {
    error.value = tm('loadFailed')
  } finally {
    loading.value = false
  }
}

function openDialog() {
  draft.value = [...props.modelValue]
  search.value = ''
  platform.value = ''
  manual.value = false
  manualId.value = ''
  dialog.value = true
  void loadAccounts()
}

function addManual() {
  if (!validManual.value) return
  const id = manualId.value.trim()
  if (!draft.value.includes(id)) draft.value.push(id)
  manualId.value = ''
}

function confirm() {
  emit('update:modelValue', [...new Set(draft.value)])
  dialog.value = false
}

onMounted(loadAccounts)
</script>

<style scoped>
.administrator-selector {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.administrator-summary, .administrator-selected {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  min-width: 0;
}
.administrator-selector > :deep(.v-btn) {
  max-width: 100%;
  min-width: 0;
  white-space: normal;
}
.administrator-selector :deep(.v-btn__content) {
  white-space: normal;
}
.administrator-summary :deep(.v-chip), .administrator-selected :deep(.v-chip) {
  max-width: 100%;
}
.administrator-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.administrator-dialog {
  max-height: calc(100dvh - 48px);
  border-radius: 8px;
}
.administrator-body {
  overflow-y: auto;
  min-height: 0;
}
.administrator-selected {
  margin-bottom: 16px;
}
.administrator-filters {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 160px 32px;
  align-items: center;
  gap: 8px;
}
.administrator-conversations {
  height: 300px;
  overflow-y: auto;
  margin: 12px 0;
  border-block: 1px solid rgba(var(--v-theme-on-surface), 0.12);
}
.administrator-person-name {
  white-space: normal;
  overflow-wrap: anywhere;
  font-size: 14px;
  line-height: 1.4;
}
.administrator-person-source {
  white-space: normal;
  overflow-wrap: anywhere;
}
.administrator-account-id {
  color: rgba(var(--v-theme-on-surface), 0.6);
  font-size: 12px;
  margin-left: 12px;
  max-width: 150px;
  overflow-wrap: anywhere;
}
.administrator-empty {
  text-align: center;
  padding: 48px 12px;
  color: rgba(var(--v-theme-on-surface), 0.6);
}
.administrator-manual {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
}
.administrator-actions {
  flex-shrink: 0;
}
@media (max-width: 600px) {
  .administrator-filters {
    grid-template-columns: minmax(0, 1fr) 32px;
  }
  .administrator-filters :deep(.v-select) {
    grid-column: 1;
    grid-row: 2;
  }
  .administrator-filters > :last-child {
    grid-column: 2;
    grid-row: 1;
  }
  .administrator-account-id {
    max-width: 96px;
  }
  .administrator-body {
    padding: 16px !important;
  }
}
</style>
