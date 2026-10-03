# 旺商聊日志与排障

接入器与插件共用 AstrBot 日志管线，不额外建立一套日志服务，也不修改现有
日志级别、保留周期或轮转设置。三路 AI、固定命令、名片任务、活动和定时任务
使用统一前缀 `Wangshangliao`。

## 查看入口

- WebUI：**数据与日志 → 日志**，查找 `Wangshangliao`，再按 `instance=` 缩小机器人范围。
- 默认主日志：`data/logs/astrbot.log`；若已自定义日志路径，以设置为准。
- 本机 systemd 部署可运行 `journalctl -u astrbot.service --since '10 minutes ago' --no-pager`，
  再筛选 `Wangshangliao`。日志级别高于 INFO 时，不会显示正常路由和权限拒绝记录。
- 多个群与机器人共用进程，不要只凭时间相近判断同一次操作，应结合关联字段。

## 字段说明

| 字段 | 含义 |
| --- | --- |
| `instance` | 机器人实例标识，不是用户昵称 |
| `stage` / `outcome` | 处理阶段及真实返回状态 |
| `correlation` | 消息或任务标识的 SHA-256 前 16 位；同一原始消息可关联路由、审核和处罚 |
| `route` | `admin_private`、`customer_private`、`customer_group` 或 `moderation`；模型选择日志使用配置字段标签 |
| `action` | 服务端固定操作名，不记录用户输入的自由文本 |
| `group` / `actor` / `session` | 群、用户、会话标识的哈希；缺失为 `-` |
| `error` | 白名单错误类别，不是异常原文 |
| `duration_ms` | 模型审核或管理工具耗时；未测量为 `-` |
| `count` | 本条汇总的事件数量，正常记录为 1 |

后台名片任务以任务 ID 为关联值；新确认消息与预览消息有不同的消息关联值。
因此 `correlation` 不是跨全部会话、全部发送分片的全局调用链 ID。

## 常见问题定位

| 日志组合 | 意义与处理 |
| --- | --- |
| `ai_route / customer_only` | 普通私聊或群内客服，没有群管工具；群内管理员也走客服路线 |
| `ai_route / management_enabled` | 当前管理员私聊已具备管理工具；不代表已经执行操作 |
| `ai_route / management_unavailable` | 管理员私聊没有拿到工具，检查人格工具白名单与插件加载 |
| `ai_route / management_history_isolated` | 管理历史已从客服会话隔离，不是故障 |
| `model_route / configured` | 采用该路配置的独立模型 |
| `model_route / session_default` | 未单独配置，沿用会话模型 |
| `model_route / session_fallback` | 指定模型不可用，保留会话模型；检查模型配置 |
| `reply_gate / reply_disabled` | 当前私聊或目标群的实际回复开关关闭 |
| `reply_gate / not_eligible` | 当前消息不具备普通回复条件 |
| `reply_gate / managed_account_blocked` | 本机托管机器人互聊保护，需使用有效且对应方向的测试窗口 |
| `reply_gate / test_window_rejected` | 测试窗口不允许当前回复，核对有效期、次数、来源及范围 |
| `reply_gate / empty_reply` | 回复为空或仅含不发送的引用组件 |
| `dispatch_gate / stale_or_unknown_time` | 历史或时间不明消息被忽略，避免重放命令 |
| `command / silent` | 命令按既有约定静默处理，例如群内发送私聊专用命令 |
| `admin_tool / rejected` | 权限、参数或确认校验未通过，不是成功写入 |
| `error=confirmation_invalid` | 确认过期、不属于本会话或不是精确确认，重新预览 |
| `error=configuration_conflict` | 预览后配置已变化，重新预览，不能强制覆盖 |
| `semantic / rule_fallback` | 审核失败，继续既有规则兜底；类别区分模型超时、响应无效、模型缺失或其他运行失败 |
| `progressive_result` | 撤回和禁言分别记录回执，不能因一个成功认定两个都成功 |
| `card_item` / `card_job` | 成员写入结果及后台任务状态，不包含原名或新名 |
| `schedule / paused` | 计划已暂停，核对权限、时间边界及实际结果后处理 |
| `lottery_tick / blocked` | 抽奖后台处理异常，先查活动状态，不要重复创建 |
| `invitation_scan / rejected` | 邀请核验失败；`permission_denied` 表示活动管理员权限已撤回 |

## 状态不能混淆

- `preview`：仅生成预览，未保存或执行。
- `saved`：配置已保存，不代表活动已开始；抽奖启动以 `started` 及活动状态为准。
- `schedule_confirmation_required`：仍需完成定时规则的确认。
- `accepted`：上游受理，不是最终状态核验完成。
- `verified`：已核验；名片和其他异步操作应以任务查询结果为准。
- `unknown`、`not_confirmed`、`needs_review`：保留现场并查询结果，**不要重发管理操作**。
- `command_reply / send_returned`：回复调用已返回，不保证实际送达；看 `reply_result` 和接入器发送账本。

正常路由与预期权限拒绝记为 INFO；异常、失败回退、未知回执记为 WARNING。
接入器原有需要人工介入的终止类错误仍为 ERROR。
重复后台错误在 60 秒内聚合，缓存最多保留 512 个失败窗口；窗口淘汰或后续
失败触发过期清理时补记被抑制的数量。没有后续事件或进程退出时，不保证输出
最终汇总；日志不是精确的持久计费或处罚计数账本。管理工具返回和逐次处罚回执不聚合。

## 隐私边界

这些结构化日志不记录聊天正文、AI 提示词、模型回复全文、工具参数全文、
确认口令、成员名片、账号密码、Token 或 Cookie；自由异常文本不直接写入。
哈希用于排障关联，不是匿名化证明，不应公开提供可关联用户的完整日志。
AstrBot 其他模块、第三方插件或 DEBUG/trace 日志可能另有正文记录，
**不能据此认为整个日志文件均已脱敏**。对外提交前仍需筛选和人工检查。

日志只负责观察，不会改变管理员身份、群授权、模型选择、处罚次数或开启自动踢群。
本次回归使用本地隔离数据，不代替真实用户的四种身份会话验收。
