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
    <v-alert
      v-if="existing && !flow.registration_code"
      :type="connectionNotice.type"
      :title="connectionNotice.title"
      variant="tonal"
      role="status"
      aria-live="polite"
      data-testid="wsl-connection-status"
    >
      {{ connectionNotice.text }}
    </v-alert>
    <v-alert
      v-if="existing && !flow.registration_code && businessAuthExpired"
      type="warning"
      variant="tonal"
      role="status"
      data-testid="wsl-business-auth"
    >
      {{
        label(
          "群成员、好友等业务查询的登录已失效，消息连接可能仍在线。若是在手机登录后发生，请先停止来回登录；这可能是同类设备登录冲突，并不代表账号被封。这里只能手动重新登录，不会自动重试。",
          "Authentication for member, friend and other business queries has expired; messaging may remain online. If this followed a phone sign-in, avoid alternating sign-ins: a same-device-class conflict is possible, not an established account ban. Reauthentication is manual, never automatic.",
        )
      }}
    </v-alert>
    <v-alert
      v-if="existing && !flow.registration_code && runtimeStat?.business_auth?.device_type === 3"
      type="info"
      variant="tonal"
      role="status"
      data-testid="wsl-device-profile"
    >
      {{ label(
        "当前保存的是安卓端登录，即使机器人运行在服务器上，也不等于电脑版登录。手机重新登录可能使它失效；更新程序不会自动转换旧登录，需要使用桌面兼容方式重新登录后再验证能否同时在线。",
        "This saved login uses the Android device class, even though the bot runs on a server. A phone sign-in may invalidate it. Updating does not convert existing credentials; a new desktop-compatible sign-in and coexistence test are required.",
      ) }}
    </v-alert>
    <v-alert
      v-if="existing && !flow.registration_code && receiveDecodeFailures > 0"
      type="warning"
      variant="tonal"
      role="status"
      data-testid="wsl-receive-health"
    >
      {{
        label(
          `本次运行有 ${receiveDecodeFailures} 条消息未通过解析或验证，未交给 AI、群管或结账处理。账号在线不代表这些消息已处理成功，请核对 AstrBot 日志。`,
          `${receiveDecodeFailures} messages failed decoding or verification during this run and were not sent to AI, moderation, or settlement. Being online does not mean they were processed. Check the AstrBot logs.`,
        )
      }}
    </v-alert>
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
          error === "account_change_requires_new_instance"
            ? "认证成功，但新账号需要创建新的机器人实例。"
            : error === "session_reference_invalid"
              ? "登录保存凭据已失效，需要重新开始登录。"
              : "认证成功但尚未连接，请保存登录。",
          error === "account_change_requires_new_instance"
            ? "Authenticated, but this new account needs a new bot instance."
            : error === "session_reference_invalid"
              ? "The login save reference expired. Start login again."
              : "Authenticated but not connected. Save your login.",
        )
      }}
      — {{ flow.nickname || flow.account_id }}
      <v-btn class="mt-2" color="primary" block prepend-icon="mdi-content-save" variant="tonal" :loading="busy" :disabled="busy || ['account_change_requires_new_instance', 'session_reference_invalid', 'account_already_configured'].includes(error)" @click="saveAuthenticated">{{ label('保存登录并连接', 'Save login and connect') }}</v-btn>
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
          errorAction === "request_sms" || errorAction === "resend_sms"
            ? "正在验证并请求短信，请稍候…"
            : errorAction === "verify_security_code"
              ? "正在验证安全码，请稍候…"
            : errorAction === "retry_connection"
              ? "正在获取消息连接，请稍候…"
            : "正在验证并提交登录，请稍候…",
          errorAction === "request_sms" || errorAction === "resend_sms"
            ? "Verifying and requesting an SMS…"
            : errorAction === "verify_security_code"
              ? "Verifying your security code…"
            : errorAction === "retry_connection"
              ? "Preparing the messaging connection…"
            : "Verifying and signing in…",
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
      <div v-else-if="secondaryStep" class="d-flex flex-column ga-3">
        <v-alert type="info" variant="tonal" role="status">
          {{ secondaryNotice }}
        </v-alert>
        <v-text-field
          v-if="flow.status === 'security_required'"
          v-model="securityCode"
          type="password"
          inputmode="numeric"
          maxlength="6"
          autocomplete="off"
          variant="outlined"
          density="comfortable"
          :label="label('二级安全码（6位数字）', 'Secondary security code (6 digits)')"
          :disabled="busy"
          @keydown.enter.prevent="completeSecondary('verify_security_code')"
        />
        <div class="d-flex flex-wrap ga-2">
          <v-btn
            v-if="flow.status === 'security_required'"
            color="primary"
            variant="tonal"
            prepend-icon="mdi-shield-check-outline"
            :loading="busy"
            :disabled="busy || securityCooldown > 0"
            @click="completeSecondary('verify_security_code')"
          >
            {{ label('验证安全码', 'Verify security code') }} {{ securityCooldown || '' }}
          </v-btn>
          <v-btn
            v-if="flow.status === 'connection_pending'"
            color="primary"
            variant="tonal"
            prepend-icon="mdi-connection"
            :loading="busy"
            :disabled="busy || securityCooldown > 0"
            @click="completeSecondary('retry_connection')"
          >
            {{ label('重新获取消息连接', 'Retry messaging setup') }} {{ securityCooldown || '' }}
          </v-btn>
          <v-btn variant="text" :disabled="busy" @click="restart">
            {{ label('重新开始', 'Start again') }}
          </v-btn>
          <v-btn variant="text" @click="cancel">{{ label('取消', 'Cancel') }}</v-btn>
        </div>
      </div>
      <template v-else>
        <v-btn-toggle v-if="flow.status !== 'sms_required'" v-model="loginMethod" mandatory :disabled="busy || awaitingCaptcha" color="primary" variant="tonal">
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
          :disabled="busy || awaitingCaptcha"
        />
        <v-text-field
          variant="outlined"
          density="comfortable"
          v-if="flow.status !== 'sms_required' && loginMethod === 'password'"
          v-model="password"
          type="password"
          :label="label('密码', 'Password')"
          autocomplete="new-password"
          :disabled="busy || awaitingCaptcha"
        />
        <v-text-field
          variant="outlined"
          density="comfortable"
          v-if="flow.status === 'sms_required'"
          v-model="sms"
          :label="label('短信验证码', 'SMS code')"
          maxlength="6"
          autocomplete="one-time-code"
          :disabled="busy || awaitingCaptcha"
        />
        <div :id="captchaElement" />
        <div class="d-flex flex-wrap ga-2">
          <v-btn
            variant="tonal"
            rounded="xl"
            prepend-icon="mdi-login"
            color="primary"
            :loading="busy"
            :disabled="busy || awaitingCaptcha || (flow.status !== 'sms_required' && loginMethod === 'sms' && cooldown > 0)"
            @click="
              verify(flow.status === 'sms_required' ? 'verify_sms' : (loginMethod === 'sms' ? 'request_sms' : 'login'))
            "
          >
            {{ label(flow.status !== "sms_required" && loginMethod === "sms" ? "验证并获取验证码" : "验证并登录", flow.status !== "sms_required" && loginMethod === "sms" ? "Verify and request SMS" : "Verify and log in") }}
            {{ flow.status !== "sms_required" && loginMethod === "sms" && cooldown > 0 ? cooldown : "" }}
          </v-btn>
          <v-btn
            v-if="flow.status === 'sms_required'"
            variant="text"
            rounded="xl"
            :disabled="busy || awaitingCaptcha || cooldown > 0"
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
          "群聊列表暂未读取成功，不代表账号已退出。请先查看上方连接状态，稍后再刷新群聊列表；不会删除登录信息或群配置。",
          "The group list could not be loaded. This does not establish that you were signed out. Check the connection status above and refresh groups later; saved login and group settings are retained.",
        )
      }}</v-alert>
      <div class="d-flex flex-wrap ga-2">
        <v-chip size="small" variant="tonal" :color="businessActive === true ? 'success' : businessActive === false ? 'warning' : undefined">
          {{ businessActive === true ? label('业务运行中', 'Business active') : businessActive === false ? label('业务暂停', 'Business suspended') : label('业务状态待核实', 'Business status unavailable') }}
        </v-chip>
        <v-chip size="small" variant="tonal">
          {{ groupsKnown ? `${label('已启用群', 'Enabled groups')} ${enabledGroupOptions.length}` : label('启用群数待核实', 'Enabled-group count unavailable') }}
        </v-chip>
      </div>
      <p class="text-body-2 text-medium-emphasis mb-0" data-testid="wsl-business-status">
        {{ businessNotice }}
      </p>
      <div v-for="group in groupsKnown ? enabledGroupOptions : []" :key="group.id" class="text-body-2">
        {{ group.name }}
      </div>
      <v-btn to="/plugin-page/wangshangliao_moderation/management"
        variant="tonal" prepend-icon="mdi-cog-outline" class="align-self-start">
        {{ label('打开旺商聊管理', 'Open Wangshangliao management') }}
      </v-btn>
    </div>
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

const props = defineProps<{
  modelValue: Record<string, any>;
  existing?: boolean;
  runtimeStat?: Record<string, any> | null;
}>();
const emit = defineEmits(["update:modelValue", "group-directory"]);
const { locale } = useI18n();
const label = (zh: string, en: string) =>
  locale.value.startsWith("zh") ? zh : en;
const receiveDecodeFailures = computed(() => {
  const count = props.runtimeStat?.receive_health?.decode_failures;
  return Number.isSafeInteger(count) && count > 0 ? count : 0;
});
const account = ref("");
const password = ref("");
const sms = ref("");
const securityCode = ref("");
const error = ref("");
const errorAction = ref<LoginAction | "">("");
const errorMessages = computed(() => {
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
    login_rejected_unspecified: [
      "旺商聊返回通用登录错误（1001），没有说明具体原因。这不等于密码错误或账号被封，请勿连续重试。",
      "Wangshangliao returned a general login error (1001) without a specific reason. This does not establish an incorrect password or an account ban. Do not retry repeatedly.",
    ],
    invalid_sms: ["短信验证码错误", "Incorrect SMS code."],
    phone_not_found: [
      "旺商聊未找到该手机号对应的账号，请核对账号绑定手机号及国家码；此返回码不代表验证码错误或账号封禁，请勿连续重试。",
      "Wangshangliao could not find an account for this phone number. Check the bound phone number and country code. This code does not indicate an incorrect SMS code or an account ban. Do not retry repeatedly.",
    ],
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
    sms_input: [
      flow.value.status === "sms_required" ? "请填写6位短信验证码" : "请填写11位手机号",
      flow.value.status === "sms_required" ? "Enter the six-digit SMS code." : "Enter an eleven-digit phone number.",
    ],
    sms_challenge: [
      "未取得有效短信验证，请取消后重新开始",
      "No valid SMS challenge was received. Cancel and start again.",
    ],
    captcha_required: [
      "请完成安全验证",
      "Complete the verification.",
    ],
    nim_kicked: [
      "旺商聊已断开此消息连接。请先核对官方 App 或其他设备的登录情况，再重新登录；不能仅据此判断账号被封。",
      "Wangshangliao closed this messaging connection. Check sign-ins in the official app or on other devices, then sign in again. This alone does not establish an account ban.",
    ],
    reauth_required: [
      "账号会话已失效，请重新登录",
      "The account session expired. Sign in again.",
    ],
    secondary_security_code_required: [
      "旺商聊要求此账号完成二级安全验证。请核对官方 App 中对应账号的二级安全码设置；若已设置仍报错，请核对登录状态或联系旺商聊客服。不要把安全码发给机器人。",
      "Wangshangliao requires secondary security verification for this account. Check its secondary security code settings in the official app. If already set, check the sign-in state or contact Wangshangliao support. Do not send the code to the bot.",
    ],
    secondary_security_unverified: [
      "此账号尚未完成本次登录的安全验证，请在下方输入安全码，不要发到聊天中。",
      "This sign-in needs security verification. Enter the code below, not in a chat.",
    ],
    secondary_security_code_invalid: [
      "安全码不正确，请核对后等待30秒再试，不要连续尝试。",
      "Incorrect security code. Check it and wait 30 seconds before trying again.",
    ],
    secondary_security_locked: [
      "安全验证已被平台锁定，请在官方 App 核对解锁时间；这里不会自动重试。",
      "Security verification is locked. Check the official app for unlock information. No automatic retry will occur.",
    ],
    secondary_security_phone_required: [
      "请先在官方 App 为此账号绑定手机号，再重新登录。",
      "Bind a phone number in the official app, then sign in again.",
    ],
    account_clearing: [
      "此账号正在由平台清理，请在官方 App 核对账号状态。",
      "The platform is clearing this account. Check its status in the official app.",
    ],
    security_code_input: [
      "请填写6位数字安全码。",
      "Enter the six-digit security code.",
    ],
    security_code_cooldown: [
      "请等待安全验证倒计时结束，不要连续提交。",
      "Wait for the security verification cooldown before submitting again.",
    ],
    security_rotation_missing: [
      "没有收到可确认的登录状态，这不表示安全码错误。请取消后重新登录，不要重复提交安全码。",
      "The sign-in state could not be confirmed. This does not mean the security code was incorrect. Cancel and sign in again instead of resubmitting the code.",
    ],
    rate_limited: [
      "平台请求受限，请稍后重试",
      "The platform rate limit was reached. Retry later.",
    ],
    transport: [
      "请求未完成，可能是网络或平台暂时无响应。请先刷新核对结果，不要连续重新提交密码、验证码或安全码。",
      "The request did not complete; the network or platform may be unavailable. Refresh and check the result before resubmitting a password or verification code.",
    ],
    business_rejected: [
      (flow.value.error_action || errorAction.value) === "request_sms" ||
      (flow.value.error_action || errorAction.value) === "resend_sms"
        ? "旺商聊未接受短信请求；不能据此判断账号被封，请勿连续重试。"
        : "旺商聊未接受登录请求；不能据此判断账号被封，请勿连续重试。",
      (flow.value.error_action || errorAction.value) === "request_sms" ||
      (flow.value.error_action || errorAction.value) === "resend_sms"
        ? "Wangshangliao did not accept the SMS request. This alone does not confirm an account ban. Do not retry repeatedly."
        : "Wangshangliao did not accept the login request. This alone does not confirm an account ban. Do not retry repeatedly.",
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
      "本次登录流程未完成，尚不能确定具体原因。请先刷新核对当前账号状态，再重新开始或联系管理员。",
      "This login flow did not complete, and the cause is not yet confirmed. Refresh and check the account state before starting again or contacting your administrator.",
    ],
    account_change_requires_new_instance: [
      "新账号不能覆盖旧机器人实例。请关闭编辑，点击「创建机器人」登录新账号；旧账本保留，数据迁移需单独处理。",
      "A new account cannot replace the existing bot instance. Close the editor and create a new bot for this account. The old ledger is retained; data migration is separate.",
    ],
    account_already_configured: [
      "此账号已有机器人实例，请返回对应实例操作，不能重复创建。",
      "This account already has a bot instance. Use that instance instead of creating a duplicate.",
    ],
    session_reference_invalid: [
      "登录保存凭据已失效，请重新开始登录，原实例配置未被覆盖。",
      "The login save reference is invalid or expired. Start login again. The existing instance was not overwritten.",
    ],
    login_required: [
      "请先完成登录认证，再保存机器人。",
      "Complete authentication before saving the bot.",
    ],
    login_save_failed: [
      "保存登录未完成，请刷新核对当前实例状态；请勿连续重复提交。",
      "Saving the login did not complete. Refresh and check the instance state before submitting again.",
    ],
    login_configuration_unavailable: [
      "暂时无法核对这个机器人的当前设置，尚未保存登录。这不表示安全码错误，请刷新核对账号后再试。",
      "The current bot settings could not be verified, so the login was not saved. This does not mean the security code was incorrect. Refresh and check the account before trying again.",
    ],
    platform_config_fields: [
      "登录验证已完成，但保存内容与服务器不兼容。请刷新页面后重新核对；不要重复提交安全码。",
      "Authentication completed, but the save payload is incompatible with the server. Refresh and check the page instead of resubmitting the security code.",
    ],
    login_business_settings_changed: [
      "登录验证已完成，但群设置发生变化，连接尚未保存。请刷新核对；群设置需在旺商聊管理页单独保存。",
      "Authentication completed, but group settings changed and the connection was not saved. Refresh and review them; save group settings separately under Wangshangliao Management.",
    ],
    ai_routes_invalid: [
      "登录验证已完成，但现有模型与群配置不匹配，连接尚未保存。请在旺商聊管理页核对配置，不要重复提交安全码。",
      "Authentication completed, but the existing model routes do not match the groups. Check Wangshangliao Management instead of resubmitting the security code.",
    ],
    developer_test_invalid: [
      "登录验证已完成，但测试配置与当前群设置不匹配，连接尚未保存。请在旺商聊管理页核对测试配置。",
      "Authentication completed, but the test configuration does not match the current groups. Check the test settings under Wangshangliao Management.",
    ],
    registration_rate_limit: [
      "尝试过于频繁，请稍后重试",
      "Too many attempts. Try again later.",
    ],
    logout_failed: ["退出失败，请重试", "Logout failed. Try again."],
    account_conflict: [
      "平台报告账号登录冲突。请核对官方 App、其他设备及重复机器人实例，再决定重新连接；不要连续重新登录。",
      "The platform reported a sign-in conflict. Check the official app, other devices and duplicate bot instances before reconnecting. Do not sign in repeatedly.",
    ],
    account_already_active: [
      "此账号已被另一个机器人实例使用。请保留一个实例运行，不要重复登录。",
      "Another running bot instance already uses this account. Keep only one instance running instead of signing in repeatedly.",
    ],
    message_key_missing: [
      "服务器缺少消息解码配置，请联系总控管理员处理；不需要重新输入账号或安全码。",
      "The server is missing its message-decoding configuration. Contact the service administrator; re-entering account credentials or a security code will not fix this.",
    ],
    login_binding: [
      "平台返回的登录信息与当前账号不一致，已停止连接。请重新登录核对；不要覆盖为其他账号。",
      "The returned login information does not match this account, so connection was stopped. Sign in again and verify the account; do not replace it with a different account.",
    ],
    login_identity: [
      "尚未确认当前账号身份，已停止连接。请重新登录核对，不会将其他账号的数据合并到这里。",
      "The account identity could not be confirmed, so connection was stopped. Sign in again and verify it; other accounts' data will not be merged here.",
    ],
    response_invalid: [
      "平台响应暂时无法识别，请查看 AstrBot 日志并联系管理员；不能据此判断密码错误或账号被封。",
      "The platform response could not be recognized. Check AstrBot logs and contact your administrator; this does not establish an incorrect password or an account ban.",
    ],
    response_shape: [
      "平台返回的数据格式与预期不符，请查看 AstrBot 日志并联系管理员；不要连续重复登录。",
      "The platform returned an unexpected data format. Check AstrBot logs and contact your administrator instead of signing in repeatedly.",
    ],
  };
  return messages;
});
const errorText = computed(() => {
  const message = Object.prototype.hasOwnProperty.call(errorMessages.value, error.value)
    ? label(...errorMessages.value[error.value])
    : label(
        "本次操作未完成，原因尚未确认。请先刷新核对状态，再联系管理员；不要连续重复提交。",
        "This operation did not complete; the cause is not confirmed. Refresh and check the state before contacting your administrator. Do not submit repeatedly.",
      );
  const code = flow.value.error === error.value ? flow.value.provider_code : null;
  return typeof code === "number" && Number.isInteger(code) &&
    code >= -2147483648 && code <= 2147483647
    ? `${message} ${label("平台返回码", "Provider code")}：${code}`
    : message;
});
const busy = ref(false);
async function saveAuthenticated() {
  if (busy.value) return;
  busy.value = true;
  error.value = '';
  try {
    let managedPolicy = Boolean(props.modelValue._business_revision);
    if (props.existing && !managedPolicy) {
      // The platform editor can receive transport-only system configuration.
      const current = await botApi.get(props.modelValue.id);
      const saved = current.data.data?.bot;
      if (current.data.status !== 'ok' || saved?.id !== props.modelValue.id ||
          saved?.type !== 'wangshangliao')
        throw new Error('login_configuration_unavailable');
      managedPolicy = Boolean(saved._business_revision);
    }
    // Reauthentication must not round-trip effective policy or read-only grants.
    const fields = [
      "id", "type", "enable", "name", "login_method",
      ...(!props.existing || !managedPolicy
        ? ["enabled_groups", "reply_private", "reply_groups", "ai_routes",
          "proactive_send", "moderation", "developer_test"]
        : []),
    ];
    const config = {
      ...Object.fromEntries(Object.entries(props.modelValue).filter(([key]) => fields.includes(key))),
      session_ref: flow.value.session_ref,
      account_id: flow.value.account_id,
      nickname: flow.value.nickname,
    };
    const response = props.existing ? await botApi.update(props.modelValue.id, config) : await botApi.create(config);
    if (response.data.status !== 'ok')
      throw new Error(response.data.message || 'login_save_failed');
    loginSaved.value = true;
    window.location.reload();
  } catch (failure: any) {
    const message = failure.response?.data?.message || failure.message;
    const reason = ["请在旺商聊插件管理页修改业务设置", "请分别保存连接设置与插件业务设置"].includes(message)
      ? "login_business_settings_changed"
      : message;
    error.value = [
      'account_change_requires_new_instance',
      'account_already_configured',
      'session_reference_invalid',
      'login_required',
      'login_configuration_unavailable',
      'platform_config_fields',
      'login_business_settings_changed',
      'ai_routes_invalid',
      'developer_test_invalid',
    ].includes(reason) ? reason : 'login_save_failed';
  } finally {
    busy.value = false;
  }
}
const awaitingCaptcha = ref(false);
const loginMethod = ref(props.modelValue.login_method || "password");
watch(loginMethod, () => {
  error.value = "";
  errorAction.value = "";
  password.value = "";
  sms.value = "";
});
const groups = ref<Array<{ id: string; name: string }>>([]);
const groupAuthExpired = ref(false);
const businessAuthExpired = computed(() => groupAuthExpired.value ||
  props.runtimeStat?.business_auth?.state === "expired");
const businessActive = computed(() => typeof props.runtimeStat?.business_active === "boolean"
  ? props.runtimeStat.business_active
  : typeof props.modelValue._business_active === "boolean"
    ? props.modelValue._business_active
    : null);
const connectionNotice = computed<{
  type: "success" | "info" | "warning" | "error";
  title: string;
  text: string;
}>(() => {
  const state = props.runtimeStat?.connection_state;
  if (state === "online")
    return {
      type: businessAuthExpired.value ? "warning" : "success",
      title: businessAuthExpired.value
        ? label("消息在线，业务登录已失效", "Messaging online; business sign-in expired")
        : label("账号已在线", "Account online"),
      text: businessAuthExpired.value ? label(
        "消息连接正常，但业务查询需要重新登录。请按下方提示处理；账号在线不代表群管和结账可以正常使用。",
        "Messaging is connected, but business queries require a new sign-in. Follow the notice below; being online does not mean moderation or settlement is available.",
      ) : label(
        "消息连接正常，无需重新登录。群功能是否运行另看下方业务状态；历史错误记录不代表当前仍然故障。",
        "The messaging connection is online; no new login is needed. Group features depend on the business status below. Historical errors do not mean the connection is still failing.",
      ),
    };
  if (state === "reconnecting")
    return {
      type: "info",
      title: label("正在连接或重连", "Connecting or reconnecting"),
      text: label(
        "消息连接暂未就绪，系统会按间隔尝试恢复；不等于密码错误或账号被封，请勿反复重新登录。连接恢复前暂停群内操作。",
        "Messaging is not ready; the service will retry at intervals. This does not establish an incorrect password or an account ban. Avoid repeated sign-ins; group actions wait for reconnection.",
      ),
    };
  if (state === "rate_limited")
    return {
      type: "warning",
      title: label("平台限流，等待恢复", "Platform rate limit; waiting"),
      text: label(
        "旺商聊暂时限制请求，系统会按平台等待时间重连。请勿连续点击登录或刷新；这不等于账号被封。",
        "Wangshangliao is temporarily limiting requests. Reconnection respects its waiting period. Avoid repeated login or refresh attempts; this does not establish an account ban.",
      ),
    };
  if (state === "stopped")
    return {
      type: "info",
      title: label("消息连接已停止", "Messaging stopped"),
      text: label(
        "当前没有运行消息连接；停用机器人会保留登录信息。需要使用时启用并保存；若仍未连接，请查看 AstrBot 日志，不要直接删除实例。",
        "No messaging connection is running. Disabling the bot retains its login. Enable and save when needed; if it still cannot connect, check AstrBot logs instead of deleting the instance.",
      ),
    };
  if (["error", "reauth_required", "account_conflict"].includes(state)) {
    const code = props.runtimeStat?.current_error ||
      (state === "error" ? "" : state);
    return {
      type: "error",
      title: label("连接已停止，需要处理", "Connection stopped; action required"),
      text: Object.prototype.hasOwnProperty.call(errorMessages.value, code)
        ? label(...errorMessages.value[code])
        : label(
          "消息连接未能启动，具体原因尚未确认。请查看 AstrBot 日志或联系管理员；不能仅据此判断密码错误或账号被封，不会自动重复提交登录。",
          "Messaging could not start, and the cause is not confirmed. Check AstrBot logs or contact your administrator. This alone does not establish an incorrect password or an account ban; login is not automatically resubmitted.",
        ),
    };
  }
  return {
    type: "info",
    title: label("连接状态待核实", "Connection status unavailable"),
    text: label(
      "尚未读取到当前连接状态，不能据此判断已退出或登录失败。请稍后刷新；不要重复创建实例。",
      "The current connection state has not been loaded. This does not establish a logout or login failure. Refresh later instead of creating a duplicate instance.",
    ),
  };
});
const groupsKnown = computed(() => Array.isArray(props.runtimeStat?.enabled_groups) ||
  (!props.existing && Array.isArray(props.modelValue.enabled_groups)));
const businessNotice = computed(() => {
  if (businessActive.value === false)
    return label(
      "业务已暂停，群内自动处理不会执行；这不等于账号退出，群配置和历史账本保留。",
      "Business is paused, so automated group actions will not run. This is not an account logout; group settings and the historical ledger are retained.",
    );
  if (businessActive.value === null || !groupsKnown.value)
    return label(
      "业务状态或启用群数尚未核实，不按0群处理，也不会自动开启群功能。请稍后刷新。",
      "Business status or the enabled-group count has not been verified. It is not assumed to be zero, and group features will not be enabled automatically. Refresh later.",
    );
  if (!enabledGroups.value.length)
    return label(
      "当前未启用任何业务群，群内自动处理不会执行；不代表登录失败，私聊仍按独立设置处理。",
      "No business groups are enabled, so automated group actions will not run. This is not a login failure; private messages follow their separate settings.",
    );
  return label(
    "已启用群仍需保持连接在线，并核实机器人具有所需群权限；不会仅因账号在线就自动开启所有功能。",
    "Enabled groups still require an online connection and the bot's necessary group permissions. An online account does not automatically enable every feature.",
  );
});
const enabledGroups = computed<string[]>(() => props.runtimeStat?.enabled_groups ?? props.modelValue.enabled_groups ?? []);
const groupOptions = computed(() => {
  const known = new Set(groups.value.map(group => group.id));
  return [...groups.value, ...enabledGroups.value
    .filter((id: string) => !known.has(id))
    .map((id: string) => ({ id, name: id }))];
});
const enabledGroupOptions = computed(() => groupOptions.value
  .filter(group => enabledGroups.value.includes(group.id))
  .map(group => ({ id: group.id, name: group.name === group.id
    ? `${label('群名待加载', 'Group name unavailable')} (${group.id})`
    : `${group.name} (${group.id})` })));
onMounted(() => { if (props.existing) void loadGroups(); });
const loadingGroups = ref(false);
const groupError = ref(false);
const flow = ref<Record<string, any>>({});
const secondaryStep = computed(() => [
  'security_required', 'security_verifying', 'security_setup_required', 'security_locked',
  'security_uncertain', 'connection_pending',
].includes(flow.value.status));
const secondaryNotice = computed(() => {
  if (flow.value.status === 'security_verifying')
    return label('安全验证仍在处理中，请稍候；不要重复提交。稍后可取消并重新登录。', 'Security verification is still in progress. Do not resubmit. Cancel and sign in again if needed.');
  if (flow.value.status === 'security_required')
    return label(
      '账号密码或短信已验证。新版旺商聊还需要验证本次登录的二级安全码；只在这里输入，不会保存。',
      'Your password or SMS was verified. The current app also requires the secondary security code for this sign-in. Enter it only here; it will not be saved.',
    );
  if (flow.value.status === 'connection_pending')
    return label(
      '账号验证已完成，但消息连接尚未准备好。可重新获取连接，无需再次提交安全码。',
      'Account verification is complete, but messaging setup has not finished. Retry setup without submitting the security code again.',
    );
  if (flow.value.status === 'security_locked')
    return label('请先在官方 App 核对安全验证锁定状态，再重新开始。', 'Check the security lock in the official app before starting again.');
  if (flow.value.status === 'security_uncertain')
    return label('安全验证结果尚不确定，已停止重试。请取消后重新登录。', 'The verification result is uncertain. Retries are stopped. Cancel and sign in again.');
  return label('请先在官方 App 完成账号安全设置，再重新开始登录。', 'Complete the security settings in the official app, then start login again.');
});
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
const securityCooldown = ref(0);
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
  | "verify_security_code"
  | "retry_connection"
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
  if (response.data.status !== "ok" || !response.data.data)
    throw new Error(response.data.message || "registration_failed");
  return response.data.data;
}

function acceptLoginState(result: Record<string, any>) {
  loginSaved.value = false;
  flow.value = result;
  cooldown.value = result.resend_after || 0;
  securityCooldown.value = result.security_retry_after || 0;
  error.value = result.error || "";
  if (!result.session_ref) return;
  void loadGroups();
  emit("update:modelValue", {
    ...props.modelValue,
    session_ref: result.session_ref,
    account_id: result.account_id,
    nickname: result.nickname,
    login_method: loginMethod.value,
  });
}

async function completeSecondary(action: "verify_security_code" | "retry_connection") {
  if (busy.value || securityCooldown.value > 0) return;
  if (action === 'verify_security_code' && !/^[0-9]{6}$/.test(securityCode.value)) {
    error.value = 'security_code_input';
    return;
  }
  const current = generation;
  busy.value = true;
  error.value = '';
  errorAction.value = action;
  try {
    const result = await request(action, action === 'verify_security_code'
      ? { security_code: securityCode.value } : {});
    if (mounted && generation === current) acceptLoginState(result);
  } catch (failure: any) {
    if (!mounted || generation !== current) return;
    error.value = failure.response?.data?.message || 'registration_failed';
    // A lost response must be reconciled without resending a security code.
    try {
      const result = await request('poll');
      if (mounted && generation === current) acceptLoginState(result);
    } catch {
      if (mounted && generation === current) flow.value = { ...flow.value, status: 'security_uncertain' };
    }
  } finally {
    securityCode.value = '';
    if (mounted && generation === current) busy.value = false;
  }
}

async function restart() {
  await cancel();
  if (mounted) await start();
}

async function loadGroups() {
  const current = generation;
  loadingGroups.value = true;
  groupError.value = false;
  try {
    const result = await request("groups");
    if (mounted && current === generation) {
      groupAuthExpired.value = false;
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
  } catch (failure: any) {
    if (mounted && current === generation) {
      groupError.value = true;
      if ((failure.response?.data?.message || failure.message) === "reauth_required")
        groupAuthExpired.value = true;
    }
  } finally {
    if (mounted && current === generation) loadingGroups.value = false;
  }
}

async function start() {
  busy.value = true;
  error.value = "";
  errorAction.value = "";
  cooldown.value = 0;
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
            acceptLoginState(result);
          } catch (failure: any) {
            if (!mounted || generation !== current) return;
            error.value = failure.response?.data?.message || failure.message || "registration_failed";
          } finally {
            if (mounted && generation === current) {
              password.value = "";
              sms.value = "";
              busy.value = false;
              captcha?.refresh();
            }
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
      if (securityCooldown.value > 0) securityCooldown.value--;
      if (flow.value.expires_in > 0) flow.value.expires_in--;
      else if (flow.value.registration_code) {
        await cancel();
        if (mounted) error.value = "registration_expired";
      }
    }, 1000);
  } catch (failure: any) {
    if (!mounted || generation !== current) return;
    error.value = failure.response?.data?.message || "registration_failed";
  } finally {
    if (mounted && generation === current) busy.value = false;
  }
}

function verify(action: LoginAction) {
  if (busy.value || awaitingCaptcha.value) return;
  if (action === "login" && (!account.value.trim() || !password.value)) {
    error.value = "login_input";
    return;
  }
  if (action === "request_sms" && !/^[0-9]{11}$/.test(account.value)) {
    error.value = "sms_input";
    return;
  }
  if (action === "verify_sms" && !/^[0-9]{6}$/.test(sms.value)) {
    error.value = "sms_input";
    return;
  }
  if (["request_sms", "resend_sms"].includes(action) && cooldown.value > 0) {
    error.value = "sms_cooldown";
    return;
  }
  const current = generation;
  error.value = "";
  errorAction.value = action;
  pendingAction = action;
  busy.value = true;
  void request(action, {
    account: account.value,
    password: action === "login" ? password.value : "",
    verification_code: sms.value,
  }).then((result) => {
    if (!mounted || generation !== current || pendingAction !== action) return;
    if ((result.error === "captcha_required" ||
      (["login_input", "sms_input", "sms_challenge"].includes(result.error) &&
        !result.error_action)) && captcha) {
      busy.value = false;
      awaitingCaptcha.value = true;
      captcha.verify();
      return;
    }
    pendingAction = "";
    awaitingCaptcha.value = false;
    acceptLoginState(result);
  }).catch((failure: any) => {
    if (!mounted || generation !== current) return;
    error.value = failure.response?.data?.message || failure.message || "registration_failed";
    pendingAction = "";
  }).finally(() => {
    if (mounted && generation === current && !awaitingCaptcha.value) {
      password.value = "";
      sms.value = "";
      busy.value = false;
    }
  });
}

async function cancel() {
  const current = ++generation;
  awaitingCaptcha.value = false;
  groups.value = [];
  groupError.value = false;
  loadingGroups.value = false;
  pendingAction = "";
  password.value = "";
  sms.value = "";
  securityCode.value = "";
  error.value = "";
  errorAction.value = "";
  cooldown.value = 0;
  securityCooldown.value = 0;
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
  if (generation !== current) return;
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
