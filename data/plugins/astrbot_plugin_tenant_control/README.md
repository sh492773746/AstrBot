# Tenant control on AstrBot

Both the control bot and tenant bots now run on AstrBot. The control bot is
`astrbot_plugin_tenant_control`, not a separate Telegram application. It uses
AstrBot command filters, plugin configuration and lifecycle hooks. Only Telegram
service updates (Stars checkout/payment and managed-bot updates) attach to the
existing adapter client. The plugin never creates a client, starts polling or
accesses Docker.

The privileged worker and model gateway remain separate infrastructure roles.
One isolated AstrBot container is provisioned for each paid, user-owned Managed
Bot. Tenants share a pinned base image, not data volumes. The existing production
data directory is **never cloned**; the worker copies only reviewed Superbot
plugin source from it. Superbot continues to be an AstrBot plugin using the
reviewed Telegram application hook for platform-specific features.

Nothing here activates real payments without operator configuration. The
previously exposed control token must be rotated first. Telethon and the AI
group-chat pilot remain out of scope.

```text
Telegram customers
  -> production AstrBot / VIP_DHBot-only config profile
     -> tenant-control plugin (commands, Stars, managed bots)
        -> shared control DB/jobs
           -> privileged worker -> per-tenant AstrBot + Superbot/custom plugins
           -> model gateway <- per-tenant proxy credentials
```

## Install the control plugin

Run infrastructure commands from this directory with the AstrBot virtualenv.
Build an installable plugin archive:

```sh
/opt/astrbot-prod/.venv/bin/python -m tenant_control build-plugin dist/astrbot_plugin_tenant_control-v0.5.0.zip
```

The archive embeds `control.py`, `store.py` and `runtime.py` from their canonical sources;
there is no independently maintained billing copy. It excludes worker/gateway
code, tokens, databases and runtime files. Existing ZIPs are not overwritten.
Install the ZIP in the production AstrBot controller, never a tenant instance.
Bind `VIP_DHBot::` to the dedicated behavior profile ahead of catchall routes.
See [the migration runbook](deploy/UNIFY.md) for the existing installation.
Do not copy the source plugin directory
alone; its bundled backend is assembled by this command.

Use the reviewed AstrBot build from this repository. The plugin requires
`register_application_hook`, `suspend_required_plugin`, and the optional adapter settings introduced here;
an arbitrary upstream image is not assumed compatible. Follow
`controller-config.example.json` for the controller's configuration:

- Put the newly rotated token in AstrBot's Telegram platform configuration.
- Use platform ID `VIP_DHBot` (the generic example uses `tenant-control`) and allow `message`, `pre_checkout_query`,
  `managed_bot`, and `callback_query` updates.
- Set `telegram_required_plugin` to `astrbot_plugin_tenant_control`. Polling
  waits for the plugin and pauses when its readiness flag is removed.
  On these dedicated instances, `/start` enters the native command pipeline
  instead of being consumed by the adapter's generic welcome response.
- Restrict the VIP profile's plugin set to this plugin, disable builtin commands
  and LLM/STT/TTS, and use wake prefix `/`. Other production profiles retain
  their own plugins and behavior. Set the plugin's `profile_id` to this profile.
- Configure verified `admin_ids`, `platform_id` and `support_text` in the plugin
  configuration. It is disabled by default. Enable it only after the shared DB,
  encryption key, new token and Bot Management Mode have been verified.

AstrBot owns polling, reconnection and shutdown. The plugin follows adapter
rebuilds, asks the adapter to stop reception and drain queued payment updates
before detaching, drains active command work on unload,
and holds a lock allowing only one controller per DB. Commands on other platform
IDs and group chats are ignored. No `python -m tenant_control bot` entry point
remains.

## Private navigation and command menu

The plugin registers customer commands in the private-chat scope after the
AstrBot adapter starts, and retries registration after client replacement.
Keep AstrBot's generic command registration disabled for this platform.
`/start` shows a two-column inline keyboard; `/plans` lists configured plans;
`/bots` exposes per-bot Telegram management and renewal buttons. Callbacks are private-only,
use the clicking customer's identity, and retain the business layer's ownership
checks. Unconfigured plans and unavailable workers still block checkout.
Administrator commands are not included in the public menu.
`/configstatus` is private and administrator-only. It reports the effective
instance, profile binding, plugin version, infrastructure heartbeats and purchase
gate without credentials. The same report is available in the admin keyboard.

Purchases remain test-only: both `allow_test_purchase` and Telegram's
official test API endpoint are required. Production checkout stays closed even
if someone configures prices. Already received successful payments are still
recorded. Template, feature whitelist and model quota defaults are snapshotted
per new tenant in the business DB, not reapplied to existing tenants.

## No-payment test grants

v0.5.0 adds `allow_trial_grants` (default false). When explicitly enabled,
the private administrator command `/admintrial USER_ID DAYS` proposes a grant
for 1-7 days. The normal actor-bound, five-minute single-use confirmation button
must be accepted before the grant exists. One customer may have at most one
unexpired grant, including an already bound grant.

Grants live in `trials`, never `orders`, and do not consume the first-month
discount. Time starts at confirmation, not at bot creation. `/create` shows the
Managed Bot creation link only while both the grant and Worker readiness are
valid. A managed-bot event rechecks eligibility. Each grant binds one Bot; token
rotation does not extend its expiry. Paid unfulfilled orders have precedence.
Existing pause, expiry, backup and recovery paths apply to test tenants as well.
Turning off the grant flag blocks new trial bindings and confirmations; already
bound tenants continue until their recorded expiry or administrative suspension.

## Clean image build

`deploy/prepare_image.py NEW_DIRECTORY` exports tracked AstrBot source files
plus explicit runtime entrypoints, license and dependency metadata. It rejects
symlinks and runtime-data paths and records source hashes. It never copies
the production data directory, plugins, sessions or virtualenv.
`uv export --frozen` supplies hashed, pinned Python dependencies; the Dockerfile
pins the Python base by digest and exposes no ports. Build only this exported
directory, not the production repository root:

```sh
/opt/astrbot-prod/.venv/bin/python deploy/prepare_image.py /private/new-build-directory
docker build -t astrbot-tenant:reviewed /private/new-build-directory
docker image inspect astrbot-tenant:reviewed --format '{{.Id}}'
```

Set `ASTRBOT_IMAGE` to that immutable local `sha256:...` image ID or a reviewed
registry digest, never the mutable build tag. This only prepares the base image:
it does not activate Worker, copy Superbot data, grant trials or deploy tenants.

## Shared state and infrastructure

For hardened deployment, keep AstrBot and worker under separate OS users: only the worker
may access Docker. Do not mount the Docker socket in the controller. The
controller, worker and model gateway share one private SQLite database. Only
the worker/controller need the Fernet key; only the gateway needs upstream secrets.
Create a dedicated `CONTROL_DB_GROUP` and make all three service users members
before starting them; set the same group in each process's environment.
The directory is mode `0770`, the DB `0660` and SQLite journal files inherit
the process umask `0007`. The controller user must not belong to the Docker
group or be allowed to invoke the worker. Restrict access to `var/`. Never
commit secrets, sessions, DB or tenant data. Set `CONTROL_DB_PATH` to the same
underlying file for all roles. If using containers, mount its entire directory
so WAL, shared-memory and controller-lock files are shared too.

The existing production AstrBot currently runs as root. Its plugin does not
invoke Docker, but sharing that root process is **not** OS-level privilege
isolation. Moving the production service to a restricted user is a separate
hardening step, not something this migration silently changes.

For migration, stop any old standalone controller first. Point the plugin at
the existing `var/control.db` and keep the **same** Fernet key. Orders, payments,
tenant IDs, expiry dates and jobs are reused without recreating bots. Do not
regenerate the key shown below for an existing database.

```sh
export CONTROL_FERNET_KEY="$(/opt/astrbot-prod/.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
export CONTROL_ROTATED_TOKEN_ACK=YES
export CONTROL_DB_PATH='/private/shared/control.db'
export CONTROL_DB_GROUP='private_tenant_control_group'
```

An isolated controller can use the above environment. The unified production
controller instead reads `data/plugin_data/astrbot_plugin_tenant_control/private/runtime.json`
under its instance root, mode `0600`, with those same three credential keys
(excluding `CONTROL_DB_GROUP`). Conflicting environment values fail startup.
Do not put secrets in the plugin schema or behavior profile.
The token and numeric operators are configured in AstrBot, not environment
variables. Start that AstrBot instance through its normal service entry point.
The worker additionally requires:

```sh
export TENANT_ROOT='/path/outside/production/to/private/tenants'
export ASTRBOT_SOURCE_ROOT='/opt/astrbot-prod'
export ASTRBOT_IMAGE='pinned-image@sha256:64_HEX_DIGITS'
export TENANT_PROVIDER_TEMPLATE='/private/path/provider-template.json'
export TENANT_PROVIDER_PROXY_URL='http://host.docker.internal:18734/v1'
export TENANT_PROVIDER_HEALTH_URL='http://DOCKER_BRIDGE_IP:18734/health'
/opt/astrbot-prod/.venv/bin/python -m tenant_control doctor
/opt/astrbot-prod/.venv/bin/python -m tenant_control worker
```

The example provider template only illustrates required keys. Use valid
AstrBot provider definitions with **empty** key fields in a reviewed,
root-readable JSON file, mode `0600`; never use production
`cmd_config.json` or its provider secrets as the template. Keep the Fernet key
stable and backed up: losing it prevents token recovery. Run the worker as a
separate service with the same DB/key and the reviewed template/image.

Start a separate model gateway, bound only to the private Docker bridge IP
(look up the host-side gateway with `docker network inspect bridge`). Do not
bind it to `0.0.0.0` or expose it on the public network. It validates per-tenant
proxy keys and caps each tenant's monthly model requests; upstream secrets
remain solely in the gateway environment:

```sh
export TENANT_GATEWAY_BIND='DOCKER_BRIDGE_IP'
export TENANT_GATEWAY_PORT=18734
export MODEL_CHAT_UPSTREAM_URL='https://your-provider.example/v1/chat/completions'
export MODEL_CHAT_UPSTREAM_KEY='PRIVATE_UPSTREAM_KEY'
export MODEL_CHAT_NAME='PLATFORM_APPROVED_CHAT_MODEL'
export MODEL_EMBED_UPSTREAM_URL='https://your-provider.example/v1/embeddings'
export MODEL_EMBED_UPSTREAM_KEY='PRIVATE_UPSTREAM_KEY'
export MODEL_EMBED_NAME='PLATFORM_APPROVED_EMBED_MODEL'
export TENANT_MONTHLY_MODEL_REQUESTS=1000
/opt/astrbot-prod/.venv/bin/python -m tenant_control gateway
```

The worker only signals readiness when this gateway's `/health` endpoint is
reachable. Without a live worker, checkout is refused. The gateway does not
store message content; it counts requests per tenant/month. The per-request
body limit is 16 KiB and chat output is capped at 512 requested tokens.
The provider template uses AstrBot's `key: []` for OpenAI-compatible chat
and `embedding_api_key: ""` for embeddings. Only tenant proxy keys, not
platform upstream keys, are written to tenant configuration.

Do **not** launch the bot with the placeholder values above. Before accepting
orders, verify the new control token, Bot Management Mode, successful
provider/template validation, pinned image, worker health, price configuration,
support contact, and service terms. No tenant domains or WebUI are required.
Worker-generated tenant configurations disable the dashboard and new containers
publish no ports. Readiness uses a fresh, tenant-specific Telegram transport
heartbeat file, not an HTTP endpoint. The internal model gateway remains an
infrastructure service, never a customer management interface.

`/manage BotID` (and legacy `/dashboard`) checks ownership and opens the tenant
Bot's private Superbot management center. No passwords or Web URLs are returned.
Group binding and tenant settings reuse Superbot's owner-checked Telegram menus.

Platform administrators use `/admin` for operations. `/adminpause`,
`/adminresume`, `/adminbackup`, `/adminrestore`, `/adminupgrade`, and
`/adminrefund` create five-minute, single-use confirmations bound to the initiating
numeric user ID. Worker operations are queued after confirmation and `/adminops`
shows status and generated snapshot IDs. `/adminrestore BotID SnapshotID` also
serves as rollback; snapshot IDs must be numeric, never filesystem paths.
Failures and interrupted operations require review instead of automatic replay.
Only Worker executes Docker operations; the controller has no Docker permissions.
Upgrades refuse unapproved custom-extension compatibility overrides.

## Operators and customers

- Admin `/adminprice first|month|year STARS` sets fixed positive Stars prices.
  Until set, a plan does not issue invoices. Values `45/50/500 U` are internal
  pricing targets, not Stars prices or USDT invoices.
- Customer `/buy first|month|year` creates one managed Bot after payment.
  `/create` retries the creation link. `/bots` and `/renew BOT_ID month|year`
  operate only on the caller's bots; each purchase is a single payment, not a
  recurring debit. First-month price is reserved for one initial purchase
  per customer. Fulfillment failures remain reviewable and refundable.
- Customer `/custom BOT_ID DESCRIPTION` records a scoped request. Admin
  `/adminquote USER_ID BOT_ID STARS DESCRIPTION` sends a custom Stars invoice;
  the operator runs `stage-extension BOT_ID ORDER_ID ZIP` to prepare a private
  tenant-specific offline test copy with Telegram, model providers and dashboard
  disabled. Stage data can still contain sensitive tenant history: only use a
  private test environment with Docker networking disabled, never share the
  snapshot externally. After testing that exact archive in the test environment
  and recording the customer's acceptance, run `approve-extension BOT_ID
  ORDER_ID ZIP`, then `install-extension BOT_ID ORDER_ID ZIP`. Installation
  rejects unapproved archives or changed hashes. Only the approved tenant
  plugin is replaced; existing data is preserved and snapshotted. Staging
  contains tenant data and must remain private; the operator must arrange the
  actual test instance separately. `/adminrefund ORDER_ID` automatically refunds only
  unfulfilled charges; active services require manual entitlement review.
  `/adminstatus` shows pending work; `/adminpause BOT_ID` and `/adminresume BOT_ID`
  are durable operator stop switches.
- `upgrade-plugin BOT_ID` backs up and upgrades one tenant's base plugin.
  Tenants with custom extensions require `--custom-approved` after a separate
  compatibility test; rollback uses `restore BOT_ID SNAPSHOT_NAME`. Image or
  native-dependency changes require a separately reviewed pinned-image rollout.
- `backup BOT_ID` snapshots config, plugin code, and consistent SQLite databases
  under the private tenant root; `restore BOT_ID SNAPSHOT_NAME` restores one
  explicit snapshot while preserving the replaced data for recovery. Plugin
  upgrades are backed up automatically. `doctor` reports readiness without printing credentials.
  `reconcile BOT_ID` is an operator-only repair command. Containers are
  paused on expiry; after 30 days marked archived without deleting tenant
  data. Database/job/audit backups require an external backup policy.

## Test-group acceptance

Create a new paid Managed Bot with a test control token and configured
Stars test environment. Verify the Bot ID, owner and login credentials
before the group owner manually adds it to `@example_test_group`
(`-1001000000003`, cached ID; reconfirm before use). Exercise only basic
community commands and tenant isolation. Leave the existing production Bot,
the two Telethon user accounts, games, advertising payments, external
collection and AI group chat untouched.
