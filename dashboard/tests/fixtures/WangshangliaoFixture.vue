<template>
  <v-app>
    <AddNewPlatform v-model:show="show" :metadata="metadata" :config_data="config" />
    <p v-if="!show">Fixture saved</p>
  </v-app>
</template>
<script setup>
import { ref } from 'vue';
import AddNewPlatform from '../../src/components/platform/AddNewPlatform.vue';
const show = ref(true);
const config = { platform: [] };
const metadata = { platform_group: { metadata: { platform: { config_template: {
  '旺商聊': { id: 'wangshangliao', type: 'wangshangliao', enable: true, account_id: '', enabled_groups: [] }
} } } } };
</script>
