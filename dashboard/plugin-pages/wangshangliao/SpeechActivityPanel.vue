<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { Check, Copy, LoaderCircle, RefreshCw, Search, X } from "@lucide/vue";
import { copyToClipboard } from "../../src/utils/clipboard";
import FocusedSpeechReport from "./FocusedSpeechReport.vue";

type Member = { id: string; name: string };
type Day = {
  date: string; total: number; active_members: number;
  kinds: Record<string, number>; top: Array<Member & { count: number }>;
  hours: number[]; member: { count: number; first_at: number; last_at: number } | null;
};
type Snapshot = {
  days: Day[]; updated_at: number; catching_up: boolean;
  members: Member[]; has_more: boolean; selected_member: Member | null;
};
const props = defineProps<{
  botId: string; groupId: string; groupName?: string;
  bridge: { apiGet: (endpoint: string, params: Record<string, string>) => Promise<Snapshot> };
}>();
const data = ref<Snapshot | null>(null);
const memberData = ref<Snapshot | null>(null);
const day = ref(0);
const query = ref("");
const choices = ref<Member[]>([]);
const selected = ref<Member | null>(null);
const searched = ref(false);
const hasMore = ref(false);
const busy = ref(false);
const searching = ref(false);
const error = ref("");
const memberError = ref("");
const copying = ref(false);
const copyNotice = ref("");
const copyError = ref("");
const groupDay = computed(() => data.value?.days[day.value]);
const copyDisabled = computed(() =>
  copying.value || busy.value || !groupDay.value?.top.length || Boolean(data.value?.catching_up));
const selectedDay = computed(() => memberData.value?.days[day.value]);
const hours = computed(() => (selected.value ? selectedDay.value : groupDay.value)?.hours || []);
const maxHour = computed(() => Math.max(1, ...hours.value));
let generation = 0;
let memberGeneration = 0;
let copyGeneration = 0;
let timer: ReturnType<typeof setInterval> | undefined;
const params = () => ({ bot_id: props.botId, group_id: props.groupId });
function clock(value: number) {
  return value ? new Date(value * 1000).toLocaleTimeString("zh-CN", {
    timeZone: "Asia/Shanghai", hour12: false,
  }) : "—";
}
async function copyRanking() {
  if (copyDisabled.value) return;
  const ranking = groupDay.value!;
  const label = (value: string) => value.replace(/[\s\u0000-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e\u2066-\u2069]+/g, " ").trim();
  const text = [
    `${label(props.groupName || "群")}（${props.groupId}）`,
    `${ranking.date} 发言活跃排行榜（北京时间）`,
    ...ranking.top.map((member, index) =>
      `${index + 1}. ${label(member.name) || member.id}（UID ${member.id}） ${member.count} 条`),
  ].join("\n");
  const current = ++copyGeneration;
  copying.value = true;
  copyNotice.value = ""; copyError.value = "";
  try {
    const copied = await copyToClipboard(text);
    if (current !== copyGeneration) return;
    if (copied) copyNotice.value = `已复制${ranking.top.length}位成员的排名`;
    else copyError.value = "浏览器未允许复制，请再次点击复制排名。";
  } catch {
    if (current === copyGeneration) copyError.value = "复制失败，请再次点击复制排名。";
  } finally {
    if (current === copyGeneration) copying.value = false;
  }
}
async function refresh() {
  if (!props.botId || !props.groupId || busy.value) return;
  const current = generation;
  busy.value = true;
  error.value = "";
  try {
    const result = await props.bridge.apiGet("speech-activity", params());
    if (current !== generation) return;
    data.value = result;
    if (selected.value && !searching.value) await choose(selected.value);
  } catch {
    if (current === generation) {
      error.value = "统计读取失败，请稍后刷新。";
      data.value = null;
    }
  } finally {
    if (current === generation) busy.value = false;
  }
}
function clearMember() {
  memberGeneration++;
  choices.value = []; selected.value = null; memberData.value = null;
  searched.value = false; hasMore.value = false; searching.value = false; memberError.value = "";
}
async function search() {
  if (!query.value.trim() || !props.groupId || searching.value) return;
  clearMember();
  const current = ++memberGeneration;
  searching.value = true;
  try {
    const result = await props.bridge.apiGet("speech-activity", { ...params(), query: query.value.trim() });
    if (current !== memberGeneration) return;
    choices.value = result.members; hasMore.value = result.has_more; searched.value = true;
  } catch {
    if (current === memberGeneration) memberError.value = "成员查询失败，请核对当前群后重试。";
  } finally {
    if (current === memberGeneration) searching.value = false;
  }
}
async function choose(member: Member) {
  const current = ++memberGeneration;
  selected.value = member;
  memberData.value = null;
  memberError.value = "";
  searching.value = true;
  try {
    const result = await props.bridge.apiGet("speech-activity", { ...params(), member_id: member.id });
    if (current !== memberGeneration) return;
    if (result.selected_member?.id !== member.id) throw new Error("Member not found");
    memberData.value = result;
  } catch {
    if (current === memberGeneration) memberError.value = "该成员的统计暂不可读，请重新搜索并选择。";
  } finally {
    if (current === memberGeneration) searching.value = false;
  }
}
watch(query, clearMember, { flush: "sync" });
watch(() => [props.botId, props.groupId, props.groupName, day.value], () => {
  copyGeneration++; copying.value = false; copyNotice.value = ""; copyError.value = "";
}, { flush: "sync" });
watch(() => [props.botId, props.groupId], () => {
  generation++; clearMember(); data.value = null; busy.value = false;
  query.value = ""; error.value = ""; day.value = 0;
  if (timer) clearInterval(timer);
  void refresh();
  timer = setInterval(() => { void refresh(); }, 30_000);
}, { immediate: true, flush: "sync" });
onBeforeUnmount(() => {
  generation++; memberGeneration++; copyGeneration++;
  if (timer) clearInterval(timer);
});
</script>

<template>
  <section class="speech-activity" aria-label="群发言活跃统计">
    <header>
      <h2>每日发言活跃</h2>
      <button type="button" class="icon-button" title="刷新发言统计" aria-label="刷新发言统计"
        :disabled="busy || !groupId" @click="refresh"><RefreshCw :size="18" /></button>
    </header>
    <p v-if="!groupId" class="muted">请选择一个已启用群。</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="busy && !data" role="status"><LoaderCircle :size="16" class="spin" />正在读取统计</p>
    <template v-if="data">
      <div class="date-row">
        <div class="day-switch" role="group" aria-label="统计日期">
          <label><input v-model="day" type="radio" name="speech-day" :value="0" />今日</label>
          <label><input v-model="day" type="radio" name="speech-day" :value="1" />昨日</label>
        </div>
        <span class="muted">{{ groupDay?.date }} · 北京时间</span>
      </div>
      <p class="muted status" role="status">{{ data.catching_up ? '正在补齐已接收消息，当前结果尚不完整' : '最近核对 ' + clock(data.updated_at) }}</p>
      <div class="metrics" aria-label="本群每日汇总">
        <div><span>有效消息</span><strong>{{ groupDay?.total || 0 }}</strong></div>
        <div><span>活跃成员</span><strong>{{ groupDay?.active_members || 0 }}</strong></div>
        <div><span>文字／表情</span><strong>{{ groupDay?.kinds.text || 0 }}</strong></div>
        <div><span>图片</span><strong>{{ groupDay?.kinds.image || 0 }}</strong></div>
        <div><span>视频</span><strong>{{ groupDay?.kinds.video || 0 }}</strong></div>
        <div><span>转发</span><strong>{{ groupDay?.kinds.forward || 0 }}</strong></div>
      </div>
      <p class="muted definition">零点换日；仅保留今日和昨日统计。最近30秒待核对，撤回／违规消息、机器人消息和管理命令不计入。此处按消息逐条计数，不改变自动白名单的有效文字榜口径。</p>
      <FocusedSpeechReport :bot-id="botId" :day="day" :bridge="bridge" />
      <section class="member-section" aria-label="单独成员发言统计">
        <header><h3>成员发言统计</h3>
          <button v-if="selected" type="button" class="icon-button" title="清除所选成员"
            aria-label="清除所选成员" @click="clearMember"><X :size="16" /></button>
        </header>
        <form class="search-row" @submit.prevent="search">
          <label>当前群成员<input v-model="query" type="search" maxlength="64"
            aria-label="搜索统计成员" placeholder="成员名称或UID" autocomplete="off" /></label>
          <button type="submit" :disabled="searching || !query.trim()"><Search :size="16" />搜索</button>
        </form>
        <p v-if="memberError" class="error" role="alert">{{ memberError }}</p>
        <p v-if="searching" role="status">正在核对成员统计</p>
        <p v-if="searched && !choices.length" class="muted">当前群未找到该成员；可搜索当天或昨日有发言的已离群成员。</p>
        <p v-if="hasMore" class="muted">匹配超过20人，请输入更完整的名称或UID。</p>
        <ul v-if="choices.length && !selected" class="choices" aria-label="统计成员搜索结果">
          <li v-for="member in choices" :key="member.id">
            <button type="button" :disabled="searching" @click="choose(member)">
              <span>{{ member.name }}</span><small>UID {{ member.id }}</small>
            </button>
          </li>
        </ul>
        <div v-if="selected" class="member-result" aria-label="所选成员统计">
          <strong>{{ memberData?.selected_member?.name || selected.name }}</strong>
          <small>UID {{ selected.id }}</small>
          <template v-if="memberData">
            <span>{{ selectedDay?.date }} · {{ selectedDay?.member?.count || 0 }} 条有效消息</span>
            <span>首次 {{ clock(selectedDay?.member?.first_at || 0) }} · 最近 {{ clock(selectedDay?.member?.last_at || 0) }}</span>
          </template>
        </div>
      </section>
      <section aria-label="小时活跃分布">
        <h3>{{ selected ? '所选成员' : '本群' }}每小时发言</h3>
        <div class="histogram" role="img" :aria-label="hours.map((count, hour) => `${hour}点${count}条`).join('，')">
          <div v-for="(count, hour) in hours" :key="hour" class="hour"
            :title="`${hour}:00—${hour}:59：${count}条`">
            <div class="track"><span :style="{ height: `${count / maxHour * 100}%` }" /></div>
            <small>{{ hour % 3 === 0 ? hour : '' }}</small>
          </div>
        </div>
      </section>
      <section aria-label="每日活跃成员排名">
        <header><h3>本群活跃前30名</h3>
          <button type="button" class="icon-button" title="复制当前日期排名" aria-label="复制排名"
            :disabled="copyDisabled" @click="copyRanking">
            <LoaderCircle v-if="copying" :size="16" class="spin" />
            <Check v-else-if="copyNotice" :size="16" />
            <Copy v-else :size="16" />
          </button>
        </header>
        <p v-if="copyNotice" class="muted" role="status">{{ copyNotice }}</p>
        <p v-if="copyError" class="error" role="alert">{{ copyError }}</p>
        <p v-if="!groupDay?.top.length" class="muted">当天暂无有效发言记录。</p>
        <ol v-else class="ranking">
          <li v-for="member in groupDay.top" :key="member.id">
            <button type="button" :disabled="searching" @click="choose(member)">
              <span>{{ member.name }}<small>UID {{ member.id }}</small></span>
              <strong>{{ member.count }} 条</strong>
            </button>
          </li>
        </ol>
      </section>
    </template>
  </section>
</template>

<style scoped>
.speech-activity { min-width: 0; }
header, .date-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
h2 { margin: 0; font-size: 18px; } h3 { font-size: 15px; margin: 16px 0 10px; }
.muted, small { color: #5f6b75; font-size: 12px; }
.status { min-height: 18px; } .definition { line-height: 1.7; }
.day-switch { display: flex; gap: 16px; margin-top: 12px; }
.day-switch label { display: flex; align-items: center; gap: 5px; }
.metrics { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); border-block: 1px solid #d9dfe5; padding: 16px 0; gap: 12px; }
.metrics div { display: grid; gap: 6px; } .metrics span { font-size: 12px; color: #5f6b75; }
.metrics strong { font-size: 22px; }
.member-section { padding-bottom: 18px; border-bottom: 1px solid #d9dfe5; }
.search-row { display: flex; gap: 10px; align-items: flex-end; }
.search-row label { display: grid; flex: 1; min-width: 0; gap: 6px; font-size: 13px; }
input[type="search"] { width: 100%; box-sizing: border-box; min-width: 0; padding: 8px 10px; border: 1px solid var(--border, #d9dfe5); border-radius: 5px; background: var(--surface, #fff); color: inherit; }
button { display: inline-flex; align-items: center; gap: 6px; }
.choices, .ranking { padding: 0; margin: 10px 0; list-style: none; }
.choices button, .ranking button { width: 100%; text-align: left; justify-content: space-between; background: transparent; border: 0; border-bottom: 1px solid #d9dfe5; border-radius: 0; padding: 10px 0; }
.choices span, .ranking span, .member-result { overflow-wrap: anywhere; min-width: 0; }
.ranking small { display: block; margin-top: 3px; }
.ranking strong { white-space: nowrap; }
.member-result { display: grid; gap: 6px; margin-top: 14px; font-size: 13px; }
.histogram { display: grid; grid-template-columns: repeat(24, minmax(0, 1fr)); gap: 3px; height: 130px; padding-top: 12px; }
.hour { display: flex; flex-direction: column; min-width: 0; }
.track { height: 105px; display: flex; align-items: flex-end; background: #f0f3f5; }
.track span { width: 100%; background: #247c79; min-height: 0; }
.hour small { height: 20px; line-height: 20px; font-size: 10px; }
.error { color: #b42318; } .spin { animation: rotate 1s linear infinite; }
@keyframes rotate { to { transform: rotate(360deg); } }
@media (max-width: 600px) { .metrics { grid-template-columns: repeat(3, minmax(0, 1fr)); } .histogram { gap: 2px; } }
</style>
