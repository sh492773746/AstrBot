<script setup lang="ts">
import { Plus, Trash2 } from "@lucide/vue";

type LiteralRule = { name: string; terms: string[]; enabled: boolean };
const props = defineProps<{ modelValue: LiteralRule[]; disabled?: boolean }>();
const emit = defineEmits<{ (event: "update:modelValue", value: LiteralRule[]): void }>();
function update(index: number, change: Partial<LiteralRule>) {
  if (props.disabled) return;
  emit("update:modelValue", props.modelValue.map((rule, i) =>
    i === index ? { ...rule, ...change } : rule));
}
function add() {
  if (props.disabled || props.modelValue.length >= 20) return;
  emit("update:modelValue", [...props.modelValue, { name: "", terms: ["", ""], enabled: true }]);
}
function remove(index: number) {
  if (props.disabled) return;
  emit("update:modelValue", props.modelValue.filter((_, i) => i !== index));
}
</script>

<template>
  <fieldset class="group-rules" :disabled="disabled">
    <legend>本群组合规则 · {{ modelValue.length }} / 20</legend>
    <div v-for="(rule, index) in modelValue" :key="index" class="rule-row">
      <label class="enabled"><input type="checkbox" :checked="rule.enabled"
        :aria-label="`启用第${index + 1}条规则`"
        @change="update(index, { enabled: ($event.target as HTMLInputElement).checked })" />{{ index + 1 }}</label>
      <label>规则名称<input :value="rule.name" maxlength="24" :aria-label="`第${index + 1}条规则名称`"
        @input="update(index, { name: ($event.target as HTMLInputElement).value })" /></label>
      <label>同时包含的词（用 / 或 | 分隔）<input :value="rule.terms.join(' | ')" maxlength="216"
        :aria-label="`第${index + 1}条组合词`"
        @input="update(index, { terms: ($event.target as HTMLInputElement).value.split(($event.target as HTMLInputElement).value.includes('|') ? '|' : '/').map(t => t.trim()) })" /></label>
      <button type="button" class="icon-button" :title="`删除第${index + 1}条规则`"
        :aria-label="`删除第${index + 1}条规则`" @click="remove(index)"><Trash2 :size="17" /></button>
    </div>
    <button type="button" :disabled="disabled || modelValue.length >= 20" @click="add"><Plus :size="16" />添加组合规则</button>
  </fieldset>
</template>

<style scoped>
.group-rules { margin: 16px 0; padding: 12px; border: 1px solid #d3dce3; min-width: 0; }
.rule-row { display: grid; grid-template-columns: 42px minmax(100px, 1fr) minmax(160px, 2fr) 36px; gap: 10px; align-items: end; margin-bottom: 12px; }
label { display: flex; flex-direction: column; gap: 5px; font-size: 13px; min-width: 0; overflow-wrap: anywhere; }
.enabled { flex-direction: row; align-items: center; height: 36px; }
input:not([type="checkbox"]) { box-sizing: border-box; width: 100%; min-width: 0; min-height: 36px; padding: 6px 8px; border: 1px solid #a9b6c0; border-radius: 4px; font: inherit; background: #fff; color: #17252d; }
button { display: inline-flex; align-items: center; justify-content: center; gap: 6px; min-height: 36px; border: 1px solid #a9b6c0; border-radius: 4px; padding: 6px 10px; background: #fff; color: #17252d; cursor: pointer; }
.icon-button { width: 36px; height: 36px; padding: 0; color: #aa3535; }
button:disabled { opacity: .5; cursor: default; }
@media (max-width: 600px) {
  .rule-row { grid-template-columns: 34px minmax(0, 1fr) 36px; gap: 8px; }
  .rule-row > label:nth-child(3) { grid-column: 2; }
  .rule-row > button { grid-column: 3; grid-row: 1 / 3; align-self: center; }
}
</style>
