<template>
  <v-app>
    <PlatformPage v-if="workbench" />
    <v-container v-else-if="existing" style="max-width: 800px">
      <WangshangliaoLogin v-model="existingConfig" :runtime-stat="runtimeStat" existing />
    </v-container>
    <AddNewPlatform v-else v-model:show="show" :metadata="metadata" :config_data="config" />
    <p v-if="!show">Fixture saved</p>
  </v-app>
</template>
<script setup>
import { ref } from 'vue';
import AddNewPlatform from '../../src/components/platform/AddNewPlatform.vue';
import WangshangliaoLogin from '../../src/components/platform/WangshangliaoLogin.vue';
import PlatformPage from '../../src/views/PlatformPage.vue';
const workbench = new URLSearchParams(window.location.search).has('workbench');
const existing = new URLSearchParams(window.location.search).has('existing');
const runtimeStat = ref(new URLSearchParams(window.location.search).has('runtime') ? {
  business_active: true, enabled_groups: ["1516387"], connection_state: "online",
} : null);
window.setWslFixtureRuntime = value => { runtimeStat.value = value; };
const existingConfig = ref({
  id: 'fixture-existing', type: 'wangshangliao', enable: false,
  account_id: '42', enabled_groups: [],
  ...(new URLSearchParams(window.location.search).has('integrated') ? {
    _business_revision: 'fixture-policy-revision',
    _business_active: true, _business_epoch: 3,
    _open_integrated: true, _open_paused: false,
    _open_groups: {'34': {enabled: true}},
    _open_managers: {'34': ['42']},
    _open_revisions: {'34': 'fixture-tenant-revision'},
    _open_native_roles: {'34': 'GROUP_ROLE_ADMIN'},
    enabled_groups: ['34'],
    reply_groups: {'34': true},
    moderation: {enabled: true, permissions: {'34': ['mute', 'recall']}},
    ai_routes: {admin_provider_id: '', groups: {'34': 'fixture-model'}},
    proactive_send: {enabled: true, targets: ['34']},
    developer_test: {groups: ['34']},
  } : {}),
});
const show = ref(true);
const config = { platform: [] };
const metadata = { platform_group: { metadata: { platform: { config_template: {
  '旺商聊': { id: 'wangshangliao', type: 'wangshangliao', enable: true, account_id: '', enabled_groups: [] }
} } } } };
</script>
