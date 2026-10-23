# Unified production controller runbook

## Scope

Production `/opt/astrbot-prod` receives VIP_DHBot updates through its native
Telegram adapter and the v0.4.0 controller plugin. The VIP behavior profile is
`de35c453-37b1-430a-9df8-10d2429b5228`, matched by `VIP_DHBot::` before
other routes. Unrelated production platform settings are preserved. A single
production restart loads reviewed adapter changes; other bots reconnect.

The old `/opt/astrbot-vip-dhbot` is only a rollback point. Its systemd condition,
disabled boot entry, disabled plugin/platform and blank runtime token prevent
accidental reuse after migration. Never manually remove the marker and start it
while production VIP_DHBot is receiving.

## Apply

Build the v0.4.0 ZIP as described in the parent README. From this directory's
parent, run with the production virtualenv:

```sh
/opt/astrbot-prod/.venv/bin/python deploy/unify_controller.py
```

Preflight verifies Bot ID, username, BMM, absence of webhook, administrator
identity and token-rotation acknowledgement. It does not send Telegram messages.
The script snapshots configuration, routes, database and original Fernet key
under `data/plugin_data/astrbot_plugin_tenant_control/private/migration-*`.
It stops the old service before copying the database or enabling production.
Existing migration state refuses automatic reapplication.

The production plugin's `runtime-status.json` provides a non-secret readiness
report. `/configstatus` returns that report's live inputs to the verified admin
in private chat. This is not proof of user-originated command delivery; manually
send `/start`, `/configstatus` and test inline buttons after switching.

## Rollback

Find the protected snapshot path in `private/migration.json`, then:

```sh
/opt/astrbot-prod/.venv/bin/python deploy/unify_controller.py --rollback /ABSOLUTE/SNAPSHOT/PATH
```

Rollback disables the production receiver and plugin first, restores the VIP
profile and routes, copies the latest business DB back to the stopped old
instance, restores its configuration/ownership, then removes the startup marker
and starts the old service. The original Fernet key is unchanged. If the native
production API is unavailable, rollback fails closed: keep both VIP receivers
stopped and repair the local production API before retrying. Do not restore an
older DB over payments received after cutover.

## Acceptance limits

This migration does not start Worker, model gateway, paid deployment, Stars
sales, Telethon or AI group chat. Real two-tenant acceptance needs two newly
created user-confirmed Managed Bots and separately reviewed infrastructure.
Do not substitute existing production bot credentials for that test. The group
owner must manually add test bots; no automatic account invitations occur.
Offline isolation/payment tests are not a live Stars test-environment purchase.

Tenant configurations disable the dashboard and Docker publishes no ports.
No customer domain or WebUI is part of delivery. The production maintenance
dashboard is internal maintenance only.
