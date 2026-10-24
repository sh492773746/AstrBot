# Unreleased: Wangshangliao business policy migration

## AI Budget Split (2026-10-18)

- Keep the site trial pool separate at CNY20 per Beijing calendar month. Paid
  administrators retain individual cycle budgets at 70% of recorded CNY receipts.
- Rename the former site monthly cap to the site safety base in
  **AI → Plans and quota**. Its effective ceiling adds granted paid-cycle budgets
  overlapping that month; zero base still pauses all AI.
- Freeze each call's cost category; upgrades, expiry and restart never move old
  charges between pools. Classify legacy costs only from recorded cycle evidence.
- Preserve supplier gates, shared administrator pools, private alerts and financial
  records. Update both language guides, reference tables and the budget screenshot.

## Existing Business Policy Migration

- Move enabled groups, proactive sending, card tasks and test-window controls from
  the bot editor into plugin management. Login and connection controls remain native.
- Add an account-bound SQLite policy service, immutable runtime snapshots, optimistic
  concurrency, policy history and operation logs.
- Revoke delayed replies, tools and test windows on suspension; retain invitation
  evidence without replaying old chat commands.
- Add private backup and latest-policy rollback export tooling without restoring ledgers.
- Preserve activity-specific revisions independently from transport/business revisions.
- Commit policy changes and success audit entries in one transaction; a failed
  audit write cannot leave an unreported policy change.
- Retain versioned cash reward prices for delayed reliable invitation evidence.
  Missing price history remains unverified, and conflicting late evidence never
  rewrites a registered payment.

Entry mappings and release gates:
[中文](../docs/zh/platform/wangshangliao-migration.md) ·
[English](../docs/en/platform/wangshangliao-migration.md).

Production cutover remains gated on suspension-spanning invitation/price recovery
and full migration acceptance. Source changes alone do not constitute deployment.
