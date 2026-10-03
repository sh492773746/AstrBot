
# 接入 Telegram

## 支持的基本消息类型

> 版本 v4.15.0。

| 消息类型 | 是否支持接收 | 是否支持发送 | 备注 |
| --- | --- | --- | --- |
| 文本 | 是 | 是 | |
| 图片 | 是 | 是 | |
| 语音 | 是 | 是 | |
| 视频 | 是 | 是 | |
| 文件 | 是 | 是 | |

主动消息推送：支持。

## 命令菜单

菜单包含 `/start` 和已启用插件的有效 Telegram 命令，内置命令使用中文说明。菜单显示不代表用户拥有执行权限，权限仍由 AstrBot 校验。更新失败会保留原菜单，并在后续注册时重试。旺商聊群管插件不适用于 Telegram。

## 1. 创建 Telegram Bot

首先，打开 Telegram，搜索 `BotFather`，点击 `Start`，然后发送 `/newbot`，按照提示输入你的机器人名字和用户名。

创建成功后，`BotFather` 会给你一个 `token`，请妥善保存。

如果需要在群聊中使用，需要关闭Bot的 [Privacy mode](https://core.telegram.org/bots/features#privacy-mode)，对 `BotFather` 发送  `/setprivacy` 命令，然后选择bot， 再选择 `Disable`。

## 2. 配置 AstrBot

1. 进入 AstrBot 的管理面板
2. 点击左边栏 `机器人`
3. 点击机器人列表上方的 `创建机器人`
4. 选择 `telegram`

弹出的配置项填写：

- ID(id)：随意填写，用于区分不同的消息平台实例。
- 启用(enable): 勾选。
- Bot Token: 你的 Telegram 机器人的 `token`。

请确保你的网络环境可以访问 Telegram。你可能需要使用 `设置 → 网络 → 代理与依赖源 → HTTP 代理` 来设置代理。

## 专用租户总控

本仓库的租户总控和租户机器人都以 AstrBot 为运行底座。仅在总控的专用
Telegram 适配器中配置以下可选字段：

- `telegram_allowed_updates`：填写 `["message", "pre_checkout_query", "managed_bot", "callback_query"]`，
  订阅消息、Stars 支付预检和托管机器人事件。留空保持原有轮询行为。
- `telegram_required_plugin`：填写 `astrbot_plugin_tenant_control`，普通机器人留空。
  总控会等待插件就绪后再接收更新；卸载插件时，适配器先暂停接收并处理完已排队的支付事件。

安装 `integrations/tenant-control` 构建的插件包，在 AstrBot 插件配置中设置经核实的
运营管理员 ID。插件复用 AstrBot 的 Telegram 客户端、命令事件和生命周期，
不会再启动一个独立轮询器。本功能依赖本仓库的适配器扩展，不能直接假定任意原版镜像兼容。
总控应与生产实例、租户实例分开部署，启用前须轮换已泄露的 Token。
共享订单库、加密密钥和独立部署器的配置见该集成的 README。

## 流式输出

Telegram 平台支持流式输出。需要进入 `配置文件`，选择 Telegram 机器人使用的配置文件，在 `AI → 通用设置 → 消息处理` 中开启 `流式输出`，再点击 `保存配置`。

### 私聊流式输出

在私聊中，AstrBot 使用 Telegram Bot API v9.3 新增的 `sendMessageDraft` API 实现流式输出。这种方式会在私聊界面展示一个「正在输入」的草稿预览动画，体验更接近「打字机」效果，且避免了传统方案的消息闪烁、推送通知干扰和 API 编辑频率限制等问题。

### 群聊流式输出

在群聊中，由于 `sendMessageDraft` API 仅支持私聊，AstrBot 会自动回退到传统的 `send_message` + `edit_message_text` 方案。

:::warning
`sendMessageDraft` 功能需要 `python-telegram-bot>=22.6`。
:::
