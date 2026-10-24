# Public Hardening Acceptance: October 14, 2026

> Historical evidence, annotated October 20, 2026. Pause, budget and failure statements
> below apply to those runs. See [current architecture](wangshangliao-architecture-permissions.md).

See [October 15 remediation](wangshangliao-remediation-20261015.md) for the current
fixes. Historical checks below do not establish deployment or live acceptance
of the latest core revision.

## Scope

The existing AstrBot process and signed-in accounts remain in use; no separate
public worker was restored. Formal groups1516387 and1488006 remain disabled
until separately approved. Public paid-model admission stays off pending an
enforced supplier budget. No cashback amounts, attribution, payment states or
reward rules were changed; all1468 protected financial rows retain their hash.

## Implemented

- Native NIM identity binding for private authority, quotas and first-use state.
  Public events bypass general framework commands and management tools.
- Complete activation context, current membership/bot/generation/version checks,
  durable pending invitation evidence and single-use hashed ten-minute codes.
  Later voluntary joins, departures and kicks block retained old invitations.
- Bounded friend-read backoff and version-checked reconciliation without
  resending uncertain approvals. Friend deletion remains completely absent.
- Model validation before quota reservation; definitely unsent calls consume
  no quota, including cancellation concurrent with reservation commit.
  Uncertain dispatched calls remain recorded; exit prevents late replies.
- Bounded queues, text limits, evidence-loss holds and reviewed recovery without
  replaying sanctions; retention preserves unresolved operations.
- Persisted account names, integrated-mode feature image selection, allocated
  versus running counts and no false unsaved prompt during bot loading.
- Additive entry-audit and ordered inviter-page indexes; skip unused payment
  eligibility aggregates outside pending views and select the latest matching
  inviter name before joining its roster record. Financial authority, snapshot
  consistency and ledger contents remain unchanged.

## Verified

Offline Wangshangliao, Telegram and relevant framework tests: **2972 passed,
zero skipped**, including all seven reproduced boundary failures. The three
dependency deprecation warnings are recorded. Browser and rollback conditions
were supplied with actual dependencies, rather than skipped.
Full frontend, API-client, HTML and PDF generation passed. Management pages and
all30 guides were checked at390px/1440px.

Controlled group1459282 passed native commands, manager confirmation/revocation,
last-manager protection, keywords, three recall warnings,5/15/60-minute mutes,
both flood thresholds with mute-before-complete-recall, announcement, reversible
card change, group mute/unmute, permission denial, real scheduled boundaries,
lottery deduplication, kick/reinvite attribution and ownership preservation.
Controlled configuration and member state were restored; formal groups and
protected finances remained unchanged. Existing-group code activation is not
represented as a fresh group's first-invitation test.

Native evidence:
`/opt/backups/wsl-integrated-live-20261014T151241/evidence.json`.
Maintenance and permission rollback metadata:
`/opt/backups/wsl-public-hardening-20261014/`.
The uninterrupted1800.11-second observation passed, with no restart, all ten
enabled platforms running and protected finances/formal groups unchanged.
Evidence: `/opt/backups/wsl-shared-soak-20261014T071235/evidence.json`.

### Supplemental Checks

331 financial regressions passed, including six new read regressions and two
ordered-index upgrade regressions.468 read-only old/new query comparisons
returned identical data. The cashier loaded the optimization without restarting
the shared AstrBot process. Protected financial/configuration hashes and paused
formal groups remain unchanged.
Backup, query-plan and deployment evidence:
`/opt/backups/wsl-query-supplement-20261014T081146/evidence.json`.

All four new mixed-load runs were retained:

| Evidence time, UTC | Ledger baseline P95 | Loaded P95 | Growth | Rule P95 |
| --- | --- | --- | --- | --- |
| 08:06:10 | 22.072ms | 44.373ms | 101.04% | 18.048ms |
| 08:17:25 | 14.929ms | 43.487ms | 191.30% | 12.858ms |
| 08:21:26 | 13.309ms | 33.921ms | 154.87% | 14.502ms |
| 08:34:31, client frames prebuilt | 12.904ms | 30.120ms | 133.42% | 12.828ms |

Final query CPU P95 was12.862 to16.272ms; waiting P95 was0.108 to14.132ms.
Raw wall/thread-CPU samples are retained. CPU time does not replace end-to-end
latency or relax the10% target. Both computation and waiting increased; these
timings do not establish a unique root cause.
The final run moved simulated client encryption before measurement; server
decoding, persistence, dispatch and rules remain timed. Correcting that boundary
did not pass the target or replace the original failure evidence.
The temporary synthetic worker exercises native decoding, persistence, dispatch
and rules using local fake models, not the full automatic-sanction pipeline of
twenty real activated groups.

### Cashier Read Optimization

545 related regressions passed. All 498 old/new read-only query variants still
match with reader reuse enabled. The implementation shares filtered inviter
aggregation between totals and outer pagination, skips empty search/detail work,
reads risk membership once per snapshot and adds the partial
`evidence_cashier_confirmation` index.
Only the standalone cashier retains up to four exclusively leased idle readers.
It caches neither authority nor results; writes remain separate and each read
rechecks authority and releases its snapshot. The cashier loaded the code without
restarting AstrBot. All columns of the 1468 ledger rows, rules and invitation
evidence match the consistent backup. Formal groups and configuration are unchanged.
Evidence: `/opt/backups/wsl-cashback-query-20261014T225339+0800/evidence.json`.
See [database maintenance](../../wangshangliao-database.md) for implementation limits.

All new samples are retained. Times below are Asia/Shanghai; evidence filenames use UTC.

| Stage / local time | Baseline P95 | Loaded P95 | Growth | Rule P95 |
| --- | --- | --- | --- | --- |
| Before changes, 22:54:58 | 28.765ms | 14.773ms | -48.64% | 11.801ms |
| SQL only, 23:12:07 | 12.149ms | 23.635ms | 94.55% | 13.990ms |
| SQL only, 23:13:12 | 12.343ms | 18.820ms | 52.47% | 13.045ms |
| SQL only, 23:14:17 | 13.836ms | 28.748ms | 107.78% | 16.839ms |
| Reader reuse, 23:28:00 | 8.701ms | 7.708ms | -11.41% | 9.893ms |
| Reader reuse, 23:29:05 | 6.826ms | 11.732ms | 71.87% | 13.202ms |
| Reader reuse, 23:30:13 | 6.175ms | 50.460ms | 717.17% | 15.011ms |
| Extra GC diagnostic, 23:36:19 | 7.469ms | 18.453ms | 147.07% | 13.949ms |

SQL-only baseline medians were 9.697/9.620/10.159ms, versus
4.451/4.501/4.522ms with reader reuse. Normal cost decreased, but only one of
three reuse runs met the relative-growth target, so stable acceptance is not approved.
The 50.460ms spike remains in `/opt/backups/wsl-native-load-20261014T153013.json`.
The final diagnostic is `/opt/backups/wsl-native-load-20261014T153619.json`.
Diagnostic GC totaled 25.451ms, with an 8.213ms maximum collection and 821
involuntary process switches. These aggregate measurements do not identify GC,
disk or CPU as the sole cause.
The tool still reads the same four production-ledger states under the same
twenty-group/100-message-per-second synthetic load, using the cashier's reader
path with writes prohibited. It excludes HTTP authentication, network and page
rendering, and does not measure online responses inside the cashier CPU quota.

## Deployment

AstrBot and the cashier run as the non-login `astrbot` user on unchanged
CPython3.12.14 under `/opt/astrbot-runtime`. Source/runtime are read-only;
financial files and socket retain600 permissions, directories700.
Actual service-namespace checks deny root-private files, backups and source
writes while allowing required caches. External Telethon sessions and locks
use named-user ACLs, not world access.
Shared AstrBot is limited to two CPU cores,1GiB soft/2GiB hard memory and256
tasks. Cashier retains its50% CPU and256/384MiB limits.
All ten enabled platforms run normally. Disk/network and the shared process are
not hard-isolated per group. Rollback must never overwrite new financial data.

## Not Yet Passed

- Stable mixed-load ledger-query P95 growth of at most10%. Rule P95 is far below
  one second with bounded queues/memory; both passing and failing query samples
  are retained, so the best single result is not full acceptance.
  Earlier supplemental rule P95:12.828ms. Ledger P95:12.904 to30.120ms,
  133.42% growth, failed.
  Evidence: `/opt/backups/wsl-native-load-20261014T083431.json`.
  Previous failure `/opt/backups/wsl-native-load-20261014T073728.json` is retained.
  Subsequent reader reuse reduces normal cost, but two of three repeated runs
  still failed, as recorded above.
- A fresh controlled group's genuine first-invitation activation, without
  deleting prior authorization history.
- Supplier-enforced credential budget evidence; local fake-model tests do not
  establish the supplier hard limit or paid-model acceptance.
- Official phone-client confirmation of robot-initiated automatic recall.
  SDK receipts and user-initiated recall notifications are insufficient.
- A genuinely new accepted friend receiving both text menu and branded Dahai
  artwork; baseline/dedup tests are not live first-friend acceptance. Daha's
  auto-approval hold has not been silently lifted.

## Activation And Friend Follow-Up

The user confirmed automatic departure after an ordinary member invited Daha;
native control records also show confirmed departure and working-data cleanup.
Silent group `开通` replies were repaired in known-disabled-group admission,
restricted reply delivery and unknown-source explanations. A controlled
ordinary sender received an explicit activation denial, verified by native
history readback:
`/opt/backups/wsl-activation-reply-20261014T124509/evidence.json`.
This is negative admission evidence, not fresh administrator-led activation.

Verified first friend acceptance, including observed manual mobile acceptance,
shares durable onboarding claims with first unmatched private messages.
Artwork follows accepted text; uncertain delivery is not retried and existing
friends receive no bulk greetings. That code revision passed2981 tests without
skips; the final send-spacing adjustment passed248 focused tests. These are
historical runs for their revisions, not a new full run during documentation updates.

Stale SDK inviter results remain guarded by latest trusted business events.
Fresh qualifying activation, mobile visual recall, real first-friend welcome,
supplier budget and performance gates remain unpassed. See
[current architecture and permissions](wangshangliao-architecture-permissions.md).

## Shared Group And Private Commands (2026-10-15)

Integrated-mode fixed management commands now work in either channel; the
`管理 ` prefix is optional. Friendship is unnecessary: the verified real inviter
sends `开通` in the group, then authorized managers use current-group commands.
Private chat still selects an authorized group first. Group chat cannot list or
switch to other groups. AI and personal cash-ledger queries remain private-only.

Member snapshots bind the group and conversation. Removal and schedule previews
also bind the authorization revision, rejecting stale confirmations after changes.
Manager limits, last-manager protection, native identity checks, bot-loop
prevention and controlled-test restrictions remain in place.

All **3124 Wangshangliao tests passed, with no skips**, including 78 command-parity
and authorization regressions. Conditional browser and rollback environments
were enabled. Page type checking/build and scoped Ruff checks passed.
Only the plugin was hot-reloaded; the shared process did not restart.
Eleven platforms remain running without reported errors. Both accounts are
online; 1468 financial records, formal-group settings, main configuration and
friend exception state are unchanged.

Deployment evidence:
`/opt/backups/wsl-welcome-repair-20261015T045351/evidence.json`.
390px/1440px dashboard verification:
`/opt/backups/wsl-account-names-20261015T045432/verified.json`.
The test group was not activated on the inviter's behalf; offline results do not
replace real-client acceptance.

## Numbered Text Tree (2026-10-15)

`菜单` / `帮助` now opens a layered text tree: numbers advance, `0` goes back,
and `取消` exits. Arguments are prompted separately and mutations require
confirmation. Member prompts use `99` for paging; `不限` means a zero
lottery limit/invitation threshold. Direct commands and backend grants remain
unchanged. Navigation binds account, real NIM sender, group and conversation,
expires after ten minutes and is capped at 4096 sessions. Menu numbers do not
also query invitations; revoked versions and abandoned previews cannot commit.
Read results cannot inject confirmation commands; group menus never solicit
activation codes.

Final full regression: **3228 passed, zero skipped**, including 104 added menu
cases. Ruff, page type checking and build passed. Only the WSL plugin was
reloaded; the main process did not restart. All 1468 financial rows/fields,
formal-group switches, main configuration and paused friend exception remained
unchanged. Enabled cohosted platforms stayed running.
Deployment evidence:
`/opt/backups/wsl-welcome-repair-20261015T053607/evidence.json`;
390/1440px browser checks:
`/opt/backups/wsl-account-names-20261015T053710/verified.json`.
No human activation was fabricated; the test group still awaits its inviter.
This run did not exercise the new menu step by step in the official mobile
client and does not replace the outstanding live acceptance below.

## Native Private Menu Check (2026-10-15)

Linxi sent native private messages to Dahao: **42 final steps passed**, verifying
ingress, accepted send receipts and replies received by the peer. Coverage includes
all eight branches, invalid numbers, back navigation, abandoned parameters,
cancelled-session digits, personal invitation queries and AI session entry/exit.
Unauthorized reads were denied or required group selection. No mutation,
sanction, removal or activation was confirmed.

The live check caught Markdown rendering changing `0. 放弃并返回` to option `2`.
Both confirmation screens now use tree prefixes; native replies preserve zero.
**282 focused regressions passed without skips**, including two new rendering
cases. The historical 3228-test suite was not rerun in this check. Only the plugin
was hot-reloaded; the shared process did not restart. All 1468 financial records,
tenant settings and sanction records remained unchanged. The temporary private
send grant was restored and the developer window closed. Model-call records,
the closed provider-budget gate and friend exception pause were unchanged.
All ten enabled platforms were running; formal groups stayed disabled.

Evidence: `/opt/backups/wsl-tree-menu-live-20261015T060534/verification.json`
and adjacent `messages.json`; deployment verification:
`/opt/backups/wsl-welcome-repair-20261015T060442/evidence.json`.
This is native messaging between controlled accounts, not phone visualization.
The test group still awaits activation, so in-group navigation and management
execution were not tested in this run.

## User Activation And Self-Reply Fix (2026-10-15)

The user's actual `开通` message from Linxi activated Dahao in test group1459282.
Linxi31846829 is the saved manager, backed by invitation211344713. This supersedes
the earlier pending-activation status; another activation message is unnecessary.
This group has prior history and does not prove first activation in a fresh group.

The misleading denial came from the Linxi instance processing its own activation
message before ordinary self-message filtering. Both candidate admission and
execution now ignore the receiver's own account. Four new regressions cover
known/unknown groups and both entry points; **400 relevant tests passed with no
skips**, covering native ingestion, lifecycle, activation and menus.

Activation does not disable bot-loop protection. Linxi remains a locally managed
robot account, so ordinary management commands from it are still blocked outside
controlled test windows. Current messages do not reliably distinguish phone-typed
and automated sends from the same account. Use dashboard management or a manager
account not configured as a robot. Existing authority was not transferred.
Dahao was a native ordinary member when checked; recall/mute testing still needs
native administrator privileges. Only the plugin was reloaded. The process,
financial fields, formal-group switches and friend exception pause were preserved.
Evidence: `/opt/backups/wsl-welcome-repair-20261015T063618/evidence.json`.

### Native Bot Administrator Gate And Private Isolation (2026-10-15)

Activation replies now distinguish a bound group from a usable bot. Until the bot
is a verified native group administrator, group commands, numeric menus, rule
changes, activities and sanctions return the administrator prerequisite notice.
Ordinary chatter generates no repeated notice. Moderation, activity workers and
schedules stop; configuration and history remain intact. Private menus, own
cashback queries and authorized group selection remain accessible.

Group listing and execution check the receiving bot account, verified private
identity and current tenant grant. Framework admin roles, quota exemptions,
another bot's grants, forged indices and revoked selections cannot widen scope.
The 122 new regressions and full Wangshangliao suite passed:
**3356 passed, no skips**, with three existing dependency deprecation warnings.

Plugin-only reload left the main process running. The final native Linxi-to-Daha
private check passed **18 steps**: only the test group appeared, the second index
was rejected, and direct/menu operations showed the same prerequisite.
The test bot remains an ordinary group member; no native privilege or sanction
was issued. An initial test's second command correctly hit the three-second
cooldown; waiting 3.2 seconds after each received reply produced the complete pass.
Temporary send grants were restored and the test window closed. All 1468 financial
records, formal-group switches and sanction records remained unchanged.

Deployment evidence:
`/opt/backups/wsl-welcome-repair-20261015T071002/evidence.json`.
Native private evidence:
`/opt/backups/wsl-tree-menu-live-20261015T071249/verification.json`
and `messages.json`. Group blocking and role restoration are offline regressions,
not mobile group visual acceptance. Previously unpassed acceptance items remain open.

## Required Cooperation

1. Create a new controlled group, invite the current Linxi robot, and provide
   its name and group ID. Preserve the genuine invitation and let the inviter
   send `开通`. Never delete an existing grant to simulate first activation.
   No new controlled group is currently available;1459282 has activation history.
2. Configure the intended public-model credential through the secure dashboard,
   not chat. Supply provider evidence binding that credential to an enforced
   spending limit, its period and a stop-calling effect. Balance, alerts and local
   daily request limits are not proof. Keep model admission off until verified.
3. Open1459282 in the official phone client and coordinate an observed automatic
   recall test. Confirm the original messages appearing and disappearing without
   manually recalling them. SDK evidence already covers all five duplicate and
   eight ordinary flood messages, but phone visualization remains unconfirmed.

**Overall acceptance is not approved. Formal groups are not auto-enabled.**
