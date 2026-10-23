# Wangshangliao Architecture Consolidation

## Current Implementation

Updated October 20, 2026 for the AI, knowledge and quota audit. Existing AstrBot accounts run integrated public mode;
no separate public worker is active. See
[current architecture and permissions](wangshangliao-architecture-permissions.md)
and [current acceptance](wangshangliao-acceptance-20261015.md).
All chat model tools are retired and private `/chat` is explanatory only.
Native @ replies, autonomous conversation and idle topics are separately opted in,
off by default at the group master switch, tool-free and exact-binding budget-gated;
they never restore old group-agent routes. Private use and consented groups share
one payer's plan and cost pool; legacy manager daily increases do not replace those limits.
Activation requires the genuine inviter's current
native owner/admin role; codes do not bypass it. Public grants use platform,
bot account and group, not automatic `admins_id` authority.

Lost bot privileges retain data; confirmed removal clears working data but
preserves finances and original ownership. Failures under the old relative P95
criterion remain historical; the user removed that release blocker on October 17.
Future reports use absolute latency, not a claim of stable SLA or real financial writes.
Older passing synthetic samples below are not final performance acceptance.
Stages after the current AI audit below are historical, not current
instructions to enable tools, grant authority or restart an independent worker.
The old AI configurator now only refuses execution; its writer and four
obsolete one-off scripts are removed. Retained maintenance/test tools are
[classified by risk](../../../scripts/README.wangshangliao.md).

## October 20 Redundancy Cleanup And Evidence Update

- Native `event.py` consolidates three equivalent `send_reply_text` call sites into
  one, preserving mutually exclusive `mentions`/`test_mid`, quoting, authorization
  and optional `auto_recall`. `policy.py` shares identical validation for the three
  keyword lists without changing limits or error codes.
- `cashback_store.py` only removes the unused private
  `_normalize_automatic_avatar_pricing` function. No old migration is executed, no
  historical financial row changes and no database is cleaned. Three production
  files shrink by 66 net lines, with 110 added regressions. The full related offline
  run passed 6564 cases, with 52 conditional skips and 3 warnings in 237.65 seconds;
  evidence is `/opt/backups/wsl-cleanup-20261020-IsoIYB/full-regression.xml`.
  Targeted 527/81/192 passes overlap and are not added together as a unique total;
  older 6429/52 and 308 results retain their original dated scope.
- All Wangshangliao management Vue files pass `vue-tsc --noUnusedLocals
  --noUnusedParameters`; no frontend item was removed without evidence.
- This cleanup changes source and rebuilds/publishes documentation only; it does
  not reload/restart services or send messages. The preceding three real warning
  checks are separate, using two fixtures of one and two, not complete escalation
  or phone acceptance; see [scoped evidence](wangshangliao-membership-testing.md).

## AI, Knowledge and Quota Audit: October 20

### Routes and Profiles

- Private `/chat` starts read-only consultation; `/exit`, cancellation or fifteen idle
  minutes end it. Account, authenticated sender and consultation role are scoped.
  Administrators have no AI action tools either. A new session is intended to exclude
  previous context, but the rejected-first-question gap below prevents an unconditional guarantee.
- Native @, autonomous replies and idle topics all resolve
  `profiles.resolve("group", account, group)`, never private `consultation`.
  They share the group persona, not members' mention histories.
  Master, mentions, autonomous and idle switches are separate; `mentions` is independent
  of `autonomous`. Saving a persona or enabling
  knowledge references does not enable AI. Factory master/autonomous/idle switches are off;
  mentions are on behind the master switch. Default hours are 09:00-23:00 Beijing;
  equal start/end hours mean all day.
- Models and personas are independent selections. Total control owns separate consultation
  and group defaults; account/group overrides apply only to the group profile.
  Referenced AstrBot configurations supply allowlisted model, persona, sampling, output
  and history options, not tools, skills, arbitrary request bodies or external agent runners.
  Persona example exchanges are style samples, not actual experiences or user history.
- The supplier gate currently stores one confirmed provider and model/address/credential
  binding, not universal approval for every model. Distinct personas may share it;
  selecting distinct model bindings does not make both routes callable. Changed models,
  addresses or credentials require matching gate and pricing verification.
- Autonomous input keeps at most twelve messages for 120 seconds, coalesced over two seconds.
  Commands, consumed violations, unverified identities and recognized financial/private
  text are excluded. Lexical filters and redaction are limited safeguards, not semantic
  privacy proof. Private and group calls share two service-level slots with no model wait
  queue; each payer pool permits only one in-flight reservation.
- Autonomous calls request `response_format={"type":"json_object"}` and validate silence
  or an exact candidate number, quote boolean and text. Only complete `json` or unlabelled
  fences are tolerated; arbitrary prose is not searched for a JSON fragment.
  Native delivery rechecks the selected source, identity and authority.
  Outcome logs contain a request digest, mode, state and reason, not message bodies.
  `completed` can mean `model_silent`, not a delivered group message.
- All group output is plain text without `【群聊AI】` or `【客服咨询】` prefixes.
  Private read-only AI is unchanged; group personas, member histories and legacy
  group-consultation content never become private context.

### Existing Runtime Evidence

This audit read only `/opt/backups/wsl-group-ai-20261020-format/runtime.json` and
`/opt/backups/wsl-ai-enable-20261020T0724Z/enable.json`. It made no new model/network calls,
test posts, restarts or runtime configuration/database/session changes.

- The former records an autonomous `auto` delivery from account `43506527` to group
  `1516387`, with a native **accepted receipt at October 20, 2026, 15:47:56 Beijing**,
  and two saved native dialogue messages. Earlier rejected outcomes remain evidence too.
  Its snapshot enables master, mentions and autonomous replies, disables idle topics,
  sets a 100-call additional group daily cap and all-day hours, and records confirmed payer
  consent plus readiness. Those are deployment settings, not factory defaults or proof
  that other groups have resumed.
- The latter selects `deepseek/deepseek-v4.1-flash` for both routes, with separate
  read-only consultation and Xiaoman social personas. Pricing and the supplier gate
  were confirmed for that exact binding. Saved evidence records `quota_limited`, `active`,
  a 1000 USD limit/remaining balance and the operator's stop-at-quota confirmation.
  Supplier units remain USD, not an inferred CNY conversion.
  **Supplier exhaustion was not deliberately tested.** This proves neither other bindings
  nor future supplier enforcement.
- The runtime file's 222 passing tests are an earlier result, not a rerun here.
  Phone display was not confirmed; native @, idle delivery and every failure branch were
  not reaccepted end to end by this audit. Do not declare all acceptance, other-group
  resumption, hard isolation or mixed-load performance passed.
  The October 14 pause statements in the historical section below do not override this
  specific later `1516387` AI evidence. Resumption of `1488006` or other business functions
  is outside these records' demonstrated scope.

### Plans and Cost Safeguards

- Code tiers are trial 20, Plus 2000 and Pro 5000 calls/day; displayed prices are
  Plus 20 USDT/30 days and Pro 50 USDT/30 days, not automated collection or upgrades.
  Total control previews and confirms actual CNY income; paid-cycle model-cost budgets
  are 70% of that receipt. Personal trial cost is capped at CNY 3/calendar month.
  Daily counts are ceilings, not a promise that cost budgets permit every listed call.
- Verified identity links private use and all self-consented groups to one pool.
  Controller nomination is not payer consent; the payer must remain a current group manager
  in the same membership generation. An additional group cap is a stricter limit;
  zero means no additional cap, not unlimited free calls. Exemption bypasses personal/site
  daily counts only, not monetary, group or supplier safeguards.
- Site trial spend is separate from paid-cycle spend; an exhausted free pool alone does
  not block paid plans. Factory trial-site allowance is CNY 20/month, while the site safety
  base starts at zero and blocks all calls until configured. Both were CNY 20 in the
  October 20 snapshot. The safety ceiling adds allocated budgets for paid periods overlapping
  that calendar month, including ended/upgraded cycles and unresolved holds; it is not
  one fixed CNY 20 free pool for every caller.
- Counts/dates use Beijing time; paid cycles last thirty days. Renewal, expiry and upgrade
  do not erase same-day usage. Integer micro-CNY reservations cover the attested full input
  ceiling and bounded output, then settle validated usage. Expired prices, changed bindings,
  missing cost bounds or anomalous usage prevent further admission. Invalid usage retains
  a hold and blocks that pricing binding. This depends on the operator's truthful billable
  bounds, not an independent verification of supplier invoices.
- Only provably unsent calls release reservations. HTTP errors, timeouts or unknown
  results do not imply free usage. Silence, invalid autonomous JSON and filtered output can
  still incur model cost. Unknown group calls/deliveries block automatic replay until
  reviewed and explicitly resumed. Quota notices go privately to the payer, not the group.

### Knowledge, History and Statistics

- Personal text belongs to the authenticated transport principal and is used only in
  that person's private consultation. Group text is exact-account/group scoped, excluding
  managers' personal spaces and other groups. Menu 8 -> 5 edits personal knowledge;
  9 -> 7 requires current group management. Writes use caller-owned preview confirmation,
  scope/version checks and revocation-aware authority.
- Saving/indexing makes no model call. Incremental FTS5 retrieval supplies at most five
  snippets and 2400 source characters. Each space permits 100 entries/1 MiB, each entry
  8 KiB for title plus body; site limits are 64 MiB/10000 entries.
  Group mention and proactive references are separate, off-by-default switches;
  autonomous and idle calls share the proactive-reference setting.
  References are untrusted data, never authority or a live ledger. Edits invalidate
  older knowledge snapshots used by generation.
- Confirmed-delivery sanitized text uses AstrBot's native conversations, not a second
  plugin history store. Persona examples, reference snippets, tool history and old mixed
  administrator dialogue are not recorded as new real-user exchanges.
  Native provider statistics count emitted HTTP requests without fabricated non-streamed
  TTFT or estimated missing tokens. Model completion, accounting and native delivery
  are separate outcomes.
- Native @ uses the new key
  `platform:GroupMessage:account/group/wsl-ai/account/group/mention/verifiedUid/peer`.
  Only that key supplies bounded `dialogue.history` allowed by the group profile;
  `history_turns=0` excludes earlier exchanges. Verified UID/peer, account and group
  isolate members' histories. Other members, groups, accounts, private chats and old
  group-consultation history are never mixed, migrated or reused.
- Autonomous/idle retain the original
  `platform:GroupMessage:account/group/wsl-ai/account/group` origin and do not read
  mention history. Autonomous candidates remain twelve messages/120 seconds with
  two-second coalescing; the profile truncates selected candidates, retaining the
  current candidate at zero without changing buffers. Native group records remain
  bounded to twenty turns, not a promise that every stored turn is model input.

### Findings and Verification Scope

The original audit findings remain below. Item 3 notes the later mention-route change;
documentation updates are not implementation regressions or production acceptance:

1. **P2: A rejected first question can restore previous private context.**
   `private_interaction.py:148` clears `fresh` before model admission.
   Rejection at `open_service.py:3108` or another pre-creation gate leaves the previous native
   conversation selected. The next question loads it with `fresh=False`, contradicting the
   fresh-consultation notice. Extracted production methods with in-memory session/conversation
   fakes reproduced this. Existing first-use and native-fresh tests cover normal paths
   separately, not the combined new `/chat` -> rejected first question -> successful next question.
2. **P2: Native history can lose an edit between validation and update.**
   The check and update at `native_conversations.py:148` are not compare-and-swap:
   an edit committed after validation but before the update can be overwritten by the old snapshot.
   An in-memory fake reproduced this with an independent write and asynchronous barriers,
   and verified that an edit completed before validation is preserved.
   Existing late-history tests change the record before `save`, not inside this interleaving.
   Separately, both AI routes check native selection/history before HTTP, not again before
   delivery. Existing documentation/tests promise pre-write checking, not cancellation on
   dashboard deletion, switching or editing; no explicit intentional policy was found either.
   Late delivery alone is therefore a contract question, not a separately confirmed P2 defect
   or evidence of an authorization bypass. It is distinct from the reproduced lost update.
3. **P2, pre-change finding: Group history options had inconsistent semantics.**
   The original AST audit of `group_ai.py:570`, native `load` and payload construction
   found candidate truncation without previous-turn group input. Native @ now uses
   the separate key and bounded configured history above, with autonomous buffers
   unchanged. The old reproduction is not evidence against the new mention payload.
   Implementation regressions must still confirm zero/one/multiple turns, isolation
   across members/groups/accounts/private chats and no legacy-history reuse.
   Configuration success or dashboard visibility is not multi-turn acceptance.
4. **Unmeasured performance risk:** major knowledge writes, reservations and settlement
   use workers, but admission/delivery guards still call synchronous profile, knowledge-version,
   payer and SQLite reads (`group_ai.py:655`, `open_service.py:3213`).
   Not all AI database IO is asynchronous. Slow-disk/lock-wait delay was not measured here.

The audit read all eleven named implementation modules and relevant group/autonomous,
profile, native-conversation, statistics, scoped-knowledge, plan/budget-split, guidance,
first-use, price-expiry and AI-audit tests. AST syntax checks covered eleven implementation
and fifteen related test files, with application-import-free in-memory/structural checks
for the paths above. Extracted production cost/JSON functions and the existing cancelled-waiter
lease test also passed fixed-point arithmetic, invalid-money, valid/silent/malformed-envelope
and cancellation checks. No pytest runner/full suite, network or production actions ran.
Reading existing test coverage is not the same as executing it in this audit.
The extracted-method script, steps and results are outside the repository at
`/opt/backups/wsl-docs-audit-20261020/ai-review/`. They use in-memory fakes, not
actual SQLite scheduling or evidence of production data corruption.

## Historical Merged Acceptance

As of October 14, 2026 (Asia/Shanghai), existing AstrBot accounts run the merged
service. Natural-language administration, group AI and administrator model tools
described in older sections are historical, not the merged-mode behavior.
Private `/chat` and `/exit` control read-only consultation; fixed commands or the
authenticated dashboard own all mutations.

`scripts/check_wsl_integrated_live.py` is restricted to the explicitly authorized
test group `1459282`, two existing managed accounts and one authenticated ordinary
test sender. It checks a complete native roster: one bot owns the group, the other
is an ordinary member. Business grants do not confer native moderation powers.
The default invocation is a dry run; `--apply --full` issues real, reversible
test actions. `--apply --phase management` repeats only that phase.

Separate live runs verified code claims and replay rejection, manager confirmations,
last-manager protection, revoked confirmation rejection, account-scoped rules,
literal keywords and unmentioned group commands. Accepted 5/15/60-minute sanctions
were checked against actual rejected ordinary sends and subsequent recovery.
Both flood thresholds produced all 13 exact peer recall notifications.
Member/group mute, native notice-list readback, reversible member cards, real future
minute boundaries, ordinary lottery enrollment/deduplication, drawing and cancellation
were verified. Real tool-free model help and prompt/code injection caused no
management writes; exiting consultation and ordinary group text made no model calls.

The latest `20261014T044737` complete run contains 102 live test records, zero failed
phases and no restoration errors. Whole-burst peer recall notifications finished
1.768 s and 2.526 s after the triggering send acknowledgment; these are not local
rule-decision timings. This run retained accumulated sanction counters and used
the 60-minute tier; earlier native runs separately prove first warnings and the
5/15-minute tiers. Counters were not reset to manufacture new first violations.
With browser and old-storage rollback fixtures enabled,
in-scope Python regression passed all 2,873 tests without skips, and frontend
regression passed 65 tests. Three dependency deprecation warnings remain.

Live testing exposed and repaired mention eligibility gates in group audit,
authorization and writes, account/group revision confusion in flood enforcement,
stripped `/chat` prefixes, missing native-permission error replies and dashboard
persistence of effective tenant overlays into the native base. Harness repairs
separately address duplicate logical/segment receipt accounting, selection refresh,
the private winner-query route and restoring empty factory activity settings.
Negative code tests and valid claims now leave additional cooldown time. Fixed,
payload-free rejection labels never include activation codes or credentials.

Private evidence includes restoration snapshots. Only unchanged test-owned state
is restored; history, sanction receipts and audits remain. No whole database is
rolled back, and cash amounts, attribution or settlement fields are not edited.
Unknown actions must not be retried. The sender session is closed after cleanup.

The merged synthetic load completed 3,000 messages across 20 simulated groups at
100 messages/second with two deliberately slow model requests. Rule P95 was
0.392 ms and durable ingress P95 1.423 ms. Read-only production ledger query P95
rose from 14.777 to 15.662 ms (5.99%). These are not native 20-group or cross-platform
peak acceptance, nor hard resource isolation.

Fresh native inviter activation/rejoining, unexpected disconnect boundaries,
provider hard-budget verification and official mobile visual recall confirmation
still need separate evidence. A peer recall notification is not a mobile screenshot.
No fresh friend request was issued in this run. Default-on auto-approval and its
30-second serial interval have offline coverage, not a new native acceptance receipt.
The old Dahai part-time group previously reported `verification_unavailable`.
On October 14, the user explicitly retired it: group `1143980` and its 19 ledger
entries, 20,810 audit rows, rules, messages, schedules, activities, member-card
records and group conversation history were removed from active local stores.
Other-group historical financial fields remain intact, and private pre-deletion
backups remain available. Only a nameless group-ID retirement marker prevents
directory discovery or legacy policy fallback from restoring it. This does not
delete the upstream platform group or phone chat history. The retired group will
not be migrated.

On October 14 the user subsequently requested suspending all functions in
`1516387` and `1488006` until complete controlled-group acceptance. Both account
bindings for each group are paused, both cashback collectors are disabled, and
the groups remain paused after testing. Original settings and historical ledgers
remain available: comparison against the pre-pause snapshot found all 1,468
financial rows unchanged, including amounts, attribution and settlement fields.
The private restoration point is
`/opt/backups/wsl-pause-two-groups-20261014T044514`.
Pause isolation now excludes paused/disabled groups from collection, makes disabled
scans perform no platform calls, and rechecks group availability during financial
write transactions. Pausing during a write rejects it; rejected-operation audits
are not blocked by the invalidated write guard. Historical reads remain available.
Production groups will not resume before the mobile visual and fresh invitation/
rejoining evidence gaps are completed.

## Historical Private Role Boundaries

On October 9, administrator management and public customer service gained
account/session/role-scoped persistent histories without changing native reply
addresses. Previous mixed histories remain archived and are not imported automatically.
Customer private chats and every group AI route permit only the genuine built-in
configured knowledge reader, not same-name MCP tools, code/filesystem operations,
other plugins, messaging, background jobs or delegation. Administrators may use
only fixed WSL management and that reader. The executor rechecks current authority,
business lifecycle and event version through native event hooks; other platforms'
tool policies are unchanged.
Knowledge selection uses the same role-scoped session key while configuration still
resolves from the native address. Mid-generation revocation blocks old administrator
output. Per-request random final markers and JSON validation discard external analysis;
history stores the validated final text. Invalid envelopes fail closed, without
guessing which prose paragraph should be displayed.
Only the terminal current-nonce envelope is delivered; earlier drafts, analysis
and stale markers are discarded. Trailing prose or invalid JSON still fails closed.
Production WSL uses its own persona with the fixed WSL
management whitelist, leaving the Lajiaoliao shared persona unchanged. Customer
requests still reject management during construction and execution. Saved settings
use the server receipt, never a model claim or persona identity, as success evidence.
`/new` and `/stats` use the current role key without resetting archived mixed or
other-role histories.

Administrator AI now queries lotteries, invitations and schedules. Immediate drawing,
cancellation, invitation tracking/point reward switches and schedule maintenance use
one-use preview confirmation with a target-state check under the original group lock.
The final schedule token must appear exactly in a fresh authenticated user message.
Unknown results remain unknown; chat AI cannot change cash amounts, register payments,
grant administrators or modify credentials. Isolated executor/injection/history
tests are distinct from separately recorded production model/private-chat acceptance.

## Release Status

This staged refactor began on 2026-10-08. Implemented code, isolated acceptance and production deployment are separate states.
Shared restarts on October 9 at 07:24, 08:03, 08:59 and 09:17 UTC loaded the role/executor hooks;
later changes use relevant-plugin reloads. Statements below about source-only or
not-yet-restarted batches are historical. Current live outcomes and limitations are
in [acceptance](wangshangliao-acceptance-20261008.md). In-scope regression is 2282
passed without skips, not independent-extension delivery or OS hard isolation.

The initial changes cover ingress-time flood evidence, per-member dispatch, bounded model work, directory caching, review database workers and indexed card-job queries. The standalone cashier and Unix Socket were deployed on 2026-10-08 at 11:55 UTC. Independent extension packaging remains incomplete. Other installations without an explicit mode change retain the embedded lifecycle.

The October 8 follow-up added an independent flood lane for committed ingress
evidence, bounded to four tasks/256 queued triggers per account with frozen authority
checks. Live first/all recall notifications took 0.976/1.712s and 1.262/2.625s:
13 exact notifications, with production AI enabled. Live 60-minute progression,
ordinary warning counters, group-wide enforcement, ordinary enrollment/automatic
drawing and actual schedule boundaries also passed. Roster-name fallback and scan
diagnostics were repaired with WSL-only hot reloads. Final regression is 1711 passed,
no skips; frontend 65 passed. See [current acceptance](wangshangliao-acceptance-20261008.md).
Independent packaging/core decoupling remain open; this lane is not hard CPU,
memory or quota isolation.

After 21:38 UTC on October 8, shared waking now calls generic
`prepare_waking_check`; WSL private permission/command handling lives in its native
event. Dashboard login/management/configuration bodies moved into
`core/platform/sources/wangshangliao/dashboard.py`, retaining original delegating
service entry points, owner-bound one-time claims, rejection codes, disk rollback
and the original configuration writer. Native rejection diagnostics no longer
import the built-in moderation package. This batch is source/isolated acceptance
only, without a production restart. Bootstrap, defaults, other shared branches
and extension delivery still require follow-up. Read-only invitation evidence and
the real revoked-creator boundary are recorded in current acceptance; isolated
tests do not replace live rejoining, disconnect or Access operator workflows.

On October 9, builtin help's direct WSL business import was also removed. Generic
event dispatch delegates to the native event and its extension-registered responder;
unload clears the callback, preserving generic behavior on other platforms.
These source changes are not loaded by a shared production restart or delivered
as an independent package. At 02:24 UTC a controlled single-receiver stop/recovery
crossed a real deadline and paused without catch-up; the rule and temporary grants
were restored, with other connections unchanged. This is not an unexpected network
fault test. Live invitation/departure evidence passed; rejoining and Access
operator acceptance still need separate proof.

## Standalone Cashier

The October 9 sandbox fix switches low-write `settings.sqlite3` to DELETE journaling,
without changing cash/message WAL stores or expanding write permissions. Scope-read
failures now report errors instead of an empty group list. Production conversion
restarted only the standalone cashier; reproduction, all 415 retained pre-fix ledger
records and browser recovery are recorded in [acceptance](wangshangliao-acceptance-20261008.md).

- `access.json` defaults to `service_mode: embedded`. In `standalone` mode the plugin runs collection and a private Socket only; the independent process owns port 6186. Do not run both HTTP services simultaneously.
- The cash data directory contains `rpc/cashier.sock`, with directory mode 0700 and Socket mode 0600. Both processes use the same OS account. Live sockets and symbolic links cannot be overwritten.
- RPC revalidates the original Access JWT and resolves the actor from the ledger. Payloads cannot supply a role or actor; only fixed cashier operations are exposed, never generic administration APIs.
- Before transaction commit, guards recheck plugin state, policy generation, snapshot and token expiry. Disabled or stale operations fail. Existing payment idempotency and version checks remain intact.
- When the plugin is offline, authorized history remains readable. The API rejects payment registration, corrections and rule changes; the page shows read-only mode. After a connection failure, check the existing registration before making another manual payment.
- Tests exercise real Unix sockets, duplicate registration, mid-flight revocation, rollback and offline reads. Desktop/mobile tests use isolated ledgers to verify read-only controls and recovery, without production payments.

The deployment template is `services/wangshangliao-cashier.service`, rooted at `/opt/astrbot-prod`. Deployment order: verified backup, frontend build, standalone configuration, plugin restart to release 6186 and create the Socket, independent service startup, then Access/read-only checks. The production unit is installed and enabled. Socket directory/file modes are 0700/0600; Tunnel and Access policy are unchanged.

Sandbox startup initially exposed indirect AstrBot core imports through logging and redaction, causing writes to protected backend log files. Redaction now lives in a dependency-free shared module and runtime-only imports are delayed. The standalone process does not initialize AstrBot Context. Actual sandboxed process startup, unauthenticated rejection and graceful exit pass in an isolated network namespace.

Cashier read endpoints and progressive reservation/warning/outcome transactions now run on workers. Reservations use `BEGIN IMMEDIATE`; identity and authorization are rechecked after database waits. Automatic mute execution, activity and scheduling repositories still have synchronous paths; this is not a complete IO migration.

Deployment backup: `/opt/backups/wsl-cashier-independent-20261008`, containing 31 configuration, credential and ledger files with SQLite integrity checks. After deployment both robots are online, 20 checked historical baseline/credit/payment records are preserved, unauthenticated requests return 401, and desktop/mobile management pages pass all 12 tabs. No production payments or sanctions were issued. Authenticated production Access registration/offline flows still require manual acceptance; signed fixture JWTs and isolated ledgers cover those paths in tests.

For rollback, stop the independent service before restoring embedded mode and reloading the plugin. Preserve all current ledger data. Existing Cloudflare routing, Access audience and domain remain unchanged.

## Initial Changes

- Deduplicate by account, group and message before the registered plugin observer enriches a trusted envelope. Commit its content, sender lane and initial routing state together before acknowledging receipt.
- Group messages are ordered per real sender; private sessions retain their original ordering. Twelve workers serve ordinary messages and four are reserved for fixed commands. Classification applies only after selecting each lane's oldest message, so commands cannot overtake their sender. Backlog remains in the inbox.
- Review concurrency is four per robot with a default twelve-second whole-review deadline. Customer concurrency is two per robot, with a conversation lease acquired before history loading and released at final event completion. Violations cannot enter customer chat.
- Flood thresholds are three messages in five seconds, eight in ten seconds, or five equal normalized texts in thirty seconds. Both trusted event time and receive time are checked. Events delayed over ten seconds or over two seconds in the future are not live flood evidence.
- Flood sanctions attempt the mute first, then recall every authenticated burst message,
  bypassing the ordinary three-recall grace period. A continuing burst gets one mute;
  later spam messages are still recalled. Durable batch and individual receipts prevent
  blind retries of unknown outcomes. Unrelated members and historical messages are excluded.
  Plugin `keyword_defaults.json` seeds missing fields in new policies, preserving custom
  lists, legacy keywords and explicitly empty lists.
- The former "Removal keywords" control is now "High-risk keywords (no auto-kick)",
  with a matching audit category label. Persisted `kick_keywords`, historical evidence
  and the automatic-removal prohibition remain unchanged.
- Windows retain at most 4096 members and 256 digests per member. Plugin reload restores recent durable live evidence from the last thirty seconds without resetting sanction history.
- Ordinary reviews may reuse a complete directory for fifteen seconds. Action execution still forces verification. Concurrent refreshes coalesce; failure invalidates usable cache without inventing a complete roster or deleting known identity mappings.
- Semantic/content review IO runs on database workers, initializing each repository schema once. Other synchronous repositories, including progressive execution and activities, still require follow-up migration.
- Card jobs are indexed by account, group and state. Empty automatic scans do not create durable jobs; uncertain outcomes still block duplicate mutations.

## Metrics and Reproduction

Robot statistics expose `performance`: rolling P50/P95 over at most 512 samples, total sample counts, loop delay, durable backlog, oldest queued age and worker count. No message text, payment details or credentials are included. Memory samples reset on restart.

```bash
.venv/bin/python -m pytest tests/test_wangshangliao_*.py -q
WSL_SOAK_SECONDS=1800 .venv/bin/python -m pytest \
  tests/test_wangshangliao_runtime_performance.py::test_transport_soak -q -s
```

The soak uses temporary storage, ten simulated groups and twenty messages per second in total. It exercises the real inbox and native dispatcher against an immediate simulated pipeline. It does not benchmark real models, ranking queries, network delivery or payment workflows. Separate tests cover slow reviews, member ordering, conversation exclusion, database contention and directory invalidation.

The initial 2026-10-08 thirty-minute baseline completed 36,000 messages with 36,000 unique completions. Dispatch P50/P95 were 1.817/3.969 ms; loop-delay P95 was 1.579 ms. This run started before the twelve-ordinary/four-command reservation change, so it does not certify the final scheduler for thirty minutes. Raw output is in `/tmp/wsl-refactor-soak-20261008.log`; these figures are retained here because temporary logs may be cleaned.

The final scheduler passed another thirty-minute run: 36,000 messages, 36,000 unique completions, dispatch P50/P95 2.312/5.529 ms, loop-delay P95 1.665 ms. Raw output: `/tmp/wsl-refactor-final-soak-20261008.log`. This remains a simulated immediate-pipeline scheduling benchmark, not complete command, AI or payment latency acceptance.

Full regression: 1379 passed with no skips. The previously skipped test was `test_rollback_ledger_accepts_migrated_database`, requiring `WSL_ROLLBACK_STORAGE`. It now passes against `storage.py` from Git baseline `e7bcab9`, opening and writing the migrated isolated message ledger. This does not certify every old business implementation against new configuration.

## Resumed Automatic Removal Regression

On 2026-10-08, work resumed from the interrupted automatic escalation fix.
The executor no longer submits automatic removal, and `authorize_kick`
unconditionally rejects legacy removal intents. Historical tables and mute counts
are preserved. Legacy `auto_kick=true`, restarts and duplicate messages cannot
restore automatic removal. Tests cover these cases, revocation and the complete
semantic sanction path. The separate private manual confirmation interface remains.
This is source-level and isolated regression work, not confirmation of a production
restart. Cashier actions record a batch as settled; they do not transfer money.
The resumed full regression passed all 1379 tests with no skips, including the
old-ledger compatibility check. Changed Python files pass Ruff. A database worker
shutdown warning occurred in the first full run but did not recur on rerun;
the final run reports nine dependency deprecation warnings. No production ledger
entries or real group members were modified by this acceptance run.

## Cross-Platform Acceptance

Additional coexistence checks on 2026-10-08 made no production configuration
changes, enabled no disabled bots, and sent no messages or sanctions.

- Saved configuration enables two Wangshangliao and three native Telegram bots.
  The created Lajiao Chat instance is disabled; other Telethon services are
  separate entries. This is not a live three-platform concurrent load test.
- All three platforms feed the shared AstrBot event queue, EventBus and Python
  event loop. The current Lajiao Chat plugin uses an independent bridge child
  process, but its business pipeline still runs in the shared main process.
  The old built-in adapter is not the current plugin entry point.
- Wangshangliao has twelve ordinary and four command workers per instance.
  The plugin Lajiao Chat adapter admits at most thirty-two unfinished events.
  EventBus has no cross-platform concurrency ceiling; Telegram does not use
  these completion-based admission limits. Upstream model quotas may still
  be shared when bots use the same provider credentials.
- Fourteen new tests in `tests/test_platform_coexistence.py` pass: asynchronous
  backlogs for each platform with shared and distinct profiles, exception and
  cancellation isolation, saturated Wangshangliao customer slots with another
  account/platform progressing, and worker-owned database contention. These
  use the real EventBus with synthetic events and simulated handlers, not live AI.
- The combined Wangshangliao, Lajiao Chat, Telegram adapter, instance policy
  and coexistence suite passed 1495 tests without skips, with six dependency
  deprecation warnings. The old-ledger test uses the actual Git baseline.
  This run did not repeat the thirty-minute soak.
- A read-only production stats snapshot showed both Wangshangliao bots online
  and three Telegram adapters running. Both Wangshangliao bots had zero pending
  messages and workers; event-loop P95 values were 1.869 and 1.884 ms. This is a
  low-load snapshot, not end-to-end response latency or peak capacity.

At that checkpoint, remaining synchronous SQLite paths in automatic sanctions, activities and
schedules can still block the shared loop under contention or slow storage.
There are no cross-platform hard resource quotas. Real three-platform load,
provider throttling, bursts, CPU/disk pressure and prolonged mixed workloads
remain unverified. Extension packaging alone does not create process or resource
isolation, and this acceptance does not complete the refactor.

## Wangshangliao-Only Database Continuation

The next source and isolated-test changes on 2026-10-08 affect Wangshangliao
only. Production was not restarted; Lajiao Chat and Telegram were not modified.
No live sanctions, group announcements or settlement changes were performed.

- A worker-owned SQLite transaction interface keeps connection, cursor,
  commit and close on one thread. Writers reserve the database before reading;
  cancellation drains in-flight queries and rolls back pending work.
- Async entry points for automatic mute intents/results, moderation operation
  ledgers, scheduled group mute, lotteries and invitation tracking now use
  worker transactions, preserving existing paths, tables and history.
- Account, business generation and applicable authority are checked after lock
  admission and before commit. Moderation checks authority again before the
  upstream mutation; received outcomes remain auditable after revocation.
- AI activity preview reads run on workers. Confirmation binds the original
  admin session and rechecks authority, settings revision and lottery state
  after acquiring the writer, preventing stale proposals from replacing changes.
- Added contention, revocation, account-change, cancellation and concurrent
  configuration tests validate source behavior using isolated databases,
  not production peak capacity or live end-to-end group workflows.

The full Wangshangliao suite passed 1402 tests without skips, including legacy
ledger rollback compatibility and 23 added cases. Ruff checks passed. The full
run emitted nine dependency deprecations and one `aiosqlite` worker warning
about returning to a closed event loop. A 129-test targeted rerun of native
adapter, developer gate, test window and new transaction tests passed with
thread warnings promoted to errors; the warning did not recur. Its cause remains
unresolved, so this is not a claim that resource teardown has been fixed.

Dashboard and card-job synchronous paths, versioned transport/business actions,
independent extension packaging, production Access acceptance and realistic
mixed-load acceptance remain pending. This phase is not a claim of fully async
database access or completed independent delivery.

## Dashboard and Card Database Continuation

The next Wangshangliao-only changes on 2026-10-08 complete the dashboard and
card hot-path migrations identified in the preceding checkpoint:

- Dashboard audit and schedule queries use worker-owned transactions. Activity
  snapshots and operational log reads/writes no longer run directly on request
  coroutines.
- Activity saves and schedule pauses revalidate authorization after acquiring
  the writer, then check saved configuration, account, adapter and plugin state
  before commit. Activity revisions are checked under the lock. Changed accounts
  cannot receive stale query results.
- Card preview, status, start, stop, progress and recovered execution use async
  database interfaces; adapter pre-execution callbacks support awaiting.
  Unknown results are not replayed, receipts cannot revive stopped jobs, and
  account/private-admin bindings remain enforced.
- Manual kick previews/one-time confirmations, violation count queries and the
  legacy keyword ledger also use worker transactions. Confirmation records,
  historical counts and paths remain intact. The stale fourth-violation kick
  message now states that automatic handling does not kick members.
- Added 26 cases covering blocked reads/writes, account changes, unload,
  concurrent settings edits, cancellation, admin revocation and stale keyword
  policy execution.

The full Wangshangliao suite passed **1428 tests without skips**, including
legacy rollback compatibility. Changed Python code passes Ruff checks.
The final run emitted nine dependency deprecations; the earlier database worker
teardown warning did not recur, but its root cause has not been fixed.

Tests use isolated databases. Production was not restarted, other platforms
were not modified, and no live sanctions, announcements, settlements or transfers
were performed. Removing direct synchronous SQL from these identified entry
points is not a complete filesystem IO audit, hard CPU/disk isolation or
peak-capacity acceptance. Versioned actions, independent extension packaging
and full production acceptance remain unfinished release gates.

## Initial V1 Action Contract Adoption

On 2026-10-08, `actions.py` and `adapter.execute_action` add immutable requests
bound to version, instance, account, business generation, policy revision and
the existing operation ID. Unsupported versions, invalid targets and stale scope
fail before submission, without implicit downgrade. Receipts retain the original
account and operation; unrecognized states become `unknown`, and revocation does
not discard already returned upstream evidence.

Scheduled group mute and card jobs now use V1. Schedule claims remain inside the
original group lock, and card/cleanup requests retain frozen preview identities.
Native role, capability, manual confirmation and ledger checks remain intact.
Asynchronous business callbacks recheck account generation after waiting.
Legacy entry points remain; ordinary commands, content rules and automatic
escalation callers have not all migrated. This is not RPC, process isolation or
a completed independent extension package.

Added 42 cases cover fixed action routing, malformed versions/parameters, stale
bindings, generation changes during callbacks, locked schedule claims, unknown
receipts, real native pre-submission checks and offline metrics isolation.

Temporary connection tracing reproduced the earlier teardown issue: offline
reply tests invoked shared `Metric.upload`, opening the global statistics pool
across test event loops. Only `test_wangshangliao_*` tests now set
`ASTRBOT_DISABLE_METRICS=1`, preventing global statistics writes and external
uploads without changing production metrics or other platform behavior.
With thread warnings promoted to errors and connection tracing enabled, the full
suite passed **1470 tests without skips**; unclosed `aiosqlite` connections after
individual test teardown were **zero**. Three dependency deprecations remain.
This fixes test isolation, not production connection-lifecycle acceptance.

Production was not restarted and no live sanctions or settlements were changed.
Remaining work includes other business action callers, adapter-owned business
dependencies, platform-specific core branches, then independent installation,
upgrade, rollback and production acceptance. Archiving the current directories
alone would not constitute independent delivery.

## Continued V1 Action Adoption

On 2026-10-08, legacy keyword moderation, content review, automatic progressive
mutes and fixed administrator commands (including privately confirmed manual
removal) adopted frozen V1 requests. Scheduled actions and card jobs retain
their earlier V1 routing. Native execution still validates platform roles,
group capabilities, member identity and the operation ledger. Administrator
authority is rechecked before a delayed native submission; revoked actions
cannot submit new upstream requests. Both V1 and the legacy native action
entry point reject retired automatic-removal operation namespaces, regardless
of the old `auto_kick` setting. The displayed rules no longer promise removal
on the fourth violation and describe the actual progressive warning sequence.
The violation-count display no longer implies a three-mute removal threshold.
Violation recall and rule warnings still use separate native send capabilities
outside the V1 moderation action contract.
The full Wangshangliao regression passed **1483 tests without skips**,
including legacy ledger compatibility. Ruff and diff whitespace checks passed;
three dependency deprecations remain. No production payments, live sanctions,
instance restarts or real cross-platform load tests were performed.

These are source changes and isolated tests, not a deployed production update.
Independent packaging remains incomplete: platform registration, dashboard
configuration/registration services, the business plugin and the adapter still
depend on core Wangshangliao paths. A directory archive does not verify
independent installation, upgrades or rollback. Authenticated production Access
registration/offline acceptance, realistic mixed-platform peak load, test-group
end-to-end operations and post-restart production acceptance are still pending.

## Recall And Warning Boundary Completion

On 2026-10-08, remaining violation recalls and rule warnings adopted V1:

- `recall` and `warning` freeze the message, original account, expected NIM
  identity and operation ID. Recall preserves its exact historical idempotency
  key; warning mentions use UTF-16 offsets. Neither exposes arbitrary wire fields.
- Native recall rechecks authority, complete membership, connection and routing
  after directory, send-lock and ledger waits. Warning rechecks business authority
  inside the lock and after reservation. Revoked requests cannot submit new
  actions; completed receipts and self-recall records retain the original account.
  Timeouts and cancellations remain `unknown` and cannot be replayed.
- Content observation, claims and completion retain the original account.
  Directory/database/recall waits recheck the original policy snapshot.
- Manual group mute overrides now pause schedules in worker-owned asynchronous
  transactions. Writer admission and commit recheck the original account,
  revision and capability; revocation or cancellation rolls back the pause.
- All logical reply segments retain one account, connection, ledger and route.
  A change between segments stops remaining text without discarding accepted
  child receipts or replaying an uncertain partial reply.
  An unreachable legacy automatic-removal branch was removed; removal remains denied.

New cases cover native waits, asynchronous callbacks, original-account evidence,
cancellation, unknown outcomes, segmented replies and acceptance scope validation.
Added **91 cases**; the full Wangshangliao suite passed **1574 tests without skips**,
including old-ledger rollback compatibility, with thread exceptions promoted
to errors. Three dependency deprecations remain. Ruff formatting, linting and
diff whitespace checks passed for this stage's Python changes.

### Browser And Read-Only Production Checks

- Isolated desktop/mobile cashier flows passed search, batch confirmation, three
  temporary-ledger payment registrations, per-group contact isolation, concurrent
  update recovery, offline read-only recovery and revocation. Screenshots were
  inspected; no overflow or JavaScript errors occurred. No production payment
  records were written.
- Both currently loaded production bots are online. The deployed management
  iframe opened all 12 tabs on desktop/mobile. Read-only APIs and rejection of
  unauthenticated/non-collector requests passed.
- A 60-second native-dispatcher soak used a fake upstream/pipeline, ten groups
  and 20 messages/second. All 1200 messages were unique; dispatch P95 was
  42.98 ms and event-loop lag P95 was 4.02 ms. Shared-bus platform backlog,
  failure, customer-slot saturation and WSL database-lock coexistence checks
  passed. These do not certify real mixed-platform peak capacity, hard CPU/disk
  isolation or model throughput.
- Strict migration comparison against the stopped backup failed because the
  main account's current `moderation` and `ai_routes` differ. Live settings were
  not overwritten and migration was not declared passed. Separate read-only
  comparisons preserved all 20 historical baseline/credit/payment records and
  database integrity. `check_wsl_deployment.py --runtime-only` explicitly skips
  migration/history comparisons, rejects `--hot-save`, and retains the original
  strict `--backup` mode.

Production checks reflect the already loaded version, not this source update.
Production was not restarted; no Lajiaoliao/Telegram changes, new live sanctions,
group announcements or payments were performed. Independent packaging,
authenticated production Access flows, real test-group operations and
post-restart acceptance remain incomplete.

## Remaining Release Gates

This stage's source was loaded at 2026-10-08 16:35:49 UTC.
The latest run passed **1578 WSL + 14 coexistence tests**. Live evidence now covers
announcements, member/group mute-unmute, card change/restoration, dialogue
revocation, activity grants and future schedule management. See
[production source acceptance](./wangshangliao-acceptance-20261008.md#production-source-load-on-october-8-utc)
for scope, first-run model latency, harness corrections and the subsequent shared
restart. These checks supersede unloaded-source/test-group-operation gates, not
all remaining release requirements.

1. Migrate remaining synchronous IO and test command latency under saturated model load, real conversation history, reload, revoked grants and stale actions.
2. Management actions, recalls and warnings now use V1. Move remaining progressive/scheduled business dependencies out of the adapter, then remove platform-specific core branches. Preserve unrelated core changes.
3. Package separate transport/business extensions without changing identities or data paths. The independent cashier is deployed; manual production Access read/offline-flow acceptance remains pending.
4. Validate each phase in a test group before production. Use additive database migrations; never overwrite newer invitation/payment history during rollback.

The pre-refactor backup is `/opt/backups/wsl-before-refactor-20261008`. Restore drills must use isolated copies rather than overwrite live ledgers.
