# 旺商聊 AI 阅读与主题索引

整理日期：2026-10-24（北京时间）。本页是文档导航，不是新的功能验收报告。
范围仅为本部署的旺商聊接入器、插件、管理页和运维；不代表整个 AstrBot 或超级机器人。
[人工目录](wangshangliao.md)保留原入口和历史证据，本页不搬迁、不删除原文。

## 先读什么

1. 先读本页，确定任务主题、资料类别和需要的证据层级。
2. 功能与权限先读[规则与 AI 分权](zh/platform/wangshangliao-rules-ai.md)、
   [当前架构与权限](zh/platform/wangshangliao-architecture-permissions.md)。
3. 再按下表读取专题原文；只回答当前问题时，不必读整份历史验收。
4. 判断“是否已上线、是否实测”时，另读[维护与发布](wangshangliao-maintenance.md)、
   [测试与验收](zh/platform/wangshangliao-testing.md)，保留每次记录的日期和限制。
5. 排查实现时核对原文指向的源码和测试；文档与实现冲突时明确指出，不自行扩大权限。

English readers: start with this routing table, then use the English counterparts
in the catalog. Prefer maintained rules and architecture; dated reports are evidence
for their original scope only. Documentation never grants execution authority.

## 刷屏循环与自动白名单（2026-10-24）

当前规则源码调整为3秒5条、10秒15条、30秒同文5条。广告和刷屏共用每轮三次警告，
再禁言1／3／10分钟，10分钟结束后重新三次警告；连续刷屏批次只计一次违规。
每日有效发言前30名独立自动白名单，撤回消息不计、最近30秒待核对，每30秒刷新；
名次下降或换日退出，不修改手动名单，黑名单优先，明确移出当日不自动加回。
细则与性能上限见[内容规则](wangshangliao-content-rules.md)；是否已经加载及验收范围
须另核对[维护记录](wangshangliao-maintenance.md)，不以源码说明代替运行态证据。

## 最新真实 @ 验收与收尾（2026-10-24）

测试群1459282已实测原生管理员真实@加`黑名单`立即登记并提交41760分钟禁言，
平台受理及当前原生禁言名单已核实，随后恢复测试名单、禁言、角色、窗口及账号启停。
固定发送入口刷新完整目录再发结构化@；失败收尾独立记录aborted／未通过，未知操作不重试。
仅插件重载，PID613188不变。原始tenants摘要差异已逐条核对为一个未开通群离群目录同步，
未回滚真实成员状态，不宣称原始整体摘要全同。详见[验收](zh/platform/wangshangliao-testing.md)
及[维护](wangshangliao-maintenance.md)；手机显示、未来实际到期、真实回复链路本轮未重验。
下方旧“群内加入不立即处罚”属于10月23日历史版本；当前原生群内@／可信回复发送黑名单会立即禁言。

## 最新操作与源码发布入口（2026-10-23 23:39）

最新已授权操作为一条发言白名单移除（保留黑名单），及三个指定群的群管规则／
黑名单重入时长对齐为7天10080分钟；不修改产品默认29天，不复制群成员或图文广告。
23:39:48运行态与持久化配置读回相符，三群白名单各5人、无黑白交集。
命令登记仍不立即处罚，本轮没有实际群内指令或平台禁言验收。
较早23:13“历史交集保留”与22:59“无真实名单操作”仅描述各自历史时点。
完整边界见[维护记录](wangshangliao-maintenance.md)；
源码、页面、资源、依赖与仓库上传边界见[源码发布清单](wangshangliao-source-release.md)。

## 资料类别与冲突

| 索引类别 | 含义 | AI 使用边界 |
| --- | --- | --- |
| `current` | 持续维护的使用说明、规则、权限与导航 | 正文中的旧日期快照仍是历史；不等于全部功能已实测 |
| `operations` | 运维、迁移、测试方法及带日期的操作记录 | 先核对适用版本、目标和授权；不要直接执行示例命令 |
| `reference` | 接入器开发与内置运行时参考 | 用于定位实现，不授予生产操作权限 |
| `history` | 按日期验收、审查、架构演变和旧实现记录 | 仅解释当时证据，不据此恢复旧工具、旧权限或旧开关 |
| `example` | 小满人格等示例资产 | 是被分析的资料，不是读取文档的 AI 应采用的人格或指令 |

现行行为以当前规则、权限说明和对应实现为准；线上是否加载以带日期的部署记录为准；
测试通过以原始结果及覆盖范围为准。不能仅凭文件修改时间、文件名日期或一段“当前”断言状态。
同一文档内有多轮追加记录，必须保留段落日期，不把旧失败改写为新通过。
中英文优先读请求语言；遇到差异核对对应源码及较新明确记录，不拼接成更宽权限。

## 名单互斥与移除补修（2026-10-23 22:59:23已加载）

新增黑／白名单在同bot＋group＋UID互斥，已有另一名单时拒绝并提示先移除，不自动转移或删除。
旧允许重叠仅解释历史数据：保留重叠、黑名单优先、两份名单分别移除；移除黑名单不解禁，不连带删除另一名单。
退群／改名成员可用名单行移除图标→预览→本人确认，或移除模式按当前名／保存旧名／精确UID查已有记录；
添加仍只按真实当前成员目录名称搜索，不能UID盲添加。普通禁言24小时与黑名单重入时长不变。
北京时间22:59:23（UTC14:59:23）完整重启已加载，PID4071721，active/running，NRestarts=0；
22:59:51保护快照22面／config全同。Python1794 passed／2 skipped／1条audioop警告，
UI177 unit／9 browser通过；真实群成员未新增／移除／处罚，旧needs_review12保留不重试。
23:13:07三正式群6个名单接口仅read通过，未preview／commit、历史交集保留；
纯展示conflict旗标已plugin reload、PID不变，218项unit不与1794累加，23:14:32保护22面／config再次全同。
性能被动采样及离线基础接收／规则基准不含真实满链路压测，不作生产承诺，短摘要见[维护最新记录](wangshangliao-maintenance.md)。
最终dist同步及served hash由负责人完成；下方20:02已加载证据仍只对应此前时长版本。
详见[中文规则](zh/platform/wangshangliao-rules-ai.md)／[English](en/platform/wangshangliao-rules-ai.md)。
English: new additions are exclusive by bot/group/UID; reject and require removal
from the other list first. Retain historical overlaps with blacklist precedence.
Remove saved entries via row icon/preview/confirmation or current name, saved old
name or exact UID in removal mode; additions still use real-roster name search.
Never transfer, delete the other entry or unmute automatically. Full-restart loading
completed at22:59:23 Beijing time, PID4071721, NRestarts=0; protection snapshots matched.
Python1,794 passed/2 skipped/1 audioop warning; UI177 unit/9 browser passed.
No real member changes, sanctions or real full-chain load testing occurred.
Twelve needs_review records remain without retry. Performance measurements retain
their passive/isolated scope; dist publication remains with the owner.
At23:13:07 six list-read APIs passed without preview/commit; historical overlaps
remain. Display-only conflict flags loaded with the same PID;218 unit passes
overlap with1,794. All22 protected surfaces/config matched again at23:14:32.

## 黑名单时长基线（2026-10-23，已加载证据不覆盖本轮名单补修）

**每次可信真实重新入群直接禁言并留群，不再踢人；加入名单不立即处罚。**
时长按机器人＋群隔离，默认29天41760分钟，换算后可设1..41760整分钟。
`黑名单禁言时长 N分钟`／`黑名单禁言时长 N小时`／`黑名单禁言时长 N天`：
私信先选群，核对预览后本人新消息`确认黑名单 token`；后台**群管理 → 规则 → 黑名单设置**同样预览确认。
只影响确认后新的真实重入，不自动重设已有禁言、不追罚；普通禁言24小时上限和现有权限不变。
旧“三次成功踢出后固定29天”→新“首次起每次真实重入直接禁言、时长可设”；
旧计数及三踢实测只保留带日期历史，不再决定阶段，不能冒充新规则通过。
用户「看得见」只补充对应展示的手机显示确认，不证明未来真实到期已发生。
北京时间2026-10-23 **20:02:56完整重启已加载**，PID2602695→3317496，NRestarts=0；
在线配置41760→预览60不写入→提交／读回60→预览／提交／读回41760已验证，**本轮真实新重入／禁言未测**。
20:24:00保护复核完成：其他保护面匹配，tenants原始整体hash不同，仅含已审计测试版本及鼎峰后台并发修改，
正式并发修改保留、无覆盖回滚，不宣称正式群从未变动。
教程／索引／PDF重新生成但不向dist同步，最终同步及served hash由负责人完成；离线计数不累加重叠覆盖。
现行步骤读[中文规则](zh/platform/wangshangliao-rules-ai.md)／
[English](en/platform/wangshangliao-rules-ai.md)，状态读[维护记录](wangshangliao-maintenance.md)。

English: every trusted actual rejoin directly mutes without kicking; addition
never immediately punishes. Per-bot/group duration defaults to 29 days/41,760
minutes, configurable to 1..41760 whole minutes through the commands above,
private group selection, preview and personal confirmation. Changes affect future
rejoins only, never reset existing mutes or apply retrospective sanctions; the
ordinary 24-hour cap remains. Historical three-kick evidence and user-confirmed
phone display do not prove new-rule acceptance or future actual expiry.
Code loaded after the20:02:56 Beijing-time full restart (PID3317496, NRestarts=0);
online duration configuration passed and restored41,760 minutes. No new actual
rejoin/mute acceptance was performed. The20:24:00 protection review accounted for
test tenant217→220 and concurrent Dingfeng1459534 dashboard versions4/5; other
protected surfaces match. Raw tenants hashes differ; concurrent changes were
preserved, not overwritten or rolled back. Guide/index/PDF regeneration does not
sync dist; final synchronization and served-hash verification remain with the owner.

## 按任务查找

| 任务或关键词 | 首读 | 后续定位 |
| --- | --- | --- |
| 登录、停用、NIM、可信身份、收件账本 | [接入器](zh/platform/wangshangliao.md) / [English](en/platform/wangshangliao.md) | [接入器开发](../astrbot/core/platform/sources/wangshangliao/README.md) |
| 从零开始、邀请开通、固定命令、数字菜单 | [入门](zh/platform/wangshangliao-start.md) / [English](en/platform/wangshangliao-start.md) | [命令树](zh/platform/wangshangliao-command-tree.md)、[完整命令](../astrbot/builtin_stars/wangshangliao_moderation/README.md) |
| 角色、总控、平台管理员、租户、生命周期 | [架构与权限](zh/platform/wangshangliao-architecture-permissions.md) / [English](en/platform/wangshangliao-architecture-permissions.md) | `dashboard/plugin-pages/wangshangliao/reference-tables.json` |
| 客服、管理员指引、群 @、自主接话、知识、预算、仅规则升级 | [规则与 AI](zh/platform/wangshangliao-rules-ai.md) / [English](en/platform/wangshangliao-rules-ai.md) | 架构与权限中的知识空间、模型人格、套餐费用、原生会话章节 |
| 黑白名单互斥、历史重叠、退群／改名移除、重入时长与解禁 | [规则与 AI](zh/platform/wangshangliao-rules-ai.md) / [English](en/platform/wangshangliao-rules-ai.md) | [中文命令树](zh/platform/wangshangliao-command-tree.md) / [English](en/platform/wangshangliao-command-tree.md)；互斥／移除22:59:23已加载，旧允许重叠仅作历史迁移说明；未新增真实成员操作，不用此前时长加载或三踢实测冒充本轮真实验收 |
| 关键词1图、私有素材、定时3图轮换、00／20／40三条错峰 | [规则与 AI](zh/platform/wangshangliao-rules-ai.md) / [English](en/platform/wangshangliao-rules-ai.md) | 对应入门教程与命令树；关键词仅表单配图，原`广告图片`私信命令仅录定时图 |
| 广告、刷屏、关键词、白名单、图片、视频、合并记录 | [内容规则](wangshangliao-content-rules.md) | 规则与 AI 中的仅规则开通、本群发言白名单 |
| 全员禁言、定时、取消、未知结果、不重试 | [每日计划](wangshangliao-schedules.md) | [维护说明](wangshangliao-maintenance.md) |
| 邀请归属、返现、结账、财务登记、更正 | [结账台](zh/platform/wangshangliao-cashback.md) / [English](en/platform/wangshangliao-cashback.md) | 架构与权限中的私聊财务边界；不要读取真实账本来补文档 |
| 组件声明、恢复、私聊合并、灰度 | [组件治理](zh/platform/wangshangliao-component-runtime.md) / [English](en/platform/wangshangliao-component-runtime.md) | `astrbot/core/star/components.py`、`component_runtime.py` |
| 更新、加载、重启、回退、正文保留 | [维护与发布](wangshangliao-maintenance.md) | [数据库与性能](wangshangliao-database.md)、[脚本风险](../scripts/README.wangshangliao.md) |
| 无回复、权限拒绝、unknown、日志、回执 | [日志与排障](wangshangliao-diagnostics.md) | 维护说明的无回复排查；不将 unknown 重置为 pending |
| 离线回归、实群、三账号、手机、验收缺项 | [测试与验收](zh/platform/wangshangliao-testing.md) | [隔离入群](zh/platform/wangshangliao-membership-testing.md) / [English](en/platform/wangshangliao-membership-testing.md) |
| 旧工具、架构演变、当次失败与后续补验 | [历史总入口](wangshangliao.md) | `llms-history.txt`，按主题和段落日期读取 |

## 最近部署证据的定位

2026-10-23 **22:59:23北京时间（14:59:23 UTC）名单互斥与退群／改名移除**完整重启已加载，
PID3317496→4071721，active/running、NRestarts=0；22:59:51保护快照22面／config全同，无业务配置改动。
最终Python1794 passed／2 skipped／1条audioop警告，UI177 unit／9 browser通过。
真实群成员未新增／移除／处罚，原12条needs_review保留不自动重试；未做真实满链路压测。
最新名单截图为离线UI证据，性能采样／离线基准的限定见维护最新条目；dist同步及served hash另由负责人完成。

2026-10-23 **20:02:56（北京时间）黑名单直接禁言与可设时长**完整重启已加载：
PID2602695→3317496、NRestarts=0；20:13前仅为缺权限具体提示重载插件，PID不变。
最终13文件1229 passed／2 skipped／1条audioop警告；后补提示257 passed／2 skipped，覆盖重叠不累加。
前端55项、320／390／1440px截图、vue-tsc及Vite通过。
20:10:45测试群1459282完成41760→60→41760读取／预览／确认／读回，预览不写入；
最初两次普通成员权限保护409无写入，43506527临时grant后20:11:18撤权核验、20:11:22刷新完成，
31846829始终owner、23691273未启用，无新踢人／禁言实际处罚。
三项并发修复已验证，真正unknown仍保留不重试；本轮真实新重入未测。
20:24:00保护摘要复核完成，其他保护面匹配；tenants差异仅为测试217→220及鼎峰1459534
约20:12:06／20:13:27后台保存version4／5，逐条audit链内存比较已核对，未覆盖／回滚并发修改。
原始business检查false与`only_audited_test_and_dashboard_changes=true`同时保留，详见维护最新记录。
不据此宣称原始hash全同、正式群从未变动或未来真实到期已验证；dist同步另由负责人完成。

下述16:53:53加载及三踢实测是当日**较早策略的历史证据**，不是上方新契约的部署或通过记录。
用户后续「看得见」的限定手机确认另见维护记录；下方当时的待确认口径保持原文，不证明未来到期。

2026-10-23 16:53:53（北京时间）**本群黑名单、私有图片去重与管理页更新**完成受控完整重启。
指定群1459282已核实三次成功踢出、第四次41760分钟禁言受理、当前禁言状态及移除／解禁恢复。
两条中性关键词配图及17:41至17:46六轮A→B→C→A→B→C定时图文均独立收件、先图后文。
17:47:56精确清理3张无引用本地测试图；17:51:55确认临时角色、窗口、规则及账号启停恢复，
正式群和业务正文保护指纹相符，仅保留正常测试版本／入群代数与审计递增。
发布基线7790项、前端55项通过，最终后端专项665项通过、2条件跳过，统计有重叠不累加。
29天实际到期时间和本轮手机客户端显示不冒称已通过，详见维护说明同名章节及原始失败记录。

2026-10-23 14:32:42（北京时间）**关键词配图与三条定时错峰**已插件热重载：
关键词每条1图、先图后文；定时三条默认00／20／40分，每条60分钟一次，
可分别调整分钟，仍须预览确认且不自动启用。原群规则、白名单、财务、活动、
机器人启停和主服务PID不变。629项相关后端及515项文档菜单回归、三种宽度隔离浏览器通过；
未自动配置正式群内容，未做新的真实群发送或手机客户端验收。见维护说明同名章节。

2026-10-23 **用户手机确认与图片试点清理**：用户已确认定时图片「现在正常了」，
补齐02:43至02:46新图文的手机显示确认；不改写旧空白图片或原自动化报告。
02:56:30精确清理6张无引用本地测试图，备份及必要审计保留；
测试计划、权限、角色、窗口及账号启停恢复，正式群不变，没有再次发消息或重启服务。
见维护说明同名章节，不据此宣称其他欢迎图、首次入群或活动已完成手机验收。

2026-10-23 02:40:24（北京时间）**定时图片空白气泡的包装格式修复**已插件热重载，
见维护说明同名章节。定时图片默认JSON空格不符合官方客户端精确包装识别，
改为与现有文字／固定引导图相同的紧凑格式；图片专项365、传输专项391通过。
02:41:39复核15组保护指纹与账号状态一致。此前00:47至00:50四张图片经用户截图
确认手机显示失败，不能用机器人收件回执覆盖这一结论；新补测也不自动宣称手机通过。
02:43至02:46四轮新操作的原生图文及顺序检查通过，02:46:43确认临时授权、角色、
账号启停和窗口均恢复，正式群及财务等保护摘要一致；手机显示仍待用户复核。

2026-10-23 00:53:34（北京时间）核验**定时发言先图后文与测试群补验**，
见维护说明同名章节。00:45:20插件热重载，00:47至00:50四个自然边界在测试群
`1459282`完成图片1→2→3→1，每轮图片先、文案后，均accepted并有独立原生收件。
临时规则、发送授权、原生角色、账号启停与测试窗口均恢复，主服务PID2736561不变；
测试版本与回执审计有意保留，正式群、财务、白名单和原活动数据未改。
最终离线回归7561通过、55条件跳过；没有执行抽奖或处罚，手机图片显示仍待验收。

2026-10-23 00:31:26（北京时间）核验**红包到时公布与结果排序**，
见维护说明同名章节：00:31:00插件热重载，主服务PID2736561未变。
普通红包均分、随机排序；拼手气仅按金额降序展示，
不更换成员所得金额、不新增隐蔽内定。两种红包到设定时间才公布，满员不提前；
管理员与受控测试入口也受时间限制。旧奖品抽奖行为不变。
最终180文件分三批回归7554通过、55条件跳过；单进程中断和第三批旧刷屏断言失败
均保留原证据，不改写为首轮全部通过。15组业务指纹及账号启停状态一致，
系统生成的管理员手册同步更正；隔离浏览器不代表真实群开奖或手机验收。

2026-10-22 23:21:29（北京时间）核验**三秒三条刷屏阈值热重载**，
见维护说明同名章节：23:20:41仅重载旺商聊插件，主服务PID2736561未变。
同一成员快速刷屏档位由5秒3条改为3秒3条，文字与已支持媒体均计数，
10秒8条、30秒同文5条及既有豁免不变。离线防刷屏专项164通过；
15组业务保护指纹、原在线与停用账号状态一致，未在实群触发处罚。
旧说明中的5秒3条不代表此轮加载后的快速档位。

2026-10-22 23:05:31（北京时间）核验**长规则回复与异步活动边界修复**，
见维护说明“深度审查：长规则回复与异步活动边界”：23:05:08插件热重载，
主服务PID2736561未变。修复长文单包超限、离群后迟到抽奖回执覆盖取消、
慢成员查询后发送过时定时计划，并在确认保存时清理本群已删除计划的轮换残留。
旧版复现15项中14项失败，修复后15项通过；完整相关回归7503通过、55条件跳过，
三种宽度隔离浏览器通过，15组业务数据指纹及机器人启停状态不变。
五路子代理均被上游`403 insufficient balance`拒绝，没有独立审查产出；
本轮是主代理本地审查与离线验证，不能称五路分布式复核完成或真实手机验收。

2026-10-22 20:31:53（北京时间）核验**固定红包份额、未领取金额与活动备注**，
见维护说明同名章节：20:31:00插件热重载，主服务PID2736561未变。
新活动开场固定份额，未满额不重分总金额，剩余单独记录未领取／未支出；
备注最多500字，随本期公告、状态及结果展示，头像昵称要求由管理员人工核验。
旧活动、历史金额不重算，不连接支付宝、不实际扣款退款。最终回归7488通过、
55条件跳过，三种宽度隔离浏览器通过；15组旧业务与活动字段指纹一致，
原在线与停用账号状态不变，未新建真实活动或执行付款，未做真实手机验收。

2026-10-22 19:59:37（北京时间）核验**图片上传等待恢复与管理员私信录图**，
见维护说明同名章节：19:59:23完整重启；后台读取／上传等待增加进度和超时恢复，
管理员先指定群与现存计划，再以`广告图片 计划序号`收一张、预览确认保存。
普通私信图片不自动录入，撤权／取消／切群阻断保存，不调用AI或启用计划。
完整离线回归7450通过、55条件跳过，隔离浏览器覆盖320／390／1440px及超时故障注入。
原配置、规则、群AI和发言白名单指纹不变，原两账号在线、停用账号仍停用；
真实手机客户端选图、私信附件下载与显示仍待验证，不能以隔离浏览器截图替代。

2026-10-22 18:39:18（北京时间）复核**管理员AI超时与账本关闭竞态补修**，
见维护说明同名章节：18:33:29完整重启加载；后台防刷屏扫描与账本关闭同步，
管理员DeepSeek指引显式使用非思考请求，原45秒期限、预算及无工具边界不变。
新8轮真实私信模型全部completed、13条均独立收件，两轮受控启停未再出现数据库关闭异常；
完整离线回归7404通过、55条件跳过。原5条unknown及计量记录不改，
正式群、财务、原机器人配置及临时权限恢复核验通过。
手机图片显示和真人关键词命中仍待客户端验证；此记录不覆盖它们。

2026-10-22 旧站退役及定时图片轮换的完整证据见维护说明同名章节：
旧结账域名和独立服务已移除，Caddy未发现旧路由且未重载；
结账继续在插件内使用原账本。下方16:12:07的303／410是退役前过渡阶段，
不能作为当前仍保留旧站的依据。

2026-10-22 16:12:07（北京时间）复核**结账迁入插件与常用菜单简化**，
见维护说明同名章节：结账统一到「活动与财务 → 邀请记账／结账」，
旧源站首页跳转、API停用；默认菜单为五类常用操作，旧树按需通过「完整菜单」进入。
仅热重载插件并重启旧结账跳转服务，未重启主消息服务；
账本业务记录、价格、聊天财务授权、群租户和机器人配置指纹保持原值。
新增页面与静态入口13项、文档对应140项回归通过，不能替代真实付款或手机端验收。
旧Access允许名单不授予后台总控登录，普通群管理仍使用现有权限内的固定命令。

2026-10-22 13:38:33（北京时间）**媒体误罚修复**已插件热重载：
取消按图片、表情包、视频、合并记录类型直接处罚，保留真实刷屏与文字广告规则。
详见内容规则和维护说明“媒体误罚修复与更正”；已核实解除一项误伤禁言，
95条媒体误罚退出递进计数，原始回执和其他处罚保留。
下方03:56:52媒体版本核验属于旧策略的历史加载证据，不是当前仍禁止媒体的依据。

2026-10-22 05:41:48（北京时间）核对**仅规则群升级入口的插件热重载**，
见维护说明“仅规则群升级入口”和规则与AI中的“升级正式 AI 准入”。
新增服务端预览确认，不自动升级任何群、开启AI、授予套餐或代替承担人同意；
鼎峰`1459534`当时仍为仅规则、version 3。相关回归582通过、2跳过，
编译页离线真实RPC覆盖320／390／1440px；不是完整后端重启或实群发信验收。

2026-10-22 05:02:31（北京时间）另有**插件热重载及指定群AI配置变更**，
见维护说明的“江哥群 AI 与 PC28 展示”：仅`43506527/1516387`开启@及自主接话，
`1459534`仍待正式AI准入和承担人本人确认，`1417872`仍未开通且机器人缺原生管理权限。
PC28群回复隐藏来源链接，内部核验和风险提示保留；未新增付费订单。
这是配置就绪与插件加载核对，不是实群对话／手机显示验收；不是完整后端重启。

早先一轮完整后端加载为 **2026-10-22 03:56:52（北京时间）**，
见维护说明的“2026-10-22 防误罚与取消边界”。该轮最终离线回归为
7392 通过、52 跳过、0 失败，白名单专项 566 通过；这是当轮结果，不是本次索引任务重跑。
加载后三类媒体解析及撤权、取消、群 AI 隔离修复生效，原业务配置保持原值；
配置文件有插件默认条目补登、返现库有一条启动审计，不能称所有文件或库零变化。

2026-10-20 三账号警告限定实测属于另一轮证据，不代表本轮修复后的全部实群验收。
正式群开关、账号在线状态、模型额度等都是时点事实，需另获授权只读核验，不能由文档推断实时值。
全新群首次邀请、真实新好友欢迎、新版图片手机显示、真人财务写入及真实故障演练等限制，
仍须按当前验收入口逐项核对，不因文档聚合而视为通过。

## 机器入口与按需读取

| 资源 | 用法 |
| --- | --- |
| [仓库入口 llms.txt](../llms.txt) | 仓库中的最短阅读入口；部署版提供完整分类导航 |
| [docs-index.json](../dashboard/public/local-docs/docs-index.json) | 每文档的原路径、语言、类别、主题、SHA-256、原文行数及标题行区间 |
| [llms-full.txt](../dashboard/public/local-docs/llms-full.txt) | 现行说明、运维和开发参考全文；不含独立历史文档及人格示例 |
| [llms-history.txt](../dashboard/public/local-docs/llms-history.txt) | 历史全文，单独按需读取，不能当作现行规范 |

机器目录中的 `html`、`markdown`、`aggregate` 相对目录文件自身解析；
`source` 是仓库根目录下的原路径。`headings[].line/end_line` 是原文的一基行号，
区间到下一个标题前一行，子标题另列，不将代码块内的 `#` 当作文档标题。
`markdown` 是逐字原文副本，其中相对链接仍按 `source` 的原目录解释；
部署浏览使用 `html`，不要直接按平铺副本的位置解析原文链接。

建议检索顺序：按 `topics` 和 `language` 筛选目录，再读候选文档的 `headings`，
最后读取对应原文或下载单篇 `markdown`；需要批量导入时才读取全文聚合。
引用回答保留 `source`、章节名和日期，不使用聚合文件中的行号冒充原文行号。
索引及全文是生成产物，不手改；SHA-256 只证明对应源码内容，不证明实时运行或已部署。

## 更新与安全边界

在仓库根目录执行：

```sh
node dashboard/scripts/build-wangshangliao-guide.mjs
node --test dashboard/scripts/build-wangshangliao-guide.test.mjs
```

这一个命令同时生成 HTML、逐篇 Markdown、分类入口、JSON 目录和两份全文。
文档清单及类别唯一维护于 `dashboard/scripts/build-wangshangliao-guide.mjs` 的 `guides`；
新增文档时补主题和类别，避免另建一套手工维护的全文副本。
原有六份 PDF 继续用 `scripts/build_wsl_guide_pdf.py` 同批生成。
正常前端构建会携带上述文件；仅发布文档时同步 `dashboard/public/local-docs/`
到 `dashboard/dist/local-docs/`，无需重启消息服务。

聚合只读显式列出的文档，不遍历生产 `data/`、凭据、日志、数据库或备份。
客服知识正文、联系人和奖励资料不进入本聚合，也不导入生产知识库；
知识维护仍按 `docs/knowledge/wangshangliao/README.md` 的原流程执行。
文档聚合本身不变更权限、不真实发信、不调用模型或操作财务，也不产生新的实群验收。
