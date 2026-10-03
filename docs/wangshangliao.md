# 旺商聊文档目录

更新：2026-10-03。以当前仓库实现为准；历史验收报告不代替当前使用说明。

## 使用与配置

| 文档 | 内容 |
| --- | --- |
| [从零开始使用](zh/platform/wangshangliao-start.md) | 登录、按名称选择管理员、群授权、聊天操作与常见问题 |
| [接入器说明](zh/platform/wangshangliao.md) | 部署、配置、身份与会话、主动发送、能力边界 |
| [插件说明](../astrbot/builtin_stars/wangshangliao_moderation/README.md) | 全部命令、权限表、排名、抽奖、邀请奖励、三路 AI |
| [内容审核与处罚](wangshangliao-content-rules.md) | 独立审核、规则兜底、5/15/60 分钟递进禁言、人工踢出 |
| [每日禁言计划](wangshangliao-schedules.md) | 预览确认、未来边界、暂停恢复、故障处理 |
| [维护与发布](wangshangliao-maintenance.md) | 账本、备份、正文保留、更新、回滚与排障 |
| [日志与排障](wangshangliao-diagnostics.md) | AI 路由、权限拒绝、不回复原因、回执语义和脱敏边界 |
| [数据库与性能维护](wangshangliao-database.md) | 存储分工、索引、连接释放、实测基准、安全升级与回滚 |
| [测试与验收](zh/platform/wangshangliao-testing.md) | 四种身份场景、测试窗口方向、证据要求及未完成项目 |
| [接入器开发说明](../astrbot/core/platform/sources/wangshangliao/README.md) | 模块责任、接口与安全约束、开发回归入口 |
| [滑块组件说明](../astrbot/core/platform/sources/wangshangliao/_solver_vendor/README.md) | 内置运行时来源、依赖、人工回退与敏感数据边界 |
| [English adapter guide](en/platform/wangshangliao.md) | Deployment, identity, routing and capability boundaries |
| [English beginner guide](en/platform/wangshangliao-start.md) | Dashboard setup and chat workflows |

## 去哪里设置

| 设置 | 入口与范围 |
| --- | --- |
| 登录、启用群、回复、主动发送、测试窗口、批量成员任务 | 机器人页；每个实例独立 |
| 聊天管理员 | 有效配置文件的 `admins_id`；可从已有私聊按名称选择，不按昵称授权 |
| 群客服模型 | 插件页 `customer_provider_id`；保存为 `ai_routes.groups[业务群ID]` 模型ID字符串 |
| 管理员群管模型 | 插件页；实例 `ai_routes.admin_provider_id` |
| 违规审核模型 | 插件页；实例 `moderation.semantic.provider_id` |
| 人格与知识库 | AstrBot 会话/配置文件；独立审核不继承 |
| 下期奖品、人数、倒计时、门槛、统一邀请积分 | 插件页活动控制，或管理员私聊固定命令/自然语言预览确认 |
| 开奖、取消、启停邀请奖励 | 授权管理员私聊；保存活动参数不自动开始活动 |
| 每日计划 | 管理员私聊预览确认；插件页可查看与暂停，不直接创建或恢复 |
| 自动规范群名片 | 插件页群开关，或管理员私聊自然语言预览确认；逐群设置，开启另需已有 `rename` 授权 |
| 指定成员群名片 | 管理员私聊先查成员、生成预览、另发确认；只改群内名片，不改账号昵称 |

所有聊天命令均可不带斜杠或“群管”前缀。普通群员的公开命令限群内排名、
抽奖报名/状态和本人邀请积分；普通私聊固定命令返回“无权限”，普通咨询可走客服。
聊天管理员、后台账号、上游群角色、动作白名单、主动发送目的地授权是不同权限。

## 当前行为速查

| 场景 | 预期行为与边界 |
| --- | --- |
| 普通群员发送 `排名`、`参加抽奖`、`我的邀请` | 走群内公共命令，不要求管理员；群启用、活动状态等仍需满足 |
| 普通用户私聊 `你是谁` | 私聊客服开关及当前消息门禁允许时进入客服，不获得群管工具 |
| 普通用户私聊 `我的权限`、`我的邀请` | 当前私聊固定命令仅向管理员开放，返回“无权限”；不能据此认定客服故障 |
| 管理员在群里 @ 机器人 | 仍是该群客服，不进入管理员自然语言配置路由 |
| 管理员私聊修改规则、活动参数或自动名片开关 | 先预览目标及前后值，再由同一私聊确认；不靠模型判断管理员身份 |
| 当前接收机器人的自动违规处理 | 撤回＋5/15/60 分钟递进禁言，后续最高60分钟；`manual_kick_only` 禁止 AI 和自动规则踢人 |
| 抽奖开场、状态、报名反馈及中奖名单 | 抽奖类消息保留；不是所有机器人群回复都在20秒后撤回 |

自然语言管理还需可用模型、插件和人格工具白名单允许 `wsl_private_management`。
勾选技能不授予管理员身份，也不替代群动作授权。历史文档中的第四次自动踢出
属于兼容分支，不能用于推断当前接收机器人的启用状态。详细判断与结果含义
见[插件说明](../astrbot/builtin_stars/wangshangliao_moderation/README.md)和[日志与排障](wangshangliao-diagnostics.md)。

## 阅读与发布

机器人页“使用教程 · PDF”打开本地入门教程。`dashboard/scripts/build-wangshangliao-guide.mjs`
生成离线 HTML；`scripts/build_wsl_guide_pdf.py` 生成中文入门 PDF。
只更新 Markdown 不会自动更新已部署 HTML/PDF，发布流程见维护说明。

历史资料：[早期测试记录](zh/platform/wangshangliao-testing-history.md)、
[2026-09-22 审查记录](wangshangliao-moderation-review-20260922.md)。
旧命令、权限范围、实现缺口和部署路径仅代表当时状态。
