
# Connecting to Telegram

## Command Menu

The menu includes `/start` and valid Telegram commands from enabled plugins. Built-in commands have Chinese descriptions. Visibility does not grant permission; AstrBot still checks authorization. Failed updates preserve the previous menu and are retried on subsequent registration. The Wangshangliao moderation plugin does not manage Telegram groups.

## Supported Message Types

> Version v4.15.0.

| Message Type | Receive Support | Send Support | Notes |
| --- | --- | --- | --- |
| Text | Yes | Yes | |
| Image | Yes | Yes | |
| Voice | Yes | Yes | |
| Video | Yes | Yes | |
| File | Yes | Yes | |

Proactive message push: Supported.

## 1. Create a Telegram Bot

First, open Telegram and search for `BotFather`. Click `Start`, then send `/newbot` and follow the prompts to enter your bot's name and username.

After successful creation, `BotFather` will provide you with a `token`. Please keep it secure.

If you need to use the bot in group chats, you must disable the bot's [Privacy mode](https://core.telegram.org/bots/features#privacy-mode). Send the `/setprivacy` command to `BotFather`, select your bot, and then choose `Disable`.

## 2. Configure AstrBot

1. Enter the AstrBot admin panel
2. Click `Platforms` in the left sidebar
3. Click `Add Adapter` above the bot list
4. Select `telegram`

Fill in the configuration fields that appear:

- ID: Enter any value to distinguish between different messaging platform instances.
- Enable: Check this option.
- Bot Token: Your Telegram bot's `token`.

Please ensure your network environment can access Telegram. You may need to configure a proxy using `Settings → Network → Proxy & Dependency Sources → HTTP Proxy`.

## Dedicated Tenant Controller

The local tenant-control integration uses AstrBot for both the controller and
tenant bot runtimes. Configure these optional fields only on its dedicated
Telegram adapter:

- `telegram_allowed_updates`: `["message", "pre_checkout_query", "managed_bot", "callback_query"]`.
  An empty list preserves the previous polling behavior.
- `telegram_required_plugin`: `astrbot_plugin_tenant_control`. Leave this empty
  for ordinary bots. The controller waits for the plugin before receiving updates;
  on unload, the adapter stops reception and drains already queued payment events.

Install the archive built by `integrations/tenant-control` and configure its
verified operator IDs in AstrBot's plugin settings. The plugin reuses AstrBot's
Telegram client, command pipeline and lifecycle; it does not start another poller.
This integration requires this repository's adapter extensions, not an arbitrary
upstream image. Keep the controller isolated from production and tenant instances.
Rotate any exposed token before enabling it. See the integration README for the
shared DB/key configuration and separate privileged deployment worker.

## Streaming Output

The Telegram platform supports streaming output. Open `Config`, select the configuration profile used by the Telegram bot, enable `Streaming Output` under `AI → Common Settings → Message Handling`, and click `Save Configuration`.

### Private Chat Streaming

In private chats, AstrBot uses the `sendMessageDraft` API (added in Telegram Bot API v9.3) for streaming output. This displays a "typing" draft preview animation in the chat interface, creating a more natural "typewriter" effect. It avoids issues with the traditional approach such as message flickering, push notification interference, and API edit frequency limits.

### Group Chat Streaming

In group chats, since the `sendMessageDraft` API only supports private chats, AstrBot automatically falls back to the traditional `send_message` + `edit_message_text` approach.

:::warning
`sendMessageDraft` requires `python-telegram-bot>=22.6`.
:::
