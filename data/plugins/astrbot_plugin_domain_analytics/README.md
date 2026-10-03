# 域名访问人数等监控机器人

`@dahai985_bot` 的 Telegram 接收和发送由 AstrBot 专用平台 `域名访问人数等监控机器人` 管理。Node 服务 `/opt/domain-analytics` 保留业务权限、域名/浏览器/目标站采集、Fragment、PostgreSQL 和日报调度。现有工作统计机器人 `tgwatch` 不参与本插件的消息处理。

## 专属配置

AstrBot 配置文件名称为“域名访问人数等监控机器人专属配置”，按平台路由 `域名访问人数等监控机器人::` 绑定全部私聊和群会话。AI 提供商、语音识别、语音合成、通用命令及 AI 定时工具关闭，插件白名单仅包含 `astrbot_plugin_domain_analytics`。消息由本插件直接处理，业务权限由原统计数据库校验，AstrBot 管理员身份不会自动获得统计查询权限。

插件设置位于 `data/config/astrbot_plugin_domain_analytics_config.json`：`enabled` 为启用开关，`platform_id` 须与机器人平台 ID 一致，`api_url` 为 `http://127.0.0.1:18373`，`api_token` 与后端 `ASTRBOT_BRIDGE_TOKEN` 配对。不要在文档、截图或共享配置中公开密钥。

平台独立开启 `telegram_dedicated_reporting` 和 `telegram_require_analytics_plugin`，关闭 `telegram_command_register` 与 `telegram_command_auto_refresh`，菜单由业务服务通过插件注册。原 Telegram 用户名保持 `@dahai985_bot`。

后端专属配置：`TG_TRANSPORT=astrbot` 固定生产收发方式；`FRAGMENT_ENABLED`、`FRAGMENT_NOTIFY` 控制 Fragment 功能，沿用部署中的原值。报表时区为北京时间，时间仍由后端控制。群监控的 Telethon 会话、统计数据库及采集容器是当前系统的必要组件，不能按旧机器人删除。

## 旧版本清理

旧入口已可恢复归档，根目录 server.mjs/ui.mjs 位于 `/opt/domain-analytics/backups/astrbot-complete-20260921/`。当前生产强制 AstrBot 收发，旧轮询已移除，不能仅改环境变量回滚；必须恢复旧应用代码并撤销 Compose 的强制收发配置。迁移备份与数据库快照保留。详见 `/opt/domain-analytics/CONVERSION.md`。

## 运行结构

- AstrBot 插件保存待转交更新，通过本机鉴权接口提交原始 Telegram 更新；后端按更新 ID 去重。
- 业务命令保持原有实现，数据库中的 owner、members、invitations、bot_admins 不迁移或重建。
- 后端将 Telegram 请求写入 `astrbot_outbox`；插件领取后通过绑定平台发送，并保存回执，收到后端确认后清除本地回执。
- 日报分段请求使用报表/收件人/段落的稳定 ID，Fragment 通知使用收件人和事件 ID。成功请求复用结果；发送中断或网络结果不确定不自动重发。
- 后端处理中退出的命令标记 `uncertain`，不自动重复业务变更。管理员需核查这些记录。
- 内部接口单独监听容器 8082，仅映射 `127.0.0.1:18373`；不会通过原有 8080 采集服务或 Tunnel 暴露。所有接口要求 Bearer 密钥。
- 适配器等待插件注册后才开始接收；插件卸载会停止该适配器轮询，重新加载后恢复。

## 功能范围

沿用 `/today`、`/yesterday`、`/month`、`/domain`、`/top`、`/site`、`/report`、`/live`、`/status`、管理及 Fragment 命令、邀请和原有按钮。当前生产 `/report` 发送文字报告，迁移不恢复旧文档中已取消的 CSV 行为；收发接口保留文件发送能力。

每日北京时间 00:03 推送、00:10 复核仍由后端原调度器控制。AstrBot 离线不影响采集；未发送任务可重试，发送结果不确定的日报收件人 `next_attempt` 为 infinity，必须先核实，不能直接清空发送状态。

## 检查

检查 `astrbot.service`、`domain-analytics-app-1` 和 `http://127.0.0.1:18372/healthz`。带插件配置中的鉴权访问内部 `GET /status` 可查看入站和出站任务数量，不返回消息正文或密钥。数据库 `astrbot_updates` / `astrbot_outbox` 的 uncertain 状态表示需要核查；不能把它当成已成功。

## 回滚

备份：`/opt/domain-analytics/backups/astrbot-migration-20260921`，包含迁移前代码、受限配置和数据库备份。不要恢复整个旧数据库，否则会丢失迁移后的采集数据。

1. 先检查并处理 pending/sending/uncertain 任务，确认交接期间不会重复发送。
2. 在维护窗口先恢复归档的旧 Node 应用，并移除 Compose 固定的 `TG_TRANSPORT: astrbot`；再执行 `/opt/astrbot-prod/.venv/bin/python /opt/domain-analytics/scripts/astrbot-migration.py rollback`。当前新代码会拒绝直接执行该回滚，防止误停机器人。
3. 执行 `systemctl restart astrbot.service`，确认该平台停止轮询。
4. 在 `/opt/domain-analytics` 执行 `docker compose up -d --no-deps app`，恢复旧轮询。

严禁让两个进程同时对同一个 Token 调用 getUpdates。默认保留全部迁移审计记录，后续可按实际数据保留要求清理已完成任务。

## 验证

新增桥接集成测试覆盖接口鉴权、更新去重、原子领取、幂等回执、明确失败重试、不确定结果及重启；业务流程测试使用专用测试 PostgreSQL 和模拟发送，覆盖授权、邀请、按钮编辑和并发输出隔离。插件测试模拟 Telegram 成功、限流和网络异常，不向真实用户发送测试消息。
