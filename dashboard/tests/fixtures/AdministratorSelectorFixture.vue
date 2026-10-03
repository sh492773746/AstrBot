<template>
  <v-app>
    <v-main>
      <div style="max-width: 680px; padding: 24px; margin: auto">
        <h1 style="font-size: 20px; margin-bottom: 20px">Administrator Selector</h1>
        <ConfigItemRenderer
          v-model="administrators"
          config-key="admins_id"
          :item-meta="{ type: 'list', readonly }"
        />
        <output data-testid="administrators">{{ JSON.stringify(administrators) }}</output>
        <v-checkbox v-model="readonly" label="Readonly" />
      </div>
    </v-main>
  </v-app>
</template>

<script setup>
import { ref } from 'vue'
import ConfigItemRenderer from '@/components/shared/ConfigItemRenderer.vue'

const administrators = ref(['20000001', 'legacy-account'])
const readonly = ref(false)
</script>
