# Wangshangliao Administrator Guidance

## Changes

- Add an independent global `admin_guidance` profile and AI management tab.
- Preserve customer/group profile selections; the first explicit administrator
  profile save enables routing without enabling group AI or bypassing budgets.
- Resolve caller-owned, account/group-scoped guidance from verified native
  group-manager grants and separate cashier/financial-administrator chat grants.
- Keep framework admins, quota exemptions, plans, dashboard logins and CF Access
  identities distinct from native chat roles.
- Separate histories by route and authorization/selection fingerprint; recheck
  scope before admission and delivery to suppress revoked or stale answers.
- Retain tool-free consultation, disabled command authority and existing cost
  admission; do not send ledger data or other administrators' identities to models.
- Synchronize Chinese/English guides, architecture mapping and UI coverage.

## Release Boundary

The exact `wangshangliao_moderation` plugin was reloaded on October 20, 2026,
without restarting the backend process or the three online connectors.
An impossible-revision probe reached the new profile validator and was rejected
without saving a profile. Protected formal-group settings, budgets, account
configuration and all 1,468 financial entries were unchanged.

The administrator profile remains unsaved, so the new routing is not yet enabled.
Verification used 6,561 passing backend tests (52 conditional skips), frontend
checks and isolated browser fixtures at 390px and 1440px. No live model calls or
native messages were sent; these checks do not establish native group acceptance.
This change does not implement the separately requested PC28 data tool, keyword
auto-replies, advertising schedules or business-knowledge integration.
