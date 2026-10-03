# Native Wangshangliao Adapter

Updated October 2, 2026. Start with the [beginner guide](wangshangliao-start.md).
The [documentation index](../../wangshangliao.md) links the complete Chinese command,
operations and acceptance references.

## Responsibilities And Deployment

The Python adapter runs inside AstrBot: business authentication, sealed session recovery,
NIM connectivity, verified directories, text transport, fixed moderation capabilities,
authorization, idempotency and receipts. No Rust bridge, DH workbench or pairing token is
required. AstrBot owns profiles, administrators, models, personas and knowledge bases.
The built-in plugin owns commands, auditing, rankings, activities and daily schedules.

![Adapter layers](/images/wangshangliao/architecture-layers.svg)

Use Python 3.12+ and `uv sync --extra wsl`; build the dashboard with `pnpm install`,
`pnpm generate:api` and `pnpm build` in `dashboard`. The serialized local captcha worker
needs Node.js and OpenCV/ONNX, starts on demand and needs no GPU. Docker builds need
the project's `INSTALL_WSL` argument. Reinstall dependencies on the target OS/architecture;
available build entry points do not prove every architecture was tested.

Set `ASTRBOT_WANGSHANGLIAO_CONFIG` to a protected deployment JSON file:

| Field | Contract |
| --- | --- |
| `origin` | HTTPS service origin |
| `headers` | Protocol request headers |
| `metadata` | Fourteen integers |
| `signing_seed`, `body_key`, `message_key`, `server_key` | Standard Base64, each decoding to 32 bytes |
| `captcha_id`, `app_key`, `device_id` | Valid deployment parameters |

Use mode `0600` on Linux/macOS or a service-account-only ACL on Windows. Never publish
credentials. Missing/invalid configurations return `deployment_missing`/`deployment_invalid`.
Legacy configs without `message_key` may authenticate but message startup returns
`message_key_missing`. When the variable is unset, an approved sealed import under
`data/platform_data/wangshangliao/deployment/` may load. Invalid explicit paths do not fall
back. Importing protocol configuration does not import passwords or account sessions.

`ASTRBOT_YIDUN_SOLVER_URL` unset or `builtin` uses the local solver; empty means manual
verification; external HTTP remains a compatibility option. Automatic failure retains
manual recovery. `.venv/bin/python scripts/check_wangshangliao.py` is offline preflight,
not live acceptance.

## Login And Identity

Create/select a bot, complete password/SMS login and device verification, select groups/profile
and save within the ten-minute login transaction. Authentication, saved configuration and
an online NIM connection are separate states. Empty enabled groups disable group handling,
not necessarily private chat. Disable-and-save retains recovery credentials; logout removes
them but not ledgers. Re-login stops the old connection. Stop old automation before migration.

Instance IDs, business UIDs, display accounts, business group IDs, display group numbers and
NIM IDs differ. Administrators and native `At.qq` use business UIDs; APIs/grants use business
group IDs. Nicknames and typed mentions do not identify targets. Incomplete/inconsistent
directories prevent moderation instead of authorizing actions from partial mappings.

```text
BOT_INSTANCE:GroupMessage:BOT_BUSINESS_UID/GROUP_BUSINESS_ID
BOT_INSTANCE:FriendMessage:BOT_BUSINESS_UID/private/PEER_BUSINESS_UID/BASE64URL_NIM_ID
```

Choose known authenticated conversations, never fabricate UMO. Local known peers are not
a complete cloud directory. Group AI requires a native mention of the current bot.
Fixed commands match the entire message without `/`, `群管` or a mention.
Quote decoration degrades to text/native mentions, not a quote bubble. Media transport
is outside current capability.

## Three AI Routes And Authorization

| Input | Provider | Persona / knowledge | Management tools |
| --- | --- | --- | --- |
| Member or admin mentioning the bot in a group | Group `customer_provider_id` | Existing group session | No Wangshangliao management tool |
| Ordinary private consultation | Current private model | Existing private profile | No Wangshangliao management tool |
| Authenticated admin private chat | Instance `admin_provider_id` | Existing private profile | Fixed authorized tool |
| Content audit | `moderation.semantic.provider_id` | Separate prompt and bounded same-group context | `tools=None` |

Blank provider choices fall back to the corresponding session model. Administrative natural
language needs a tool-capable model and persona access to `wsl_private_management`.
Other persona tools are not globally disabled. Ordinary users' fixed private commands,
including help, permissions and personal invitations, return `无权限`; ordinary consultation
follows the reply switch. Group administrators still receive customer service when mentioning.

Private-only fixed commands sent in groups are consumed silently: no private-chat guidance,
execution or customer-agent fallback. Help aliases and content-mode administrator group kicks
are also silent. Public rankings/activity queries and permitted group actions retain existing replies.

Manage administrators in the effective profile's `admins_id`, optionally selecting known
private conversations by display name. Authorization uses exact UIDs. Dashboard login,
chat administrators, upstream roles, action grants and proactive destinations are separate.
Natural-language configuration requires a before/after preview and a new private confirmation.
It cannot grant authority or credentials, or punish as a configuration side effect.
See the plugin reference for field scopes and the additional schedule confirmation.

Saved `ai_routes.groups` maps business group IDs directly to provider-ID **strings**.
`customer_provider_id` is the page/API input name, not a nested saved-group object.
`reply_private` and unspecified `reply_groups` default true; enabled groups are still required.
`proactive_send.enabled` and moderation booleans default false.
Audit timeout defaults12 seconds (1..30), context count defaults20 (1..50),
cooldown defaults60 seconds (10..86400). Keywords: at most50 per list,100 characters each.

## Sending, Persistence And Output

Proactive sending defaults off and needs the switch plus exact destination grants.
`private_peer_unknown` requires a real authenticated conversation, not synthetic records.
Managed-bot tests additionally need a receiving-side window.

Authenticated `POST /api/v1/im/messages` uses a known `umo` and components: `plain` for
text and `at.qq` for a **string business UID**, not NIM/nickname. Follow its operation-ID
contract; query `GET /api/v1/im/operations/{operation_id}?platform_id=BOT_INSTANCE`,
never resend to check delivery. `accepted` is not delivery or readback; `verified` requires
capability-specific verification; `unknown` is not blindly retried; `needs_review` retains
interrupted processing without regenerating.

IM access needs Dashboard JWT or supported `im`-scoped API authentication. Use a stable
caller-owned `operation_id` (1..256 characters) for each business operation; do not change
content under an old ID or use a new ID to blindly retry an uncertain request.

Storage is under `data/platform_data/wangshangliao/<SHA256_INSTANCE_ID>/`, including
`session.key`, `session.sealed` and message/moderation/cards/activities/schedule databases.
Same-host sealing does not protect against the host administrator. Protect backups as
credentials. Messages persist before ACK; at most16 concurrent sessions/1,024 active queued
sessions, persistent overflow and same-session order.

Applicable group feature replies are recalled twenty seconds after each accepted segment.
Lottery announcements, cancellation notices, results, signup and status replies are exempt.
This covers command/ranking/activity/violation replies, not private messages, ordinary AI,
user commands or arbitrary proactive messages. Jobs persist; over-five-minute overdue jobs
expire; uncertain attempts are not retried. Member-message recall separately needs `recall`.

AI output becomes plain text, preserving links/native mentions and disabling text-to-image
for those results. Paired `think/thinking/thought/analysis` blocks, leading unclosed tags and
orphan closing tags are filtered. This does not cover every streaming path/unlabeled reasoning
and may leave an empty answer. Limited credential/contact masking is not DLP or input/history/log
sanitization.

## Testing And Troubleshooting

Managed bots, including disabled instances, are excluded by default. Configure a window on
the **receiving bot**, with the sender as source. Saving scope does not start the window.
Explicitly activate it; at most300 seconds/ten inbound events, in-memory, invalid after
restart/stop/account replacement. Reverse traffic remains excluded. Private AI, private/group
commands and group rules are separate; group rules use `WSL_TEST_` in a specified group.
Windows grant no authority and do not replace ordinary external-account acceptance.

For silence check receiving direction, saved replies, native mentions, `ignored_bot`, window
and effective profile. For private denial distinguish fixed commands from consultation.
For blank pages check generated assets, browser errors and login proxies; do not loosen iframe
sandbox. Missing activity/schedule routes require matching running backend/plugin/page versions.
For unknown actions/backlog inspect durable results, do not delete databases or replay actions.

October2 offline regression found three formatting failures involving native-mention spacing
and ranking line breaks. They are recorded in acceptance, not repaired by this documentation update.

See [acceptance](../../zh/platform/wangshangliao-testing.md) and
[maintenance](../../wangshangliao-maintenance.md). Offline tests/historical screenshots do not
prove full live isolation, peer delivery or continuous-operation stability.
