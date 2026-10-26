# 旺商聊数据库与性能维护

更新：2026-10-20。本文记录当前实现、升级边界和可重复验收步骤。
配合[维护与发布](wangshangliao-maintenance.md)、[日志与排障](wangshangliao-diagnostics.md)使用。

## 数据放在哪里

业务数据存放于`data/platform_data/wangshangliao/<64位目录名>/`。
合并模式由受保护的`account-roots.json`把账号绑定到已有目录，实例重建可复用本账号绑定；
初次绑定沿用实例ID的SHA-256目录名。不能仅按当前实例ID重新计算后删除“旧目录”，
也不能靠改名合并其他账号。绑定索引不是可清理缓存。
会话凭证`Vault`仍按实例ID摘要目录密封，不随账号业务目录一起重绑定；
同账号重建实例后，会话目录与账本目录可能不同，备份必须覆盖两者。
当前绑定校验检查格式但不检查不同账号的目录值唯一性，新账号初绑仍取实例ID摘要。
删除旧实例后将同一ID用于另一账号可能指向旧目录；不要复用已退役ID给其他账号，
不能仅凭64位目录名断言账号隔离已成立。这是待修代码风险，本轮没有改动索引或数据。
策略库仍会拒绝旧实例的账号不匹配；该保护不等于目录索引已经验证唯一性，
本轮也没有证明生产存在目录重合。

| 文件 | 主要数据 | 不能随意删除的原因 |
| --- | --- | --- |
| `messages.sqlite3` | 入站消息、发送意图和回执、同步游标、回复撤回任务、正文保留开关 | 维持去重、未知结果隔离、排名来源及撤回路由 |
| `moderation.sqlite3` | 管理动作、审批、违规审核、人工踢出确认、自动处罚与递进记录 | 保存操作幂等、确认是否已消费和处罚证据 |
| `activities.sqlite3` | 抽奖设置、活动、报名、邀请核验、奖励账本和成员状态快照 | 保证不重复报名、不重复奖励、退群统计与活动状态一致 |
| `schedules.sqlite3` | 全员禁言计划、确认、执行和规则变更记录 | 防止重复执行、错过边界后盲目补发 |
| `cards.sqlite3` | 名片任务、稳定生成名、已处理成员 | 防止重复改名，保留未知任务供核查 |
| `friend_approval.sqlite3` | 逐账号好友同意设置、读取退避、未知审批和欢迎观察 | 保留冷却及异常暂停，不能删除后自动重试 |
| `private_interaction.sqlite3` | 首次私聊／好友欢迎认领、私聊咨询有效期、按群开通代次的引导认领 | 未确认投递仍保留认领，删除会使首次发送保护失效 |
| `speech_activity.sqlite3` | 无正文的今日／昨日有效消息计数来源及增量游标 | 北京时间零点换日并清理早于昨日数据，不能用清空游标实现长期回补 |
| `speech_report.json` | 可选的专门发言汇报名单与群范围，不存句数或聊天正文 | 绑定当前机器人账号和固定UID；不跨账号共享，不随统计换日清理配置 |

专门发言汇报通过已登录总控的只读扩展入口`GET speech-report?bot_id=...`读取，
每次异步查询都在前后核对机器人及所有指定群的启用范围。只汇总有效文字；统计内容和保留期不扩大。
`speech_report.json`由运维在当前账号绑定的业务目录维护，结构为
`{"account":"机器人UID","groups":[{"id":"群UID","name":"显示名"}],"members":[{"id":"成员UID","name":"显示名"}]}`。
最多20群、30人，拒绝重复／无效UID、控制字符、符号链接及超过16KiB的配置；
身份须先用真实群目录核对，不通过相似昵称猜人。未配置不展示专门汇报，任一群停用会阻断完整汇报，
不能静默缩小统计范围。未读完的计数显示补齐状态，不作为完整汇总复制，不向群自动发出。

表按功能首次使用逐步创建；没有某张表不一定是故障。账号与群通常还有表内联合键，
目录隔离不能代替业务身份和授权校验。人格、知识库和 AstrBot 对话历史不属于这些库。

集中业务数据在`data/plugin_data/`，不是可随源码清理的缓存：

| 相对路径 | 职责 |
| --- | --- |
| `wangshangliao_open/open-service.sqlite3` | 合并进程的群租户、授权、开通／离群清理代次、群AI及扣费人、逐域知识、额度、预算及审计；名称含open不代表独立进程 |
| `wangshangliao_moderation/settings.sqlite3` | 实例业务策略；不以旧策略覆盖公共逐群规则 |
| `wangshangliao_moderation/group_management.sqlite3` | 兼容授权存储；当前公共授权以租户库为准，不通过清空兼容库重置权限 |
| `wangshangliao_cashback/cashback.sqlite3` | 金额、真实邀请归属、资格、付款及财务审计；不自动删除或重算 |

代码清理只移除确认无引用的实现，不能删除上述库、未知回执或原管理归属标记。
`model.json`、`access.json`等受保护侧文件也参与模型／采集账号绑定，
不能只备份SQLite。不要打印这些文件或会话密文以证明备份有效。

### 2026-10-20 隔离实测与死代码清理

三账号在`1459282`的3次真实警告使用两个独立`test-advertising/<唯一标记>/moderation.sqlite3`
计数账本，分别1次和2次，不是同轮完整三级升级。正常警告、处罚及自动禁言计数前后未变，
正式群与财务保护摘要一致；原生收发、撤回回执及最小审计仍写入原账本，
不能把“计数隔离”说成“所有生产库零写入”。规则、临时角色恢复且窗口关闭，
详见[测试与验收](zh/platform/wangshangliao-testing.md)。
本轮源码仅删除`cashback_store.py`未使用的私有旧头像自动定价迁移函数
`_normalize_automatic_avatar_pricing`，不执行该迁移、不清表、不清理索引或账本，
不改历史财务行；文档与源码整理不重载／重启服务，也不重新发送测试消息。

### 离群清理与保留的区别

插件暂停、网络断开、目录失败不触发离群删除。确认当前账号离开指定群后，
`lifecycle.py`可清除本群工作正文、活动报名及群策略等非财务数据，取消待执行任务，
并保留未知／已受理动作证据和返现账本；不能把30天正文保留规则理解成离群也保留全部正文。
清理涉及多个数据库，逐库检查代次并提交，完成后才标记清理完成，不是跨库原子事务。
途中失败可能留下部分已清理状态，应保留离群暂停和清理进度供既有流程续办，
不能手工标记完成、清库或恢复旧账本。原生撤回观察另有90天有界清理，
超限／过大入站拒绝记录在一小时后清理，不受正文保留批准开关控制。

## 已有索引与连接优化

采用新增索引和及时释放连接，不删除历史记录，不更改业务列定义，不重算奖励。
消息账本继续使用 `WAL` 和 `synchronous=FULL`，保留入站先落盘再确认、
发件先记录意图再请求上游的时序。其他数据库没有统一切换日志模式。

| 数据库 | 新索引 | 加速范围 |
| --- | --- | --- |
| 消息 | `inbox_ranking_window(account,team,received_at)` | 按机器人账号、群和接收时间范围读取排名候选消息 |
| 消息 | `inbox_conversation(account,team)` | 按群反向读取指定消息之前的有限上下文，消除临时排序 |
| 活动 | `lottery_group_history(account,group_id)` | 按 rowid 查询最近抽奖期数 |
| 活动 | `lottery_group_created(account,group_id,created DESC)` | 最近创建的活动 |
| 活动 | `lottery_pending_tick(account,ends)` | 仅索引 `announcing/open/drawing`，减少后台扫描已结束活动 |
| 活动 | `invite_pending_scan(account,group_id,checked_at,first_seen,member)` | 仅索引待核验邀请，按既有顺序选取最多 200 人 |
| 活动 | `invite_credit_totals(account,group_id,inviter,amount)` | 邀请人数、奖励合计的覆盖索引 |
| 活动 | `invite_credit_history(account,group_id,recorded DESC)` | 最近邀请奖励记录 |
| 活动 | `invite_credit_presence(account,group_id,status,left_at,member)` | 已记账邀请的在群／退群状态汇总 |
| 定时 | `schedule_execution_history(account,group_id,planned DESC)` | 最近执行结果 |
| 定时 | `schedule_rule_history(account,group_id,id DESC)` | 最近规则变更 |
| 审核 | `semantic_group_created(account,group_id,created DESC)` | 最近 AI 审核记录 |
| 审核 | `content_group_observed(account,group_id,observed DESC)` | 最近违规记录、排名排除的违规消息 |

共 13 种索引定义，按对应数据库和已存在的表创建，不代表每个机器人一定生成 13 个索引。
初始化使用 `CREATE INDEX IF NOT EXISTS`；重复启动不重复建立同名索引。
旧消息库先完成原有 `received_at` 等列的兼容迁移，再创建新消息索引。
审核索引在对应审核路径初始化时补齐；尚未使用的功能不会凭空产生业务记录。

处罚、审批、固定命令及规则处理中的短连接，改为事务结束后明确关闭。
正常返回仍提交，异常仍回滚；记录操作意图的提交仍发生在网络调用之前。
没有把多个请求合用一条写连接，也没有跨请求合并事务。
下面的结账台只读复用是后续独立优化，不适用于这些写事务。

## 结账台读取优化（2026-10-14）

`cashback_store.py`的邀请人分页将筛选后的分组汇总复用于总额和外层分页，
不再为这两部分分别扫描账目。仍最多返回五个邀请人，每人一百条成员记录；
全量汇总、成员分页、名称更新时间相同时的顺序和空页回退不变。
空搜索不执行名字匹配；没有匹配账目时跳过明细、审计和风险查询。
已登记付款的历史记录仍不依赖当前邀请证明才能显示。
风险黑名单在同一读取快照内查一次，不对每条汇总重复查。

新增部分索引`evidence_cashier_confirmation`：
`evidence(group_id,member,inviter,member_peer,inviter_peer)`，
仅覆盖`source='cashier_confirmation'`，加快人工归属证据查询。
索引不会重算金额、更改归属或结账状态；旧索引及可信来源核验仍保留。

独立`StandaloneCashier`启用只读连接复用；其他采集、写入路径默认不启用。
每个空闲连接只归一个请求使用，最多保留四个空闲连接；多余连接用完即关闭。
这不是账目、用户权限或查询结果缓存：每次仍读取当前身份、群范围和数据，
`list_entries`仍通过`BEGIN`取得一致快照，用完结束事务，下一次看到已提交更新。
只读连接同时使用SQLite`mode=ro`和`query_only=ON`；写事务仍使用独立新连接、
`BEGIN IMMEDIATE`与原写入保护。服务退出关闭空闲连接，在途连接结束后关闭。
不降低`WAL`／`synchronous=FULL`的持久化保护，也不放宽财务鉴权。

相关回归覆盖空结果鉴权、旧付款显示、一次分组汇总、索引重复升级、
并发连接独占、撤权后重新查询、并发写入快照、只读禁止写入及退出释放。
真实账本快照的旧版／新版状态、时间、搜索和分页组合只读比对结果相同。
测量工具`check_wsl_open_load.py`改为使用和结账服务相同的只读复用路径，
仍测四种状态、同一账本和同样的二十群／每秒一百条混合负载，记录`reader_reuse`。
保留SQL优化前、仅SQL优化及连接复用后的全部样本；单轮波动不代表稳定验收。
该工具测的是账本读取基准，不包括Cloudflare登录、网络和浏览器渲染，
也不等于二十个真实群的完整处罚管线。当前结果见
[当前验收](zh/platform/wangshangliao-acceptance-20261015.md)。
2026-10-15完整接收／规则链路负载中账本P95从11.665ms升至17.846ms，
增幅52.99%，仍超过10%目标。旧单轮改善、SQL副本剖析和连接复用通过
不等于严格在线混合负载指标通过；本次资料清理不改SQL或降低账务保护。

### 哪些规则没有改变

- 排名仍按北京时间当天统计，并保留原消息时间校验、30 秒发言间隔和 5 分钟重复内容过滤。
- 排名仍排除命令、托管机器人和已记录的违规消息；10 秒缓存及 10 万条扫描上限保留。
- 审核上下文仍限定同账号、同群、当前消息之前，并由原逻辑检查时间、文本预算和来源。
- 抽奖的参与唯一性、邀请奖励唯一键、待审核不计奖、旧账不重算均不变。
- 邀请奖励仍是一次性记账；成员离群只更新状态快照，不扣回积分。状态从完整成员目录
  刷新后开始可信，不能补录此前离群或两次同步之间短暂进出的人。
- 权限、确认、5/15/60 分钟递进禁言，以及禁止AI执行管理动作／自动踢群的边界不变。
- `unknown` 和 `needs_review` 不会因性能优化变成成功或可自动重试。

## 离线基准及限制

在项目目录执行：

```bash
.venv/bin/python scripts/benchmark_wsl_database.py --rows 100000 --repeats 7
```

脚本只使用自动清理的临时数据库和合成消息，不导入机器人运行时，
不读取真实聊天记录、不连接机器人、不修改生产库。允许 1000–500000 条数据、
1–50 次重复；每个阶段先预热，再取查询耗时中位数，并核对结果一致。

2026-10-03 本机样本，SQLite 3.53.1、10 万条历史数据：

| 查询 | 优化前中位数 | 优化后中位数 | 返回数据 |
| --- | --- | --- | --- |
| 排名候选范围 | 24.778ms | 0.473ms | 相同的 1000 条 |
| 审核上下文 | 10.013ms | 0.027ms | 顺序相同的 20 条 |

这些是**合成数据、热缓存、纯 SQL 查询**结果，不是线上 SLA，也不是机器人完整
回复耗时。排名文本归一化、成员查询、AI 模型、网络、写锁竞争和冷缓存均未纳入。
小数据量收益可能不明显；新增索引也会占用磁盘并增加写入维护成本。

执行计划应包含 `inbox_ranking_window` 或 `inbox_conversation`；
上下文查询不再出现 `USE TEMP B-TREE FOR ORDER BY`。不同 SQLite 版本的计划
文字可能略有差异，以索引使用和结果正确为准，不把耗时阈值写死为测试条件。

## 安全升级

1. 先检查消息积压、群管后台任务和活动状态，避开正在执行的操作与临近开奖／定时边界。
2. 保存兼容旧代码，协调停止AstrBot与独立结账服务的业务写入，使用完整旺商聊备份创建新目录。
3. 检查备份完整性与可用磁盘，再部署新代码；索引建立可能短暂持有写锁。
4. 启动服务。核对实例在线、消息处理恢复、目标索引和关键功能查询，不用真实处罚做性能压测。
5. 对比备份前后配置的**解析后值**；JSON 排序或格式重写不等于业务参数变化。
   有真实差异时先调查，不用整份旧配置覆盖其他并发修改。

本机 systemd 示例；其他部署方式替换为对应停止和启动命令：

```bash
systemctl stop astrbot.service
systemctl stop wangshangliao-cashier.service
PYTHONPATH=. .venv/bin/python scripts/wsl_policy_migrate.py backup \
  --data /opt/astrbot-prod/data --output /ABSOLUTE/NEW/BACKUP_DIRECTORY
# Confirm backup success before deployment and startup.
systemctl start astrbot.service
systemctl start wangshangliao-cashier.service
.venv/bin/python scripts/wangshangliao_maintenance.py status
```

备份使用 SQLite backup API 并校验完整性。不要在服务运行时仅复制主 `.sqlite3` 文件
而遗漏 WAL；也不要手动删除正在使用的 `-wal`／`-shm` 文件。
备份包含会话密钥及密文，应保持目录 0700、文件 0600，不公开分享。
这里的`wsl_policy_migrate.py backup`包含主配置、原生目录及
`plugin_data/wangshangliao_{moderation,open,cashback}`中的相关库和侧文件，
生成SHA-256清单。旧`wangshangliao_maintenance.py backup`仅覆盖原生目录与主配置，
不包含集中租户、策略及返现库，不能单独作为合并部署完整备份。
两者都不覆盖整个AstrBot数据：全局人格、知识库、对话库及分配置需另行备份。
逐库SQLite backup不是跨库原子快照；只停AstrBot而让结账服务继续写入也不足以保证一致性。

本次没有执行 `VACUUM`、清空历史、重建处罚计数、切换 `synchronous=OFF`、
提高扫描上限或自动开启正文清理。完整性检查不放进每条消息的处理链；审核路径
首次使用仍可能创建缺失索引，大库宜在维护窗口预建。后续 `IF NOT EXISTS`
只检查同名索引是否已存在，不会每条消息重建索引。

## 验收与回滚

```bash
.venv/bin/python -m pytest -q tests/test_wangshangliao_database_performance.py
.venv/bin/python -m pytest -q tests/test_wangshangliao_*.py
```

覆盖旧消息库升级、重复初始化、范围查询计划、活动与计划索引、奖励合计与唯一性、
审批成功／拒绝后的连接关闭。完整套件继续验证权限、确认、处罚、抽奖和消息去重。

纯索引优化回滚可恢复兼容旧代码并保留新增索引，不必删除业务表或恢复整库。
如另有数据损坏，应先停止服务并保全现场，再单独制定恢复方案；直接覆盖备份会丢失
备份之后的新消息和账目。旧版本消息插入语句仍必须满足原有迁移兼容要求，见维护文档。

### 仍需关注

- 大群当日有效消息很多时，排名仍有 Python 文本处理成本，并非索引后无需扫描当天消息。
- 名片任务仍使用原有 JSON 存储；本次没有改为独立明细表，也没有改变任务恢复方式。
- 部分数据库路径仍为同步短查询；不要据此宣称整个 AstrBot 已完成异步数据库改造。
- `settings.py:save`仍同步等待SQLite写锁，最长受10秒`busy_timeout`约束；
  单条SQL短不代表锁等待短。线程化的`AsyncDatabase`仅覆盖实际使用它的路径。
- 出现 `database is locked` 时检查长事务、任务并发和磁盘问题，不盲目重试已经提交的动作。
- 不从日志打印消息正文、群员名片、账号凭据或完整配置来证明优化效果。
