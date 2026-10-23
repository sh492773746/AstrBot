# Trial readiness: 2026-09-28

## Deployed

Production controller plugin is v0.5.0. The targeted native plugin upgrade
preserved the production AstrBot process (PID 2478178); the old standalone
controller and collector remain stopped. Private configuration/DB snapshots
were taken before the upgrade under the controller's private directory.

`allow_trial_grants=true`, `allow_test_purchase=false`,
`default_model_quota=0`. `/admintrial USER_ID DAYS` is private-admin-only,
requires the normal five-minute single-use confirmation, and creates a
non-payment record in `trials`. Duration is 1-7 days from confirmation. An
unexpired grant cannot be duplicated or consumed for a second bot.
No grant was issued automatically: orders, bots and trials remain zero.
Do not issue expiring grants until deployment prerequisites are ready.

The customer `/create` route supports confirmed trial grants but refuses to
provide a creation link while Worker is unavailable. Managed Bot events recheck
the same conditions. Payment records and first-month eligibility are unchanged.

## Image

Built `astrbot-tenant:community-20260928` from the allowlisted context:
`/opt/astrbot-runtime/tenant-build-20260928`.

Immutable local image ID:
`sha256:b2e2da32593e9155bcc4c561857f98b6d0f21e4c71623612e258b90d461f42ef`

The context contains 576 selected source/license/metadata files and generated
locked dependencies; it excludes production data, credentials, sessions,
plugins and the host virtualenv. Source file hashes are in
`source-manifest.json`. The image has no exposed-port or volume declaration.
No production folders were mounted during smoke tests.

Offline container checks:

- No `/AstrBot/data` in the clean image before startup.
- Telegram adapter and managed-bot API imports succeeded.
- With generated empty configuration, `dashboard.enable=false`, no platforms,
  no model providers and network disabled, AstrBot reached `AstrBot started`
  and `WebUI disabled` and stayed running throughout the 15-second check.
- AstrBot still attempts optional dashboard/metadata/resource fetches at startup.
  Those failed under the disabled network, but did not prevent core startup.

This is a base-image smoke test, not a real Telegram-connected tenant acceptance.

## Verification

71 controller tests passed. Ruff checks passed. Both authorized Telethon
accounts passed live customer command/menu regression and could not issue
`/admintrial` grants. No unauthorized grant records were created. Forged admin
callbacks were rejected by Telegram, not exercised as backend requests.
Sessions disconnected when the bounded test finished.

## Remaining Gates

- Worker and model gateway are not installed as running services.
- Reviewed gateway chat/embedding upstream configuration has not been selected;
  no production model key was copied or spent during this work.
- Separate Worker/gateway users, narrowly scoped filesystem access and Docker
  socket ownership must be deployed and verified before enabling Worker.
- Production AstrBot still runs as root; this has not been silently changed.
- Test tenant image/plugin combination still needs full startup acceptance with
  a newly user-confirmed Managed Bot, then a second tenant for isolation checks.
- No real payments, tenant creation, group messages, AI group chat or account
  invitations occurred.
