# 数据清理与数据库维护

核对日期：2026-10-03。仅供维护人员使用，不导入玩家帮助或客服知识库。

## 1. 清理边界

“清理测试和历史垃圾”不是清空数据库，也不等于删除测试群。测试群中的真实点击可能已经产生积分、派发、广告或处罚；这些记录与正式业务一样保留。不能按群名包含“测试”、备注包含“test”或用户昵称批量删除。

**本工具不删除：**

- 群绑定、平台／逐群配置、授权、密钥、Telethon 会话。
- 群钱包、积分账本、奖励结算记录、下注、开奖证据、游戏桌、个人投入、派发、退款。
- 广告订单、充值、付款单、链上交易认领、地址版本、扫描进度和退款证据。
- 审计、处罚、待核查记录及正在执行的任务。
- 消息受理主键、按钮操作记录、会话顺序标记、删除任务和其他防重放记录。
- 积分逐群迁移前的只读归档，包括旧 `chat_seen`；禁止移除冻结触发器。

确需取消测试订单或移除测试积分，必须单独列出精确订单／流水及金额，走业务取消、退款或有审计的调整流程，不能直接删账本或归零钱包。当前工具没有这类功能。

## 2. 本工具的白名单

| 数据 | 清理条件 | 处理 |
|---|---|---|
| `callbacks`、`dialogs` | 已过期 | 删除临时凭证／输入会话；不影响永久受理主键 |
| `group_chat_seen` | 北京时间日期早于当前日期减7天 | 删除聊天正文去重摘要；保留奖励流水和每日奖励累计 |
| `ak_seen` | 超过7天 | 删除短期广告行为缓存；处罚日志不动 |
| `cm_tickets` | 过期超过24小时 | 删除临时操作凭证；案件和举报审计不动 |
| `keno_poll_runs` | 超过7天 | 删除采集性能诊断；开奖证据不动 |
| `gt_delivery_timing` | 超过30天 | 删除投递性能诊断；消息收据与删除任务不动 |
| `game_panels.rendered` | 面板 `done`、创建超过7天，且同群同消息ID的删除任务已 `deleted/archived` | 只清空冗余渲染正文；保留面板ID、来源、状态和操作记录 |

每个表每批最多500条，一次执行最多20批；超过上限的剩余数量会显示，可再次预览后执行。预览只读，实际执行逐批提交；中途失败时已提交批次保留，重跑不会重放业务。当前新增工具按需人工运行，不自动调度。

### 已有后台保留规则仍生效

- 反馈超过30天且所有相关撤回均确认完成，迁移到 `gt_archive`，原主键保留为防重放标记；这不是彻底删除，归档仍占空间。
- 广告疑似脱敏正文7天；已关闭案件及可清理处罚正文30天，必要事实和审计保留。未完成核查不按普通缓存删除。
- 广告样本90天到期停用，保留撤销／到期事实，不能通过旧按钮重新启用。
- 加拿大28采集诊断由后台同时限制为最多5000条、最长7天。
- 未知发送、删除失败、已冻结订单不自动改成成功，不盲目重发。

## 3. 安全操作步骤

先选择**本实例**数据库，检查没有在途付费提交、开奖投递或广告置顶操作。备份代码和配置；数据库必须使用SQLite备份API，不能仅复制在线 `.sqlite3` 而漏掉WAL。

以下命令从项目根目录执行；将 `/absolute/path/...` 替换为核实的真实路径，备份文件必须尚不存在。命令无需Telegram登录，不发送测试消息、不调用模型、不付款。

```bash
.venv/bin/python data/plugins/astrbot_plugin_superbot/maintenance.py status /absolute/path/superbot.sqlite3
.venv/bin/python data/plugins/astrbot_plugin_superbot/maintenance.py cleanup /absolute/path/superbot.sqlite3
.venv/bin/python data/plugins/astrbot_plugin_superbot/maintenance.py cleanup /absolute/path/superbot.sqlite3 --apply --destination /absolute/path/new-cleanup-backup.sqlite3
.venv/bin/python data/plugins/astrbot_plugin_superbot/maintenance.py optimize /absolute/path/superbot.sqlite3 --apply --destination /absolute/path/new-optimize-backup.sqlite3
.venv/bin/python data/plugins/astrbot_plugin_superbot/maintenance.py verify /absolute/path/superbot.sqlite3
```

- 不带 `--apply` 的 `cleanup/optimize` 均只读；`optimize` 预览仅显示空间与完整性，不模拟优化收益。
- 带 `--apply` 必须提供新的 `--destination`；先完成并验证一致性备份，才允许写入。备份文件权限0600。
- 返回 `affected` 为本次实际处理数量，`remaining` 为固定截止时间下仍符合条件的数量；面板项是清空正文条数，不是删除订单数。
- 工具拒绝缺少Superbot核心表的数据库；每个实际改动批次与 `transient_cleanup` 审计同事务提交，包含各表数量，不记录正文、密钥或收款信息。
- `verify`检查完整性、群钱包与账本对账；上线还应对比配置、订单、资金流水和未结束任务。

## 4. 性能优化做了什么

新增索引随对应模块增量初始化，重复加载不会重复建索引：

| 索引 | 用途 |
|---|---|
| `notices_pending(status) WHERE status='pending'` | 只索引待发送通知，避免大量历史成功记录降低索引选择性 |
| `dialogs_expiry(expires)` | 过期输入会话清理 |
| `group_chat_seen_day(day)` | 逐群聊天摘要保留期清理 |
| `ak_seen_age(at)` | 广告行为缓存到期清理 |
| `cm_ticket_expiry(expires)` | 群管理临时凭证到期清理 |
| `gb_cleanup_due(status,next)` | 旧公告精确ID删除队列 |
| `game_panels_retention(status,created)` | 已完成面板正文清理 |

已有下注按群历史、订单状态、反馈投递及删除队列索引保留。使用 `EXPLAIN QUERY PLAN` 验证索引命中，而不是给所有字段盲目加索引；索引也会增加空间和写入成本。

`optimize`执行有限量统计信息优化和 `wal_checkpoint(PASSIVE)`；不修改事务可靠性，不关闭外键，不降低同步级别，不添加共享SQLite写线程。checkpoint结果为 `[busy, WAL帧数, 已回写帧数]`，读事务可能使部分帧暂时不能回写，不因此删除WAL文件。

删除行不保证主文件立即变小：空闲页会供后续写入复用；PASSIVE checkpoint也不保证WAL文件立即缩短。工具**不自动VACUUM**。仅在空闲页比例明显、确有磁盘压力且插件可安全停用时，另行安排压缩；不得在开奖或付款执行中强制锁库。

## 5. 测试、故障和恢复

离线测试覆盖只读预览、截止边界、保留当前缓存、旧归档冻结、批量上限、事务回滚、面板待核查不清理、主键保留、无备份拒绝写入及索引命中。完整Superbot与Telegram适配器回归通过后，再做插件级热加载。

- `database is locked`：停止本轮维护，检查长事务和正在运行的任务；不强杀业务进程，也不无限重试。
- 完整性或钱包对账异常：停止清理，保留快照和证据，先排查账务。
- 没有符合条件记录：处理数量为0是正常结果，不能为了缩小文件放宽边界。
- 清理误操作：先停维护工具，对照备份按表／精确主键核查恢复；线上已产生新流水时，禁止整库覆盖。临时凭证一般不恢复，避免复活旧按钮。
- 索引／代码回归：恢复兼容代码并只重载插件，保留已完成业务和新增字段；索引无需随代码回滚删除。

维护报告应区分“删除缓存”“清空展示正文”“更新查询统计”“新增索引”和“物理回收空间”，不得把测试通过或文件大小变化说成真实请求延迟保证。

## 6. 2026-10-03 实际维护记录

- 删除3条超过保留期限的群聊天去重摘要；清空2条已确认撤回的旧面板渲染正文。面板主键及删除记录均保留，最终预览无剩余符合条件记录。
- 核对19个群钱包、833条群积分流水、233条加拿大28订单、50条快三订单及2条广告订单，维护前后内容一致；旧共享钱包／账本、角色和群登记也未改变。设置中的采集健康、链上扫描游标，以及群主核验时间由正常后台任务更新，不是本工具重置配置。
- 钱包对账差异0，外键问题0，SQLite完整性 `ok`。配置文件未修改。
- 7项新增索引已安装。通知查询由 `SCAN notices` 改为 `SEARCH notices USING INDEX notices_pending`；同一查询、同一通知数据的一次SQLite虚拟机指令计数由150降至11，**不等于端到端延迟下降同等比例**。
- 最终完整回归1147项通过、58条现有依赖弃用警告；Ruff通过。
- 最终主文件1258页、每页4096字节、空闲页0；本轮未做VACUUM。索引和统计会增加少量空间，不宣称回收了磁盘容量。PASSIVE checkpoint结果 `[0,640,640]`。
- 代码、配置、一致性数据库快照保存在维护目录 `/root/Projects/agents/telethon-ai-deployment/database-maintenance-20261003/`；清理前数据快照为 `before-cleanup.sqlite3`。代码回退亦可参考此前清理备份；不得用这些快照覆盖上线后资金流水。
- 本轮仅调用插件重载接口。另在12:14:22 UTC观察到整服务停止／启动，触发来源未确定，不计为本轮主动操作；最终服务为active，插件加载及Telegram轮询正常。
