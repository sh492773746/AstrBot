# Upstream Compatibility and Page Loading Verification

## Current Verdict: v0.4.0, October 1, 2026

The v0.4.0 package now includes `telethon_ai_service`, a registered plugin-owned
Telegram platform, and `NativePipeline`, which manages unchanged upstream
PipelineScheduler instances. Neither uses the old native Telegram hooks or
EventBus.ensure_scheduler. The upstream tracked checkout remains unchanged.

`verify_clean_runtime.py` exercises the extracted ZIP under `unshare --net`,
with a cleared environment, separate ASTRBOT_ROOT/HOME and fake credentials.
The actual upstream core, plugin loader, platform manager, config manager,
native pipeline, provider/persona integration and dashboard API are exercised.
Only external Telegram HTTP requests and the model provider use deterministic
fixtures. Complete core initialization, controller commands, dynamic customer
enrollment/idempotency, owner checks, profile changes, native AI reply and quota
settlement, authenticated Page/status API, hot reload and unload pass.

This is offline application compatibility evidence for the pinned v4.28.1
commit below, not official certification, arbitrary-version support, live
Telegram/model acceptance or payment acceptance. The role-aware native config
UI still requires the optional local core/frontend extension and is not in
the ZIP. Legacy `type=telegram` service platforms still require their reviewed
local hooks until explicitly migrated. Existing IDs, tokens and profile routes
must be preserved; stop old polling before changing platform type.

The previous failure remains documented below because it describes the old
v0.3.x integration accurately.

### Production Migration

On October 1, 2026, `migrate_portable_transport.py --apply` changed only
VIP_DHBot and AIClient_8910402926 to the plugin-owned service type. It backed
up configuration, profiles, plugin settings, business/core databases, private
keys, account registry and both SQLite sessions before stopping old polling.
Collector resumed its prior connection; keywords remained stopped. Existing
tenant IDs, owner, Bot, expiry, budget, usage, leases, groups and enable state
were preserved. Unrelated Bot configurations, start times and states did not
change; the entire core was not restarted.

Read-only live Bot API checks confirmed identities, no webhook, command menus,
and the controller's Bot Management Mode. Authenticated native Page and existing
role-config desktop/mobile acceptance passed without mutations. No new Managed
Bot was created, trial renewed, AI model called or group message sent in this
migration. These checks do not replace fresh live installation/enrollment on
vanilla upstream or real two-tenant isolation acceptance.

## Historical Verdict: v0.3.3

Vanilla AstrBot v4.28.1 is NOT approved for this plugin's standalone control
workflow. This was an isolated verification, not a modification of upstream
to make the test pass. Production continues to use its reviewed local extensions.

## Reproduction Environment

- Source: https://github.com/AstrBotDevs/AstrBot.git
- Tag v4.28.1, commit ab42c0d9b726d82ad0f9563e04c53a4460c00d61.
- Checkout: /opt/astrbot-compat-v4.28.1-20260928.
- Independent virtual environment, Python 3.12; upstream dependencies resolved
  from pyproject.toml using uv. Upstream ships no uv.lock for this checkout; the
  generated lockfile records this run's resolution, not an upstream lock.
- Plugin requirements installed only into that isolated virtual environment.
- Package v0.3.3 extracted to isolated/data/plugins/astrbot_plugin_telethon_ai.
- ASTRBOT_ROOT and HOME point to isolated directories; no production configuration,
  account registry, Session, model secret or Bot Token was copied.
- Probe process runs under `unshare --net`, with a cleared environment. No Bot
  polling, group messages, live model request or exposed dashboard was started.
- `git status --short --untracked-files=no` confirms upstream tracked source is
  unchanged. Independent data, dependencies and test artifacts are untracked.

## Results

`probe_clean_install.py` imports the packaged plugin and calls its actual
initialize method against an actual upstream TelegramPlatformAdapter constructed
with a nonfunctional placeholder token. Profile provisioning is disabled in this
probe so it can specifically test native control attachment without a model or
account registry. This is not a complete application start or live purchase test.

- Plugin import: passed.
- Native control attachment: failed.
- Missing adapter methods: register_application_hook,
  unregister_application_hook, suspend_required_plugin.
- The local EventBus ensure_scheduler extension is also absent; this is a detected
  local dependency, not proof that no alternative upstream lifecycle exists.
- Initial probe failed with AttributeError. The v0.3.3 guard now fails with an
  explicit compatibility RuntimeError before attaching handlers.
- Before/after evidence: isolated/compatibility-before-guard.json and
  isolated/compatibility-result.json in the clean checkout.

To repeat using the existing isolated environment:

```sh
cd /opt/astrbot-compat-v4.28.1-20260928/isolated
env -i PATH=/usr/bin:/bin \
  HOME="$PWD/home" ASTRBOT_ROOT="$PWD" \
  PYTHONPATH="$(dirname "$PWD"):$PWD" \
  unshare --net ../.venv/bin/python \
  /opt/astrbot-prod/integrations/telethon-ai/probe_clean_install.py
```

Remaining portability work: provide a supported plugin-owned Telegram integration
or upstream-supported extension contract, profile lifecycle handling, native Page
integration, then rerun clean full-application startup and isolated live acceptance.
Do not ship production core patches or production configuration as an implicit
part of an allegedly standalone plugin installation.

## Page Loading Issue

The screenshot shows the Page's initial loading text, not the Telethon connection
state. Original code could wait forever on bridge readiness or a status request;
script resource failure also left the static loading text unchanged.

The user's remote browser network/console session was not directly available.
The exact trigger of that screenshot remains unconfirmed. An unauthenticated
loopback API request returning 401 is expected security behavior and does not
diagnose the user's session. The URL's unauthorized marker is not sufficient
evidence of the root cause; authenticated loopback browser tests pass even when
that marker is present.

v0.3.3 changes:

- Eight-second bridge timeout and twelve-second status timeout, including retry.
- Cleared timers, so stale timeouts do not overwrite successful retries.
- HTML watchdog reports a missing application script independently of app.js.
- Classic deferred application script removes unnecessary module/CORS dependency
  in an opaque-origin iframe. This is a robustness change, not a proven diagnosis
  of the original remote failure. Sandbox and authorization remain unchanged.
- Bounded mutation/preview waits report uncertain results without automatic retry.

Verification:

- 46 automated tests passed, including compatibility rejection and admin access.
- Actual Page assets tested offline in Chromium for six scenarios: success,
  missing bridge, stalled bridge, stalled API, authorization denial, missing script.
- All five scenarios with loaded application code recovered after retry; no
  stale timeout reverted the successful state.
- Native authenticated Page visited all four tabs at desktop/mobile sizes with
  the unauthorized URL marker present; no JavaScript error or body overflow.
- Main plugin reloaded; previously running collector transport restored through
  native platform update. Control attached, collector running, keywords disabled,
  tenant usage unchanged at 1/30. No additional AI/group messages.

The user must reopen the Page to load the new assets; their external browser
recovery is not yet independently confirmed.
