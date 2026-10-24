# Wangshangliao Redundancy Cleanup And Documentation Refresh

## Source Cleanup

- Consolidated three equivalent `send_reply_text` call sites in native `event.py`
  into one, preserving mutually exclusive `mentions`/`test_mid`, quoting,
  authorization and optional `auto_recall`.
- Shared identical validation for `keywords`, `mute_keywords` and `kick_keywords`
  in native `policy.py`, without changing limits or error codes.
- Removed only the unused private `_normalize_automatic_avatar_pricing` function
  from `cashback_store.py`. No migration runs, database cleanup or historical
  financial-row edits are performed.
- Three production files shrink by 66 net lines: 28 adapter and 38 plugin lines.
  All Wangshangliao management Vue files pass strict unused-local/parameter type
  checks; no frontend component was forcibly removed.

## Verification

- Added 110 regressions: 101 adapter and 9 plugin cases.
- Full related offline regression: **6564 passed, 52 conditionally skipped,
  3 warnings**, in 237.65 seconds. Skips are not acceptance passes.
  Evidence: `/opt/backups/wsl-cleanup-20261020-IsoIYB/full-regression.xml`.
- Targeted adapter 527, plugin 81 and controller safety 192 passes overlap and
  must not be added together as a unique total.
- Ruff passes for the three changed production files and two new test files.
  Repository-wide checks still find one pre-existing `I001` in untouched
  `scripts/reset_dashboard_live.py` and 66 untouched formatting differences;
  this is not a repository-wide clean-lint claim.
- This cleanup edits source and rebuilds/publishes documentation only. It does
  not reload/restart services, send messages, change production settings/data
  or establish that the running backend has loaded these source changes.

## Documentation And Prior Live Evidence

- Current Chinese/English entry points, testing, operations, architecture,
  migration, command, database and knowledge guidance now separate dated
  history from the later October 20 scoped warning acceptance.
- The earlier live run used three existing online connections in test group
  1459282: owner 31846829, receiver 43506527 and sender 23691273. Three warnings
  ran across two isolated `test_advertising` fixtures of one and two, not full
  first/second/third escalation in one fixture.
- Native `userNick` supplied the correct `@大蛤` and target rather than
  `groupMemberNick=testfork`. Independent peer records confirmed original recalls
  and warning delivery/self-recalls at 12.112/12.115/12.125 seconds after acceptance.
- Rules, the receiver's temporary native-admin role and test windows were restored
  to their original state. Finance, formal groups and normal sanction counts stayed
  unchanged; native receipts/minimal audit remain. That live run had zero model
  calls, no extra NIM connections and no service restart.
- The first verifier's hash-as-JSON error was recovered read-only without
  resending; failed-run evidence remains. These are nickname/target and warning
  recall checks only, not mute escalation, full functionality or phone rendering.
  Evidence: `/opt/backups/wsl-warning-live-20261020-I5Z5qf/result.json`.
- Generated HTML/PDF publication is coordinated separately from source editing
  and needs artifact synchronization plus desktop/mobile verification.
