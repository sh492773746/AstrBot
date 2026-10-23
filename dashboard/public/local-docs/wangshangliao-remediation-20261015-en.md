# Wangshangliao Remediation Acceptance: October 15, 2026

> Historical remediation, annotated October 20, 2026. "Current" and "this run" below
> refer to the dated revision, not runtime guarantees today.
> See [current architecture and permissions](wangshangliao-architecture-permissions.md).

## Scope

The existing integrated AstrBot and signed-in accounts remain in use.
This page records this revision's code, offline checks and deployment after the
user confirmed a maintenance window, not a replay of historical live evidence.
On October 15, 2026, AstrBot loaded the fixes at 11:05:42 UTC and the standalone
cashier at 11:06:22 UTC. No live group-management actions were issued.

Formal groups `1516387` and `1488006` remain paused even after passing tests.
The public-model budget gate stays closed. Friend deletion remains removed,
and existing uncertain friend-approval holds are not silently cleared.
Historical financial review is read-only: no bulk price, attribution, payment,
freeze or clawback operation is performed.

## Fix Matrix

| Item | Implementation and verification boundary |
| --- | --- |
| Real group identity | Native trusted sender addresses construct identity; missing identity denies sensitive operations without breaking ordinary dispatch |
| Private isolation | Receiving account, real sender and current authorized group bind listing, selection and execution |
| Manager commits | Admission and commit check exact group version, membership, native authority, departure and pending suspension; old confirmations expire |
| Authorization cache | Compatible writes refresh or invalidate authority after commit; cancellation cannot preserve usable stale grants |
| Departure cleanup | Only explicit business-group namespaces match; finances and minimal audit survive, with no broad raw-transport-ID deletion |
| Execution barriers | Guards cover database, HTTP lock, send lock and every fragment; revocation while waiting stops unsubmitted work |
| Advertising and activities | Advertising binds its rule version and toggle; activity announcements/results bind the creator's current authority |
| Receipts | Definitely unsent, accepted and uncertain are distinct; failed readback cannot erase acceptance, and unknown sanctions are not replayed |
| Reply throttling | Menu, navigation, denial and exception replies share the three-second silent cooldown; cancel and `/exit` change state immediately |
| Message fragmentation | Full serialized size includes identity, client metadata and native mentions, including multibyte text and lists |
| Membership history | Invitations, departures, kicks and voluntary joins persist; old invitations cannot override later admission cycles |
| Per-record source faults | Quarantine and audit preserve other records; unattributed and voluntary joins do not contribute payable amounts |
| Debt evidence cycles | New collection baselines are separate from existing debts; metadata is added only from reliable proof |
| Price confirmation | Server preview binds entry/rule versions, original/target amounts and digest; stale clients are rejected |
| Friend backoff | Welcome observation and auto-approval share durable read backoff with actual HTTP timeout/retry handling; uncertain writes are not resent |

Friend pacing additionally retains the pre-dispatch intent and persists the
completion timestamp after success, failure or cancellation. Subsequent requests
wait at least three seconds and approvals thirty seconds. Long shared-HTTP-lock
waits cannot consume that cooldown early, and no sleep is added inside that lock.
Twelve barrier/virtual-clock regressions cover slow calls, failures, repeated
cancellation and restart.

Only a backend administrator may register an external payment already made,
with both explicit actual-payment confirmation and a reason.
Waiting, speech and departure conditions may be waived for registration, but
recipient identity, confirmed attribution, price, risk and idempotency may not.
Original qualification evidence and exception reasons remain audited.
Responses and paid rows identify exception registration; withdrawal does not
revive invalidated reward qualification.

Browser checks found that exception labels compared two independently sampled
timestamps. Classification now precisely matches structured payment evidence
from the same entry's latest payment audit, without a tolerance window.
Real-clock regressions cover pagination, reader reuse, withdrawal and re-registration.
Cross-review also repaired accepted announcements being misclassified by later
readback errors and private data leaking after revocation. The actual query
freezes its original group scope through the send lock and every fragment,
rather than guessing from the current selection or command at send time.
This includes numbered menus, pagination and back navigation.

## Retired Entry Points

| Former entry | Current behavior |
| --- | --- |
| Standalone Unix Socket HTTP control | Startup refuses; HTTP returns explicit retirement without parsing or dispatching requests |
| `call_open` and dashboard network fallback | No network calls; missing integrated runtime reports retirement |
| Standalone launch/deployment scripts and unit template | All flags refuse execution; the template cannot restart the retired worker |
| Separate account, credential and artwork selection | UI controls current AstrBot accounts/provider references only; obsolete controls and environment fallback are removed |

Necessary stored data and read compatibility remain. Old activation history is
not deleted to fabricate first activation. Retirement applies only to the
standalone public bot, **not the standalone cashier or cashback RPC**.

## Data And Browser Evidence

Consistent backup and advisory historical review:
`/opt/backups/wsl-remediation-20261015-preflight/audit.json`.
The protected financial fingerprint for 1468 entries is
`88566f7c5acae3604134e67e77636b40462d5e6712a70070825be6450e8d0407`.
The list contains 64 distinct candidates: 63 attribution conflicts and 60
historical missing recipients, with overlap. They are not confirmed new defects
and have not been automatically repaired.

Before deployment, migration was verified on a temporary consistent-backup copy.
Every old column in 16 original tables was compared row by row. Only the two
groups' source cursor/scan metadata reset once; all 1468 old entry rows, 1729
evidence records, payments, requests and audit remained unchanged.
Additions are evidence-cycle metadata and empty membership/quarantine tables.
Integrity and foreign-key checks passed, repeat migration was unchanged, and
82 read-only compatibility checks passed.
Evidence: `migration-verification.json` in the backup directory above.

Management checks cover 320/390/768/1440px and retired-mode refusal.
Desktop/mobile cashier checks cover price races, actual-payment confirmation,
exception labels, withdrawal recalculation, source recovery, concurrent settings,
revocation and absence of overflow or script errors.
Screenshots are archived under `management-browser/` and `cashier-browser/`
in the backup directory. Identities and ledger data are synthetic, not live payments.

### Final Offline Regression

The final Wangshangliao, Telegram, platform-coexistence and related-framework run
finished with **3697 passed, zero skipped** and three existing dependency
deprecation warnings. All seven conditional login-browser cases, the friend-page
case and rollback-compatibility case ran with their actual prerequisites.
The twelve completion-time pacing cases are included in this full run.
Report: `final-regression.xml` in the backup directory.

The complete cashier browser run passed again after the final ledger optimization.
Frontend type checking, temporary management/cashier builds, API-client
regeneration, scoped Ruff and diff-format checks passed.
Temporary browser servers are stopped. The offline phase did not replace served
pages or restart services; dependencies were not upgraded.
The subsequent user-approved publication and shared-service restart are below.

### Mixed Load

Narrower internal evidence projections and linear wide-row conversion produced
identical full responses in 40 alternating samples per version on the same copy.
Four-state wall P95 changed from 9.638 to 7.761ms; thread CPU P95 from 9.640
to 7.763ms. No result, identity or qualification cache was introduced, and
candidate indexes with unclear benefit were not adopted.
Details: `cashback-row-comparison.json` in the backup directory.

All load runs remain, including failures, without cherry-picking:

| Evidence time, UTC / stage | Rule P95 | Snapshot baseline P95 | Loaded P95 | Growth |
| --- | --- | --- | --- | --- |
| 10:15:12, before wide-row optimization | 18.438ms | 12.250ms | 49.122ms | 300.99% |
| 10:35:48, optimized run 1 | 12.522ms | 13.172ms | 16.595ms | 25.98% |
| 10:37:28, optimized run 2 | 13.715ms | 9.743ms | 13.811ms | 41.76% |
| 10:38:54, optimized run 3 | 11.146ms | 12.747ms | 13.443ms | 5.46% |

Twenty synthetic groups, one hundred group messages per second, two local slow
models and private flooding processed 3000 group messages over thirty seconds.
Durable-ingress P95 was 7.079ms, normal/private queue peaks were 4/2, and maximum
process RSS was approximately 205MiB. Rules passed the one-second bound; ledger
growth failed the ten-percent bound. Query thread CPU P95 was 11.353 to 24.215ms;
wait P95 was 0.519 to 17.528ms. These figures do not establish a unique root cause.
Evidence: `/opt/backups/wsl-native-load-20261015T101512.json`.
Its `live_database=false` explicitly identifies migrated-snapshot queries,
not online service response times.
The three optimized reports use suffixes `103548`, `103728`, `103854` in the
same directory. Only one met the ten-percent bound; stable acceptance is not approved.

Read-only production protection recheck:
`/opt/backups/wsl-remediation-20261015-postcheck/protection-check.json`.
Old financial columns, rules, four formal-group bindings and main configuration
are unchanged; only two backend read-audit rows were added.
AstrBot and cashier retain their original running PIDs with zero restarts.
The friend exception hold and public-model budget gate remain in place.
A final consistent backup after the full tests again matched protected finances,
rules, formal groups and main configuration against the initial protection point:
`finalcheck/audit.json` in the backup directory.

## Maintenance Deployment

Fresh user-approved maintenance backups:
`/opt/backups/wsl-remediation-deploy-20261015T1102/`.
They include running and stopped consistent SQLite copies, main configuration,
previous installed service units/pages and an explicitly labeled candidate
source archive. The candidate archive is not a complete pre-edit source backup
and must not be treated as the previous running revision. Earlier source
snapshots also require file-by-file verification, not blind restoration.
Rollback restores verified code, pages and service settings only, never newer
financial data.

The cashier stopped before AstrBot. Tested pages were published, AstrBot started
first, and the cashier started after incremental migration completed.
The installed `astrbot-open.service` now explicitly refuses the retired worker.
The independent cashier and cashback RPC remain active.
Restarting the shared process briefly reconnects other platforms; this was not
a plugin hot reload.

Live migration compared old columns in all 16 original tables. All 1468 entry
rows, 1729 evidence rows, existing payments, attribution and requests remained
unchanged. Only two groups' source cursor/scan metadata reset once.
Read-only checks append normal audits without changing old audit rows.
Integrity passed with zero foreign-key violations. Financial fingerprint,
reward rules, four formal-group bindings and main configuration match the
stopped backup. Evidence: `live-migration-verification.json`.

All ten enabled platform instances resumed running; both Wangshangliao and both
Lajiaoliao accounts are online. Telegram adapters resumed polling, which is not
per-account message-delivery acceptance. The management page passed 390/1440px
checks against live read-only backend data. Installed cashier files match the
tested build byte for byte. A further check opened the actual AstrBot page and
its real iframe at desktop/mobile sizes, including architecture/permission tabs,
without script errors or horizontal overflow:
`/opt/backups/wsl-deployed-page-20261015T113627/`.
Anonymous pages and APIs reject access, which does
not replace an authenticated production Access/payment-flow check.
No production Access credentials were forged and no live payments registered.
Service ownership, resource bounds and private socket/database permissions
remain in place. Evidence: `deployment-health.json`.

Log checks do not mean zero warnings. Startup still reports missing matching
main-WebUI version metadata, one missing historical model reference, other
plugins' remote-visibility/group-command warnings and two unsupported frames.
The group-command `BadRequest` warnings also occurred before maintenance.
Other plugins were not changed in this scope, and no artificial version marker
was added to conceal unknown asset provenance. Redacted categories are recorded
in `runtime-log-check.json`; a running adapter does not prove every business flow.

Maintenance regressions include 69 read-only observer cases; with documentation,
financial audits, retired entry points and deployment boundaries the result is
**166 passed, zero skipped**. Observer cases use virtual time, mocks and temporary
read-only SQLite databases. Report: `deployment-regression.xml`.
The cashier health probe does not forward dashboard credentials. The final
script also completed a one-minute real read-only check:
`/opt/backups/wsl-shared-soak-20261015T112723/evidence.json`.

Enhanced read-only observation covers both services' PIDs, restart counts,
memory/tasks, enabled-platform completeness, connections, cashier authentication,
friend holds, main configuration, finances, formal-group settings and queues.
**The October 15, 2026 window from 11:09:13 to 11:39:13 UTC passed:
1800.13 seconds and 61 samples.** Both service PIDs remained unchanged with zero
automatic restarts. Peak memory was approximately 464/46MiB and peak tasks 41/5,
within their respective limits. The last sample had no pending or rejected backlog.
Evidence: `/opt/backups/wsl-shared-soak-20261015T110913/evidence.json`;
summary: `soak-result.json` in the maintenance deployment directory.
The final consistent backup still matches stopped protected finances, reward
rules, formal groups and main configuration:
`final/audit.json`, `final-protection-check.json`.

The actual deployed source, service templates, pages, observer scripts and
current documentation are separately sealed in `deployed-release.tar.gz`,
with `release-manifest.json`. This is distinct from the earlier candidate archive
and cannot replace unverified pre-edit source. Stable observation under current
traffic does not pass the twenty-group mixed-peak performance gate.

## Unpassed Items

The root filesystem is approximately 94% full, with about 13GB available;
business data and backups share it. No historical data or backups were
automatically deleted. Capacity monitoring and a retention policy remain needed.
No usable real Access login session is available for a directly authenticated
production cashier flow. Synthetic browser checks and anonymous rejection do
not replace this check.

The current controlled bot in group `1459282` lacks native administrator
authority; recall or mute success cannot be claimed.
Genuine first activation requires a fresh controlled group without activation
history. Official-phone confirmation of robot-initiated recall, genuine
first-friend welcome and supplier-enforced hard budget still need live evidence.
The paid-model gate is not opened for these tests.
Snapshot load checks are not online HTTP/page response SLAs under service quotas.

**Deployment is not overall acceptance. Formal groups remain disabled.**
