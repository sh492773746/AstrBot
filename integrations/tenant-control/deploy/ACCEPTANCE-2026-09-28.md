# Production migration acceptance: 2026-09-28

## Completed

- Installed controller plugin v0.4.0 through the native AstrBot plugin manager.
- Production `astrbot.service` is the single VIP_DHBot receiver.
- Stopped and disabled `astrbot-vip-dhbot.service`; its migration marker prevents
  systemd startup and its inactive runtime configuration has no bot token.
- Bound `VIP_DHBot::` first to the VIP profile
  `de35c453-37b1-430a-9df8-10d2429b5228`.
- VIP profile enables only the controller plugin, disables builtin commands and
  LLM/STT/TTS, and uses the `/` wake prefix.
- Enabled the production plugin with administrator 6206764051 and private
  credentials outside AstrBot's editable plugin/configuration schema.
- Migrated the existing SQLite database with the unchanged Fernet key.
  `PRAGMA integrity_check` returned `ok`. Preexisting orders, bots and operations
  were empty; migration added one audit record.
- Verified Bot ID 8941933661, username VIP_dhbot, BMM enabled, no webhook.
- Verified one controller plugin entry, fresh polling readiness and the private
  customer command menu through the Bot API.
- Compared all unrelated production platform configurations to the protected
  snapshot: unchanged. After the single controlled restart, all six loaded
  platforms reported running with zero errors through AstrBot's statistics API.
- Verified the live configuration report says purchases closed, Worker and
  model gateway not ready, and zero tenants.
- Sent one migration notification with navigation buttons to administrator
  6206764051 (Telegram message 35). No group messages or user-account actions.

## Automated checks

- 64 controller tests passed, including configuration guards, administrator
  checks, callbacks, payments, tenant ownership, entitlements, quota enforcement,
  runtime credential checks and migration backup helpers.
- 44 Telegram adapter tests passed.
- Ruff checks passed for the changed controller code and tests.

These are offline tests, not successful live tenant provisioning or live Stars
payments. No test result should be represented as customer acceptance.

## Pending

- Customer inbound acceptance now passes with two authorized Telethon accounts
  (see follow-up below). Administrator must still send `/configstatus` to verify
  the positive administrator-only path; neither session is the administrator.
- Worker, reviewed/pinned tenant image, model gateway and approved deployment
  environment are not activated by this migration.
- Two user-confirmed new Managed Bots, live tenant isolation, manual group
  admission and end-to-end suspend/restore/upgrade acceptance remain pending.
- No formal Stars sales, paid deployments, Telethon or AI group chat enabled.
- Production AstrBot runs as root. Application-level permission checks do not
  provide OS-level isolation from Docker/host privileges. A restricted service
  user and filesystem/Docker access hardening require a separate reviewed change.
- Automatic rollback helpers were tested against temporary data, but a live
  rollback was not exercised after this successful cutover.

For protected backups and rollback procedure, see `UNIFY.md`. Do not include
private snapshots, credentials or runtime databases in an exported report.

## Live two-account follow-up

With operator authorization, `deploy/accept_telegram.py` connected both existing
retired collector sessions under exclusive session locks. Both were distinct
ordinary customer accounts, not administrator 6206764051.

For each account, live Telegram checks passed:

- `/start` returned the hosting menu with a three-row customer inline keyboard.
- `/plans` correctly reported sales closed and `/bots` reported no tenant bots.
- `/admin` and `/configstatus` returned no privileged response during the bounded
  observation window; a subsequent `/bots` response confirmed reception was live.
- The real `tc:bots` inline callback returned the customer's own empty bot list.
- Forged `tc:admin` and `tc:configstatus` callback payloads were rejected by
  Telegram with `DataInvalidError`, before the backend. This is transport-layer
  rejection, not evidence of backend authorization; offline tests cover that.

The first runner attempt incorrectly treated transport rejection as a failed
test. Its classification was corrected and both account runs completed on the
second pass. Sanitized results are in `var/telegram-acceptance.json`, mode 0600.
Both clients disconnected after testing. No sessions were cloned, no groups
were joined/read, no payments or Managed Bot creation occurred, and the retired
collector and old independent controller remained stopped.
