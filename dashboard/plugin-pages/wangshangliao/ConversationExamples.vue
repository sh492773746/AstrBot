<script setup lang="ts">
import { computed, ref } from "vue";
import {
  Check,
  LockKeyhole,
  MessageCircle,
  ShieldCheck,
  Terminal,
  Trophy,
  UserRound,
  Users,
} from "@lucide/vue";

const props = defineProps<{ locale: string }>();
const isEnglish = computed(() => !props.locale.startsWith("zh"));
const t = (zh: string, en: string) => (isEnglish.value ? en : zh);
const section = ref<"private" | "lottery" | "invites">("private");
const privateFlow = ref<
  "admin-command" | "admin-ai" | "member-cards" | "customer-service" | "denied-command"
>("admin-command");
const activity = ref<"lottery" | "invites">("lottery");

const privateFlows = computed(() => [
  {
    id: "admin-command" as const,
    label: t("管理员命令", "Admin command"),
    icon: Terminal,
    route: t(
      "精确命令 → 权限校验 → 直接执行，不经过 AI",
      "Exact command → permission check → direct execution; AI is bypassed",
    ),
    condition: t(
      "发送者在 AstrBot 管理员名单中；私聊回复已开启。",
      "Sender is an AstrBot admin; private replies are enabled.",
    ),
    channel: t("管理员私聊 · 固定命令", "Admin DM · Fixed command"),
    messages: [
      { from: "admin", name: t("管理员", "Admin"), text: "群列表" },
      {
        from: "bot",
        name: t("机器人", "Bot"),
        text: t(
          "可管理群：1. 大海兼职群",
          "Manageable groups: 1. Sample Group",
        ),
      },
      { from: "admin", name: t("管理员", "Admin"), text: "选择群 1" },
      {
        from: "bot",
        name: t("机器人", "Bot"),
        text: t("当前已选群：大海兼职群", "Selected group: Sample Group"),
      },
      { from: "admin", name: t("管理员", "Admin"), text: "禁言 2 3" },
      {
        from: "bot",
        name: t("机器人", "Bot"),
        text: t("禁言已受理：3 分钟。", "Mute accepted for 3 minutes."),
      },
    ],
  },
  {
    id: "admin-ai" as const,
    label: t("管理员 AI 管理", "Admin AI management"),
    icon: ShieldCheck,
    route: t(
      "普通文字 → AI 对话 → 白名单管理工具 → 再校验管理员身份",
      "Natural language → AI → allowlisted management tool → admin recheck",
    ),
    condition: t(
      "还需当前人格启用 wsl_private_management，模型支持工具调用。",
      "The active persona must allow wsl_private_management and the model must support tool calling.",
    ),
    channel: t("管理员私聊 · AI 管理", "Admin DM · AI management"),
    messages: [
      {
        from: "admin",
        name: t("管理员", "Admin"),
        text: t("我不会设置，你能帮我做什么？", "I am not sure how to configure things. What can you help with?"),
      },
      {
        from: "bot",
        name: t("机器人", "Bot"),
        text: t(
          "我可以带你设置抽奖、邀请奖励和群管理规则。你想先做哪一项？",
          "I can guide you through lotteries, invite rewards and group rules. Which would you like to start with?",
        ),
      },
      {
        from: "admin",
        name: t("管理员", "Admin"),
        text: t("先做抽奖吧", "Start with a lottery"),
      },
      {
        from: "bot",
        name: t("机器人", "Bot"),
        text: t("可以，想在哪个群开？", "Sure. Which group is it for?"),
      },
      {
        from: "admin",
        name: t("管理员", "Admin"),
        text: t(
          "帮我看看大海兼职群有哪些成员",
          "Show me the members in Sample Group",
        ),
      },
      {
        from: "event",
        name: "",
        text: t(
          "普通文字未匹配固定命令，进入 AI 会话。",
          "No fixed command matched; message enters the AI conversation.",
        ),
      },
      {
        from: "bot",
        name: t("机器人", "Bot"),
        text: t(
          "我先读取可管理群和成员目录，再按平台返回的信息回答。",
          "I will read the authorized group and member directory, then answer from platform data.",
        ),
      },
      {
        from: "bot",
        name: t("机器人", "Bot"),
        text: t(
          "大海兼职群当前成员目录已读取。此示例不代表真实群成员数据。",
          "The member directory for Sample Group was read. This demo does not contain live member data.",
        ),
      },
    ],
  },
  {
    id: "member-cards" as const,
    label: t("名片设置", "Member cards"),
    icon: UserRound,
    route: t(
      "管理员私聊 → 验证群与成员 → 预览 → 新消息确认 → 回读结果",
      "Admin DM → verify group and member → preview → new confirmation → verify result",
    ),
    condition: t(
      "目标群已授权修改成员名片，机器人具备平台管理权限；不修改账号昵称。",
      "The group must grant rename and the bot must have upstream admin rights; account nicknames stay unchanged.",
    ),
    channel: t("管理员私聊 · 名片管理", "Admin DM · Member cards"),
    messages: [
      { from: "admin", name: t("管理员", "Admin"), text: t("帮我开启大海兼职群的自动规范名片", "Enable automatic card normalization for Sample Group") },
      { from: "bot", name: t("机器人", "Bot"), text: t(
        "设置预览：大海兼职群，自动规范名片由关闭改为开启，仅影响本群。后台将按现有规则扫描，通常保留原名的前两个字符，短名称采用备用规则。尚未保存，请发送：确认设置 <本次确认码>。",
        "Preview: Sample Group, automatic normalization off → on, this group only. Background scans use the existing two-character normalization and fallback naming rules. Not saved yet. Send: 确认设置 <preview token>.",
      ) },
      { from: "admin", name: t("管理员", "Admin"), text: "确认设置 <本次确认码>" },
      { from: "bot", name: t("机器人", "Bot"), text: t("已保存，后台将按周期扫描本群；不表示全群已经改完。", "Saved. Background scans will process this group; this does not mean every card has already changed.") },
      { from: "admin", name: t("管理员", "Admin"), text: t("把大海兼职群里张三的群名片改成值班小张", "Change Zhang San’s card in Sample Group to On-duty Zhang") },
      { from: "event", name: "", text: t("查询当前成员目录。重名时先让管理员选择，不猜目标。", "Look up the current member directory. Ask the admin to disambiguate duplicate names.") },
      { from: "bot", name: t("机器人", "Bot"), text: t("修改预览：大海兼职群，张三，原名片“小张” → “值班小张”。尚未执行；核对无误后另发“确认修改名片”。", "Preview: Sample Group, Zhang San, card “Zhang” → “On-duty Zhang”. Not executed. Send a new message “确认修改名片” after checking it.") },
      { from: "admin", name: t("管理员", "Admin"), text: "确认修改名片" },
      { from: "bot", name: t("机器人", "Bot"), text: t("修改任务已排队，我再查一下结果。", "The change is queued. I will check its result.") },
      { from: "bot", name: t("机器人", "Bot"), text: t("已回读确认：本群名片是“值班小张”，账号昵称未修改。", "Verified by readback: this group’s card is “On-duty Zhang”; the account nickname is unchanged.") },
    ],
  },
  {
    id: "customer-service" as const,
    label: t("普通用户客服", "Member customer service"),
    icon: MessageCircle,
    route: t(
      "普通文字 → 正常 AI 客服会话；不提供管理工具",
      "Natural language → regular AI support; no management tool",
    ),
    condition: t(
      "私聊回复开启，且已配置可用的人格、模型或知识库。",
      "Private replies are enabled and a working persona, model, or knowledge base is configured.",
    ),
    channel: t("普通用户私聊 · AI 客服", "Member DM · AI support"),
    messages: [
      {
        from: "member",
        name: t("群成员", "Member"),
        text: t("我能用你做什么？", "What can I use you for?"),
      },
      {
        from: "bot",
        name: t("机器人", "Bot"),
        text: t(
          "我可以帮你了解业务和活动用法。在活动群里可以发“排名”看发言榜，有抽奖时发“参加抽奖”，发“我的邀请”查自己的邀请记录。",
          "I can explain business information and activities. In the activity group, send “排名” for chat rankings, “参加抽奖” for an active lottery, or “我的邀请” for your own invite record.",
        ),
      },
      {
        from: "member",
        name: t("群成员", "Member"),
        text: t("抽奖怎么参加？", "How do I join the lottery?"),
      },
      {
        from: "event",
        name: "",
        text: t(
          "普通咨询进入 AI 客服；机器人不会因此获得群管权限。",
          "This is handled as customer support; it does not grant moderation access.",
        ),
      },
      {
        from: "bot",
        name: t("机器人", "Bot"),
        text: t(
          "如果群内正在进行抽奖，请回到群聊发送“参加抽奖”。也可以发送“抽奖状态”查看活动状态。",
          "If a lottery is active, return to the group and send “参加抽奖”. Send “抽奖状态” to check it.",
        ),
      },
    ],
  },
  {
    id: "denied-command" as const,
    label: t("普通用户命令", "Member command"),
    icon: UserRound,
    route: t(
      "命中管理命令 → 管理员身份校验失败 → 拒绝，不进入 AI 管理",
      "Management command → admin check fails → denied; no AI management",
    ),
    condition: t(
      "普通用户仍可使用已开放的群内公共命令；此处只演示私聊管理命令。",
      "Members may still use enabled public group commands; this demonstrates a private admin command.",
    ),
    channel: t("普通用户私聊 · 权限拦截", "Member DM · Access denied"),
    messages: [
      { from: "member", name: t("群成员", "Member"), text: "禁言 @小周 3" },
      {
        from: "event",
        name: "",
        text: t(
          "命令被识别为管理操作，尚未调用 AI。",
          "Recognized as a management command; AI has not been called.",
        ),
      },
      { from: "bot", name: t("机器人", "Bot"), text: "无权限" },
    ],
  },
]);

const activePrivateFlow = computed(
  () => privateFlows.value.find((flow) => flow.id === privateFlow.value)!,
);

const messages = computed(() => {
  const adminChannel = t(
    "管理员私聊 · 旺商聊群管",
    "Admin DM · Wangshangliao Bot",
  );
  const groupChannel = t(
    "大海兼职群 · 活动演示",
    "Sample Group · Activity demo",
  );
  if (section.value === "private") {
    return activePrivateFlow.value.messages.map((message) => ({
      ...message,
      channel: activePrivateFlow.value.channel,
    }));
  }
  if (activity.value === "lottery") {
    return [
      {
        channel: adminChannel,
        from: "admin",
        name: t("管理员", "Admin"),
        text: "群列表",
      },
      {
        channel: adminChannel,
        from: "bot",
        name: t("机器人", "Bot"),
        text: t(
          "可管理群：1. 大海兼职群",
          "Manageable groups: 1. Sample Group",
        ),
      },
      {
        channel: adminChannel,
        from: "admin",
        name: t("管理员", "Admin"),
        text: t("选择群 1", "选择群 1"),
      },
      {
        channel: adminChannel,
        from: "bot",
        name: t("机器人", "Bot"),
        text: t("当前已选群：大海兼职群", "Selected group: Sample Group"),
      },
      {
        channel: adminChannel,
        from: "admin",
        name: t("管理员", "Admin"),
        text: "抽奖奖励 18元猪脚饭",
      },
      {
        channel: adminChannel,
        from: "bot",
        name: t("机器人", "Bot"),
        text: "已设置抽奖奖励：18元猪脚饭",
      },
      {
        channel: adminChannel,
        from: "admin",
        name: t("管理员", "Admin"),
        text: "中奖人数 3",
      },
      {
        channel: adminChannel,
        from: "bot",
        name: t("机器人", "Bot"),
        text: "已设置中奖人数：3",
      },
      {
        channel: adminChannel,
        from: "admin",
        name: t("管理员", "Admin"),
        text: "抽奖倒计时 10",
      },
      {
        channel: adminChannel,
        from: "bot",
        name: t("机器人", "Bot"),
        text: "已设置抽奖倒计时：10 分钟",
      },
      {
        channel: adminChannel,
        from: "admin",
        name: t("管理员", "Admin"),
        text: "参与上限 15",
      },
      {
        channel: adminChannel,
        from: "bot",
        name: t("机器人", "Bot"),
        text: "已设置参与上限：15",
      },
      {
        channel: adminChannel,
        from: "admin",
        name: t("管理员", "Admin"),
        text: "抽奖邀请门槛 0",
      },
      {
        channel: adminChannel,
        from: "bot",
        name: t("机器人", "Bot"),
        text: "已设置抽奖邀请门槛：0",
      },
      {
        channel: adminChannel,
        from: "admin",
        name: t("管理员", "Admin"),
        text: "领奖联系人 秦铭",
      },
      {
        channel: adminChannel,
        from: "bot",
        name: t("机器人", "Bot"),
        text: "已设置领奖联系人：秦铭",
      },
      {
        channel: adminChannel,
        from: "admin",
        name: t("管理员", "Admin"),
        text: "开启抽奖",
      },
      {
        channel: adminChannel,
        from: "bot",
        name: t("机器人", "Bot"),
        text: t(
          "抽奖已开启：18元猪脚饭，3个名额，10分钟后开奖。群通知已受理；发送“抽奖状态”可查询进度。",
          "Lottery opened: 3 sample meal vouchers, drawing in 10 minutes. Group announcement accepted. Send “抽奖状态” to check progress.",
        ),
      },
      {
        channel: groupChannel,
        from: "bot",
        name: t("机器人", "Bot"),
        text: t(
          "🎉 抽奖开始！奖品：18元猪脚饭\n发送「参加抽奖」报名，限15人，10分钟后开奖。",
          "🎉 Lottery is open! Prize: sample meal voucher.\nSend “参加抽奖” to enter. Up to 15 members; draw in 10 minutes.",
        ),
      },
      {
        channel: groupChannel,
        from: "event",
        name: "",
        text: t(
          "另有14位示例成员已报名",
          "14 other sample members have entered",
        ),
      },
      {
        channel: groupChannel,
        from: "member",
        name: t("群员（示例）", "Sample member"),
        text: "参加抽奖",
      },
      {
        channel: groupChannel,
        from: "bot",
        name: t("机器人", "Bot"),
        text: "参加抽奖成功。",
      },
      {
        channel: groupChannel,
        from: "event",
        name: "",
        text: t(
          "抽奖通知、报名反馈和中奖名单保留，不自动撤回；开奖记录也可私聊查询。",
          "Lottery notices, signup replies and results are kept; draw records are also available in private chat.",
        ),
      },
      {
        channel: t("大海兼职群 · 成功示例", "Sample Group · Success example"),
        from: "result",
        name: t("机器人 · 开奖结果", "Bot · Draw result"),
        text: t(
          "🎉 抽奖开奖啦！\n总共参与15人，综合中奖率20.00%\n\n恭喜以下中奖用户：\n1. @示例成员甲 获得：18元猪脚饭\n2. @示例成员乙 获得：18元猪脚饭\n3. @示例成员丙 获得：18元猪脚饭\n\n抽奖创建者：秦铭（示例）\n联系群管理员领奖",
          "🎉 The draw is complete!\n15 participants · 20.00% overall win rate\n\nWinners:\n1. @SampleMemberA · sample meal voucher\n2. @SampleMemberB · sample meal voucher\n3. @SampleMemberC · sample meal voucher\n\nCreated by: Sample Admin\nContact a group admin to claim",
        ),
      },
    ];
  }
  return [
    {
      channel: adminChannel,
      from: "admin",
      name: t("管理员", "Admin"),
      text: "群列表",
    },
    {
      channel: adminChannel,
      from: "bot",
      name: t("机器人", "Bot"),
      text: t("可管理群：1. 大海兼职群", "Manageable groups: 1. Sample Group"),
    },
    {
      channel: adminChannel,
      from: "admin",
      name: t("管理员", "Admin"),
      text: "选择群 1",
    },
    {
      channel: adminChannel,
      from: "bot",
      name: t("机器人", "Bot"),
      text: t("当前已选群：大海兼职群", "Selected group: Sample Group"),
    },
    {
      channel: adminChannel,
      from: "admin",
      name: t("管理员", "Admin"),
      text: "设置邀请奖励 5",
    },
    {
      channel: adminChannel,
      from: "bot",
      name: t("机器人", "Bot"),
      text: t(
        "统一邀请奖励已设置：每人 5.00 积分；已记账奖励不变。",
        "Uniform invitation reward set to 5.00 points per valid invite. Existing credits are unchanged.",
      ),
    },
    {
      channel: adminChannel,
      from: "admin",
      name: t("管理员", "Admin"),
      text: "开启邀请奖励",
    },
    {
      channel: adminChannel,
      from: "bot",
      name: t("机器人", "Bot"),
      text: t(
        "邀请奖励已开启，每人 5.00 积分；现有126位成员作为基线不计奖。",
        "Invitation rewards enabled at 5.00 points each. The 126 current members are baselined and do not earn retroactive credit.",
      ),
    },
    {
      channel: groupChannel,
      from: "event",
      name: "",
      text: t(
        "新成员小周加入群聊。机器人将在后台周期核验平台邀请归属（约35秒一次）；不会即时群发到账通知，待核验最长24小时。",
        "Sample member Zhou joins. The bot checks native inviter attribution in the background (about every 35 seconds); it does not send an instant group credit notice. Pending verification can take up to 24 hours.",
      ),
    },
    {
      channel: groupChannel,
      from: "event",
      name: "",
      text: t(
        "成功示例：平台归属核验通过后，奖励已记账。",
        "Success example: native attribution verified and points credited.",
      ),
    },
    {
      channel: groupChannel,
      from: "member",
      name: t("邀请人小林（示例）", "Sample inviter Lin"),
      text: "我的邀请",
    },
    {
      channel: groupChannel,
      from: "result",
      name: t("机器人 · 私人账户查询", "Bot · Account query"),
      text: t(
        "有效邀请：1 人\n累计奖励：5.00 积分\n每人奖励：5.00 积分\n奖励状态：开启",
        "Verified invitations: 1\nTotal rewards: 5.00 points\nReward per invite: 5.00 points\nStatus: enabled",
      ),
    },
    {
      channel: groupChannel,
      from: "event",
      name: "",
      text: t(
        "群内查询回复20秒后自动撤回；积分记录仍保留。",
        "Group query replies are recalled after 20 seconds; the credit ledger is retained.",
      ),
    },
  ];
});
</script>

<template>
  <section
    class="conversation-examples"
    id="panel-examples"
    role="tabpanel"
    aria-labelledby="tab-examples"
  >
    <div class="examples-heading">
      <div>
        <h2>{{ t("对话与执行路径", "Conversation and execution paths") }}</h2>
        <p>
          {{
            t(
              "对照查看谁能说什么、机器人会怎样处理",
              "See who can send which message and how the bot handles it",
            )
          }}
        </p>
      </div>
      <span class="demo-label">{{
        t("示例数据 · 不会发送", "DEMO · NOT SENT")
      }}</span>
    </div>

    <div
      class="example-switch section-switch"
      role="tablist"
      :aria-label="t('对话类别', 'Conversation category')"
    >
      <button
        type="button"
        role="tab"
        :aria-selected="section === 'private'"
        :class="{ selected: section === 'private' }"
        @click="section = 'private'"
      >
        <LockKeyhole :size="16" />{{ t("私聊与权限", "Private chat & access") }}
      </button>
      <button
        type="button"
        role="tab"
        :aria-selected="section === 'lottery'"
        :class="{ selected: section === 'lottery' }"
        @click="
          section = 'lottery';
          activity = 'lottery';
        "
      >
        <Trophy :size="16" />{{ t("群抽奖", "Lottery") }}
      </button>
      <button
        type="button"
        role="tab"
        :aria-selected="section === 'invites'"
        :class="{ selected: section === 'invites' }"
        @click="
          section = 'invites';
          activity = 'invites';
        "
      >
        <Users :size="16" />{{ t("邀请奖励", "Invite rewards") }}
      </button>
    </div>

    <template v-if="section === 'private'">
      <div
        class="example-switch private-switch"
        role="tablist"
        :aria-label="t('私聊身份与处理方式', 'Private chat identity and route')"
      >
        <button
          v-for="flow in privateFlows"
          :key="flow.id"
          type="button"
          role="tab"
          :aria-selected="privateFlow === flow.id"
          :class="{ selected: privateFlow === flow.id }"
          @click="privateFlow = flow.id"
        >
          <component :is="flow.icon" :size="15" />{{ flow.label }}
        </button>
      </div>
      <div class="route-summary">
        <div>
          <span>{{ t("处理路径", "Processing route") }}</span
          ><strong>{{ activePrivateFlow.route }}</strong>
        </div>
        <div>
          <span>{{ t("生效条件", "Requirements") }}</span>
          <p>{{ activePrivateFlow.condition }}</p>
        </div>
      </div>
    </template>

    <div class="chat-demo">
      <header class="chat-header">
        <div class="chat-avatar"><MessageCircle :size="19" /></div>
        <div class="chat-title">
          <strong>{{ t("旺商聊群管", "Wangshangliao Bot") }}</strong>
          <span>{{
            section === "private"
              ? activePrivateFlow.channel
              : t("群聊活动 · 演示", "Group activity · demo")
          }}</span>
        </div>
        <span class="demo-label compact"
          ><LockKeyhole :size="13" />{{ t("仅演示", "DEMO") }}</span
        >
      </header>
      <div class="chat-messages">
        <template
          v-for="(message, index) in messages"
          :key="`${section}-${privateFlow}-${activity}-${index}`"
        >
          <div
            v-if="
              index === 0 || message.channel !== messages[index - 1]?.channel
            "
            class="chat-channel"
          >
            {{ message.channel }}
          </div>
          <div class="chat-line" :class="message.from">
            <div class="chat-bubble">
              <span
                v-if="message.from !== 'admin' && message.from !== 'event'"
                class="chat-sender"
                >{{ message.name }}</span
              >
              <pre>{{ message.text }}</pre>
              <span v-if="message.from === 'result'" class="success-stamp">
                <Check :size="13" />{{ t("成功示例", "SUCCESS EXAMPLE") }}
              </span>
            </div>
          </div>
        </template>
      </div>
      <footer class="chat-footer">
        <span class="chat-lock"><LockKeyhole :size="14" /></span>
        <span>{{
          t(
            "此处为静态示例，不会调用机器人或保存设置",
            "Static preview only. No bot message is sent and no settings are saved.",
          )
        }}</span>
      </footer>
    </div>
  </section>
</template>
