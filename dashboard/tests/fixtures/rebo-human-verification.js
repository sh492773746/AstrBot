import { createApp, h, ref } from "vue";
import { createVuetify } from "vuetify";
import { VApp } from "vuetify/components";
import "vuetify/styles";
import PluginHumanVerification from "../../src/components/shared/PluginHumanVerification.vue";

window.verificationResults = [];
const active = ref(true);
window.reopenVerification = () => { active.value = true; };
const app = createApp({
  render: () => h(VApp, null, {
    default: () => active.value ? h(PluginHumanVerification, {
      onVerified: result => {
        window.verificationResults.push({ kind: "verified", ...result });
        active.value = false;
      },
      onFailed: reason => {
        window.verificationResults.push({ kind: "failed", reason });
        active.value = false;
      },
      onCancel: () => {
        window.verificationResults.push({ kind: "cancel" });
        active.value = false;
      },
    }) : null,
  }),
});
app.use(createVuetify()).mount("#app");
