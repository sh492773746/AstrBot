# 旺商聊文档目录

[AI 阅读与主题索引](wangshangliao-ai-index.md)是统一的按需阅读入口；
提供文档优先级、任务映射及自动生成的 `llms.txt`、`docs-index.json` 和分类全文。
索引整理日期：2026-10-23（北京时间）；不重跑原验收、不改写下方各轮历史日期。
黑名单新契约为每次可信真实重新入群直接禁言不踢人，默认29天41760分钟，可按机器人＋群设置
1..41760整分钟并预览本人确认，私信先选群；只影响后续重入，不重设已有禁言、不追罚，
加入名单不立即处罚。北京时间2026-10-23 20:02:56完整重启已加载，在线配置流程验证通过；
本轮真实新重入／禁言未测。20:24:00保护摘要已复核，其他保护面匹配，测试版本与已审计后台并发修改保留，
不宣称tenants原始整体hash全同或正式群从未变动。重新生成教程／索引／PDF但不向dist同步，
最终同步及served hash由负责人完成。
旧三踢及用户「看得见」的限定手机显示确认见[维护说明](wangshangliao-maintenance.md)，不证明未来真实到期。
2026-10-22早先完整后端加载与7392／52离线回归见维护说明的
“2026-10-22 防误罚与取消边界”；下方2026-10-20“未加载”仅描述当时清理轮次。

- [组件治理试点：统一声明、故障恢复、私聊合并与灰度边界](zh/platform/wangshangliao-component-runtime.md)
- [Component governance and pilot (English)](en/platform/wangshangliao-component-runtime.md)

- [规则与AI分权：配置归属、群内／私信命令、关键词、广告、PC28](zh/platform/wangshangliao-rules-ai.md)
- [Rules and AI ownership (English)](en/platform/wangshangliao-rules-ai.md)

同步日期：2026-10-20（北京时间）。当前为现有 AstrBot 账号的**公共合并模式**，不运行独立公共机器人进程。
本文是维护入口；历史报告保留当时证据，不能替代当前权限和使用说明。

## 当前使用

| 文档 | 用途 |
| --- | --- |
| [从零开始](zh/platform/wangshangliao-start.md) / [English](en/platform/wangshangliao-start.md) | 登录、开通、群内和私聊使用、常见问题 |
| [命令树](zh/platform/wangshangliao-command-tree.md) / [English](en/platform/wangshangliao-command-tree.md) | 菜单逐层回复数字，`0`返回，「取消」退出 |
| [插件命令索引](../astrbot/builtin_stars/wangshangliao_moderation/README.md) | 完整固定命令、参数范围、模块入口 |
| [邀请返现结账台](zh/platform/wangshangliao-cashback.md) / [English](en/platform/wangshangliao-cashback.md) | 独立财务身份、筛选、登记、更正和撤回 |
| [内容规则](wangshangliao-content-rules.md) / [每日禁言计划](wangshangliao-schedules.md) | 撤回、递进禁言、定时边界及结果不明处理 |

群内和私聊使用相同固定命令，`管理 `前缀可省略。群内绑定当前群；私聊只选择本人获授权的群。
真实邀请者必须是当前平台群主／管理员，由本人发`开通`；之后还须把机器人设为群管理员。
菜单不会授予权限，AI不会代替用户操作。
私聊只读咨询保持原行为；群内@、自主接话和冷场统一使用本账号／本群的群聊配置及覆盖，不走私聊咨询人格。
群内共享人格，但@历史按核验成员独立、有界带入，不跨成员、群、账号或私聊，不迁移或复用旧群客服历史；
`mentions`独立于`autonomous`，自主候选缓存不变。群输出只发纯文本，不加`【群聊AI】`或`【客服咨询】`前缀。

用户授权「加载核实」后，上述群@配置分流、成员历史隔离及群输出去前缀已于2026-10-20
09:47:09 UTC（北京时间17:47:09）随`astrbot.service`完整重启加载，非插件重载；新管理页标签也已发布。
该加载阶段未另发测试消息或调用模型，不等于实群验收；随后完成的三账号警告实测见下文，
不把警告验收扩大为群AI或手机显示验收。

### 2026-10-20 三账号隔离警告实测

随后在测试群`1459282`复用群主`31846829`、接收机器人`43506527`和测试发送者`23691273`
三条已有在线连接，使用`test_advertising`隔离计数完成3次真实警告。两个独立fixture分别为1次及2次，
不是同一fixture的完整第1／2／3次升级。原生`userNick`的`@大蛤`及目标身份通过核对，
不以`groupMemberNick=testfork`覆盖该昵称；违规原消息撤回与警告送达、撤回均有对端原生证据。
三次警告发送受理至对端收到撤回通知分别为12.112／12.115／12.125秒，不承诺硬实时。
规则和接收机器人的临时原生管理员角色已恢复为原普通成员，窗口已关闭；
财务、正式群及正常处罚计数未变。实测0模型调用、无额外NIM连接、未重启服务，
不涵盖禁言升级、全功能或手机画面。原生回执及最小审计保留，并非所有生产库零写入。
证据：`/opt/backups/wsl-warning-live-20261020-I5Z5qf/result.json`；
详细范围及首次核验脚本只读恢复记录见[测试与验收](zh/platform/wangshangliao-testing.md)。

## 架构与权限

| 文档／源文件 | 用途 |
| --- | --- |
| [当前架构与权限](zh/platform/wangshangliao-architecture-permissions.md) / [English](en/platform/wangshangliao-architecture-permissions.md) | 62个插件模块、31个原生接入模块、七类角色、生命周期和客服／管理员指引权限分流 |
| `dashboard/plugin-pages/wangshangliao/reference-tables.json` | 管理页面架构表／权限表的双语数据源，回归核对解析器覆盖 |
| [接入器说明](zh/platform/wangshangliao.md) / [English](en/platform/wangshangliao.md) | 登录、传输、可信身份和平台能力 |
| [接入器开发说明](../astrbot/core/platform/sources/wangshangliao/README.md) | 底层模块责任、接口与安全约束 |
| [架构收敛记录](zh/platform/wangshangliao-refactor.md) / [English](en/platform/wangshangliao-refactor.md) | 历次重构与当前合并模式的边界，不是旧模式启用教程 |

## 运维与开发

| 文档 | 用途 |
| --- | --- |
| [维护与发布](wangshangliao-maintenance.md) | 备份、更新、回滚、正文保留及服务保护 |
| [数据库与性能](wangshangliao-database.md) | 存储分工、连接、索引与性能验证 |
| [日志与排障](wangshangliao-diagnostics.md) | 脱敏日志、权限拒绝、回执语义 |
| [脚本分类与风险](../scripts/README.wangshangliao.md) | 只读检查、离线压测、真实测试、收费调用及历史迁移工具 |
| [滑块组件](../astrbot/core/platform/sources/wangshangliao/_solver_vendor/README.md) | 内置运行时、人工回退与敏感数据边界 |

项目边界：`astrbot/core/platform/sources/wangshangliao/`负责接入；
`astrbot/builtin_stars/wangshangliao_moderation/`负责业务；
`dashboard/plugin-pages/wangshangliao/`负责页面；`scripts/`保存显式调用的维护及测试工具；
`tests/test_wangshangliao_*.py`负责回归。不要把测试操作接入公共命令或AI。

## 当前验收与历史

| 文档 | 性质 |
| --- | --- |
| [测试方法与门槛](zh/platform/wangshangliao-testing.md) | 当前验收入口，区分离线、真实消息、平台回读和手机观察 |
| [2026-10-15 验收](zh/platform/wangshangliao-acceptance-20261015.md) / [English](en/platform/wangshangliao-acceptance-20261015.md) | 历史真实功能证据及后续追加记录，不能单独代表今天的状态 |
| [2026-10-15 问题补齐](zh/platform/wangshangliao-remediation-20261015.md) / [English](en/platform/wangshangliao-remediation-20261015.md) | 十五项修复、旧入口停用、财务只读核对 |
| [隔离入群测试](zh/platform/wangshangliao-membership-testing.md) / [English](en/platform/wangshangliao-membership-testing.md) | 固定测试对象、真实成员核验和防夺权 |
| [2026-10-14 报告](zh/platform/wangshangliao-acceptance-20261014.md) | 历史加固与真实测试，不表示今天全部通过 |
| [2026-10-08 报告](zh/platform/wangshangliao-acceptance-20261008.md) | 历史生产验收 |
| [早期测试记录](zh/platform/wangshangliao-testing-history.md) | 历史命令与旧实现缺口，不照旧恢复权限 |
| [2026-09-22 审查](wangshangliao-moderation-review-20260922.md) | 历史规则分析 |

2026-10-20加载后只读核对：大蛤账号`43506527`在线；群`1516387`的群AI配置为version 5，
`enabled=false`、`mentions=false`、`autonomous=false`、`idle=false`，本次加载前后配置未变。
早先群AI开启快照及同日15:47:56（北京时间）的自主回复受理／原生对话记录保留为历史，
不代表当前群AI开启，也不证明新版本实群或手机显示已验收。
当前DeepSeek模型／凭证绑定的预算门禁已经确认；供应商返回有限额度元数据，
尚未通过实际耗尽额度验证拒绝边界。更换模型、凭证或价格仍须重新核验，不自动解除门禁。

仍有缺项：全新群首次邀请开通、真实新好友欢迎和新版原生图片手机显示、
Access真人财务写入及真实故障演练。测试群归零不冒充全新群，隔离测试付款不是真实付款。
旧10%相对延迟门槛已由用户于2026-10-17取消，保留失败历史，后续报告绝对P95。
本轮文档扫描不修改账本、群开关、账号会话或AI预算，也不重新执行真实测试。

## 文档发布与清理约定

2026-10-20本轮清理三份生产代码净减66行：合并原生回复调用与关键词校验，
删除未使用私有旧头像定价迁移函数；无迁移执行、数据库清理或历史财务行修改。
新增110项回归，完整相关离线6564通过、52条件跳过、3 warnings，237.65秒，
证据`/opt/backups/wsl-cleanup-20261020-IsoIYB/full-regression.xml`。
修改文件Ruff及管理页Vue严格未使用项检查通过；全仓仍有未触及的一条I001和66份格式差异。
此轮只整理源码并重建发布文档，不重载／重启、不发消息，不宣称新后端已加载。
以下四份脚本删除等说明属于此前轮次，不是本轮再次执行。

- 修改功能时同步中英文使用说明、架构权限文档及页面JSON；权限以服务端校验为准。
- `dashboard/scripts/build-wangshangliao-guide.mjs`生成离线HTML，
  `scripts/build_wsl_guide_pdf.py`生成中文PDF；只改Markdown不会更新已部署教程。
- 按日期命名的验收证据不删除、不改写成新结果；旧截图只作历史参考。
- 旧独立进程入口和旧AI管理入口保留**拒绝执行的兼容壳**，不保留可用写入后门。
- 已删除一次性合并器、旧账号名片测试及仅互相引用的旧部署检查，共四份脚本；
  仍被回归引用的修复工具保留并标注风险。
- 财务、凭据、持久状态、验收证据和其他平台文件不纳入本次清理。
