# 旺商聊脚本分类与风险 / Script Inventory

同步日期：2026-10-22（北京时间）。脚本从项目根目录显式运行，不注册到公共命令、AI或生产定时任务。
`check_`前缀**不代表只读**；先读源码及参数解析，确认支持后再用`--help`，
真实写入需要明确授权、固定对象和恢复点。
本文不列凭据、开通码或可复用测试令牌。

不要批量运行清单，也不要自动对所有脚本运行`--help`：旧工具可能没有参数解析，
直接执行就会访问线上、发消息或修改设置。`wsl_test_membership.py`是历史测试辅助库，
不是当前邀请CLI；成员操作使用`wsl_test_lab.py`。
下表的“只读”是业务数据边界，仍可能读取认证平台、写私有证据、截图或审计日志。

English: never run this inventory as a batch or assume every script implements
`--help`. Some legacy runners immediately perform live work. Read source first.
Read-only business checks can still produce local evidence or audit records.

## root工具日志保护 / Root CLI Logging

以root运行任何可能导入AstrBot的本地工具时，每条命令显式添加
`env ASTRBOT_DISABLE_FILE_LOGGING=1`，预览和回归也适用。例如：

```sh
env ASTRBOT_DISABLE_FILE_LOGGING=1 .venv/bin/python -m scripts.check_wsl_test_activation_live
```

该变量仅关闭CLI初始化时的文件接收器，保留AstrBot同一logger及控制台；
不要全局export或加入服务配置，不关闭运行中AstrBot文件日志。
root导入可能创建服务用户不可写的trace文件，2026-10-16已实际导致重启失败；
仅核对后修复受影响文件的属主／0600权限，不递归放宽应用目录，也不删除原日志。
观察前保存`NRestarts`基线，只检查本次增量；不得清零失败计数伪造通过。

English: prefix root-run CLIs that may import AstrBot with
`env ASTRBOT_DISABLE_FILE_LOGGING=1`, including previews and tests. This affects
only initial CLI file sinks, retaining the existing AstrBot logger/console.
Do not change global or service environments. Preserve historical startup failures,
repair only verified affected file ownership/mode, and compare soak restarts to
their initial baseline rather than clearing the counter.

## 推荐入口 / Current Entry Points

| 类别 | 脚本 | 影响与边界 |
| --- | --- | --- |
| 财务只读快照 | [audit_wsl_finance.py](audit_wsl_finance.py) | SQLite一致性备份、财务摘要及疑点清单；不修账或登记付款 |
| 离线预检 | [check_wangshangliao.py](check_wangshangliao.py) | 部署参数检查；不打印凭据，不证明真实登录成功 |
| 部署范围检查 | [check_wsl_deployment.py](check_wsl_deployment.py) | 必须选择`--runtime-only`或原始`--backup`；前者不比较历史迁移；`--hot-save`是真实写入，仅能配合备份范围 |
| 旧独立结账进程 | `check_wsl_cashier_process.py`（已移除） | 2026-10-22退役独立站，不再启动6186；现测插件结账入口 |
| 本地文档 | [build_wsl_guide_pdf.py](build_wsl_guide_pdf.py)、[check_wsl_docs.py](check_wsl_docs.py) | 前者写PDF，后者检查既有离线HTML，不证明源文件最新或产物已部署；使用`WSL_GUIDE_CHROMIUM`／`--browser` |
| 前端文档构建 | [build-wangshangliao-guide.mjs](../dashboard/scripts/build-wangshangliao-guide.mjs) | 生成离线教程，源文件为Markdown，不手改输出HTML；会写静态产物 |
| 合成页面 | [check_wsl_policy_page.py](check_wsl_policy_page.py)、[check_wsl_group_management_page.py](check_wsl_group_management_page.py)、[check_wsl_open_page.py](check_wsl_open_page.py)、[check_administrator_selector.py](check_administrator_selector.py) | 本地桥或Vite fixture，真实写入被拦截；部分会写截图 |
| 隔离财务页面 | `tests/test_wangshangliao_settlement_page.py` | 插件结账使用临时账本、合成后台身份及真实本地API；旧独立站4个页面脚本已移除，不等于生产付款验收 |
| 线上读取＋合成编辑 | [check_wangshangliao_page.py](check_wangshangliao_page.py)、[check_wsl_cards_browser.py](check_wsl_cards_browser.py) | 使用真实部署页面／读取，拦截设置写入；不是纯离线，也不证明真实改名片 |
| 好友入口移除检查 | [check_wsl_friend_removal.py](check_wsl_friend_removal.py) | 访问真实后台并探测已移除路由；`--reload`重载插件，`--docs`写截图；不是好友删除或纯合成测试 |
| 只读性能／本地负载 | [benchmark_wsl_database.py](benchmark_wsl_database.py)、[check_wsl_open_load.py](check_wsl_open_load.py) | 前者临时合成库；后者默认真实库只读，可`--database`指定快照；当前绝对报告显式加`--absolute-query-report` |
| 固定测试实验室 | [wsl_test_lab.py](wsl_test_lab.py) | 邀请、移除及平台角色等测试能力；预览确认、对象白名单，不用于正式群；预览也访问平台并保存测试日志 |
| 旧群离线归零 | [reset_wsl_test_group.py](reset_wsl_test_group.py) | 默认预览；`--apply`只处理已授权1459282，双服务停止、备份／摘要确认；不是全新群证明 |
| 当前真实验收 | [check_wsl_pair_live.py](check_wsl_pair_live.py)、[check_wsl_pair_rejoin_live.py](check_wsl_pair_rejoin_live.py) | 大蛤为机器人、林夕为管理、受控群；会临时处罚／改配置，须恢复和留证 |
| 三账号在线验收 | [check_wsl_three_live.py](check_wsl_three_live.py) `--apply --phase ...` | 复用林夕、大蛤、test的已有连接，不创建第四条NIM连接；仅1459282，临时原生管理权限结束后恢复，知识库测试仅增删本轮资料 |
| 无提权协作验收 | [check_wsl_cooperative_live.py](check_wsl_cooperative_live.py) | 默认只列计划；`--apply`使用43506527既有私信授权联系31846829，并由23691273触发1459282固定命令拒绝；只开短时测试窗口、不改策略或角色。`--phase permissions`不重发帮助检查；新帮助未加载时完整阶段会明确失败 |
| 三账号引用矩阵 | [check_wsl_quotes_live.py](check_wsl_quotes_live.py) | 默认计划，`--preflight`核验当前配置绑定，`--apply`发送9条固定群消息、各由另外两账号见证；无需为见证账号开通群服务或提权，不证明组件试点已加载 |
| 独立收费测试 | [check_wsl_model_cost.py](check_wsl_model_cost.py) | 固定最多4次模型调用；只在单独授权后运行，不打开公共AI门禁 |
| 知识检索模型测试 | [check_wsl_knowledge_model.py](check_wsl_knowledge_model.py) `--apply` | 独立测试库检索、当前咨询／热群人格各一次真实调用；不传正式知识，不等于公共AI聊天门禁通过 |
| 语义模型专项 | [check_wangshangliao_semantic.py](check_wangshangliao_semantic.py) | 六个合成场景使用真实配置模型，可能计费，无`--apply`门禁；不执行平台处罚，但不是离线回归 |
| 套餐原生命令 | [check_wsl_tree_menu_live.py](check_wsl_tree_menu_live.py) `--billing` | 受控两账号原生私聊，查本人套餐及数字菜单；临时私信发送授权和测试窗口会恢复，不授予付费套餐、不调用模型 |
| 后台复核 | [check_wsl_readonly_retest.py](check_wsl_readonly_retest.py) | 默认真实读取及页面检查；`--private`会临时改发送授权、开窗口并发固定私聊；`--platform`访问认证平台，不能笼统称零写入 |
| 财务聊天演练 | [check_wsl_finance_chat_live.py](check_wsl_finance_chat_live.py) | 默认读取；`--native`／`--extended`使用独立模拟账本但真实私聊、窗口及临时设置；`--reload`重载插件，不转账或写正式账本 |
| 旧结账日志检查 | `check_wsl_cashier_logging.py`（已移除） | 不再重启独立结账进程；日志由插件入口统一记录 |

English: prefer the entries above. Offline/rendering checks do not prove upstream
success. Real tests can mutate roles, messages, rules or schedules and need
explicit admission and restoration. Cost tests spend money and do not establish
provider hard-budget enforcement.

### 当前证据与旧前置条件 / Current Evidence And Stale Preconditions

2026-10-21（北京时间）三账号无提权协作完成9条真实群消息与6次相互引用，
每条都有另外两账号的原生接收证据。复用43506527既有私信授权，
31846829返回非客服负责人拒绝和取消成功；23691273触发的两条群命令
由43506527按原生群管不足固定拒绝，均有对端回执，未调用AI。
私聊“帮助”当时真实返回旧菜单，缺权限目录，补齐代码与别名回归后，已于后续插件热发布加载；
未重发该轮实测消息，不把加载证明当作对端原生验收。
初版配置对比误包含实时目录字段，原失败保留；只读策略历史复核零变更，未重发。
测试区间零模型／计费记录新增、零持久化策略写入，窗口关闭，生产进程和保护摘要不变。
旺商聊插件和管理页已热发布，通用核心路由仍404、组件试点仍关闭；
不能把这些传输和拒绝证据当成完整新执行器、合并或模型验收。
证据：`/opt/backups/wsl-quotes-live-20261020T182508Z/evidence.json`、
`/opt/backups/wsl-cooperative-live-20261020T183348Z/evidence.json`。

English: cooperative native acceptance needs no service activation for every
owner/witness account. Nine originals/quotes had two native witnesses each; two
private controls and two group permission refusals passed under existing grants.
Help omitted the shared catalog; its fix was later hot-loaded with the plugin,
without replaying the native test. An overly broad
volatile-state comparison failed; independent durable policy history verified no
changes. Failed evidence remains without replay. No new model/charge records,
policy writes, connector restarts or production pilot activation occurred.

2026-10-20后续三账号均在线后，在`1459282`复用群主`31846829`、机器人`43506527`、
发送者`23691273`已有连接，通过`test_advertising`完成3次真实警告核对。
两个fixture分别为1次、2次，不是同轮完整三级升级；原生`userNick`的`@大蛤`与目标一致，
不优先用群名片`testfork`。原消息撤回及警告送达、自撤回均有对端原生证据，
警告受理后12.112／12.115／12.125秒收到撤回通知。规则、临时原生管理员角色恢复为原普通成员，
窗口关闭；正式群、财务和正常处罚计数未变，原生回执与最小审计保留。
0模型调用、无额外NIM连接、未重启服务；未测禁言升级、全功能或手机画面。
证据：`/opt/backups/wsl-warning-live-20261020-I5Z5qf/result.json`。
首条验收脚本把SHA-256误作JSON解析，已只读恢复且未重发，原失败证据保留；
该私有临时脚本不是新公共或日常验收入口，不要照抄备份脚本再次发送。

English: three connected accounts verified three real warnings in 1459282 across
two isolated fixtures of one and two, not complete escalation in one fixture.
Native nickname/target and independent peer recalls passed at 12.112/12.115/12.125
seconds after acceptance. Rules, temporary native roles and windows were restored;
formal groups, finance and normal sanction counts stayed unchanged. Native receipts
and minimal audit remain. No model calls, extra NIM connections or restarts occurred.
Phone rendering, mute escalation and full functionality remain outside this result.
The first verifier error was recovered read-only without replaying the first message.

2026-10-20私有`wsl-ai-enable-20261020T0724Z/enable.json`已确认供应商
`quota_limited`有限额度及本地预算确认；没有测试实际耗尽停服，也没有由该脚本调用模型。
`wsl-group-ai-20261020-format/runtime.json`记录222项相关回归及北京时间15:47:56
的群AI自动发送`accepted`；没有另发群测试消息，未确认本轮手机显示。
默认安全值不等于运行配置，旧`unrestricted`证据也不覆盖本次有限额度确认。

`check_wsl_shared_soak.py`仍断言预算未确认；`check_wsl_account_names.py`仍要求
双账号和零启用群，`check_wsl_friend_removal.py`要求双账号，
`check_wsl_test_activation_live.py`还固定旧双账号实例，当前三账号绑定不适用，
`check_wsl_readonly_retest.py`等旧工具还要求正式群暂停。运行基线已变化时，
先审阅适用性，不为通过旧脚本而停用已授权群、改预算或恢复旧账号配置。
旧共享平台脚本已于2026-10-21移除：它只检查返回行的`running`，
缺少启用实例的完整集合断言，空返回也可能通过，且重载选项指向旧采集插件。
现在须将当前配置中的全部启用实例与`GET /api/v1/bots/stats`逐项比对，
同时核对`connection_state`；不能用进程`running`代替原生在线或真实消息证据。
`check_wsl_shared_soak.py`检查内存／任务当前值，不读取有效cgroup硬上限；
低占用观察不能证明资源限制已安装，仍须独立核对有效systemd属性。

负载工具的旧相对增长断言仍默认存在。当前显式选择`--absolute-query-report`，
报告绝对P95并保留相对诊断；用户已于北京时间2026-10-17取消旧10%增长上线阻断，
不篡改旧失败，也不由EXIT 0推断生产持续SLA。临时库、独立CPU进程与生产服务
cgroup须分开记录，CPU绑核不等于模拟服务配额。

English: October 20 evidence confirms a finite supplier allowance and an accepted
automatic group-AI send, not exhaustion enforcement, independent delivery or phone
rendering. Some older scripts require an unconfirmed AI budget, exactly two accounts
or paused formal groups. Do not change authorized runtime state to satisfy those
preconditions. Absolute query reporting preserves old failures and does not prove a
production SLA.

三账号工具的群测试消息使用后台白名单操作`test_probe`，固定从test向1459282发送，
须先在大蛤开启该来源的短时测试窗口，并校验恰好三个受控成员。
仅允许窗口中的固定命令或`WSL_TEST_`关键词；每次一条，至少三秒间隔，最多十次。
不接收群号、方法或URL，不授予群管理，不修改发送账号的正式群开关；
不明结果只留回执，不自动重发。该入口不向聊天、模型或公共菜单开放。
未开通群业务的test账号仅在原窗口有效时观察大蛤在1459282的原生文本回复，
核对传输身份与群号，最多二十条，按忽略消息保存，不投递命令、模型或群管队列。
自动规则对已接入机器人账号的保护仍保留；仅大蛤在1459282收到test的消息，
且原测试窗口已按`group_rules`接纳该消息时，允许测试撤回／禁言。
每次执行仍核验窗口、身份和原规则；关闭窗口或修改配置后旧消息不能继续处罚。
完整刷屏批次回执按测试窗口隔离，同一窗口内续发不重复禁言；
见证写入使用独立事务，取消或失效不提交，也不回滚普通消息及出站回执。
五条重复消息阈值可按三秒间隔实测；八条／十秒阈值仍需独立受控证据，不能冒称覆盖。
三个账号均为已接入机器人时，默认抽奖阶段只验证拒绝机器人报名、零参与者开奖；
单独选择下述 `lottery-member` 受控阶段才验证固定普通成员角色报名及中奖，
不取消正常活动的机器人报名保护，也不等于真人领奖验收。
测试实验室默认每天十次角色／成员变更。完整验收可显式指定
`check_wsl_three_live.py --native-write-limit 20`，累计仍查同一审计库，
不重置旧次数，最多二十次，保留三十秒写入间隔。
三账号脚本默认不变更原生角色，先读实时目录；请在官方App预先设置大蛤为管理员。
必须显式增加`--manage-test-role`才使用上述角色写入额度，不能用旧缓存代替目录核验。
`check_wsl_three_guides_live.py`默认只打印计划，`--apply`在短时窗口向1459282
重发两张固定欢迎图，以另一账号原生接收记录、出站回执、原图和缩略图摘要核验。
后台`test_guides`不能传文件、URL、群号或任意方法，最多消耗窗口中的两个发送名额，
继续遵守三秒冷却。它不重置首次欢迎、不假称首次加好友或手机显示通过。
`--phase lottery-member`显式启用固定成员抽奖测试；后台`test_lottery`只操作
原窗口的一次标记活动。报名使用真实接收，创建／开奖明确标为后台测试，不冒充群主聊天。
`check_wsl_three_card_live.py --apply`在独立测试数据目录预览并要求确认改名及恢复，
固定同一三人角色，不连接NIM、不改生产名片库；未知结果停下核对，不自动恢复或重试。
仅创建短测试名，原名必须可恢复；平台异常只保存固定路由、脱敏错误标签和数字返回码，
不记录请求凭据。2026-10-19已真实验证短名修改和原名恢复，旧长名未知请求保留，
只读核对不等于证明其历史结果，不能自动重放旧操作。
`wsl_test_lab.py --action transfer-owner`和`--action restore-owner`是单独的群主
转让测试，固定1459282的林夕与大蛤；不等于`grant`管理员，也不开放后台或聊天入口。
转回必须绑定同一本地审计库中已验证的前向转让，仍需独立预览和隐藏输入确认；
先核对实际角色，无法确认结果时不重试。共用原有每日额度，转让前至少剩两次，
不删旧计数、不新建审计库规避限制。转回后若原管理员角色未恢复，另行确认恢复。

English: `check_wsl_three_live.py` reuses all three online connections. Its
dashboard-only `test_probe` action admits one fixed-group message per request,
at least three seconds apart, only under the receiver's bounded test window and
an exact controlled roster. It accepts no group/method/URL parameter and grants
no business authority. Unknown outcomes are not replayed. Five repeated messages
can exercise the thirty-second flood threshold; this does not prove eight messages
within ten seconds.
Managed-account sanction protection remains on outside the exact admitted
`group_rules` message from the fixed test account to the fixed receiver in1459282.
Window closure, identity changes and policy invalidation stop pending sanctions.
Completed flood receipts are window-scoped; continued messages within one window
do not repeat a mute. Witness persistence uses an isolated transaction so a
revoked or cancelled observation cannot commit or roll back unrelated writes.
Native roles are read fresh; role changes require explicit `--manage-test-role`
and remain subject to the persistent daily cap. Fixed-guide replay has a separate
opt-in runner, original-window guards and independent native image receipts;
it does not prove first-friend acceptance or phone rendering.
Local `wsl_test_lab` owner-transfer actions are separate from administrator grants.
Only the fixed owner and peer in1459282 can exchange ownership; return needs a
verified forward journal receipt and a new confirmation. They share the existing
write budget and never reset its counters, retry unknown outcomes or expose a
production/chat/AI ownership tool. Returning ownership may require a separately
confirmed restoration of the peer's original administrator role.
With three managed bots, the default lottery phase covers managed-entrant refusal
and an empty draw. Only the explicit `lottery-member` phase exercises the fixed
controlled native-member enrollment and winner notification; this passed on
October19,2026, but does not prove real-person claiming or actual prize delivery.
Normal managed-account participation protection is not disabled.
The isolated card runner uses a short test card and records only bounded provider
error labels/codes, never credentials. Native change and restoration passed on
October19; the prior long-card unknown remains recorded and is never replayed.

## 专项与历史验收 / Targeted And Historical Checks

以下不是常规启动流程。参数与旧测试对象可能不适合当前状态，不能整批执行：

- 接收／权限／活动：[check_wsl_integrated_live.py](check_wsl_integrated_live.py)、
  [check_wsl_authorization_live.py](check_wsl_authorization_live.py)、
  [check_wsl_flood_cycle_live.py](check_wsl_flood_cycle_live.py)、
  [check_wsl_flood_live.py](check_wsl_flood_live.py)、
  [check_wsl_workflows_live.py](check_wsl_workflows_live.py)、`check_wsl_tree_menu_live.py`。
  其中真实消息、禁言、公告和活动脚本可能修改测试状态，不能把失败当作安全重跑依据。
  特别是`check_wsl_authorization_live.py`没有`--apply`预览门禁，直接执行即开始真实流程；
  它仍被公共辅助库与回归引用，保留不等于适合当前线上重跑。
  恢复配置不等于没有发送或没有历史副作用。
  旧`check_wsl_progressive_live.py`已退役：其旧测试群与处罚档位不适用于当前部署，
  且无运行时或测试引用。替代脚本默认只预览，显式`--apply`才在固定测试群
  1459282执行；1／3／10分钟均自然等待，窗口分段且每段不超过300秒。
  到期回读延迟与未知结果单独记录；只读确认旧处罚结束后可用
  `--resume-directory`沿用原账本续验排名，不重发处罚、不重置违规计数。
- 私聊／页面：[check_wsl_private_invitations_live.py](check_wsl_private_invitations_live.py)、
  [check_wsl_ai_dialogue_live.py](check_wsl_ai_dialogue_live.py)、
  [check_wsl_migration_dialogue.py](check_wsl_migration_dialogue.py)、
  [check_wsl_live_page.py](check_wsl_live_page.py)、
  [check_wsl_integrated_page.py](check_wsl_integrated_page.py)、
  `check_wsl_cashier_browser.py`。
  旧AI管理轮次仅保留历史证据，当前不能据此重新开放工具或公共模型。
- 目录／邀请／配置：[check_wangshangliao_invitations.py](check_wangshangliao_invitations.py)、
  [check_wsl_invitation_history.py](check_wsl_invitation_history.py)、
  [check_wsl_account_names.py](check_wsl_account_names.py)、
  `check_wsl_cashier_speech.py`、`check_wsl_cashier_agent_only.py`。
  `--reload`类选项会重载插件，`--docs`会写仓库截图，不能一概当作纯读取。
  涉及真实查询时遵守平台冷却；脚本断言通过不代替手机或财务人工核对。
- 历史内部库：[wsl_test_membership.py](wsl_test_membership.py)仍有回归引用，
  允许旧普通邀请者的构造路径不是当前生产准入成功案例，不独立运行，也不替代TestLab。
- 历史灰度观察：[check_wsl_shared_soak.py](check_wsl_shared_soak.py)仍要求旧AI预算未确认，
  不列入当前推荐流程；先核对适用性，不为通过断言改变生产预算或开关。
- 历史开通替代测试：[check_wsl_test_activation_live.py](check_wsl_test_activation_live.py)
  默认只列计划；固定旧双账号及实例，不适用当前三账号。保留离线回归依赖，
  不为通过它停用现用账号或恢复旧绑定；其`--apply`原流程会停止接入器并真实发送`开通`。

## 管理及一次性修复 / Administrative Tools

下列可改配置、服务、知识库或财务数据，**不属于验收必跑项**：

| 工具 | 用途／保留原因 |
| --- | --- |
| [harden_wsl_shared_service.py](harden_wsl_shared_service.py) | 主服务加固的历史工具，绑定旧固定备份／CPython版本，不作为通用重跑或回滚入口；旧独立站部署脚本已移除 |
| [wsl_policy_migrate.py](wsl_policy_migrate.py) | 兼容的策略迁移，不用旧配置覆盖公共租户规则 |
| [wangshangliao_maintenance.py](wangshangliao_maintenance.py) | `status`只读；`backup`写含敏感数据的私有备份；`approve-retention`真实授权后续清理，须24小时健康观察及精确实例，不是普通检查 |
| [wangshangliao_moderate.py](wangshangliao_moderate.py) | 历史人工动作CLI，`--confirm`可按实际策略执行平台动作；没有TestLab固定对象边界，不作为公共模式验收推荐入口 |
| [correct_wsl_cashback_amounts.py](correct_wsl_cashback_amounts.py) | 已有金额修复及回归引用；历史工具，不因本次清理再次执行 |
| [enable_wsl_agent_only.py](enable_wsl_agent_only.py) | 已确认的返现政策变更工具；不擅自重跑 |
| [fix_wsl_telegram_session_access.py](fix_wsl_telegram_session_access.py) | 跨平台会话修复；不纳入旺商聊日常更新 |
| [retire_wsl_dahai_group.py](retire_wsl_dahai_group.py) | 用户明确授权过的废弃群清理；不是通用删群／删账入口 |
| [prepare_wsl_knowledge.py](prepare_wsl_knowledge.py)、[provision_wsl_business_knowledge.py](provision_wsl_business_knowledge.py)、[import_wsl_knowledge.py](import_wsl_knowledge.py) | 受控知识库预备／导入，仍有回归引用 |
| [generate_wsl_open_guide.py](generate_wsl_open_guide.py)、[publish_wsl_feature_guide.py](publish_wsl_feature_guide.py) | 品牌功能图生成／发布；生成会调用配置的外部图片API，发布写静态产物，不是免费离线校验；与运行时首次菜单去重分离 |

保留不代表建议执行。金额、邀请归属、付款记录及现有异常暂停不因源码整理改变。

仓库[共享加固模板](../services/astrbot-hardening.conf)为CPU 200%、MemoryHigh 1G、
MemoryMax 2G、TasksMax 256。旧结账服务及其加固单元于2026-10-22移除，
不再配置或启动第二个结账进程；下文旧限额仅为当时记录。
这是模板，不是当前有效systemd值。共享进程限额不等于逐群硬隔离。
[Telegram会话例外](../services/astrbot-telegram-session.conf)开放既有会话目录写入，
涉及其他平台，不可在WSL更新中无意移除或扩大。
[通用用户服务示例](astrbot.service)不是生产单元替代品。

## 拒绝兼容与已删除 / Retirement

2026-10-22（北京时间）本轮文件清理仅补记一个无对应源码的
`developer_gate.cpython-312.pyc`已备份后删除；本次文档收尾不执行删除，
不删除其他脚本、缓存或历史。下列2026-10-21及更早记录保留原日期与结论，
不计为本轮再次清理或回归。本次未运行脚本、测试或构建，也未重启或部署。

English: this round records only one previously backed-up and removed orphan
`developer_gate.cpython-312.pyc`. No other scripts, caches or history are removed.
Earlier dated cleanup and acceptance conclusions remain historical; this
documentation pass runs no scripts, tests, builds, restarts or deployments.

2026-10-21脚本清理移除以下8个无生产、脚本或回归导入依赖的过期入口，
旺商聊Python脚本由75个减为67个。删除前源码及受影响文档／测试已归档：
`/opt/backups/wsl-script-cleanup-20261021-gyHsG6/before.tar.gz`（私有目录）。
归档只供审阅或定向恢复；不要整包覆盖现有工作区，也不要从备份重跑旧实网操作。

| 已移除入口 | 过期原因 | 当前受控入口／核对方式 |
| --- | --- | --- |
| `check_wsl_activation_reply.py` | 绑定旧实例，额外使用缓存令牌登录NIM，已不适用当前认证方式 | [开通服务离线回归](../tests/test_wangshangliao_open_service.py)；实网另核当前三账号与原生授权，旧双账号工具不能直接替代 |
| `check_wsl_activities_live.py`、`check_wsl_group_authorization.py` | 绑定废弃群1143980／旧账号9798577，并临时修改后台或群权限 | [check_wsl_three_live.py](check_wsl_three_live.py)的显式受控阶段；不恢复旧自然语言写配置流程 |
| `check_wsl_live_capabilities.py` | 绑定旧群，无apply门禁，直接执行公告、禁言及临时扩权 | [wsl_test_lab.py](wsl_test_lab.py)、[check_wsl_three_card_live.py](check_wsl_three_card_live.py)；不等于完整平台动作验收 |
| `check_wsl_invitation_recalls.py` | 旧实例及额外缓存令牌NIM登录，已由现有连接和账本核对替代 | [check_wsl_invitation_history.py](check_wsl_invitation_history.py)、受控三账号阶段 |
| `check_wsl_saved_suffix.py` | 无预览，直接修改废弃群1143980名片及固定大海传媒后缀 | [check_wsl_three_card_live.py](check_wsl_three_card_live.py)，独立数据目录、预览确认及恢复 |
| `check_wsl_shared_platforms.py` | 旧采集插件重载入口，空统计也能通过且未验证原生在线 | 配置启用实例全集与`GET /api/v1/bots/stats`及连接状态逐项核对 |
| `check_wsl_welcome_repair.py` | 已完成的特定重邀／单张旧欢迎图修复验收，不代表现行双图流程 | [check_wsl_three_guides_live.py](check_wsl_three_guides_live.py)，使用既有连接及独立对端回执 |

English: eight unreferenced, obsolete runners were archived and removed on
October 21, 2026. The private archive preserves the pre-cleanup sources and affected
documentation/tests. Current controlled tools are not blanket replacements for
all old live actions: review their scope and explicit opt-in requirements first.
Shared helpers, deployment/backup/rollback tools and fail-closed compatibility
guards remain. No production configuration, financial ledger, native connection
or service was changed or restarted for this cleanup.

2026-10-20本轮冗余扫描未删除额外脚本或前端组件：三份生产源码净减66行，
仅合并`event.py`等价回复调用、`policy.py`关键词校验，删除`cashback_store.py`
未使用私有旧头像定价迁移函数。无迁移执行、数据库清理、历史账务修改或后端加载。
新增回归110项；完整相关回归6564通过、52条件跳过、3 warnings，耗时237.65秒，
证据`/opt/backups/wsl-cleanup-20261020-IsoIYB/full-regression.xml`。
专项527／81／192项结果有重叠，不累加为唯一总数。修改文件Ruff通过；
全仓仍有未触及`reset_dashboard_live.py`的I001及66份原有格式差异，不称全仓检查全绿。
全部管理页Vue通过严格未使用项类型检查。文档构建发布与源码清理不重载／重启、不发消息；
下列删除记录属于此前轮次，不是本轮再次执行。

[run_wsl_open.py](run_wsl_open.py)、[deploy_wsl_open.py](deploy_wsl_open.py)及
[services/astrbot-open.service](../services/astrbot-open.service)
仅保留明确拒绝启动的兼容保护；不能恢复独立进程或旧Socket网络回退。
`ai_config.py`只返回旧AI管理已停用，不保留自然语言写入实现；
财务结账Socket不在此停用范围。

已移除`merge_wsl_open.py`、`check_wsl_automatic_cards_live.py`、
`check_wsl_open_deployment.py`和`finish_wsl_rule_management.py`：
分别为已完成的一次性合并器、绑定旧账号与废弃群的名片测试，以及仅互相引用的
旧部署验收工具；旧条件要求空公共worker和空授权，不适合当前合并模式。
均不是当前运行或回归依赖。
源码清理前备份在私有备份目录；历史证据不删除。不要从旧备份复制脚本恢复旧行为。

[文档目录](../docs/wangshangliao.md) ·
[测试门槛](../docs/zh/platform/wangshangliao-testing.md) ·
[维护与发布](../docs/wangshangliao-maintenance.md)。
## Scheduled Image Rehearsal

`check_wsl_activity_pilot.py --apply --phase scheduled`仅在1459282运行定时图文补测，
不进入抽奖、刷屏或解除禁言阶段。默认角色操作每日上限10次；
显式`--daily-write-limit 20`使用TestLab已有的20次档位，共用同一持久审计与计数，
不重置计数、不使用额外角色扩容豁免，临时管理员授权结束后须独立核验恢复。
报告中的`passed`仅表示`acceptance_level=transport_only`；
`phone_verified=false`不能据此改为手机图片验收通过。

## Native Quote Rehearsal

[check_wsl_quotes_live.py](check_wsl_quotes_live.py) is opt-in and restricted to the existing three accounts
in test group1459282. Run with `--preflight` to verify existing connections and
whether the tested handler is loaded. Default invocation sends nothing.
`--apply` opens a bounded developer window and sends three originals plus six
quote directions, sequentially at least three seconds apart. Unknown outcomes
are not retried; each step requires two independently decoded witnesses.
No extra native login, role grant, group enablement, AI call or financial write
is performed. Evidence stays under the private `/opt/backups` directory.
Phone rendering must still be separately confirmed.

## AI Page Deployment Regression

[dashboard/tests/wangshangliaoAI.browser.py](../dashboard/tests/wangshangliaoAI.browser.py)
checks the source fixture by default.
Use `--built-page astrbot/builtin_stars/wangshangliao_moderation/pages/management/index.html`
from the project root to test the actual compiled application and embedded styles
against the same write-denying bridge at390/1440px. Source-only tests cannot prove
the deployed page includes new controls. This caught an outdated page on October19;
build into a separate temporary output, verify it, back up the existing HTML and
replace only `index.html`. Do not erase packaged guides with a default clean build.
No production API, model request or configuration save is used by this regression.

## 离线验证索引 / Offline Verification Index

Python回归同时收集`tests/test_wangshangliao_*.py`和`tests/test_wsl_*.py`；
近期六类`test_wsl_audit_*_20261019.py`及三个`test_wsl_distributed_*`不能漏测。
`tests/conftest.py`隔离WSL指标／模型统计及会话偏好，但不是所有路径或网络的沙箱。
登录截图仍有`data/temp`硬编码，部分独立浏览器工具也默认写此目录或旧固定备份目录；
在隔离检出目录运行，并显式选择本轮截图和产物输出，不复用生产数据目录。
`check_wsl_cashier_agent_only.py`固定写`/opt/backups/wsl-agent-only-20261013`，
重跑可能覆盖原日期截图；无法选择新输出目录的历史工具不要重跑，
隔离工作目录或设置`ASTRBOT_ROOT`并不能重定向绝对路径。
当前条件变量和fixture路径见[测试与验收](../docs/zh/platform/wangshangliao-testing.md)。

[前端文档测试](../dashboard/tests/wangshangliao-docs.test.mjs)校验链接打包及临时HTML；
[后台审计测试](../dashboard/tests/wslAuditDashboard20261019.test.mjs)校验切换范围、
失败读和迟到响应；AI配置及预算还有独立Node测试。它们不会调用生产接口，
不能证明编译产物已更新、手机客户端显示或供应商扣费。
上述脚本代码链接对应当前`*wsl*`／`*wangshangliao*` Python文件；
三账号专项还包括[欢迎图](check_wsl_three_guides_live.py)和
[隔离名片](check_wsl_three_card_live.py)，不能从默认阶段推断全部已执行。
源码链接用于仓库审阅；离线HTML不打包Python、测试或service源码，
Node文档测试只保证Markdown文档链接可映射，不能据此声称所有源码链接均可离线打开。

## Real AI Dialogue Experiments

[check_wsl_guidance_dialogue_live.py](check_wsl_guidance_dialogue_live.py) is opt-in
(`--apply --evidence /opt/backups/EXISTING_PRIVATE_DIRECTORY`). It uses only the
existing online 林夕31846829 → 小满43506527 private peers, checks native receipts
and independent witnesses for eight guidance questions, restores its exact-peer
send grant with revision checks and closes its developer window. No NG guidance
trial, plan grant, group AI or financial permission is consumed or created.
Supplier requests can incur actual charges. Unknown sends are not replayed and
existing evidence is not overwritten.

[check_wsl_warm_dialogue_model.py](check_wsl_warm_dialogue_model.py) uses the current
bound warm-group configuration and persona with real supplier/PC28 requests, but
isolated authority, SQLite metering and simulated delivery. It cannot prove native
group rendering or grant readiness. The existing minute cooldown stays enabled;
at most sixteen model requests are made per run. `--cases` selects bounded cases,
including normal chat/history, authority refusal, all five PC28 query types,
risk questions, autonomous participation/silence, idle and disabled mentions.
Inspect semantic answers, tool results, settlement and safety fallbacks rather
than treating a zero exit code as unconditional acceptance.
