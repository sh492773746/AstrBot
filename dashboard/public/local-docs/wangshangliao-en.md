# Native Wangshangliao Adapter

## Groups And WebUI Only

This source batch removes all private-chat menus/group selection, friend
automation/welcomes, personal knowledge, dedicated support, private AI/admin
guidance, finance chat and image intake. Retain historical conversations,
knowledge, ledgers and audits. Verified native group mention/reply list controls,
group rules and WebUI rules, images, activities and Invitation Settlement remain.
Ordinary group administrators gain no global Dashboard access; existing
authorized operators maintain images and finance.
See the [old-to-new mapping](wangshangliao-start.md#current-entrances-groups-and-webui)
and [architecture/permissions](wangshangliao-architecture-permissions.md).
This batch is not deployed; mute-first and raid changes await loading and live
acceptance. Raid defense defaults off.

Adapter reference previously calibrated October 20, 2026. Start with the [beginner guide](wangshangliao-start.md).
The [documentation index](../../wangshangliao.md) links the complete Chinese command,
operations and acceptance references.

### Native Conversations, Statistics And Traces

AstrBot owns configurations, personas, provider credentials, Chinese retrieval
tokenization, technical logs, conversations, statistics and traces. Domain code
still enforces scoped knowledge ownership, atomic shared cost reservations,
financial transactions and authorization/receipts for fixed mute schedules;
these must not become arbitrary native agent jobs. Recording failures emit a
sanitized AstrBot warning without retrying model requests or replies.

## Management Navigation

The dashboard uses two levels: a category followed by a feature tab. Both rows
scroll horizontally on narrow screens instead of forming a tall grid.

Each category remembers its last feature during the current page session.
Switching tabs never submits changes. Rule and AI drafts survive navigation until
an explicit save, refresh or group change. Plan panels mount
on first visit and retain their inputs on return. Account/group authorization,
chat commands, the separate cashier, formal-group switches and existing ledger
data are unchanged.

These existing screenshots predate the group-mention route fix and use synthetic
groups with unsaved inputs. They illustrate navigation, not current persona routing
or enabled production groups:

![Two-level navigation on mobile](../../zh/platform/images/wangshangliao-navigation-en-mobile.png)
![Two-level navigation on desktop](../../zh/platform/images/wangshangliao-navigation-en-desktop.png)

## Responsibilities And Deployment

The Python adapter runs inside AstrBot: business authentication, sealed session recovery,
NIM connectivity, verified directories, text transport, fixed moderation capabilities,
authorization, idempotency and receipts. No Rust bridge, DH workbench or pairing token is
required. AstrBot owns profiles, administrators, models, personas and knowledge bases.
The built-in plugin owns activation, independent group managers, fixed rules,
rankings, activities, schedules and read-only AI. `admins_id` does not
automatically grant public-group management authority.

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
| `protocol_version` | Protocol for new logins: `2.8.3` selects desktop compatibility, `3.7.3` selects Android, and omission retains `3.7.1` compatibility. Saved sessions restore their own version, without converting credentials. |

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

| Status | Interpretation |
| --- | --- |
| Authenticated but not saved | Save, then verify the messaging connection separately |
| Runtime status unavailable | Unverified, not stopped or zero groups |
| Group list or member permissions unavailable | Check business authentication and native authority; never substitute stale membership |
| Business paused or zero enabled groups | Inspect separate runtime gates and eligible group scope; zero groups alone does not imply pause |
| Historical errors | Retained historical records, not proof of a current outage; never raw provider responses |

With official 3.7.3 authentication, enter the secondary security code only in the
authenticated Dashboard field when the provider requests it. It is never saved
or requested in chat. No field is shown when verification is not required.
Provider `1400` means the code is not set; `1402` means this sign-in is not yet
verified. Incorrect code `1403` requires at least a 30-second wait; `1404` requires
checking the lock in the official app, and `1405` requires binding a phone.
After successful verification, failed messaging setup can be retried without
resubmitting the code. Uncertain verification stops retries and requires a fresh
login. Do not disclose the code in logs, screenshots or chat.

Successful security verification does not require credential rotation. Prefer
the response header, then a credential in the success body; if neither supplies
a new credential, retain this login's credential and fetch the IM token.
Absent or unchanged credentials do not imply an incorrect security code.
Explicit rejection, malformed receipts and uncertain transport still stop
without resubmitting the code. A verified IM token is required before saving
completed authentication.

Current password and SMS login requests include `version`, `appVersion` and
`appHash` from the verified release, not just updated headers. Release 3.7.3 has
version code 335. A changed release requires new verification; do not guess its
code from the filename or reuse an old package digest. Official code `1001` is a
generic error, not proof of an incorrect password, an account ban or a security
code error. The Dashboard retains the numeric code without logging raw provider
responses or credentials and does not automatically retry login. Offline tests
do not establish successful real-account authentication.

The login panel's business-running/paused badge and enabled-group count follow the
instance's runtime business state, not transport-only login configuration.
Unavailable runtime status is shown as unverified rather than falsely paused.
The panel also shows a connection reason and next step. Missing connection state
or group counts stay unavailable rather than becoming "stopped" or zero.
The error-details entry labels historical records explicitly: records can remain
after recovery and do not establish a current outage. Unknown errors use fixed
messages, never raw provider responses.

Business pause follows the instance's runtime gate, not an empty operating-group
list or an offline connection. Operating counts exclude inactive, absent, paused,
unverified or membership-pending groups and respect capacity suspension; saved
group rows alone do not establish that a group is running.
When messaging stays online but the group directory explicitly requires
reauthentication, the page separately requires sign-in. Transport is not falsely marked
offline, nor does its online status establish working member reads or settlement.

`business_auth` separately exposes credential state and device class, never device
identifiers, credentials or keys. `credential_present` means credentials exist,
not that all business APIs were verified. A definitive `401` from any business
request marks authentication `expired`; subsequent authenticated requests fail
locally. The UI shows this even when a previous directory read succeeded.
Transport errors and `1400` are not presented as confirmed device conflicts.

### Phone And Bot Sign-Ins

Running on a server does not make an Android login a desktop login. The client's
device classes identify desktop as `1` and Android as `3`. Android `3.7.3` uses
Android-specific device, client and signing bindings. A phone sign-in may
invalidate it; this is not proof that official desktop/phone coexistence was
removed.

The desktop `2.8.3` compatibility branch follows the published desktop signing
version, device and client identifiers, current `get-nim-token` route and stable
IM device ID. It still verifies the server's bound login token and any required
secondary verification. It neither relabels Android credentials nor fabricates
Android installation hashes.

IM connections preserve the token endpoint's static, dynamic or callback
authentication mode. Callback tokens are not sent as static tokens. Unknown
modes fail closed without falling back to a static token; diagnostics never
include tokens or raw authentication responses. Legacy replies without a mode
retain static compatibility.

After an operator selects the new-login protocol, a new Dashboard sign-in and save
are required. Saved Android, legacy and desktop sessions restore their own
protocols. Deployment changes and restarts do not convert credentials, reset
device IDs, automatically sign in again or delete ledgers. Offline desktop tests
are not live coexistence acceptance: a successful fresh sign-in and business reads
after another phone sign-in remain necessary evidence.

Connectivity, group-list reads, complete member reads and enabled business scope
are separate checks. Member-read failures no longer all say the public service
is disconnected: fixed notices distinguish identity conflicts, incomplete
rosters, size limits, provider rejection, security verification and rate limits.
Diagnostics use AstrBot's native logger without payloads or credentials. Partial
rosters never authorize management or settlement, and reads are not blindly retried.

Runtime `receive_health` separates unhandled non-chat packets, invalid packets,
decode failures and unsupported message formats. `decode_reasons` contains only
fixed processing stages, never plaintext, ciphertext, keys or raw responses.
Non-chat packets no longer increment `unsupported_pushes`; they retain a separate
counter without claiming support. The login panel warns only on actual decode
failures. Counters cover the current run; old aggregate totals cannot be
retroactively assigned to these categories.
`decoded_messages` counts actual successful decodes. Bounded `decode_shapes`
summarize field numbers and wire types only, never field values.

Reception supports legacy envelopes and the official 3.7.3 `MessagePacket`
version 1. Both the metadata AEAD and inner message AEAD must pass; packet
metadata cannot confer identity or permissions. A zero compression marker still
requires authentication and routing checks, never plaintext trust. Unknown packet
versions, nested packets and either authentication failure are rejected. The
ordinary-message 1 MiB limit is unchanged.
The integer wire variants accepted by the official codec are normalized before
authentication; conflicting encodings of the same field are rejected.
Live traffic still includes envelopes without authentication fields. They remain
rejected and cannot reach moderation, AI or settlement. Full compatibility is
not claimed; further support requires trustworthy format evidence.

Instance IDs, business UIDs, display accounts, business group IDs, display group numbers and
NIM IDs differ. Administrators and native `At.qq` use business UIDs; APIs/grants use business
group IDs. Nicknames and typed mentions do not identify targets. Incomplete/inconsistent
directories prevent moderation instead of authorizing actions from partial mappings.

The management page's current-group selector also lists joined but disabled groups,
marked "Not enabled" and unavailable for actions. Group-name refreshes are cached for
up to 30 seconds. Select and save groups under Groups & Runtime before processing;
joining alone grants no moderation, proactive sending or cashback authority.

The default keyword file contains 47 rules for legacy instance settings and the
versioned first-activation template. Public groups use their own persisted lists;
updating the file never overwrites existing custom or explicitly cleared lists,
or enables an inactive group.
Broad words, short numbers and domain suffixes require advertising context;
domain suffixes must match an actual hostname. Reports, quotations, ordinary numbers
and lone links do not directly trigger sanctions. Explicitly cleared lists stay empty.

Built-in combinations, updated from locally retained administrator-recall evidence
on 2026-10-17, also detect payment-account screenshot recruitment, timed social-account
rewards, paid nearby-user recruitment, influencer-material solicitation, guaranteed
project recruitment, paid-prediction referrals and spelling variants of edited-image
betting promotions. A topic word alone is insufficient; reports and explicit quotation
prefixes remain exempt. These combinations also apply with an empty custom keyword
list, but never bypass a group's advertising switch, permissions or message-age checks.
Native group/contact numbers are content evidence, never identity or authorization.
Missing recalled originals are not inferred to be pictures or advertisements.
Historical classification does not replay sanctions, enable paused groups or change
cashback records.

Server-maintained speech exemptions are stored in the collector's native data
directory as `member_exemptions.sqlite3`. They match the bot account, group,
member UID and authenticated NIM identity exactly. They suppress automatic
advertising, media and flood sanctions only, without granting management,
configuration, AI quota or financial privileges. Admission and pre-submit guards
recheck exemptions. Records remain until explicitly revoked by the server;
historical sanctions, uncertain receipts and other administrators' actions are
not removed or overridden. Nicknames and forwarded content cannot create exemptions.

On 2026-10-20, retained manual-recall texts informed six additional combinations:
explicit registration-credit offers, task solicitation with a contact number,
complete nearby-user hourly-pay offers, numbered project-group recruitment,
paid forwarding with an external group destination, and odds solicitation with a
tagged contact number. Each requires its own combined evidence; ordinary discussion,
lone numbers, reports and explicit quotations do not qualify. Manual recalls are not
automatic violation labels, and harassment or disputes are not relabeled as advertising.
The additions require no model calls, platform requests or history scans. They retain
group advertising switches, custom keywords and the existing sanction sequence.

Generation responses report tokens in `usage`. The current relay separately exposes
read-only `/v1/usage` aggregates with `cost` and discounted `actual_cost`; do not
apply the multiplier twice. The receipt currently declares `USD` and
`mode=unrestricted`, not verified CNY deductions or proof of an enforced supplier cap.
These aggregates alone never unlock public model calls.

On 2026-10-20, explicit free betting-credit solicitation and no-funds recruitment
phrases were added. A fresh 5–12 digit contact can be correlated with up to 64 native
inbound messages from the preceding 120 seconds, restricted to the same account,
group, sender, rule revision and test generation. Evidence requires explicit
solicitation before the number, or a broadcast-start cue followed by the same number
at least twice. Bare numbers, ordinary counts, reports, recalled messages and stale
history do not qualify. Flood thresholds remain unchanged; old messages are not replayed.
The audit page now shows the current scoped member name above the immutable account ID.
Missing directory labels may fall back only to an exact original-message and sender match
within the same account and group, explicitly marked "Historical name". Otherwise the page
shows "Name unavailable". Labels never authorize actions or trigger platform directory requests.

Rule outcomes now separate the original recall from the warning send receipt and any
historical warning cleanup. The October 20 warning fix was loaded by the full backend restart above:
native `actions.py` no longer forces `auto_recall=False` or trusts caller-supplied
`userName`/ID labels. An exact match in this account/group's authoritative complete
roster supplies the real nickname for a native mention. Verified identity without a
nickname sends warning text only, never an opaque-ID mention; identity mismatches
still suppress delivery. `auto_recall=True` schedules cleanup twelve seconds after
an actual send receipt. This was not a plugin reload. The deployment stage sent no
warning; subsequent three-account tests in 1459282 verified three real warnings
across two isolated fixtures containing one and two warnings, not a complete
first/second/third progression in one fixture. Native `userNick` supplied `@大蛤`
with the verified target, rather than `groupMemberNick=testfork`. The peer observed
original-message recalls and warning delivery/self-recalls; warning receipt-to-recall
notifications took 12.112/12.115/12.125 seconds, not measured phone disappearance.
Rules and the receiver's original native-member role were restored, and windows closed.
Formal groups, finances and normal punishment counters were unchanged; native
receipts/minimal audit were retained. Testing used zero model calls, no extra NIM
connections and no service restart. Mute escalation, complete functionality and phone
rendering were not tested; see [isolated acceptance](wangshangliao-membership-testing.md).
The 619 passing tests precede
the missing-nickname fallback refinement and remain historical, not final acceptance;
see [current architecture and permissions](wangshangliao-architecture-permissions.md) for final offline results.
A failed notification does not erase an accepted sanction; platform acceptance
is not a read receipt. Dated evidence remains unchanged; old warnings and sanctions are not replayed.
The separate Recall Sources table reads up to 200 scoped native notices, deduplicates by
message and native identities, and displays at most 50, ordered by event time when valid.
Bot execution requires a matching accepted local receipt and native bot identity;
command/reply cleanup, author recalls, other-member recalls and unverified sources remain
distinct. A manual recall from the official app under the bot account is not automatically
reported as an automated sanction. Notifications do not replace phone-screen verification.

## Member Card Normalization

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

Storage is under `data/platform_data/wangshangliao/<64_HEX_DIRECTORY>/`, including
`session.key`, `session.sealed` and message/moderation/cards/activities/schedule databases.
Integrated mode's protected `account-roots.json` binds each account to its existing
directory across instance recreation; first binding uses the instance-ID SHA-256.
Never delete a supposedly old directory by recomputing the current instance hash.
Same-host sealing does not protect against the host administrator. Protect backups as
credentials. Messages persist before ACK; control, fixed commands and ordinary
messages use separate slots, bounded backlogs and same-session ordering.
Current capacities are listed in [maintenance](../../wangshangliao-maintenance.md).

AI output becomes plain text, preserving links/native mentions and disabling text-to-image
for those results. Paired `think/thinking/thought/analysis` blocks, leading unclosed tags and
orphan closing tags are filtered. This does not cover every streaming path/unlabeled reasoning
and may leave an empty answer. Limited credential/contact masking is not DLP or input/history/log
sanitization.

## Testing And Troubleshooting

The current formatting regressions pass. If native-mention spacing or ranking line breaks
still differ, inspect the actual native mentions and message parts; offline results do not
replace confirmation in the official mobile client.

See [acceptance](../../zh/platform/wangshangliao-testing.md) and
[maintenance](../../wangshangliao-maintenance.md). Offline tests/historical screenshots do not
prove full live isolation, peer delivery or continuous-operation stability.
