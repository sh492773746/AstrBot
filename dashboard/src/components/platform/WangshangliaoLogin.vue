<template>
  <form class="d-flex flex-column ga-4" @submit.prevent>
    <p class="text-body-2 text-medium-emphasis mb-0">
      {{
        label(
          "私信文本无需 @ 即可处理；群聊仅回复明确提及。",
          "Private texts need no mention; group replies require an explicit mention.",
        )
      }}
    </p>
    <div class="d-flex align-center flex-wrap ga-3">
      <v-chip size="small" variant="tonal" color="primary">{{
        label(loginMethod === "sms" ? "创建方式：短信登录" : "创建方式：账号登录", loginMethod === "sms" ? "SMS login" : "Account login")
      }}</v-chip>
    </div>
    <v-alert v-if="error" type="error" variant="tonal" class="mb-3">{{
      errorText
    }}</v-alert>
    <v-alert
      v-if="loginPending"
      ref="saveNotice"
      type="warning"
      variant="tonal"
      class="mb-3 login-save-notice"
      role="alert"
    >
      {{
        label(
          "认证成功但尚未连接：请核对群聊和权限配置，然后保存登录。",
          "Authenticated but not connected. Review groups and permissions, then save your login.",
        )
      }}
      — {{ flow.nickname || flow.account_id }}
      <v-btn class="mt-2" color="primary" block prepend-icon="mdi-content-save" variant="tonal" :loading="busy" @click="saveAuthenticated">{{ label('保存登录并连接', 'Save login and connect') }}</v-btn>
    </v-alert>
    <v-alert
      v-if="awaitingCaptcha && !error"
      type="success"
      variant="tonal"
      class="mb-3"
      >{{
        label("自动验证失败，请完成滑块验证", "Automatic verification failed. Complete the slider verification")
      }}</v-alert
    >
    <v-alert
      v-if="busy && flow.registration_code"
      type="info"
      variant="tonal"
      class="mb-3"
      role="status"
    >
      {{
        label(
          "安全验证已通过，正在向旺商聊提交登录，请稍候…",
          "Verification passed. Signing in to Wangshangliao…",
        )
      }}
    </v-alert>
    <div v-if="!flow.session_ref" class="d-flex flex-column ga-3">
      <v-btn
        v-if="!flow.registration_code"
        class="align-self-start"
        color="primary"
        prepend-icon="mdi-login"
        rounded="xl"
        variant="tonal"
        :loading="busy"
        @click="start"
      >
        {{
          label(
            existing ? "重新登录" : "开始登录",
            existing ? "Log in again" : "Start login",
          )
        }}
      </v-btn>
      <template v-else>
        <v-btn-toggle v-if="flow.status !== 'sms_required'" v-model="loginMethod" mandatory :disabled="busy" color="primary" variant="tonal">
          <v-btn value="password">{{ label('密码登录', 'Password login') }}</v-btn>
          <v-btn value="sms">{{ label('短信登录', 'SMS login') }}</v-btn>
        </v-btn-toggle>
        <v-text-field
          variant="outlined"
          density="comfortable"
          v-if="flow.status !== 'sms_required'"
          v-model="account"
          :label="loginMethod === 'sms' ? label('手机号（+86）', 'Phone (+86)') : label('账号', 'Account')"
          autocomplete="off"
          :disabled="busy"
        />
        <v-text-field
          variant="outlined"
          density="comfortable"
          v-if="flow.status !== 'sms_required' && loginMethod === 'password'"
          v-model="password"
          type="password"
          :label="label('密码', 'Password')"
          autocomplete="new-password"
          :disabled="busy"
        />
        <v-text-field
          variant="outlined"
          density="comfortable"
          v-if="flow.status === 'sms_required'"
          v-model="sms"
          :label="label('短信验证码', 'SMS code')"
          maxlength="6"
          autocomplete="one-time-code"
          :disabled="busy"
        />
        <div :id="captchaElement" />
        <div class="d-flex flex-wrap ga-2">
          <v-btn
            variant="tonal"
            rounded="xl"
            prepend-icon="mdi-login"
            color="primary"
            :loading="busy"
            :disabled="busy"
            @click="
              verify(flow.status === 'sms_required' ? 'verify_sms' : (loginMethod === 'sms' ? 'request_sms' : 'login'))
            "
          >
            {{ label(flow.status !== "sms_required" && loginMethod === "sms" ? "验证并获取验证码" : "验证并登录", flow.status !== "sms_required" && loginMethod === "sms" ? "Verify and request SMS" : "Verify and log in") }}
          </v-btn>
          <v-btn
            v-if="flow.status === 'sms_required'"
            variant="text"
            rounded="xl"
            :disabled="busy || cooldown > 0"
            @click="verify('resend_sms')"
          >
            {{ label("重发短信", "Resend SMS") }} {{ cooldown || "" }}
          </v-btn>
          <v-btn variant="text" rounded="xl" @click="cancel">{{
            label("取消", "Cancel")
          }}</v-btn>
        </div>
      </template>
    </div>
    <div v-if="flow.session_ref || existing" class="d-flex flex-column ga-3">
      <v-divider />
      <v-btn
        class="align-self-start"
        variant="text"
        rounded="xl"
        color="primary"
        prepend-icon="mdi-refresh"
        :loading="loadingGroups"
        @click="loadGroups"
        >{{ label("刷新群聊列表", "Refresh groups") }}</v-btn
      >
      <v-alert v-if="groupError" type="error" variant="tonal">{{
        label(
          "群聊读取失败，请重试；账号登录状态保留",
          "Could not load groups. Retry; login is preserved.",
        )
      }}</v-alert>
      <v-autocomplete
        density="comfortable"
        hide-details="auto"
        multiple
        chips
        closable-chips
        variant="outlined"
        :menu-props="{ maxHeight: 360 }"
        :items="groupOptions"
        item-title="name"
        item-value="id"
        :model-value="modelValue.enabled_groups || []"
        :label="
          label(
            '勾选启用群聊（留空不处理群聊）',
            'Enabled groups (empty disables group handling)',
          )
        "
        @update:model-value="
          (value) =>
            emit('update:modelValue', { ...modelValue, enabled_groups: value })
        "
      />
      <v-alert v-if="groups.length" type="info" variant="tonal">{{
        label(
          "群目录来自平台；分页完整性尚未确认。角色未知不代表普通成员，管理操作必须重新校验权限。",
          "Platform directory completeness is unverified. Unknown roles require verification before moderation.",
        )
      }}</v-alert>
      <p class="text-body-2 text-medium-emphasis mb-0">
        {{
          label(
            "发送和回复无需群管理员身份；群管理操作需要管理员权限。",
            "Sending and replying do not require group admin status. Moderation requires admin permission.",
          )
        }}
      </p>
    </div>
    <WangshangliaoPermissions :model-value="modelValue" :groups="enabledGroupOptions" @update:model-value="emit('update:modelValue', $event)" />
    <WangshangliaoCards v-if="existing" :instance="modelValue.id" :groups="enabledGroupOptions" />
    <WangshangliaoTestWindow v-if="existing" :instance="modelValue.id" :enabled-groups="modelValue.enabled_groups || []" :group-options="enabledGroupOptions" :draft="modelValue.developer_test" @update:draft="emit('update:modelValue', { ...modelValue, developer_test: $event })" />
    <div v-if="existing" class="d-flex flex-column ga-3">
      <v-divider />
      <v-switch
        color="primary"
        density="comfortable"
        hide-details
        inset
        :model-value="modelValue.enable"
        :label="
          label(
            '启用机器人（停用保留登录会话）',
            'Enable bot (disabling keeps the session)',
          )
        "
        @update:model-value="
          (value) => emit('update:modelValue', { ...modelValue, enable: value })
        "
      />
      <v-btn
        class="align-self-start"
        variant="text"
        rounded="xl"
        size="small"
        prepend-icon="mdi-logout"
        color="error"
        :disabled="busy"
        @click="logout"
        >{{
          label("退出账号并删除本地会话", "Log out and delete local session")
        }}</v-btn
      >
    </div>
  </form>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onBeforeUnmount, ref, watch } from "vue";
import { onBeforeRouteLeave } from 'vue-router';
import { useI18n } from "@/i18n/composables";
import { botApi } from "@/api/v1";
import WangshangliaoPermissions from './WangshangliaoPermissions.vue';
import WangshangliaoCards from './WangshangliaoCards.vue';
import WangshangliaoTestWindow from './WangshangliaoTestWindow.vue';

const props = defineProps<{
  modelValue: Record<string, any>;
  existing?: boolean;
}>();
const emit = defineEmits(["update:modelValue", "group-directory"]);
const { locale } = useI18n();
const label = (zh: string, en: string) =>
  locale.value.startsWith("zh") ? zh : en;
const account = ref("");
const password = ref("");
const sms = ref("");
const error = ref("");
const errorText = computed(() => {
  const messages: Record<string, [string, string]> = {
    deployment_missing: [
      "平台尚未配置，请联系管理员",
      "Platform is not configured. Contact your administrator.",
    ],
    deployment_invalid: [
      "平台部署配置有误，请联系管理员",
      "Platform configuration is invalid. Contact your administrator.",
    ],
    invalid_credentials: ["账号或密码错误", "Incorrect account or password."],
    invalid_sms: ["短信验证码错误", "Incorrect SMS code."],
    sms_expired: [
      "短信验证码已过期，请重新登录",
      "SMS code expired. Start login again.",
    ],
    sms_cooldown: [
      "请等待短信重发倒计时结束",
      "Wait for the SMS resend cooldown.",
    ],
    login_input: [
      "请填写账号和密码后再验证",
      "Enter your account and password first.",
    ],
    nim_kicked: [
      "消息连接被服务端终止，请核对其他客户端后重连",
      "Messaging was terminated by the server. Check other clients before reconnecting.",
    ],
    reauth_required: [
      "账号会话已失效，请重新登录",
      "The account session expired. Sign in again.",
    ],
    rate_limited: [
      "平台请求受限，请稍后重试",
      "The platform rate limit was reached. Retry later.",
    ],
    transport: [
      "登录服务连接失败，请检查网络后重新验证",
      "Login connection failed. Check your network and verify again.",
    ],
    business_rejected: [
      "旺商聊拒绝了登录请求，请核对账号或稍后重试",
      "Wangshangliao rejected the login. Check your account or retry later.",
    ],
    http_rejected: [
      "旺商聊服务暂时未接受请求，请稍后重试",
      "Wangshangliao is temporarily rejecting requests. Try later.",
    ],
    captcha_failed: ["人工验证失败，请重试", "Verification failed. Try again."],
    captcha_load_failed: [
      "验证组件加载失败，请取消后重试",
      "Verification could not load. Cancel and try again.",
    ],
    registration_expired: [
      "登录已过期，请重新开始",
      "Login expired. Start again.",
    ],
    registration_failed: [
      "登录失败，请重新开始或联系管理员",
      "Login failed. Restart or contact your administrator.",
    ],
    registration_rate_limit: [
      "尝试过于频繁，请稍后重试",
      "Too many attempts. Try again later.",
    ],
    logout_failed: ["退出失败，请重试", "Logout failed. Try again."],
  };
  return messages[error.value]
    ? label(...messages[error.value])
    : label(
        "登录请求失败，请重试或联系管理员",
        "Login request failed. Retry or contact your administrator.",
      );
});
const busy = ref(false);
async function saveAuthenticated() {
  busy.value = true;
  error.value = '';
  try {
    const config = { ...props.modelValue, session_ref: flow.value.session_ref, account_id: flow.value.account_id, nickname: flow.value.nickname };
    const response = props.existing ? await botApi.update(props.modelValue.id, config) : await botApi.create(config);
    if (response.data.status !== 'ok') throw new Error('save_failed');
    loginSaved.value = true;
    window.location.reload();
  } catch {
    error.value = 'registration_failed';
  } finally {
    busy.value = false;
  }
}
const awaitingCaptcha = ref(false);
const loginMethod = ref(props.modelValue.login_method || "password");
const groups = ref<Array<{ id: string; name: string }>>([]);
const groupOptions = computed(() => {
  const known = new Set(groups.value.map(group => group.id));
  return [...groups.value, ...(props.modelValue.enabled_groups || [])
    .filter((id: string) => !known.has(id))
    .map((id: string) => ({ id, name: id }))];
});
const enabledGroupOptions = computed(() => groupOptions.value
  .filter(group => (props.modelValue.enabled_groups || []).includes(group.id))
  .map(group => ({ id: group.id, name: group.name === group.id
    ? `${label('群名待加载', 'Group name unavailable')} (${group.id})`
    : `${group.name} (${group.id})` })));
onMounted(() => { if (props.existing) void loadGroups(); });
const loadingGroups = ref(false);
const groupError = ref(false);
const flow = ref<Record<string, any>>({});
const loginSaved = ref(false);
const saveNotice = ref<any>(null);
const loginPending = computed(() => !!flow.value.session_ref && !loginSaved.value);
function warnBeforeUnload(event: BeforeUnloadEvent) {
  if (!loginPending.value) return;
  event.preventDefault();
  event.returnValue = '';
}
watch(loginPending, async pending => {
  window.removeEventListener('beforeunload', warnBeforeUnload);
  if (pending) {
    window.addEventListener('beforeunload', warnBeforeUnload);
    await nextTick();
    saveNotice.value?.$el?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
});
watch(() => props.modelValue.session_ref, (value, previous) => {
  if (previous && !value && flow.value.session_ref === previous) loginSaved.value = true;
});
onBeforeRouteLeave(() => !loginPending.value || window.confirm(label(
  '登录尚未保存，离开后机器人不会连接。确定离开？',
  'Your login is not saved. Leaving will not connect the bot. Leave anyway?',
)));
const cooldown = ref(0);
const captchaReady = ref(false);
const captchaElement = `wang-captcha-${crypto.randomUUID()}`;
let captcha: any;
let captchaTimeout: ReturnType<typeof setTimeout> | undefined;
let mounted = true;
let pendingAction: LoginAction | "" = "";
let generation = 0;
let timer: ReturnType<typeof setInterval> | undefined;

type LoginAction =
  | "request_sms"
  | "groups"
  | "start"
  | "poll"
  | "login"
  | "verify_sms"
  | "resend_sms"
  | "cancel"
  | "logout";
async function request(action: LoginAction, fields: Record<string, any> = {}) {
  const response = await botApi.registration("wangshangliao", {
    action,
    instance_id: flow.value.instance_id || props.modelValue.id,
    ...(flow.value.registration_code
      ? { registration_code: flow.value.registration_code }
      : {}),
    ...fields,
  });
  return response.data.data;
}

async function loadGroups() {
  const current = generation;
  loadingGroups.value = true;
  groupError.value = false;
  try {
    const result = await request("groups");
    if (mounted && current === generation) {
      emit("group-directory", {
        instance: props.modelValue.id,
        names: Object.fromEntries(result.groups.map((group: any) => [String(group.id), group.name])),
      });
      groups.value = result.groups.map((group: any) => ({
        ...group,
        name: `${group.name} · ${label(
          (
            {
              owner: "群主",
              admin: "管理员",
              member: "成员",
              unknown: ({
                role_conflict: "角色证据冲突",
                members_shape: "成员响应格式异常",
                member_not_found: "成员列表未找到当前账号",
                role_unrecognized: "平台角色未识别",
                member_request_failed: "成员查询失败",
              } as Record<string, string>)[group.role_reason] || "权限未知",
            } as Record<string, string>
          )[group.role || "unknown"],
          group.role || "unknown",
        )}`,
      }));
    }
  } catch {
    if (mounted && current === generation) groupError.value = true;
  } finally {
    if (mounted && current === generation) loadingGroups.value = false;
  }
}

async function start() {
  busy.value = true;
  error.value = "";
  const current = ++generation;
  if (!props.existing)
    emit("update:modelValue", {
      ...props.modelValue,
      id: `wangshangliao_${crypto.randomUUID().slice(0, 8)}`,
    });
  try {
    await nextTick();
    const state = await request("start");
    if (!mounted || generation !== current) {
      await botApi.registration("wangshangliao", {
        action: "cancel",
        instance_id: state.instance_id,
        registration_code: state.registration_code,
      });
      return;
    }
    flow.value = state;
    captchaReady.value = false;
    if (captchaTimeout) clearTimeout(captchaTimeout);
    captchaTimeout = setTimeout(() => {
      if (mounted && generation === current && !captchaReady.value)
        error.value = "captcha_load_failed";
    }, 15000);
    await nextTick();
    if (!(window as any).initNECaptcha) {
      await new Promise<void>((resolve, reject) => {
        const script = document.createElement("script");
        script.src = "https://cstaticdun.126.net/load.min.js";
        script.async = true;
        script.onload = () => resolve();
        script.onerror = () => {
          script.remove();
          reject(new Error("captcha_load_failed"));
        };
        document.head.append(script);
      });
    }
    if (!mounted || generation !== current) return;
    (window as any).initNECaptcha(
      {
        onClose: () => {
          if (!mounted || generation !== current || busy.value) return;
          pendingAction = "";
          awaitingCaptcha.value = false;
        },
        captchaId: state.captcha_id,
        element: `#${captchaElement}`,
        mode: "popup",
        width: "320px",
        apiVersion: 2,
        onVerify: async (failure: unknown, data: { validate: string }) => {
          if (!mounted || generation !== current || !pendingAction) return;
          const action = pendingAction;
          if (failure || !data?.validate) {
            busy.value = false;
            error.value = "captcha_failed";
            return;
          }
          pendingAction = "";
          awaitingCaptcha.value = false;
          busy.value = true;
          error.value = "";
          try {
            const result = await request(action, {
              account: account.value,
              password: action === "login" ? password.value : "",
              verification_code: sms.value,
              validate_str: data.validate,
            });
            if (!mounted || generation !== current) return;
            loginSaved.value = false;
            flow.value = result;
            cooldown.value = result.resend_after || 0;
            error.value = result.error || "";
            if (result.session_ref) void loadGroups();
            if (result.session_ref)
              emit("update:modelValue", {
                ...props.modelValue,
                session_ref: result.session_ref,
                account_id: result.account_id,
                nickname: result.nickname,
                login_method: loginMethod.value,
              });
          } catch {
            error.value = "registration_failed";
          } finally {
            password.value = "";
            sms.value = "";
            busy.value = false;
            captcha?.refresh();
          }
        },
      },
      (instance: any) => {
        if (!mounted || generation !== current) {
          instance.destroy?.();
          return;
        }
        if (captchaTimeout) clearTimeout(captchaTimeout);
        captcha = instance;
        captchaReady.value = true;
        if (error.value === "captcha_load_failed") error.value = "";
      },
      () => {
        if (!mounted || generation !== current) return;
        if (captchaTimeout) clearTimeout(captchaTimeout);
        error.value = "captcha_load_failed";
      },
    );
    timer = setInterval(async () => {
      if (cooldown.value > 0) cooldown.value--;
      if (flow.value.expires_in > 0) flow.value.expires_in--;
      else if (flow.value.registration_code) {
        error.value = "registration_expired";
        await cancel();
      }
    }, 1000);
  } catch (failure: any) {
    error.value = failure.response?.data?.message || "registration_failed";
  } finally {
    busy.value = false;
  }
}

function verify(action: LoginAction) {
  if (busy.value) return;
  if (action === "login" && (!account.value.trim() || !password.value)) {
    error.value = "login_input";
    return;
  }
  error.value = "";
  pendingAction = action;
  busy.value = true;
  void request(action, {
    account: account.value,
    password: action === "login" ? password.value : "",
    verification_code: sms.value,
  }).then((result) => {
    if (!mounted || !pendingAction) return;
    if (["login_input", "sms_input", "sms_challenge"].includes(result.error) && captcha) {
      busy.value = false;
      awaitingCaptcha.value = true;
      captcha.verify();
      return;
    }
    pendingAction = "";
    awaitingCaptcha.value = false;
    loginSaved.value = false;
    flow.value = result;
    cooldown.value = result.resend_after || 0;
    error.value = result.error || "";
    if (result.session_ref) void loadGroups();
    if (result.session_ref)
      emit("update:modelValue", {
        ...props.modelValue,
        session_ref: result.session_ref,
        account_id: result.account_id,
        nickname: result.nickname,
        login_method: loginMethod.value,
      });
  }).catch(() => {
    error.value = "registration_failed";
    pendingAction = "";
  }).finally(() => {
    if (!awaitingCaptcha.value) {
      password.value = "";
      sms.value = "";
      busy.value = false;
    }
  });
}

async function cancel() {
  generation++;
  awaitingCaptcha.value = false;
  groups.value = [];
  groupError.value = false;
  loadingGroups.value = false;
  pendingAction = "";
  password.value = "";
  sms.value = "";
  busy.value = false;
  if (timer) clearInterval(timer);
  if (captchaTimeout) clearTimeout(captchaTimeout);
  captcha?.destroy?.();
  captchaReady.value = false;
  try {
    if (flow.value.registration_code) await request("cancel");
  } catch {
    /* Server TTL also expires abandoned transactions. */
  }
  flow.value = {};
  const config = { ...props.modelValue };
  delete config.session_ref;
  if (mounted) emit("update:modelValue", config);
}

async function logout() {
  await cancel();
  try {
    await botApi.registration("wangshangliao", {
      action: "logout",
      instance_id: props.modelValue.id,
    });
    error.value = "";
    emit("update:modelValue", { ...props.modelValue, enable: false });
  } catch {
    error.value = "logout_failed";
  }
}

onBeforeUnmount(() => {
  window.removeEventListener('beforeunload', warnBeforeUnload);
  mounted = false;
  void cancel();
});
</script>

<style scoped>
.login-save-notice {
  position: sticky;
  top: 12px;
  z-index: 5;
  flex-shrink: 0;
}
</style>
