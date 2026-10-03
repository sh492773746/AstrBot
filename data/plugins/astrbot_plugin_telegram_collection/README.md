# Telegram 采集管理

在插件列表打开“管理中心”，统一管理 Telegram 用户账号和业务订阅。页面继承 AstrBot WebUI 登录鉴权，内部管理密钥保留在服务端。

账号：连接、身份、限流等待；订阅：按账号添加群、启停、关键词规则、群内人员；模块：现有能力与扩展说明；运行状态：队列、错误及采集缺口。

插件设置仅有 `api_url=http://127.0.0.1:6190`，不存储管理密钥。插件在服务器本地读取 `/opt/telegram-chat-collector-prod/config.json` 的 `unified.tokens.admin`；非标准安装可通过服务环境变量 `TELEGRAM_COLLECTOR_CONFIG` 指定文件。业务机器人继续使用各自受限接口，不获得管理密钥。Telegram 登录、验证码及两步验证只能在服务器本地操作。

公开群优先免入群，需要加入时管理员确认；审批或网络结果不明时不得重复点击入群。暂停一个业务模块不影响其他模块，其他模块仍使用的群不能退出。

扩展协议、数据口径、迁移映射、部署和回滚见 `/opt/telegram-chat-collector-prod/UNIFIED.md`。该插件只提供管理界面，不处理聊天消息，不启用 AI 自动回复。
