<script setup>
import axios from 'axios'
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import GroupAllowlistControl from '@/components/shared/GroupAllowlistControl.vue'
import verificationDocument from './lajiaoliao-captcha.html?raw'

const props = defineProps({
  modelValue: { type: Object, required: true },
  existing: Boolean,
  configuredIds: { type: Array, default: () => [] },
})
const emit = defineEmits(['update:modelValue'])
const base = '/api/v1/plugins/extensions/astrbot_plugin_lajiaoliao/'
const account = ref('')
const password = ref('')
const sessions = ref([])
const draft = ref('')
const recoveryRef = ref('')
const busy = ref(false)
const error = ref('')
const checking = ref(false)
const reauthOpen = ref(false)
const operationRef = ref('')
const captchaOpen = ref(false)
const confirmDiscard = ref(false)
const captchaSource = ref('')
const captchaFrame = ref(null)
let alive = true
let cancelVerification
const saved = computed(() => sessions.value.find(row => row.session_ref === props.modelValue.session_ref))
const connected = computed(() => saved.value?.state === 'bound' && saved.value.instance_id === props.modelValue.id)
const resumable = computed(() => saved.value && ['authorized', 'bound'].includes(saved.value.state) && !connected.value)
const recoverable = computed(() => sessions.value.filter(row => {
  if (row.session_ref === props.modelValue.session_ref) return false
  return !row.instance_id || !props.configuredIds.includes(row.instance_id)
}))
const recovery = computed(() => recoverable.value.find(row => row.session_ref === recoveryRef.value))
const recoveryOptions = computed(() => recoverable.value.map(row => ({
  title: `${row.account_hint || row.account_id || '待登录'} · ${row.state === 'draft' ? '待验证' : '待保存'}`,
  value: row.session_ref,
})))
const errors = {
  session_access_denied: '没有此账号的访问权限。',
  login_draft_already_exists: '当前机器人已有待完成登录，请刷新登录状态。',
  login_draft_limit_reached: '待完成登录已达上限，请先取消不需要的草稿。',
  login_draft_expired: '登录已过期，请重新登录。',
  account_already_registered: '此账号已有接入实例，不能重复登录。',
  instance_already_exists: '此机器人已关联其他账号，未作修改。',
  session_already_bound: '此账号仍在其他机器人中使用，不能重复接入。',
  existing_account_requires_resume: '此机器人已有会话，请刷新登录状态；需要换号时删除机器人并重新创建。',
  verification_cancelled: '已取消验证，未提交密码。',
  verification_timeout: '验证已超时，未提交密码。',
  verification_failed: '官方验证未完成，未提交密码。',
  verification_sdk_failed: '官方验证码加载失败，未提交密码，请稍后重试。',
  verification_init_failed: '官方验证码初始化失败，未提交密码。',
  lajiaoliao_captcha_required: '需要重新完成官方验证。',
  lajiaoliao_login_failed: '登录失败，请核对账号、密码与验证结果。',
  lajiaoliao_device_confirmation_required: '官方要求设备确认，请先在官方客户端完成确认。',
  lajiaoliao_verification_config_unavailable: '无法获取官方验证配置，未提交密码。',
  lajiaoliao_im_token_refresh_failed: '未取得消息登录凭据，请刷新登录状态核验。',
  session_binding_mismatch: '账号与此机器人不匹配，未更换账号。',
  login_result_requires_poll: '账号已完成授权，请刷新登录状态，未取消会话。',
  session_in_use: '此账号仍在机器人中使用，请先删除该机器人。',
  reauth_account_mismatch: '不是原机器人账号，原会话未替换。更换账号请新建机器人。',
  credential_version_changed: '会话凭据已更新，请取消本次授权，刷新后重新验证。',
  reauth_expired: '重新登录已过期，请重新发起。',
  reauth_commit_interrupted: '授权切换未完成，请刷新状态核验。',
  reauth_result_unavailable: '授权结果尚不可用，请刷新状态核验。',
  instance_changed_refresh_required: '机器人设置已被其他页面修改，请刷新后重新编辑。',
}
function update(values) { emit('update:modelValue', { ...props.modelValue, ...values }) }
async function api(method, endpoint, body) {
  const response = await axios({ method, url: base + endpoint, data: body, timeout: 75000 })
  if (response.data?.status === 'error') throw new Error(response.data.message)
  return response.data?.data ?? response.data
}
async function refresh() {
  const result = await api('get', 'sessions')
  if (!alive) return
  sessions.value = result.sessions || []
  const pending = sessions.value.find(row => row.pending_instance_id === props.modelValue.id && row.state === 'draft')
  if (pending) draft.value = pending.session_ref
  else if (draft.value && !sessions.value.some(row => row.session_ref === draft.value && row.state === 'draft')) {
    draft.value = ''
    checking.value = false
  }
  if (recoveryRef.value && !recoverable.value.some(row => row.session_ref === recoveryRef.value)) {
    recoveryRef.value = ''
  }
  if (props.existing && saved.value) {
    const state = await api('post', 'reauth-status', { session_ref: saved.value.session_ref })
    if (!alive) return
    if (state.state === 'draft') {
      operationRef.value = state.operation_ref
      reauthOpen.value = true
    } else if (state.state === 'completed' || state.state === 'none') {
      operationRef.value = ''
      reauthOpen.value = false
      checking.value = false
    } else if (state.state === 'failed') {
      operationRef.value = state.operation_ref
      reauthOpen.value = true
      error.value = errors[state.error] || '授权未完成，请取消本次授权后重试。'
    }
  }
}
async function act(fn) {
  if (busy.value) return
  busy.value = true; error.value = ''
  try { await fn() } catch (exc) {
    if (alive) error.value = errors[exc.response?.data?.message || exc.message]
      || '操作结果待核验，请刷新登录状态，不要重复提交。'
  } finally {
    password.value = ''
    if (alive) busy.value = false
  }
}
async function verify(config) {
  if (config.required === false) return ''
  if (config.required !== true || !config.region || !config.prefix || !config.scene_id) {
    throw new Error('lajiaoliao_verification_config_unavailable')
  }
  return new Promise((resolve, reject) => {
    const requestID = crypto.randomUUID()
    let finished = false
    const finish = (value, code) => {
      if (finished) return
      finished = true; clearTimeout(timer); window.removeEventListener('message', receive)
      captchaOpen.value = false; captchaSource.value = ''; cancelVerification = undefined
      code ? reject(new Error(code)) : resolve(value)
    }
    const receive = event => {
      const data = event.data
      if (event.source !== captchaFrame.value?.contentWindow || event.origin !== 'null'
        || data?.source !== 'lajiao-captcha' || data.requestID !== requestID) return
      if (data.type === 'ready') {
        captchaFrame.value.contentWindow.postMessage({ source: 'lajiao-form', requestID, config }, '*')
      } else if (data.type === 'success' && typeof data.value === 'string'
        && data.value.length > 0 && data.value.length < 48000) finish(data.value)
      else if (data.type === 'cancelled') finish('', 'verification_cancelled')
      else if (data.type === 'failed') finish('', ['verification_sdk_failed', 'verification_init_failed'].includes(data.value) ? data.value : 'verification_failed')
    }
    const timer = setTimeout(() => finish('', 'verification_timeout'), 180000)
    cancelVerification = () => finish('', 'verification_cancelled')
    window.addEventListener('message', receive)
    captchaSource.value = verificationDocument
      .replace('__REQUEST_ID__', JSON.stringify(requestID))
      .replace('__PARENT_ORIGIN__', JSON.stringify(location.origin))
    captchaOpen.value = true
  })
}
async function login() {
  if (props.existing) {
    const prepared = await api('post', 'prepare-reauth', {
      instance_id: props.modelValue.id, session_ref: props.modelValue.session_ref,
    })
    if (!alive) return
    operationRef.value = prepared.operation_ref
    const proof = await verify(prepared.challenge)
    if (!alive) return
    checking.value = true
    await api('post', 'reauthenticate', {
      session_ref: props.modelValue.session_ref, operation_ref: operationRef.value,
      account: account.value.trim(), password: password.value, captcha_verify_param: proof,
    })
    if (!alive) return
    operationRef.value = ''; reauthOpen.value = false; checking.value = false
    await refresh()
    return
  }
  const prepared = await api('post', 'prepare-login', {
    instance_id: props.modelValue.id, ...(draft.value ? { session_ref: draft.value } : {}),
  })
  if (!alive) return
  draft.value = prepared.session_ref
  const proof = await verify(prepared.challenge)
  if (!alive) return
  checking.value = true
  const result = await api('post', 'login', {
    session_ref: draft.value, instance_id: props.modelValue.id,
    account: account.value.trim(), password: password.value, captcha_verify_param: proof,
  })
  if (!alive) return
  update({ session_ref: result.session_ref })
  checking.value = false; draft.value = ''
  await refresh()
}
async function poll() {
  if (draft.value) {
    const result = await api('post', 'poll', { session_ref: draft.value })
    if (!alive) return
    if (result.state === 'bound' && result.instance_id === props.modelValue.id) {
      update({ session_ref: result.session_ref }); draft.value = ''; checking.value = false
    } else if (result.state === 'draft') {
      checking.value = false
      error.value = '登录尚未完成，可重新验证后登录。'
    }
  }
  await refresh()
}
async function resume() {
  const result = await api('post', 'connect-session', {
    session_ref: saved.value.session_ref, instance_id: props.modelValue.id, confirm: true,
  })
  if (!alive) return
  update({ session_ref: result.session_ref }); await refresh()
}
async function restore() {
  const row = recovery.value
  if (!row) return
  if (row.state === 'draft') {
    if (!row.pending_instance_id) {
      error.value = '旧版登录草稿无法在创建弹窗继续，请取消该草稿。'
      return
    }
    update({ id: row.pending_instance_id, session_ref: '', enable: false })
    draft.value = row.session_ref
  } else if (row.state === 'bound') {
    update({ id: row.instance_id, session_ref: row.session_ref, enable: false })
    draft.value = ''
  } else if (row.state === 'authorized') {
    const result = await api('post', 'connect-session', {
      session_ref: row.session_ref, instance_id: props.modelValue.id, confirm: true,
    })
    if (!alive) return
    update({ session_ref: result.session_ref, enable: false })
    await refresh()
  }
}
async function discardRecovery() {
  const row = recovery.value
  if (!row || !['draft', 'authorized', 'bound'].includes(row.state)) return
  if (row.state === 'draft') {
    await api('post', 'cancel-login', { session_ref: row.session_ref })
  } else {
    await api('post', 'revoke', { session_ref: row.session_ref, confirm: true })
  }
  if (!alive) return
  if (draft.value === row.session_ref) draft.value = ''
  if (props.modelValue.session_ref === row.session_ref) update({ session_ref: '', enable: false })
  recoveryRef.value = ''
  confirmDiscard.value = false
  await refresh()
}
async function cancelDraft() {
  await api('post', 'cancel-login', { session_ref: draft.value })
  if (!alive) return
  draft.value = ''; checking.value = false
  await refresh()
}
async function cancelReauth() {
  if (operationRef.value) {
    await api('post', 'cancel-reauth', {
      session_ref: props.modelValue.session_ref, operation_ref: operationRef.value,
    })
  }
  if (!alive) return
  operationRef.value = ''; reauthOpen.value = false; checking.value = false
}
onMounted(async () => {
  if (!props.existing && !props.modelValue.session_ref) {
    update({ id: `lajiaoliao_${crypto.randomUUID().replaceAll('-', '').slice(0, 16)}` })
    await nextTick()
  }
  await act(refresh)
})
onBeforeUnmount(() => { alive = false; cancelVerification?.(); password.value = '' })
</script>

<template>
  <div class="lajiaoliao-login">
    <v-alert v-if="error" type="warning" variant="tonal" class="mb-3" role="status">{{ error }}</v-alert>
    <div v-if="!existing && recoveryOptions.length && !connected" class="recovery-bar">
      <v-select v-model="recoveryRef" :items="recoveryOptions" label="未完成的机器人"
        variant="outlined" density="compact" hide-details clearable :disabled="busy" />
      <v-btn prepend-icon="mdi-restore" variant="tonal" :disabled="busy || !recovery" @click="act(restore)">恢复</v-btn>
      <v-btn prepend-icon="mdi-delete-outline" variant="text" color="error"
        :disabled="busy || !recovery" @click="confirmDiscard = true">注销</v-btn>
    </div>
    <p v-if="connected" class="text-body-2 mb-3" role="status">已登录：{{ saved.account_hint || saved.account_id }}</p>
    <template v-else-if="resumable && !reauthOpen">
      <p class="text-body-2 mb-3">已保存账号：{{ saved.account_hint || saved.account_id }}</p>
      <v-btn prepend-icon="mdi-login" variant="tonal" :disabled="busy" @click="act(resume)">使用已保存账号</v-btn>
    </template>
    <v-btn v-if="existing && saved && !reauthOpen" prepend-icon="mdi-login" variant="tonal"
      :disabled="busy" @click="reauthOpen = true">重新登录</v-btn>
    <form v-if="(!existing && !connected && !resumable) || (existing && reauthOpen)" @submit.prevent="act(login)">
      <v-text-field v-model="account" label="手机号" inputmode="tel" autocomplete="username"
        variant="outlined" density="compact" maxlength="14" :disabled="busy" />
      <v-text-field v-model="password" label="密码" type="password" autocomplete="current-password"
        variant="outlined" density="compact" :disabled="busy" />
      <v-btn type="submit" prepend-icon="mdi-login" variant="tonal" color="primary" :loading="busy"
        :disabled="busy || checking || !/^(?:\+86)?1[0-9]{10}$/.test(account.trim()) || !password">{{ existing ? '验证并重新登录' : '登录' }}</v-btn>
      <v-btn v-if="existing" prepend-icon="mdi-close" variant="text" :disabled="busy"
        @click="act(cancelReauth)">取消重新登录</v-btn>
    </form>
    <p v-if="existing && !saved" class="text-body-2 mb-3" role="status">账号会话不可用，请核对机器人后重新创建。</p>
    <v-btn prepend-icon="mdi-refresh" variant="text" :disabled="busy" @click="act(poll)">刷新登录状态</v-btn>
    <v-btn v-if="!existing && draft && !connected" prepend-icon="mdi-close" variant="text"
      :disabled="busy" @click="act(cancelDraft)">取消本次登录</v-btn>
    <v-dialog :model-value="captchaOpen" max-width="440" persistent>
      <v-card>
        <v-card-title class="text-h3 pa-4 pb-0 pl-6">官方安全验证</v-card-title>
        <iframe ref="captchaFrame" :srcdoc="captchaSource" sandbox="allow-scripts allow-forms"
          referrerpolicy="no-referrer" title="官方滑块验证码" class="captcha-frame" />
        <v-card-actions><v-spacer /><v-btn variant="text" @click="cancelVerification?.()">取消验证</v-btn></v-card-actions>
      </v-card>
    </v-dialog>
    <v-dialog v-model="confirmDiscard" max-width="400">
      <v-card>
        <v-card-title class="text-h3 pa-4 pb-0 pl-6">注销未创建的账号</v-card-title>
        <v-card-text>确认注销 {{ recovery?.account_hint || recovery?.account_id || '此登录草稿' }}？此操作会清除该会话。</v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="confirmDiscard = false">取消</v-btn>
          <v-btn color="error" variant="tonal" :loading="busy" @click="act(discardRecovery)">确认注销</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
    <v-switch :model-value="modelValue.enable === true" label="启用机器人" color="primary"
      :disabled="!connected" hide-details @update:model-value="update({ enable: $event === true })" />
    <GroupAllowlistControl :model-value="modelValue.message_policy"
      :session-ref="connected ? modelValue.session_ref : ''" :directory-api="base + 'groups'"
      @update:model-value="update({ message_policy: $event })" />
    <v-btn href="#/plugin-page/astrbot_plugin_lajiaoliao/management" target="_blank"
      rel="noopener noreferrer" prepend-icon="mdi-account-group" variant="text">辣椒聊群管</v-btn>
  </div>
</template>

<style scoped>
.lajiaoliao-login { padding: 16px 0; min-width: 0; }
.lajiaoliao-login p { overflow-wrap: anywhere; }
.captcha-frame { display: block; width: 100%; height: min(500px, 65dvh); border: 0; }
</style>
