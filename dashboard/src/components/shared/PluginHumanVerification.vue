<script setup>
import { onBeforeUnmount, onMounted, ref } from "vue";
import verificationDocument from "./plugin-human-verification.html?raw";

const emit = defineEmits(["verified", "failed", "cancel"]);
const frameRef = ref(null);
const ready = ref(false);
const requestKey = [...crypto.getRandomValues(new Uint32Array(4))].join("-");
// Only this fixed document gets cookie access; plugin HTML remains opaque.
const frameDocument = verificationDocument.replace(
  '"__REQUEST_KEY__"',
  JSON.stringify(requestKey),
);
let initTimer;
let expiryTimer;
let finished = false;

const handleMessage = (event) => {
  if (
    finished ||
    event.source !== frameRef.value?.contentWindow ||
    event.origin !== window.location.origin
  ) return;
  const message = event.data;
  if (
    message?.channel !== "astrbot-human-verification" ||
    message.requestKey !== requestKey
  ) return;
  if (message.kind === "ready") {
    ready.value = true;
    clearTimeout(initTimer);
  } else if (
    message.kind === "verified" &&
    typeof message.validate === "string" &&
    message.validate.length > 0 &&
    message.validate.length <= 4096
  ) {
    finished = true;
    emit("verified", { validate: message.validate });
  } else if (message.kind === "failed") {
    finished = true;
    const reasons = [
      "verification_init_timeout",
      "verification_script_error",
      "verification_sdk_load_failed",
      "verification_service_unavailable",
    ];
    emit("failed", reasons.includes(message.reason)
      ? message.reason : "verification_service_unavailable");
  }
};

onMounted(() => {
  window.addEventListener("message", handleMessage);
  initTimer = setTimeout(() => emit("failed", "verification_init_timeout"), 30000);
  expiryTimer = setTimeout(() => emit("failed", "verification_timeout"), 150000);
});
onBeforeUnmount(() => {
  window.removeEventListener("message", handleMessage);
  clearTimeout(initTimer);
  clearTimeout(expiryTimer);
});
</script>

<template>
  <v-dialog :model-value="true" max-width="400" persistent>
    <v-card>
      <v-card-title class="text-h3 pa-4 pb-0 pl-6">安全验证</v-card-title>
      <v-card-text class="pa-2">
        <p class="mb-2" role="status">
          {{ ready ? "请手动完成下方验证，完成后才会请求短信。" : "正在加载官方人机验证…" }}
        </p>
        <iframe
          ref="frameRef"
          title="官方人机验证"
          :srcdoc="frameDocument"
          sandbox="allow-scripts allow-same-origin"
          referrerpolicy="no-referrer"
          style="width:100%;height:310px;border:0;background:white"
        ></iframe>
      </v-card-text>
      <v-card-actions>
        <v-spacer />
        <v-btn variant="text" @click="emit('cancel')">取消</v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>
