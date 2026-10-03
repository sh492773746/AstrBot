# Telegram 群聊工作统计

独立采集账号 → SQLite → 本地鉴权 API → AstrBot 插件 → 工作群榜单及管理员私聊。

采集服务独立于 AstrBot；不调用大模型，只按管理员明确请求加入指定群，不自动替员工发言。第一版支持 1 个账号、最多 30 个启用群和 50 个启用人员。机器人通过 BotFather 创建，并配置到 AstrBot 的 Telegram 平台。

## 1. 安装和登录

需要 Python 3.10+ 和 uv。在本目录运行：

```sh
uv sync
cp config.example.json config.json
chmod 600 config.json
```

在 `config.json` 填入从 https://my.telegram.org 获得的 `api_id` 和 `api_hash`。下面的命令直接将随机 API 密钥写入配置，不打印密钥：

```sh
uv run python - <<'PY'
import json
import secrets
from pathlib import Path
path = Path('config.json')
config = json.loads(path.read_text())
config['api_token'] = secrets.token_urlsafe(32)
path.write_text(json.dumps(config, indent=2) + '\n')
path.chmod(0o600)
PY
```

本机登录并运行：

```sh
uv run tg-collector --config config.json login
uv run tg-collector --config config.json run
```

登录只在本地终端输入手机号、验证码和两步验证密码。`var/account.session` 是持久登录会话，勿发送给机器人或上传到仓库。登录命令与采集进程不能同时占用此会话；重新登录前先停服务。服务账号未授权时 API 保持可用，状态显示 `login_required`，不会在后台索要验证码。

API 默认监听 `127.0.0.1:6190`。数据路径相对于配置文件定位，不依赖工作目录。必要时配置 `proxy`：

```json
{"proxy_type": "socks5", "addr": "127.0.0.1", "port": 1080, "rdns": true}
```

以上对象填入 `config.json` 的 `proxy` 字段。采集器的代理与 AstrBot 机器人代理分别配置；不需要代理则保持 `null`。

## 2. AstrBot 插件

本次已安装到相邻 AstrBot 的 `data/plugins/astrbot_plugin_tgwatch`。可移植 ZIP 位于本项目 `dist/astrbot_plugin_tgwatch.zip`，也可以将解压后的插件目录复制到其他 AstrBot 的 `data/plugins/`。

在 AstrBot 重新加载插件后，填写配置：

| 配置 | 内容 |
| --- | --- |
| `enabled` | 配置完成后设为 `true` |
| `platform_id` | AstrBot 中现有 Telegram 平台的 ID，不是机器人用户名 |
| `work_chat_id` | 接收榜单的工作群数字 ID，通常为 `-100...` |
| `admin_ids` | 管理员 Telegram 数字 ID 列表 |
| `api_url` | `http://127.0.0.1:6190` |
| `api_token` | 与采集服务配置完全相同的密钥 |

将机器人加入工作群并给予发消息权限。管理员先私聊机器人点击 Start，再发送 `/tgwatch`。不需要启动第二个机器人进程，复用现有 AstrBot Telegram 适配器；当前定制版本已提供专用汇报机器人隔离开关。插件默认关闭，空配置不会连接或发送消息。

管理命令：

```text
/tgwatch dialogs                       选择采集账号已加入的群
/tgwatch groups                        按钮启停已配置群
/tgwatch                              打开菜单，点“开始监控”选择群
@username 员工乙                     在机器人提示后输入（10 分钟内有效）
/cancel                              取消当前输入
/tgwatch pause                       暂停全部监控
/tgwatch resume                      恢复全部监控
/tgwatch person name 123456 新名字      重命名，不改变采集起点
/tgwatch person disable 123456          停用
/tgwatch person enable 123456           重新启用
/tgwatch people                        人员列表及启停按钮
/tgwatch records 2026-09-20             日期筛选
/tgwatch records 2026-09-20 123456       指定人员
/tgwatch records 2026-09-20 123456 -1001000000002
/tgwatch status                        采集进度与待核查发送
```

只有白名单管理员可以在私聊中管理和查询；每次按钮操作重新鉴权。人员按群配置：选择群后输入用户名，只在所选群监控该人员，同一个人可以配置到多个群。首次启用从当时起算；暂停期间不计数，恢复也不补算暂停期间。

### 按钮流程与暂停

私聊 `/tgwatch` → **开始监控** → 从采集账号已加入的群中选择 → 输入 `@用户名` 或数字 ID（可追加显示名）。配置成功后显示该群监控人员，支持继续添加或单独暂停/恢复。输入等待 10 分钟超时；`/cancel`、返回菜单或插件重载取消尚未完成的输入。

- **暂停本群人员**：只暂停这一人在这一群的监控，其他群不受影响。
- **暂停群**：该群所有人员暂停；恢复后保留每人的原有开关。
- **暂停全部**：持久记录总暂停区间，不删除记录或改变各群、各人员开关。恢复后不补算暂停期间。历史查询、榜单发送与账号健康检查继续运行。
- `/tgwatch person disable ID` / `enable ID` 仍支持对某人员的全部群暂停/恢复；`name` 改名作用于该人员全部群。旧 `person add` 入口会提示改用选群流程，避免意外跨群监控。
- **运行状态**：展示账号在线/登录失效/限流、总开关、群和监控人数、今日条数、最近消息、最后同步、历史缺口和报表结果。监控开关打开不代表账号网络一定正常，应一起查看账号状态。

升级至 0.2.0 时会一次性把既有全局名单迁移为既有群的关联；现有历史和启停区间保留。新增加的群不会自动继承旧人员，需明确添加。

## 3. 统计和运行语义

- 北京时间每个整点发上一完整小时的榜单，并显示该自然日累计及参与群数。00:00 的小时榜属于前一天的 23:00—24:00。
- 每日 01:30 发昨日 00:00—24:00 总榜。首次启动或恢复时只补最近一份已到发送时间的日榜；历史小时榜不补发。当次小时榜仅在整点后 5 分钟内重试。
- 文字、贴纸、媒体、转发等普通消息每个消息 ID 计一次，相册逐条计；编辑不增加条数，服务通知、匿名/频道身份不计入个人。
- 不下载媒体，仅保存类型、配文和回复关联 ID。原文保持原样，机器人使用安全转义的 MarkdownV2 展示，避免原文被解释为格式指令。
- 原文保存 90 天，定时清除；非正文的消息事实长期保留，支持历史统计和去重，不需要永久保留正文。无原文的旧记录显示“原文已过保留期”。统计按长期事实查询汇总，并非另存一份容易失配的日汇总表。
- 只有带明确群 ID 的删除事件才处理。删除后隐藏原文，但保留已观察到的发言次数。Telegram 不保证完整删除通知，断线时已删除的内容也可能无法恢复。
- 补采使用独立历史游标，实时消息不会提前推进它。恢复后补采仍可访问的消息；历史缺口保留，即使补采完成，受影响报表仍提示可能不完整。
- 数据库错误/API 不可用不视为零消息，不生成伪零榜。群同步超 180 秒、登录失效和限流等状态可查。
- 报表先记录发送意图，再调用 Bot API。明确拒绝可重试；超时、连接中断或发送中进程退出标为 `uncertain`，不自动重发。多段榜单逐段记录进度。管理员需到工作群核对，不提供自动补发不确定报表的按钮。
- 不主动发送异常、恢复或违规通知；管理员通过 `/tgwatch status` 查询，采集不完整在榜单中标注。
- AstrBot 停止不影响采集服务。Mac 休眠、断网时不能实时采集；恢复后尽力补采。

## 4. 内部 API

所有端点都需要 `Authorization: Bearer <api_token>`。默认仅本机监听；不将接口直接暴露公网。

| 方法 | 路径 | 参数或请求体 |
| --- | --- | --- |
| GET | `/v1/status` | 账号状态、总开关、群/人员/关联、有效监控人数、各群今日条数、最近消息、同步时间及缺口 |
| GET / PUT | `/v1/control` | 查询状态，或提交 `paused` 布尔值暂停/恢复全部 |
| GET / PUT | `/v1/watches` | GET 可按 `group` 筛选；PUT 提交 `group`、`person` 数字 ID 和 `enabled` 布尔值 |
| GET | `/v1/members?group=群ID&page=0` | 已配置群的可访问成员，每页最多 10 位；受 Telegram 权限限制 |
| GET | `/v1/dialogs` | 采集账号可访问且已加入的群 |
| GET | `/v1/resolve?user=...` | 数字 ID 或可解析的用户名 |
| GET | `/v1/groups`、`/v1/people` | 已配置名单 |
| PUT | `/v1/groups`、`/v1/people` | `id`、`enabled`（布尔）、可选 `name`；启用群时检查已加入 |
| GET | `/v1/stats` | `start`、`end`、`cumulative_start`：带时区 ISO 时间 |
| GET | `/v1/messages` | `start`、`end`、`person`、`group`、`page`；0 表示全部，页码从 0 起 |

时间窗口左闭右开，响应时间统一 UTC ISO 格式。统计窗口最大 366 天；累计起点最多早于排名起点 24 小时。消息每页最多 10 条，返回 `has_next`。查询带 `health` 字段，包括 `incomplete`。

设置群内监控前须先配置群和人员；仅添加人员到 `/people` 不会自动开始监控，需通过 `/watches` 建立群关联。

鉴权失败 401、参数错误 400、上游/存储故障 503；错误响应不包含 Telegram RPC 原文或密钥。数据库由采集服务独占管理，AstrBot 只访问 API。

## 5. macOS 常驻与备份

`deploy/local.telegram-chat-collector.plist.example` 是 launchd 模板。把其中所有 `/ABSOLUTE/PATH/telegram-chat-collector` 替换为本项目绝对路径（含空格时 XML 文本无需 shell 转义），保存到 `~/Library/LaunchAgents/local.telegram-chat-collector.plist`。先手动完成登录，再运行：

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.telegram-chat-collector.plist
launchctl kickstart gui/$(id -u)/local.telegram-chat-collector
```

停用常驻服务：

```sh
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/local.telegram-chat-collector.plist
```

launchd 在用户登录后运行并自动重启；不要同时再启动手工采集进程。模板通过虚拟环境 Python 启动，不依赖 launchd 的 PATH。运行日志在 `var/`，可在 Console.app 查看；轮转和备份见下方定期维护说明。

在线一致性备份：

```sh
uv run tg-collector --config config.json backup /absolute/path/activity-backup.sqlite3
```

备份目标必须不存在。命令使用 SQLite backup API，不直接复制 WAL 数据库。恢复时停止采集服务，将备份恢复到 `var/activity.sqlite3`，移除旧数据库对应的 WAL/SHM 文件后再启动。登录会话需要另行保护性备份或重新登录；AstrBot 自己的数据也需备份以保留插件的报表发送进度。

## 6. 验证

```sh
uv run pytest -q
uv run ruff format .
uv run ruff check .
```

AstrBot 插件测试在 AstrBot 项目目录运行：

```sh
.venv/bin/python -m pytest data/plugins/astrbot_plugin_tgwatch/tests -q
```

这些测试使用模拟 Telegram 客户端、真实临时 SQLite 和本地 HTTP API，不证明实际 Telegram 账号网络可用。真实验收步骤：在可控测试群启用一名人员，发送文字、贴纸/图片、编辑和删除消息，核对原始条数；暂停人员后发消息，再启用确认暂停期间不计；停止 AstrBot 后继续发消息确认采集不停；等待整点和 01:30 核对榜单，并用非管理员验证查询被拒绝。

### 按管理员请求入群

`PUT /v1/join` 接收 `{"link": "https://t.me/群用户名"}`，也支持邀请链接。仅由鉴权客户端显式调用；返回 `joined`、`already_joined` 或 `pending`。不会自动配置群与监控人员；未确认结果不会自动重试。日志不记录邀请链接。原先只允许选择已加入群的限制扩展为允许管理员通过机器人私聊请求采集账号加入指定群。

### 链接规则接口

`GET/PUT /v1/link_rules` 查询或替换某人员规则；PUT 字段为 `person`、`patterns`（正则数组）、`enabled`。`GET /v1/violations?person=0&before=游标` 按当前规则查询违规链接（省略游标从最新存储记录开始），返回最多 10 条、下一批游标、规则数及完整性状态。扫描上限 200 条，原文保留期同为 90 天。匹配完整 URL，超时返回 `regex_timeout`，不产生自动告警。

## 首次安装、定期备份与恢复

首次配置可用 `uv run tg-collector --config config.json setup`，交互式生成开发者凭据配置与随机 API 密钥，拒绝覆盖已有文件。`uv run tg-collector --config config.json doctor` 只检查本地配置及文件，不输出密钥；账号是否在线以 `/v1/status` 为准。后续执行 `login`，再启动服务。

定期维护命令：

```sh
uv run tg-collector --config config.json maintenance --astrbot-root /ABSOLUTE/PATH/AstrBot
```

使用 SQLite 一致性备份并执行完整性检查，保留最近 14 份 `var/backups/scheduled-*.zip`。包含采集数据库、采集配置、插件配置、该插件 KV、对应 Telegram 接入器配置、平台路由与对应专属配置文件；不备份登录会话。归档权限 0600，含密钥及聊天原文。复制到外部介质时应保持同等保护。`deploy/local.telegram-chat-collector-backup.plist.example` 每天本机时间 03:15 运行维护；替换绝对路径后安装到用户 LaunchAgents。Mac 未登录时不运行，休眠时按系统恢复调度。

采集日志 `collector.log` 自动按 2 MiB 轮转，保留 5 份；维护任务另将超过 2 MiB 的 `service.log`、`service-error.log` 截断归档，保留 3 份。日志轮转不影响采集数据库。

恢复到全新目录（拒绝覆盖现有目录）：

```sh
uv run tg-collector restore /path/to/scheduled-backup.zip /path/to/new-collector-data
```

恢复命令校验 SQLite，生成新目录下 `config.json` 和 `var/activity.sqlite3`。从现有代码目录使用 `--config /path/to/new-collector-data/config.json login` 重新授权，再启动 `run`。先停旧服务，避免两个采集账号会话或端口冲突。恢复文件置于私有目录，原服务不会被自动修改。

AstrBot 资料导出到 `astrbot-restore/`：通过后台恢复 Telegram 平台与专属配置、平台路由；在插件停用时恢复 `plugin-config.json`，随后重载。`plugin-preferences.json` 是插件 KV 的精确导出，恢复发送状态时应由维护人员在 AstrBot 停止后按 `scope/scope_id/key` 导入 preferences；不能直接覆盖整个 AstrBot 数据库。若未恢复发送状态，最近日榜可能再次发送，须先核对工作群。`sending` 状态在插件启动时转换为待核查，不能视为已确认失败。

`PUT /v1/remove_group` 接收 `group` 与布尔 `leave`，只允许已暂停的配置群。可选择由采集账号退出群；成功后逻辑归档监控群并停用群内人员，保留历史数据。

## 专属插件架构与管理文档

完整的系统架构、功能权限、Telethon 开发流程、工作群发现与选择、原消息分页、配置职责和发送状态机见 [插件 README](../AstrBot/data/plugins/astrbot_plugin_tgwatch/README.md)。采集账号入群与官方机器人入群是两个独立流程；选择工作群后需要显式启用榜单。

`PUT /v1/remove_person`：`person` 为正数人员 ID，`group` 为指定负数群 ID 或 0（全部群）。无需先暂停；事务删除对应消息、监控关联与启用区间，无剩余关联时清除人员档案及规则。接口需 Bearer 鉴权，机器人端先展示范围并确认。

### 群生命周期一致性

可选群、启用校验和后台巡检共用明确失效判定：已退出、已停用、Telegram 返回禁止访问群实体。正常运行时后台约每 60 秒巡检一次（网络与限流可延迟），确认失效后暂停并归档，状态和人员关联列表同步隐藏；历史消息不自动删除。超时等临时错误只记录缺口，不推断退群。重新添加群不会自动恢复旧人员关联，需明确启用。
