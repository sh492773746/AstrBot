# LajiaoLiao

## Group AI and Delegated Private Management

The group management page's automation settings provide independent group AI and
fixed-command switches. New groups default to AI off and commands on. Saved legacy
groups without the AI field preserve their previous behavior. AI still follows the
effective AstrBot configuration profile, model, persona, wake checks and session
service switch; disabling conversational AI does not disable moderation or commands.

Private management resolves explicit user-ID grants within the current instance.
A single authorized group is selected automatically; multiple groups require an
explicit directory selection. Every operation rechecks current authority, and
delegation never grants a global AstrBot administrator role.

Processing remains bounded: 32 in-flight events per account, four database workers,
64 admitted database operations, bounded settings caching and at most four account
scheduler tasks. Capacity limits apply backpressure. Offline 100-group benchmarks
are not a production model throughput or long-term stability guarantee.

Authorization and official verification are handled in Create Bot. The plugin page is for group management only; the plugin provides transport internally. AstrBot owns permissions, models, personas, replies, and conversation configuration.

## Authorization

Choose LajiaoLiao in Create Bot, enter the phone number and password, and select Login. Complete the official slider inside the current dialog. The instance ID is generated automatically and successful authorization is associated with it automatically. Save the bot to complete creation; no manual ID or binding action is required. Drafts expire after 15 minutes.

The verification frame has an opaque sandbox origin and receives only public challenge configuration, never passwords or Dashboard credentials. Failed or cancelled verification does not submit a login request. Password inputs are cleared after the attempt or component removal. The dashboard uses authenticated HTTPS plugin APIs; the protocol worker remains loopback-only. Rendering the slider does not constitute real human verification or account acceptance.

New bots are disabled with an empty group allowlist and private chat off. After a timeout, refresh login status before retrying. Restore or revoke unsaved sessions from the "Unfinished bots" selector in Create Bot without entering an ID. Orphaned bindings can only resume with their original instance ID; sessions still in use cannot be revoked. Cancelling creation does not automatically revoke a completed authorization.

The only plugin page is Group Management (`management`); the separate `accounts` page has been removed. The plugin ID and account data directories remain unchanged.

## Reauthorize An Existing Bot

Select Re-login in the bot editor and complete official verification with the original account. A separate fifteen-minute authorization operation cannot consume messages. Only the same OpenIM user can replace credentials; changing accounts requires a new bot. Instance ID, session reference, cursor, enabled state and policy are preserved.

Cancellation, identity mismatch and failures do not overwrite the existing credentials. An official login may invalidate the old device session, so preserving local files is not a promise of upstream rollback. After a timeout, refresh status rather than resubmitting the password. Device confirmation and kicked sessions require explicit human handling.

Runtime IM renewal only responds to an explicit OpenIM expiry error, uses the existing business token, coalesces concurrent requests and checks credential versions. Invalid business authorization requires re-login. Network failures use capped reconnect backoff and are not treated as expiry. Synthetic tests do not validate the private server's real expiration behavior.

## Group And Private Controls

Different accounts may run as separate instances. Pending authorizations are scoped to each bot, so one unfinished login no longer blocks another. Each Dashboard user may keep up to eight unexpired login drafts; this is a temporary authorization limit, not an active-bot limit or capacity guarantee. Cancel Login only removes unfinished authorizations; completed or uncertain logins must be checked first. Drafts expire after fifteen minutes.

Open Bots > LajiaoLiao bot > Connection settings > Message control. The former group-mode selector has been replaced by a fixed `allowlist`. An empty list denies every group.

Alternatively open the plugin's Group Management page and select a bot. Only created instances bound to the current Dashboard user are shown. Both group policy and full bot saves use the same instance revision; stale edits return 409 and require refreshing. Policy changes update the live instance without reconnecting or enabling a disabled bot.

The management page has responsive light/dark styling and groups the bot selector, per-group switches, and save controls into distinct sections.

- Refresh groups reads the account-owned directory. Disabled bots use a temporary directory-only process that cannot consume or send messages. Load more retrieves subsequent pages.
- Toggle each group independently, search by name or ID, or add an ID manually. Save changes commits the edited policy; adding a group does not immediately enable traffic.
- Private chat has an independent master switch, off by default for new bots. Existing private settings are preserved.
- Saving policies does not enable the bot. Both inbound processing and outbound sends enforce the policy; existing permission and wake-up checks still apply.
- Use AstrBot's conversation configuration bindings for per-conversation models and personas. Group switches do not grant moderation rights.

Legacy `deny_all` is converted to an empty allowlist on save. LajiaoLiao configurations containing `all` are rejected. Other platforms retain their existing behavior.

## Capability Boundaries

Authorization, private session isolation, binding, per-group switches, read-only directories, and text transport are implemented. Synthetic tests do not constitute real-account acceptance.

Text quotes are implemented: incoming messages preserve their new text and an AstrBot `Reply` containing the original text, sender and timestamp. Send `Reply(id=original_id)` with text, or use the internal bridge's `reply_to`; do not combine both. Automatic quoting uses AstrBot's per-profile `platform_settings.reply_with_quote`, not a transport-side rule.

Original messages are resolved from the same account and conversation's latest 200 messages through the existing connection, without changing the consumer cursor or replaying historical events. Only textual originals are supported; quoting a quote flattens its own text to one level. Missing, cross-conversation, unsupported or unavailable originals return explicit errors without silently sending plain text. See the packaged acceptance record for the live-tested scope.

Group Management uses top-level bot/group selection and tabs. Manual native actions remain removed. Saved, default-off rules provide daily quiet hours, 20-second recall of eligible functional replies, single scheduled lotteries and daily notifications. Plans use Beijing time, run at future boundaries only and pause when missed by more than 30 seconds. Principals, grants and policies are rechecked before execution; unknown receipts never retry.

System recall (2101) and group-card (1516) notifications are ACKed separately and never enter the model pipeline. Recall suppresses matching pending replies and keeps bounded ordering protection; it cannot undo completed model calls, erase historical context or recall replies already sent. New notifications are shown without rewinding the consumer cursor.

JPEG/PNG transport is implemented, with a 20 MiB or lower server limit, 40-million-pixel limit, two media tasks and public-address/redirect validation. Audio, files, video, new mentions and image quotes are excluded. Automatic mute exists; member recall and card synchronization require ordinary-member live acceptance before their switches become usable. Cleanup remains disabled because the authenticated account profile currently omits authoritative unavailable-account status; names and avatars are never evidence.

The bundled `capabilities.json` separates static protocol evidence, implemented receive/send/actions and real acceptance. An SDK method is evidence to investigate, not proof of compatibility or a claim that the platform is unsupported.

## Controlled Acceptance

The `test-window` and `test-send` plugin APIs are closed by default. Opening requires the account ID, explicit confirmation and up to four selected private/group targets. Windows last at most 300 seconds with at most ten sends; normal instance policy still applies. Operations are persisted before submission, duplicate IDs return the recorded receipt and unknown sends are not retried.

The deployment has designated two bound test bots and test group 72567012 for image and isolation checks. Real member enrollment, invitation credits and sanctions still require an explicitly designated ordinary member. Do not lift bot/admin protections, choose unrelated contacts or disrupt host networking. The evidence matrix distinguishes implementation from live acceptance.

## Noncontainer Operation

Trusted accounts under one administrator can use independent protocol processes without one container per bot. This is not an operating-system permission boundary: the main service and trusted plugins may still read other account files.

Account management uses per-session locks. Slow directory requests no longer hold a shared exclusive account lock. Process launches and directory reads each have concurrency limits of four; each internal HTTP session permits eight connections. Status and directory request deadlines are three and ten seconds respectively. Accounts still share the main process, model providers, and host resources.

Run credential-free local testing with `PYTHONPATH=. .venv/bin/python services/lajiaoliao-bridge/plugin_stability_test.py --seconds 120 --workers 4`. This does not open upstream message connections. Real-account capacity, long-running stability, and recovery still require acceptance testing; local status latency is not model response latency.
