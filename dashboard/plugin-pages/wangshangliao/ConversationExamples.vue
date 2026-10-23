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
  | "admin-command"
  | "admin-ai"
  | "group-ai"
  | "customer-service"
  | "own-invites"
  | "denied-command"
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
      "发送者已获本群管理授权，机器人具备群管理权限。",
      "Sender has this group's management grant and the bot has native admin rights.",
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
        text: t("当前已选群：大海兼职群\n可直接发送「规则」「禁言名单」「关键词回复」「定时发言」；不用先选菜单。", "Selected group: Sample Group. Send the relevant command directly; no menu navigation required."),
      },
      { from: "admin", name: t("管理员", "Admin"), text: "定时状态" },
      {
        from: "bot",
        name: t("机器人", "Bot"),
        text: t("当前没有启用的每日禁言计划。", "No daily mute schedule is active."),
      },
    ],
  },
  {
    id: "admin-ai" as const,
    label: t("管理员只读咨询", "Manager read-only consultation"),
    icon: ShieldCheck,
    route: t(
      "/chat → 只读答疑 → 用户自己使用固定命令",
      "/chat → read-only guidance → caller uses fixed commands",
    ),
    condition: t(
      "后台独立选择AstrBot配置、人格和模型，保留只读安全限制，不继承工具；预算门禁未通过时暂停调用，保存配置不会自动开放。",
      "Dashboard selects profiles, personas and models separately; read-only safety remains, with no tools; unverified budget gates block calls and saving a profile never opens them.",
    ),
    channel: t("管理员私聊 · 只读咨询", "Manager DM · Read-only consultation"),
    messages: [
      {
        from: "admin",
        name: t("管理员", "Admin"),
        text: "/chat",
      },
      {
        from: "bot",
        name: t("机器人", "Bot"),
        text: t(
          "公共AI尚未核实供应商硬预算，暂未开放。固定命令仍可使用。",
          "AI is unavailable until the supplier hard budget is verified. Fixed commands remain available.",
        ),
      },
      {
        from: "admin",
        name: t("管理员", "Admin"),
        text: "/exit",
      },
    ],
  },
  {
    id: "group-ai" as const,
    label: t("热群开关", "Group AI switches"),
    icon: UserRound,
    route: t(
      "本群管理 → 直接查询 → 后台维护独立方式",
      "Group manager → direct query → dashboard maintains individual modes",
    ),
    condition: t(
      "仅本人获授权群；@、自主接话和冷场使用本群热群人格；@按成员隔离会话；独立方式开关，预算门禁不因菜单解除。",
      "Authorized groups only; @, autonomous and idle replies use the group profile; @ conversations are isolated by member; separate switches never bypass budgets.",
    ),
    channel: t("本群管理 · 固定命令", "Group manager · Fixed commands"),
    messages: [
      {
        from: "admin",
        name: t("管理员", "Admin"),
        text: "热群状态",
      },
      {
        from: "bot",
        name: t("机器人", "Bot"),
        text: t(
          "本群AI总开关：关闭\n@对话、普通聊天自主接话、冷场发话按本群独立配置。",
          "Group AI master switch: off. Mentions, autonomous chat and idle topics have independent group settings.",
        ),
      },
      {
        from: "admin",
        name: t("管理员", "Admin"),
        text: "本群额度",
      },
      {
        from: "bot",
        name: t("机器人", "Bot"),
        text: t(
          "显示本人可查询的本群额度；没有本人扣费同意时，不会自动使用他人额度。",
          "Shows the authorized group's quota. Another person's allowance is never used without that person's consent.",
        ),
      },
      {
        from: "admin",
        name: t("管理员", "Admin"),
        text: "/chat",
      },
      {
        from: "bot",
        name: t("机器人", "Bot"),
        text: t(
          "进入只读管理员指引；固定命令和本人确认仍由你发送。",
          "Read-only administrator guidance. Fixed commands and confirmations remain yours to send.",
        ),
      },
      { from: "admin", name: t("管理员", "Admin"), text: "怎么设置定时图片？" },
      {
        from: "bot",
        name: t("机器人", "Bot"),
        text: t(
          "在群管理→规则→定时发言上传，最多3张，按顺序每次发1张。也可私信先选群，发“广告图片 计划序号”，再发一张图片，收到预览后按提示确认保存。",
          "Upload up to three images under Group Management → Rules → Scheduled Messages. One rotates per occurrence. Or select a group by DM, send 广告图片 followed by its plan number, then one image; confirm the returned preview.",
        ),
      },
      { from: "admin", name: t("管理员", "Admin"), text: "/exit" },
      {
        from: "bot",
        name: t("机器人", "Bot"),
        text: t(
          "已结束咨询，未修改任何设置。",
          "Consultation ended. No settings were changed.",
        ),
      },
    ],
  },
  {
    id: "customer-service" as const,
    label: t("普通用户客服", "Member customer service"),
    icon: MessageCircle,
    route: t(
      "私聊 /chat → 只读客服；/exit 退出",
      "Private /chat → read-only support; /exit ends the session",
    ),
    condition: t(
      "内置只读客服提示；仅供应商硬预算核验后才调用已选模型。",
      "Built-in support prompt; the selected model is called only after supplier hard-budget verification.",
    ),
    channel: t("普通用户私聊 · AI 客服", "Member DM · AI support"),
    messages: [
      {
        from: "member",
        name: t("群成员", "Member"),
        text: "/chat",
      },
      {
        from: "bot",
        name: t("机器人", "Bot"),
        text: t(
          "公共AI尚未核实供应商硬预算，暂未开放。固定命令仍可使用。",
          "AI is unavailable until the supplier hard budget is verified. Fixed commands remain available.",
        ),
      },
      {
        from: "member",
        name: t("群成员", "Member"),
        text: "菜单",
      },
      {
        from: "event",
        name: "",
        text: t(
          "菜单由固定规则返回，不调用模型，也不授予群管权限。",
          "Fixed rules return the menu without a model call or management grants.",
        ),
      },
      {
        from: "bot",
        name: t("机器人", "Bot"),
        text: t(
          "命令菜单\n1. 开通与身份\n2. 群与成员\n3. 审核规则\n…\n0 返回上一步；取消退出。",
          "Command menu\n1. Activation and identity\n2. Groups and members\n3. Moderation rules\n...\n0 Back; 取消 Exit.",
        ),
      },
    ],
  },
  {
    id: "own-invites" as const,
    label: t("本人邀请", "My invitations"),
    icon: Users,
    route: t(
      "本人查询 → 已开启返现群名菜单 → 回复编号 → 本群本人汇总",
      "Own query → enabled cashback group menu → number → own group totals",
    ),
    condition: t(
      "私聊回复开启；返现采集机器人与群已配置。",
      "Private replies are enabled and the cashback collector and groups are configured.",
    ),
    channel: t("普通用户私聊 · 我的邀请", "Member DM · My invitations"),
    messages: [
      { from: "member", name: t("群成员", "Member"), text: "我的邀请" },
      {
        from: "bot",
        name: t("机器人", "Bot"),
        text: "【我的邀请】\n1. 业务交流群\n2. 通知群\n\n回复群序号查询。",
      },
      { from: "member", name: t("群成员", "Member"), text: "1" },
      {
        from: "bot",
        name: t("机器人", "Bot"),
        text: "【我的邀请】\n业务交流群\n邀请记录：2 人\n当前在群：2 人\n核验中：1 人\n待结账：5.00 元\n已结账：0.00 元\n已作废：0 人",
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
      { from: "member", name: t("群成员", "Member"), text: "添加管理员 2" },
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
        "有效邀请：1 人\n累计奖励：5.00 积分\n每人奖励：5.00 积分",
        "Verified invitations: 1\nTotal rewards: 5.00 points\nReward per invite: 5.00 points\nStatus: enabled",
      ),
    },
    {
      channel: groupChannel,
      from: "event",
      name: "",
      text: t(
        "群内命令和对应回复安排12秒撤回；积分记录仍保留。",
        "Group commands and replies are scheduled for recall after 12 seconds; the credit ledger is retained.",
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
