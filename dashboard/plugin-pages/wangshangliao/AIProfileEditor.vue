<script setup lang="ts">
const props = defineProps<{
  modelValue: Record<string, any>;
  choices: { configs: any[]; personas: any[] };
  providers: any[];
  label: string;
  privateChat?: boolean;
}>();
const emit = defineEmits<{
  "update:modelValue": [value: Record<string, any>];
}>();
function updateProvider(event: Event) {
  const value = (event.target as HTMLInputElement).value;
  emit("update:modelValue", {
    ...props.modelValue, provider_id: value,
  });
}
function levels(key: string) {
  const values: Record<string, number[]> = {
    temperature: [0.2, 0.5, 0.9],
    top_p: [0.6, 0.85, 1],
    max_tokens: props.privateChat ? [256, 512, 1024] : [128, 256, 512],
    history_turns: props.privateChat ? [2, 6, 12] : [2, 4, 6],
  };
  return values[key];
}
function level(key: string) {
  const value = props.modelValue[key];
  if (value === null || value === undefined) return "";
  const index = levels(key).indexOf(value);
  return index < 0 ? "existing" : String(index);
}
function chooseLevel(key: string, event: Event) {
  const value = (event.target as HTMLSelectElement).value;
  if (value === "existing") return;
  const index = Number(value);
  if (value !== "" && (!Number.isInteger(index) || index < 0 || index > 2)) return;
  emit("update:modelValue", {
    ...props.modelValue, [key]: value === "" ? null : levels(key)[index],
  });
}
</script>

<template>
  <div class="fields">
    <div class="binding"><span>AstrBot配置</span>
      <output :aria-label="`${label} · AstrBot配置`">{{ choices.configs.find(c => c.id === modelValue.config_id)?.name
        || (modelValue.config_id ? '绑定配置不可用' : '尚未创建专用配置') }}</output>
    </div>
    <div class="binding"><span>人格</span>
      <output :aria-label="`${label} · 人格`">{{ modelValue.persona_id === '__builtin__' ? '内置安全人格'
        : choices.personas.find(p => p.id === modelValue.persona_id)?.name
          || (modelValue.persona_id ? '绑定人格不可用' : '跟随固定配置') }}</output>
    </div>
    <label>模型<select :value="modelValue.provider_id" :aria-label="`${label} · 模型`"
      @change="updateProvider($event)">
      <option value="">跟随配置或共用模型</option>
      <option v-for="p in providers" :key="p.id" :value="p.id">{{ p.id }} · {{ p.model }}</option>
    </select></label>
    <label v-for="setting in [
      {key: 'temperature', name: '回复随机性'},
      {key: 'top_p', name: '表达范围'},
      {key: 'max_tokens', name: '回复长度'},
      {key: 'history_turns', name: privateChat ? '保留对话轮数' : '聊天上下文'},
    ]" :key="setting.key">{{ setting.name }}
      <select :aria-label="`${label} · ${setting.name}`" :value="level(setting.key)"
        @change="chooseLevel(setting.key, $event)">
        <option value="">跟随所选配置</option>
        <option value="0">低</option>
        <option value="1">中</option>
        <option value="2">高</option>
        <option v-if="level(setting.key) === 'existing'" value="existing" disabled>现有设置（保留）</option>
      </select>
    </label>
  </div>
</template>

<style scoped>
.fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; margin: 16px 0; }
label, .binding { display: flex; flex-direction: column; gap: 7px; min-width: 0; font-size: 14px; }
.binding output { overflow-wrap: anywhere; padding: 9px 0; }
input, select { width: 100%; box-sizing: border-box; min-width: 0; font: inherit; padding: 9px; border: 1px solid #9daaaa; border-radius: 4px; background: #fff; color: #202828; }
* { letter-spacing: 0; }
@media (max-width: 600px) { .fields { grid-template-columns: minmax(0, 1fr); } }
</style>
