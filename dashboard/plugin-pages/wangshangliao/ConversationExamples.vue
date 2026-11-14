<script setup lang="ts">
import { computed, ref } from "vue";
import { Check, LockKeyhole, MessageCircle, Trophy, Users } from "@lucide/vue";

const props = defineProps<{ locale: string }>();
const t = (zh: string, en: string) => (props.locale.startsWith("zh") ? zh : en);
const activity = ref<"lottery" | "invites">("lottery");
const messages = computed(() => {
  const webChannel = t("网页 · 活动与财务", "WebUI · Activities & Finance");
  const groupChannel = t("示例群 · 活动演示", "Sample group · Activity demo");
  if (activity.value === "lottery") return [
    {
      channel: webChannel, from: "event", name: "",
      text: t("管理员选群并保存奖品、名额、报名口令与时间，点击「开启活动」或「预约发布」。",
        "The administrator selects a group, saves prizes, capacity, entry phrase and time, then starts or schedules the draw."),
    },
    {
      channel: groupChannel, from: "bot", name: t("机器人", "Bot"),
      text: t("示例餐券\n中奖名额：3人\n发送「参与抽奖」报名，限15人，10分钟后开奖。\n领奖联系人：示例管理员",
        "Sample meal voucher\n3 winners\nSend 参与抽奖 to enter. Up to 15 members; draw in 10 minutes.\nClaim contact: Sample admin"),
    },
    {
      channel: groupChannel, from: "member", name: t("示例成员", "Sample member"),
      text: "参与抽奖",
    },
    {
      channel: groupChannel, from: "bot", name: t("机器人", "Bot"),
      text: t("参加抽奖成功。", "Entry accepted."),
    },
    {
      channel: groupChannel, from: "event", name: "",
      text: t("另有14位示例成员报名，设定时间到达后统一开奖。", "Fourteen other sample members entered. Results are drawn at the configured deadline."),
    },
    {
      channel: groupChannel, from: "result", name: t("机器人 · 开奖结果", "Bot · Draw result"),
      text: t("抽奖结束\n15人参与，中奖率20.00%\n1. @示例成员甲 · 示例餐券\n2. @示例成员乙 · 示例餐券\n3. @示例成员丙 · 示例餐券\n联系群管理员领奖。",
        "Draw complete\n15 participants, 20.00% win rate\n1. @SampleA · meal voucher\n2. @SampleB · meal voucher\n3. @SampleC · meal voucher\nContact a group administrator to claim."),
    },
  ];
  return [
    {
      channel: webChannel, from: "event", name: "",
      text: t("管理员在「邀请奖励」为本群保存每人5.00积分。已有成员不追溯计奖，已记账奖励不变。",
        "The administrator saves a reward of 5.00 points per invite in Invitation Rewards. Existing members earn no retroactive credit; previous credits remain unchanged."),
    },
    {
      channel: groupChannel, from: "event", name: "",
      text: t("示例新成员入群；后台核验平台邀请归属，通过后记账。", "A sample member joins; the invitation is credited after native attribution is verified."),
    },
    {
      channel: groupChannel, from: "member", name: t("示例邀请人", "Sample inviter"),
      text: "我的邀请",
    },
    {
      channel: groupChannel, from: "result", name: t("机器人", "Bot"),
      text: t("有效邀请：1人\n累计奖励：5.00积分\n每人奖励：5.00积分",
        "Verified invitations: 1\nTotal rewards: 5.00 points\nReward per invite: 5.00 points"),
    },
    {
      channel: webChannel, from: "event", name: "",
      text: t("结账员在「邀请记账／结账」核对账本；记录不等于实际付款。",
        "The settlement operator reviews the ledger in Invitation Settlement; a record does not represent an actual payment."),
    },
  ];
});
</script>

<template>
  <section class="conversation-examples" id="panel-examples" role="tabpanel" aria-labelledby="tab-examples">
    <div class="examples-heading">
      <h2>{{ t("群聊活动示例", "Group activity examples") }}</h2>
      <span class="demo-label">{{ t("示例数据 · 不会发送", "DEMO · NOT SENT") }}</span>
    </div>
    <div class="example-switch section-switch" role="tablist" :aria-label="t('活动类别', 'Activity category')">
      <button type="button" role="tab" :aria-selected="activity === 'lottery'"
        :class="{ selected: activity === 'lottery' }" @click="activity = 'lottery'">
        <Trophy :size="16" />{{ t("群抽奖", "Lottery") }}
      </button>
      <button type="button" role="tab" :aria-selected="activity === 'invites'"
        :class="{ selected: activity === 'invites' }" @click="activity = 'invites'">
        <Users :size="16" />{{ t("邀请奖励", "Invite rewards") }}
      </button>
    </div>
    <div class="chat-demo">
      <header class="chat-header">
        <div class="chat-avatar"><MessageCircle :size="19" /></div>
        <div class="chat-title">
          <strong>{{ t("旺商聊群管", "Wangshangliao Bot") }}</strong>
          <span>{{ t("群聊活动 · 演示", "Group activity · demo") }}</span>
        </div>
        <span class="demo-label compact"><LockKeyhole :size="13" />{{ t("仅演示", "DEMO") }}</span>
      </header>
      <div class="chat-messages">
        <template v-for="(message, index) in messages" :key="`${activity}-${index}`">
          <div v-if="index === 0 || message.channel !== messages[index - 1]?.channel" class="chat-channel">
            {{ message.channel }}
          </div>
          <div class="chat-line" :class="message.from">
            <div class="chat-bubble">
              <span v-if="message.from !== 'event'" class="chat-sender">{{ message.name }}</span>
              <pre>{{ message.text }}</pre>
              <span v-if="message.from === 'result'" class="success-stamp">
                <Check :size="13" />{{ t("成功示例", "SUCCESS EXAMPLE") }}
              </span>
            </div>
          </div>
        </template>
      </div>
    </div>
  </section>
</template>
