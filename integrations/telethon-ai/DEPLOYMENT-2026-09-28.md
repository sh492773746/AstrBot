# Telethon AI Gateway Deployment

## Architecture Correction

The fictional Xiaoliao personality belongs to the Telethon account AI feature,
not to a customer-service Bot.

- VIP_DHBot retains its existing controller profile, exact plugin allowlist,
  disabled LLM setting and default persona. No controller core changes.
- Trial Bot 8852060111 no longer reads SOUL.md or overwrites the chat-only system
  prompt. It preserves the native AstrBot profile/persona pipeline.
- The trial remains a separate AstrBot instance using its tenant gateway key.
  It does not mount or copy the production VIP config, administrator list or keys.
- The original production Superbot was not modified or restarted.
- `astrbot_plugin_telethon_ai` is installed in production through AstrBot's
  authenticated loopback plugin upload API. Later changes were reloaded only
  for this plugin, not through a production restart.

## Native Objects

| Account | Platform | Profile | Persona |
| --- | --- | --- | --- |
| collector | TelethonAI_collector | Telethon AI - collector | telethon-ai-collector |
| keywords | TelethonAI_keywords | Telethon AI - keywords | telethon-ai-keywords |

Initial model selection: Gemini/gemini-3.8-flash, copied as an ID from the VIP
profile, not as a credential. Subsequent profile/persona changes are independent.
Models and personalities are managed in AstrBot's native pages.

The new Page is available at Plugin Pages -> Telethon AI Account Gateway, or
Plugin Details -> accounts. It is hosted by the existing Dashboard, not a new
server, public listener or domain.

## Verified

- 22 tests: admission limits, restart persistence, duplicate suppression, sender
  and group denial, stale message rejection, mention gating, single bound reply,
  send failure pause, no proactive sending, private administrator checks,
  customer-service prompt preservation, two simultaneous session leases excluding
  the collector.
- Native Plugin Page tested with Chromium/Playwright on desktop and mobile.
- Actual model preview produced Xiaoliao's AI introduction through the native
  account profile; the preview made no Telegram send.
- Page pause/unpause tested, original unpaused state restored, platforms disabled.
- Real trial Bot private commands and model requests verified after removing the
  mistakenly installed persona. It now uses the native default model voice.
- Production AstrBot, tenant worker and model gateway remained running.
- Retired collection service remains stopped.

## Not Enabled / Not Claimed

- No Telethon AI connections, group listeners or real group replies were started.
- Both account entries are disabled, group/sender allowlists empty, and disclosure
  confirmation false. Operator must specify authorized scope before enabling.
- `/tgai` and `/tgaistop` exist in plugin code but are not enabled on VIP_DHBot:
  its controller requires an exact tenant_control-only plugin allowlist.
  Do not loosen that guard casually; use the authenticated Page in this release.
- Native transport sending is tested with mocks, not a live group.
- No paid tenant account pool, random account assignment or commercial enablement.
- Existing external Cloudflare login/access issues were not changed.

## Files

Installed source:
`/opt/astrbot-prod/data/plugins/astrbot_plugin_telethon_ai`

Pre-install private backup:
`/root/Projects/agents/telethon-ai-deployment/20260928T113526`

Screenshots and customer-service acceptance report:
`/root/Projects/agents/telethon-ai-deployment`

The backup contains credentials and production DB data; keep private.
For rollback, disable/uninstall only this plugin and remove only its two disabled
platform/profile/persona objects after checking for operator edits. Do not replace
the live production database with the pre-install backup as a routine rollback.
