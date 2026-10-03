# 旺商聊维护修复操作说明

更新：2026-10-02。[文档目录](wangshangliao.md)。操作前核对当前实例与有效配置。

## 调度与持久化

不改变登录、业务菜单、权限或前端。接入器最多同时处理 16 个会话，待调度队列最多 1,024 个会话；其余消息保留于 SQLite。每个会话按消息入库顺序逐条处理，完成后重新排队。启动、重连、消息入库、任务完成均唤醒调度，每 30 秒补查遗漏通知。空闲会话不再永久轮询。

群名片和清理任务使用内部版本校验及不可逆停止标记。停止不能撤回已经发送到上游的操作，但状态刷新和启动恢复不会复活已停止任务。未知结果不自动重试。

## 正文保留

内部收件账本的终态正文保留 30 天，按本地完成时间计算。旧记录从迁移时开始计算，非上游消息时间。仅清除正文、姓名、提及等非必要载荷，保留发送身份、消息标识、会话及撤回路由。待处理、处理中和待核查记录不清理；AstrBot 聊天历史、发件去重凭据和群管理审计记录不受影响。

清理任务每小时运行，单批最多 500 条，不运行 VACUUM。上线后默认禁用，迁移满 24 小时且管理员确认观察健康后才允许开启。释放的 SQLite 页面可重用，文件大小不一定立即下降。身份及审计元数据仍持续增长，应结合磁盘监控维护。

在项目目录运行以下只读命令查看各实例状态、磁盘空间及清理开关：

```bash
.venv/bin/python scripts/wangshangliao_maintenance.py status
```

确认过去 24 小时连接正常、积压能消退、无新的调度或数据库错误后，对当前使用的实例单独启用；`INSTANCE_HASH` 为 `data/platform_data/wangshangliao/` 下完整目录名：

```bash
.venv/bin/python scripts/wangshangliao_maintenance.py approve-retention --instance INSTANCE_HASH --healthy-observation-confirmed
```

此命令不会替管理员验证 24 小时日志；确认参数只能在实际核验后使用。过早执行会被拒绝。启用后下一轮小时任务生效。

## 发布与恢复

数据库索引、连接生命周期、离线基准与升级验收详见[数据库与性能维护](wangshangliao-database.md)。

1. 运行旺商聊全套测试及修改文件的 Ruff 检查。只用模拟上游，不执行真实群操作。
2. 保存旧代码并准备兼容回滚副本：旧 `storage.py` 的 pending INSERT 和旧 `adapter.py` 的 rejected_identity INSERT 必须明确指定原五个列名，否则新增列后旧版无法写入。
3. 用 `status` 确认没有 queued/running 群管理任务或 processing 消息。停止 AstrBot 后立即使用下面的命令生成最终一致性备份，失败时不要继续上线。

```bash
.venv/bin/python scripts/wangshangliao_maintenance.py backup --destination /ABSOLUTE/NEW/BACKUP_DIRECTORY
```

备份包含旺商聊本地会话密钥及密文、SQLite 在线备份快照与 `cmd_config.json`，目录 0700、文件 0600，并执行 SQLite 完整性检查。必须将备份视作敏感数据保护。

同时核对消息、群管、名片、活动和 `schedules.sqlite3` 都在备份清单中。
人格/知识库与其他 AstrBot 配置另按所属存储备份；不要假设接入器备份覆盖全部资料。
不打印 `session.key`、协议部署 JSON、JWT 或密封会话全文。

4. 启动新版；读取连接日志、`status` 和本地 WebUI 健康响应。观察 24 小时后才执行正文清理批准命令。
5. 回滚先停止 AstrBot，再恢复兼容旧代码，保持现有数据库不变，然后启动旧版。不要恢复整个业务数据库或删除上线后记录。已清理正文只能经单独批准从备份恢复，代码回滚不恢复正文。

上线回滚副本验证可指定环境变量后运行测试：

```bash
WSL_ROLLBACK_STORAGE=/ABSOLUTE/ROLLBACK/adapter/storage.py .venv/bin/python -m pytest -q tests/test_wangshangliao_maintenance.py
```

## 排障

结构化日志字段、权限拒绝、回复拦截和 AI 审核回退见[日志与排障](wangshangliao-diagnostics.md)。

- `dispatch/scan_failed`：检查 SQLite 可读写、磁盘与数据库完整性。队列数据仍在账本，下轮会重新扫描。
- `dispatch/processing_failed`：检查处理异常；调度会限速重试，processing 状态不能盲目改为 pending，以免重复操作。
- `maintenance/prune_failed`：正文清理失败，下小时重试；检查磁盘空间、载荷格式与数据库权限，不删除待核查记录。
- `card_version_conflict`：过期任务快照被拒绝；重新读取当前任务，不强制覆盖，也不自动重发上游请求。
- needs_review 或 outbox unknown：保留现场，只做结果核验，不通过重启触发重放。

## 文档、页面与代码更新

### 代码清理边界

- 管理动作统一调用接入器 `execute_moderation`。未被使用的 `mute_member`、
  `unmute_member`、`mute_all`、`unmute_all` 和 `announce` 便捷封装已移除；
  群命令、管理员私聊、定时任务及后台审批接口仍保留相同的权限检查和动作能力。
- 移除未挂载的旧 `WangshangliaoModeration.vue` 组件，不删除仍在使用的审批 API
  或审批数据库。当前插件管理页、平台名片组件及测试窗口不受影响。
- 移除无人引用的一次性 `enable_wsl_content_rules.py`。以后通过管理页或
  已授权的管理员配置流程调整规则；不要通过直接改生产 JSON 批量补充群授权。
- 插件页面只发布自包含 `index.html`；脚本和样式仍内嵌，构建不再留下重复
  `management.js`、`management.css`，不增加跨域资源请求。
- 旧命令别名、旧规则配置兼容、数据库迁移和幂等记录仍有用途，继续保留；
  本次清理不删除聊天记录、知识库、邀请奖励账本、处罚记录、配置或备份。

仓库根目录生成教程：

```sh
node dashboard/scripts/build-wangshangliao-guide.mjs
.venv/bin/python scripts/build_wsl_guide_pdf.py
```

PDF 需要 Playwright Chromium，可用 `WSL_GUIDE_CHROMIUM` 指向已安装 Chromium。
可执行 `.venv/bin/python scripts/check_wsl_docs.py --browser <Chromium路径>`
检查390px/1440px全部本地教程的脚本错误、图片加载与横向溢出；
`--screenshots <输出目录>` 保存代表页面截图。此检查不联系真实机器人API或修改配置。
生成目录为 `dashboard/public/local-docs`，正常 Dashboard 构建将其复制到
`dashboard/dist/local-docs`。仅发布文档时同步该目录，不只更新 Markdown；
中文 PDF 与 HTML 必须同批更新。生成器将本组文档链接转换为可用本地 HTML，
插件源码/参考表变更需另运行 `npm run build:wangshangliao-page`（在 dashboard 目录）。

插件页面由 `pages/management` 提供；活动/定时 API 由运行插件注册。
“未找到该路由”先查插件存活与后端/静态资源版本，白屏再查浏览器错误、经典单文件构建
和认证代理。不能为了加载页面添加 iframe `allow-same-origin` 或绕过后台权限。

接入器 `text.py`、核心 pipeline/鉴权或 provider 更新必须重启 AstrBot；
插件热重载不会重载已经导入的核心模块。重启使开发窗口、内存 AI 草案失效，
未知动作仍不重发，计划错过边界须复核恢复。只更新静态教程无需重启消息服务。

## 无回复排查

1. 确认消息实际发给哪个旺商聊账号，业务 UID 与显示账号/实例 ID 不混用。
2. 看接收实例入站账本；未入站查连接，`ignored_bot` 查互斥及接收端窗口。
3. 看私聊/当前群回复是否已保存、群是否启用、群 AI 是否真实 @ 当前机器人。
4. 精确命令查有效 `admins_id` 与确定性路由；普通咨询查模型、人格/工具、知识库耗时。
5. 管理执行查动作授权、完整目录和平台角色；输出查可见正文、推理标签过滤及 outbox。
6. 只查询操作结果，不伪造入站、不把 unknown 重置 pending，不重放历史消息。

完整复现矩阵见[当前验收](zh/platform/wangshangliao-testing.md)，
旧记录中的固定部署路径、临时权限及旧行为不用于本次操作。
