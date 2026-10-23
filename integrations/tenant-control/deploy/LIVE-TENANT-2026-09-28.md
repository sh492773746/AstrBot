# Live trial delivery: 2026-09-28

## Confirmed and deployed

- Owner: Telegram user 8695244435, using the existing authorized user session.
- The administrator confirmed this owner's two-day grant in Telegram.
- Managed Bot: `@AstrTrial444351790591355Bot`, ID 8852060111.
- Grant expires September 30, 2026 at 10:28:06 UTC.
- Manager: VIP_dhbot. Managed Bot event was received by the native production
  controller plugin, which encrypted its token and queued Worker deployment.
- Worker deployed `astrbot-tenant-8852060111` and reports active/healthy.
- No payment order was created.

Bot creation used the user's existing authenticated Telethon session and
Telegram's Managed Bot API. A username-availability check initially exposed
Telethon's ValueError wrapper; it was changed to the explicit MTProto
ResolveUsername request before any creation call. The successful creation
request was made only once. Account 8354653022 remains unconfirmed and was
not used to create a Bot.

## Live checks

Owner received responses to `/start`, `/uid`, `/chat`, a short Gemini message,
and `/exit`. The model replied with the requested acceptance phrase.
The owner sees the Superbot management entry.

The other user received the public menu without the management entry.
Sending the management command text explicitly returned `无权限`.
This verifies non-owner denial on one tenant; it does not substitute for
two deployed tenants' database/backup/extension isolation acceptance.

Container inspection verified non-root UID/GID 992:984, no published ports,
capability drop ALL, and a single mount for this tenant's own data. No Docker
socket, production data or other tenant mount is present.

## Lifecycle acceptance

A server-side operator acceptance run was restricted in code to this exact
confirmed non-paid trial. It recorded actions with actor `operator-acceptance`,
not as fabricated Telegram administrator confirmations.
Worker was temporarily stopped to prevent reconciliation racing the operator
test, then restarted afterward.

Passed:

- Snapshot generation.
- Pause and resume with fresh Telegram/business health.
- Temporary expiry caused the container to stop.
- Original expiry restored, with successful startup.
- Backup restore recovered an acceptance marker's prior contents.
- Base plugin upgrade and rollback both recovered healthy service.

Snapshot: `1790591851253177329` under this tenant's backups directory.
The detailed result is `/var/lib/astrbot-tenant-worker/lifecycle-acceptance.json`.
The temporary marker was removed from live data. The original expiry/enabled
state was restored. Production DB integrity returned `ok`.

Real deployment exposed BOM-prefixed AstrBot configuration after startup.
Worker now reads UTF-8-SIG for configuration/template files, and a regression
test covers subsequent reconciliation. No Bot identity or tenant data was
recreated to resolve it.

## Outstanding

Account 8354653022 still needs its own administrator-confirmed grant. Renew the
expired confirmation from the administrator's private chat:

```text
/admintrial 8354653022 2
```

After that confirmation, rerun the guarded creation script, verify the second
Bot, and complete two-real-tenant database/configuration/model quota/backup and
extension isolation checks. Neither bot has been invited into a group.
Formal sales and AI group chat remain disabled.
