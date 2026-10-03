# Local customizations

This fork integrates the local 4.28.1 customization delta into the upstream
4.29.0-beta.1 codebase. Upstream history, versioning, and newer execution-policy
fixes are retained. This source merge does not deploy or restart a running bot.

## Included functionality

| Area | Source | Scope |
| --- | --- | --- |
| Group business bot | `data/plugins/astrbot_plugin_superbot` | Group-scoped points, games, moderation, advertising, owner management, help, maintenance and audit tools |
| Telethon integration | `data/plugins/astrbot_plugin_telethon_ai` | Account enrollment, scoped authorization, lifecycle and account management UI |
| Telegram collection | `data/plugins/astrbot_plugin_telegram_collection` | Collection workflows and management integration |
| Telegram monitoring | `data/plugins/astrbot_plugin_tgwatch` | Monitoring and configured notifications |
| Live service integration | `data/plugins/astrbot_plugin_rebo_live` | Live-service adapter and management |
| Tenant controller | `data/plugins/astrbot_plugin_tenant_control` | Dedicated tenant-control workflow and deployment-worker integration |
| Domain analytics | `data/plugins/astrbot_plugin_domain_analytics` | Domain activity monitoring and reporting |
| Framework and WebUI | `astrbot`, `dashboard`, `openspec` | Wangshangliao native integration, Telegram adapter extensions, administrative UI and generated API client |

Supporting source, offline tests, maintenance tools, static artwork and local
animation generation are included. Retired advertising-AI experiments remain
under `integrations/superbot_legacy`, not in the active classification path.

## Installation and configuration

Use the root setup instructions for AstrBot. Install each selected plugin's
declared requirements as well; optional integrations are not a promise that
every third-party service is installed or authorized. Wangshangliao's optional
solver dependencies are available through the `wsl` extra. Frontend commands
are documented in `dashboard/package.json`.

Plugin source is intentionally versioned beneath `data/plugins`; runtime
`data/config`, databases, account sessions, caches and plugin working data are
not. Configure credentials, operator IDs, target groups, permissions and
budgets separately on the destination installation. Review feature defaults
and explicitly enable only the intended groups. Do not copy production
databases to initialize a public example.

The tenant controller and its deployment worker require their own isolated
environment. See `integrations/tenant-control/README.md` before enabling them.
Public source does not authorize payments, paid model calls, account logins
or production deployment.

## Privacy and operational evidence

No production tokens, API credentials, Telethon sessions, private keys, live
configuration, wallet/order databases or database backups are included. Public
test account and group identifiers are synthetic and must not be used as
authorization defaults. Historical live-acceptance reports containing account
details are excluded or replaced by pointers; they are not portable proof of
acceptance for a new installation.

The public Gitleaks configuration extends default detection with narrowly
scoped exceptions for a redaction-test fixture, a media-cache identifier and
a fixed public wire-protocol transform constant. Real authentication secrets
must remain outside Git and be rotated if previously exposed.

## Validation and rollout

Run offline regression tests before deployment. Live acceptance scripts are
operator tools, not unit tests: inspect their target settings and explicitly
authorize any external action before execution. Never use example identifiers
to contact real users. Reuse of a source-level passing result does not prove
real payments, live permissions, account sessions or game delivery on a new
installation.

Back up source, configuration and a consistent database snapshot before a
separate deployment. Keep incremental database fields and post-deployment
ledger entries on rollback; do not replace a live database with this repository.
