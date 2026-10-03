# 旺商聊数据库与性能维护

更新：2026-10-03。本文记录当前实现、升级边界和可重复验收步骤。
配合[维护与发布](wangshangliao-maintenance.md)、[日志与排障](wangshangliao-diagnostics.md)使用。

## 数据放在哪里

每个机器人独立存放于 `data/platform_data/wangshangliao/<实例ID的SHA-256>/`。
目录名不是用户 ID，也不是群号；更改机器人实例 ID 不会自动合并旧目录。

| 文件 | 主要数据 | 不能随意删除的原因 |
| --- | --- | --- |
| `messages.sqlite3` | 入站消息、发送意图和回执、同步游标、回复撤回任务、正文保留开关 | 维持去重、未知结果隔离、排名来源及撤回路由 |
| `moderation.sqlite3` | 管理动作、审批、违规审核、人工踢出确认、自动处罚与递进记录 | 保存操作幂等、确认是否已消费和处罚证据 |
| `activities.sqlite3` | 抽奖设置、活动、报名、邀请核验和奖励账本 | 保证不重复报名、不重复奖励及活动状态一致 |
| `schedules.sqlite3` | 全员禁言计划、确认、执行和规则变更记录 | 防止重复执行、错过边界后盲目补发 |
| `cards.sqlite3` | 名片任务、稳定生成名、已处理成员 | 防止重复改名，保留未知任务供核查 |

表按功能首次使用逐步创建；没有某张表不一定是故障。账号与群通常还有表内联合键，
目录隔离不能代替业务身份和授权校验。人格、知识库和 AstrBot 对话历史不属于这些库。

## 本轮优化

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
| 定时 | `schedule_execution_history(account,group_id,planned DESC)` | 最近执行结果 |
| 定时 | `schedule_rule_history(account,group_id,id DESC)` | 最近规则变更 |
| 审核 | `semantic_group_created(account,group_id,created DESC)` | 最近 AI 审核记录 |
| 审核 | `content_group_observed(account,group_id,observed DESC)` | 最近违规记录、排名排除的违规消息 |

共 12 种索引定义，按对应数据库和已存在的表创建，不代表每个机器人一定生成 12 个索引。
初始化使用 `CREATE INDEX IF NOT EXISTS`；重复启动不重复建立同名索引。
旧消息库先完成原有 `received_at` 等列的兼容迁移，再创建新消息索引。
审核索引在对应审核路径初始化时补齐；尚未使用的功能不会凭空产生业务记录。

处罚、审批、固定命令及规则处理中的短连接，改为事务结束后明确关闭。
正常返回仍提交，异常仍回滚；记录操作意图的提交仍发生在网络调用之前。
没有把多个请求合用一条写连接，没有跨请求合并事务，也没有引入新的连接池。

### 哪些规则没有改变

- 排名仍按北京时间当天统计，并保留原消息时间校验、30 秒发言间隔和 5 分钟重复内容过滤。
- 排名仍排除命令、托管机器人和已记录的违规消息；10 秒缓存及 10 万条扫描上限保留。
- 审核上下文仍限定同账号、同群、当前消息之前，并由原逻辑检查时间、文本预算和来源。
- 抽奖的参与唯一性、邀请奖励唯一键、待审核不计奖、旧账不重算均不变。
- 权限、确认、5/15/60 分钟递进禁言，以及当前禁止 AI／自动踢群的设置不变。
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
2. 停止服务，保存兼容旧代码，使用维护工具创建一个全新的备份目录。
3. 检查备份完整性与可用磁盘，再部署新代码；索引建立可能短暂持有写锁。
4. 启动服务。核对实例在线、消息处理恢复、目标索引和关键功能查询，不用真实处罚做性能压测。
5. 对比备份前后配置的**解析后值**；JSON 排序或格式重写不等于业务参数变化。
   有真实差异时先调查，不用整份旧配置覆盖其他并发修改。

本机 systemd 示例；其他部署方式替换为对应停止和启动命令：

```bash
systemctl stop astrbot.service
.venv/bin/python scripts/wangshangliao_maintenance.py backup --destination /ABSOLUTE/NEW/BACKUP_DIRECTORY
# Confirm backup success before deployment and startup.
systemctl start astrbot.service
.venv/bin/python scripts/wangshangliao_maintenance.py status
```

备份使用 SQLite backup API 并校验完整性。不要在服务运行时仅复制主 `.sqlite3` 文件
而遗漏 WAL；也不要手动删除正在使用的 `-wal`／`-shm` 文件。
备份包含会话密钥及密文，应保持目录 0700、文件 0600，不公开分享。
维护工具备份旺商聊目录与主配置；人格、知识库、分配置文件需另行备份。

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
- 出现 `database is locked` 时检查长事务、任务并发和磁盘问题，不盲目重试已经提交的动作。
- 不从日志打印消息正文、群员名片、账号凭据或完整配置来证明优化效果。
