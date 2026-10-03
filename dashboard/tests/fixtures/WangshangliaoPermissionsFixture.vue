<template>
  <v-app>
    <v-main>
      <div class="pa-4 mx-auto" style="max-width: 720px">
        <WangshangliaoPermissions v-model="config" :groups="groups" />
        <div id="saved-policy" hidden :data-value="JSON.stringify(config.moderation)" />
      </div>
    </v-main>
  </v-app>
</template>

<script setup lang="ts">
import { ref } from 'vue';
import WangshangliaoPermissions from '../../src/components/platform/WangshangliaoPermissions.vue';

const groups = [{ id: '5', name: '审核验收群' }];
const config = ref({
  id: 'fixture-bot',
  enabled_groups: ['5'],
  reply_private: true,
  moderation: {
    enabled: true,
    automation_enabled: true,
    content_rules_since: 1790812800,
    permissions: { '5': ['mute', 'unmute', 'recall', 'kick'] },
    auto_kick: { '5': true },
    semantic: { enabled: true, provider_id: 'fixture-model', timeout_seconds: 12, context_limit: 20 },
  },
});
</script>
