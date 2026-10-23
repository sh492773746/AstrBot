<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { Check, CreditCard, Save, ShieldCheck } from "@lucide/vue";

const props = defineProps<{
  billing: any; tenant: any; members: Array<{ id: string; name: string }>;
  providers: Array<{ id: string; model: string }>; api: Function; disabled: boolean;
}>();
const emit = defineEmits(["saved"]);
const busy = ref(false);
const error = ref("");
const notice = ref("");
const settings = ref({ contact_name: "", contact: "", monthly_cny: "0", trial_monthly_cny: "20" });
const manager = ref("");
const tier = ref("plus");
const received = ref("134");
const preview = ref<any>(null);
const provider = ref("");
const priceVersion = ref(0);
const priceDefaults = {
  input_per_million: "", output_per_million: "", multiplier: "0.25", credit_cny: "1",
  input_ceiling: 0, output_ceiling: 4096, all_output_billed_in_limit: false, evidence: "",
};
const price = ref<typeof priceDefaults & { valid_until?: number }>({ ...priceDefaults });
const locked = computed(() => busy.value || props.disabled);
const pool = computed(() => props.billing?.pools.find((r: any) => r.uid === manager.value));
const payer = computed(() => props.billing?.payers.find((r: any) =>
  r.account === props.tenant?.account && r.group_id === props.tenant?.group_id));
const managers = computed(() => props.members.filter(m => props.tenant?.managers.includes(m.id)));
const yuan = (value: number) => (value / 1000000).toFixed(4);
const date = (value: number) => value ? new Date(value * 1000).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" }) : "默认长期";
watch(() => props.billing, value => {
  if (!value) return;
  settings.value = {
    contact_name: value.settings.contact_name, contact: value.settings.contact,
    monthly_cny: String(value.settings.monthly_micro / 1000000),
    trial_monthly_cny: String((value.settings.trial_monthly_micro ?? 20000000) / 1000000),
  };
  preview.value = null;
  choosePrice();
}, { immediate: true });
watch(() => props.tenant?.group_id + "/" + props.tenant?.account, () => {
  manager.value = props.tenant?.managers[0] || "";
  preview.value = null;
}, { immediate: true });
watch(tier, value => { received.value = value === "plus" ? "134" : "335"; preview.value = null; });
watch(manager, () => { preview.value = null; });
function choosePrice() {
  const row = props.billing?.prices.find((r: any) => r.provider === provider.value && r.current);
  price.value = { ...(row?.config || priceDefaults) };
  priceVersion.value = row?.version || 0;
}
async function run(task: () => Promise<void>) {
  if (locked.value) return;
  busy.value = true; error.value = ""; notice.value = "";
  try { await task(); } catch (e: any) { error.value = e?.message || "操作未确认，请刷新核对"; }
  finally { busy.value = false; }
}
async function saveSettings() {
  await props.api("billing_settings", { version: props.billing.settings.version, ...settings.value });
  notice.value = "升级客服、免费池和安全预算已保存";
  emit("saved");
}
async function grantPreview() {
  const row = props.tenant;
  const payload = {
    account: row.account, group_id: row.group_id, tenant_version: row.version,
    uid: manager.value, version: pool.value?.version || 0, tier: tier.value, received_cny: received.value,
  };
  preview.value = { payload, result: await props.api("plan_preview", payload) };
}
async function grant() {
  await props.api("plan_grant", { ...preview.value.payload, confirmation: preview.value.result.confirmation });
  preview.value = null; notice.value = "套餐已授予，未开启模型或群AI";
  emit("saved");
}
async function nominate() {
  if (!window.confirm("更换本群额度承担人后，需由其本人发送「确认本群扣费」，不会自动扣费。继续？")) return;
  await props.api("payer_nominate", {
    account: props.tenant.account, group_id: props.tenant.group_id, tenant_version: props.tenant.version,
    uid: manager.value, version: payer.value?.version || 0, pool_version: pool.value?.version || 0,
  });
  notice.value = "已指定候选承担人，等待本人确认";
  emit("saved");
}
async function savePrice() {
  await props.api("billing_price", { provider: provider.value, version: priceVersion.value, config: price.value });
  notice.value = "模型计费上界已登记，供应商硬预算门禁保持原状";
  emit("saved");
}
</script>

<template>
  <section v-if="billing" class="ai-plans" aria-label="AI套餐与费用保护">
    <h3><CreditCard :size="18" />AI套餐与共享额度</h3>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <div class="table-wrap">
      <table>
        <thead><tr><th>套餐</th><th>每日共享次数</th><th>有效期</th><th>售价</th><th>模型费用上限</th></tr></thead>
        <tbody>
          <tr><td>试用</td><td>20</td><td>默认长期</td><td>免费</td><td>每人每自然月3元</td></tr>
          <tr><td>Plus</td><td>2000</td><td>30天</td><td>20 USDT</td><td>实收70% · 默认93.80元</td></tr>
          <tr><td>Pro</td><td>5000</td><td>30天</td><td>50 USDT</td><td>实收70% · 默认234.50元</td></tr>
        </tbody>
      </table>
    </div>
    <div class="fields">
      <label>统一升级客服名称<input v-model="settings.contact_name" maxlength="80" :disabled="locked" /></label>
      <label>客服账号或联系方式<input v-model="settings.contact" maxlength="300" :disabled="locked" /></label>
      <label>全站免费试用预算（元／自然月）<input v-model="settings.trial_monthly_cny" inputmode="decimal" :disabled="locked" /></label>
      <label>全站安全基础预算（元／自然月）<input v-model="settings.monthly_cny" inputmode="decimal" :disabled="locked" /></label>
    </div>
    <button type="button" :disabled="locked" @click="run(saveSettings)"><Save :size="16" />保存客服与成本上限</button>
    <dl v-if="billing.site" class="budget-summary">
      <div><dt>免费池 · {{ billing.site.month }}</dt><dd>¥{{ yuan(billing.site.trial_cost_micro) }} / ¥{{ yuan(billing.site.trial_limit_micro) }}</dd></div>
      <div><dt>付费池 · 本月重叠周期</dt><dd>¥{{ yuan(billing.site.paid_cost_micro) }} / ¥{{ yuan(billing.site.paid_budget_micro) }}</dd></div>
      <div><dt>全站安全总上限 · 基础额＋已授予付费预算</dt><dd>¥{{ yuan(billing.site.cost_micro) }} / ¥{{ yuan(billing.site.limit_micro) }}</dd></div>
      <div v-if="billing.site.legacy_cost_micro"><dt>历史未分类费用 · 计入全站保护</dt><dd>¥{{ yuan(billing.site.legacy_cost_micro) }}</dd></div>
    </dl>
    <template v-if="tenant?.active">
      <h4>本群管理员套餐</h4>
      <div class="fields">
        <label>已核验管理员<select v-model="manager" :disabled="locked" aria-label="套餐管理员">
          <option v-for="m in managers" :key="m.id" :value="m.id">{{ m.name }}</option>
        </select></label>
        <label>授予套餐<select v-model="tier" :disabled="locked" aria-label="授予套餐"><option value="plus">Plus · 每天2000次</option><option value="pro">Pro · 每天5000次</option></select></label>
        <label>本次已实收人民币<input v-model="received" inputmode="decimal" :disabled="locked" /></label>
      </div>
      <p v-if="pool" class="summary">当前{{ pool.name }} · 今日 {{ pool.used }} / {{ pool.daily }} 次 · 到期 {{ date(pool.expires) }} · 本周期成本 ¥{{ yuan(pool.cost_micro) }} / ¥{{ yuan(pool.budget_micro) }}</p>
      <p v-else>当前试用 · 每天20次</p>
      <div class="actions">
        <button type="button" :disabled="locked || !manager || !settings.contact_name || !settings.contact" @click="run(grantPreview)"><CreditCard :size="16" />预览授予套餐</button>
        <button type="button" :disabled="locked || !manager" @click="run(nominate)"><ShieldCheck :size="16" />指定本群额度承担人</button>
      </div>
      <p>本群承担人：{{ members.find(m => m.id === payer?.uid)?.name || '等待开通者确认' }} · {{ payer?.confirmed ? '已本人确认' : '尚未确认，不扣共享额度' }}</p>
      <div v-if="preview" role="alertdialog" aria-label="确认授予AI套餐" class="confirmation">
        <strong>{{ preview.result.tier === 'plus' ? 'Plus' : 'Pro' }} · {{ members.find(m => m.id === preview.payload.uid)?.name }}</strong>
        <p>实收 ¥{{ preview.payload.received_cny }} · 成本上限 ¥{{ yuan(preview.result.budget_micro) }}</p>
        <p>{{ date(preview.result.starts) }} 至 {{ date(preview.result.ends) }}（北京时间）</p>
        <p>{{ preview.result.upgrade ? '立即生效，原套餐结束；不自动折算退款。' : '在现有套餐结束后生效。' }} 当日用量不清零。</p>
        <p v-if="preview.result.deferred_periods?.length">另有 {{ preview.result.deferred_periods.length }} 个已付费续期保留，顺延至新套餐结束后。</p>
        <div class="actions">
          <button type="button" :disabled="locked" @click="run(grant)"><Check :size="16" />确认已收款并授予</button>
          <button type="button" :disabled="locked" @click="preview = null">取消</button>
        </div>
      </div>
    </template>
    <details>
      <summary>模型计费依据</summary>
      <div class="fields">
        <label>模型<select v-model="provider" aria-label="计费模型" :disabled="locked" @change="choosePrice">
          <option value="">选择已核算模型</option><option v-for="p in providers" :key="p.id" :value="p.id">{{ p.id }}</option>
        </select></label>
        <label>每百万输入Token原价（供应商扣费单位）<input v-model="price.input_per_million" inputmode="decimal" :disabled="locked" /></label>
        <label>每百万输出Token原价（供应商扣费单位）<input v-model="price.output_per_million" inputmode="decimal" :disabled="locked" /></label>
        <label>扣费倍率<input v-model="price.multiplier" inputmode="decimal" :disabled="locked" /></label>
        <label>每扣费单位对应人民币<input v-model="price.credit_cny" inputmode="decimal" :disabled="locked" /></label>
        <label>供应商单次全部输入Token硬上界<input v-model.number="price.input_ceiling" type="number" min="1" max="10000000" :disabled="locked" /></label>
        <label>供应商单次全部输出Token硬上界<input v-model.number="price.output_ceiling" type="number" min="1" max="1000000" :disabled="locked" /></label>
        <label>计费及上界核验依据<textarea v-model="price.evidence" rows="2" maxlength="500" :disabled="locked" /></label>
        <label class="check"><input v-model="price.all_output_billed_in_limit" type="checkbox" :disabled="locked" />已确认输出限额包含所有计费推理Token且无额外费用</label>
      </div>
      <p v-if="price.valid_until">当前计费依据有效至 {{ date(price.valid_until) }}（北京时间）</p>
      <button type="button" :disabled="locked || !provider || !price.all_output_billed_in_limit || !price.evidence.trim()" @click="run(savePrice)"><ShieldCheck :size="16" />保存已核验计费依据</button>
      <p v-for="p in billing.prices.filter((r: any) => r.blocked)" :key="p.binding" class="error">{{ p.provider }} · 用量计费异常已暂停，需核对后重新登记。</p>
    </details>
    <details v-if="billing.pools.length">
      <summary>共享池与关联群</summary>
      <div class="table-wrap"><table>
        <thead><tr><th>管理账号</th><th>套餐</th><th>今日用量</th><th>周期费用</th><th>关联群数</th></tr></thead>
        <tbody><tr v-for="p in billing.pools" :key="p.pool">
          <td>{{ p.uid || '试用用户' }}</td><td>{{ p.name }}</td><td>{{ p.used }} / {{ p.daily }}</td>
          <td>¥{{ yuan(p.cost_micro) }} / ¥{{ yuan(p.budget_micro) }}</td>
          <td>{{ billing.payers.filter((r: any) => r.uid === p.uid && r.confirmed).length }}</td>
        </tr></tbody>
      </table></div>
    </details>
    <details v-if="billing.notices.length || billing.unknown.length">
      <summary>私信通知与未知费用</summary>
      <p v-for="n in billing.notices" :key="n.id">{{ date(n.created) }} · {{ n.uid || '试用用户' }} · {{ n.kind }} · {{ n.state }}</p>
      <div v-for="r in billing.unknown" :key="r.request" class="error">
        <p>{{ date(r.created) }} · 未知费用占用 ¥{{ yuan(r.charged_micro) }} · {{ r.request.slice(0, 12) }}</p>
        <pre v-if="r.usage">{{ r.usage }}</pre>
      </div>
    </details>
  </section>
</template>

<style scoped>
.ai-plans { min-width: 0; }
h3, .actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
h3 { font-size: 16px; margin: 28px 0 12px; border-top: 1px solid #dce2e2; padding-top: 20px; }
h4 { font-size: 15px; margin: 20px 0 12px; }
.fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; margin: 16px 0; }
.budget-summary { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; margin: 20px 0; }
.budget-summary div { min-width: 0; }
dt, dd { font-size: 14px; margin: 0; overflow-wrap: anywhere; }
dd { margin-top: 8px; font-weight: 600; }
label { display: flex; flex-direction: column; gap: 7px; min-width: 0; font-size: 14px; }
input, select, textarea { width: 100%; box-sizing: border-box; min-width: 0; font: inherit; padding: 9px; border: 1px solid #9daaaa; border-radius: 4px; background: #fff; color: #202828; }
.check { flex-direction: row; align-items: center; }
.check input { width: 18px; height: 18px; flex-shrink: 0; }
button { display: inline-flex; align-items: center; justify-content: center; gap: 7px; min-height: 36px; border: 1px solid #becaca; border-radius: 4px; padding: 7px 12px; color: #164d46; background: #eef5f3; cursor: pointer; white-space: normal; }
button:disabled { opacity: .5; cursor: default; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; text-align: left; }
th, td { padding: 10px 8px; border-bottom: 1px solid #dce2e2; min-width: 70px; vertical-align: top; }
details { margin: 20px 0; }
summary { cursor: pointer; min-height: 30px; font-size: 14px; }
p, .summary { font-size: 14px; overflow-wrap: anywhere; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 12px; }
.confirmation { border-left: 3px solid #12786b; padding: 12px; margin: 12px 0; }
.error { color: #b52b35; }
* { letter-spacing: 0; }
@media (max-width: 600px) { .fields, .budget-summary { grid-template-columns: minmax(0, 1fr); } }
</style>
