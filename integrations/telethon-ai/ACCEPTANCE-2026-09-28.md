# Acceptance: 2026-09-28

## v0.3.3 Loading Recovery and Clean Upstream Verification

See COMPATIBILITY-2026-09-28.md for the reproducible isolated upstream result and
Page failure tests. Vanilla v4.28.1 package import passed, but native controller
initialization failed because required local Telegram hooks are absent. This is
a verified incompatibility, not a passing clean-install certification.

The Page now handles bridge/API timeouts and application script failures with
explicit errors and bounded retries. 46 automated tests and six offline Chromium
failure scenarios passed. Native desktop/mobile tests also passed with the same
unauthorized URL marker; that marker alone does not explain the original failure.
The exact external-browser cause and user-side recovery remain unconfirmed.

v0.3.3 was loaded and collector reconnected after reload. Main control attached,
keywords disabled, tenant usage unchanged at 1/30. No live group test was sent.

## Operations Page v0.3.1

- Added Accounts, Tenants, Diagnostics and Preview views to the native Page.
- Added actual tenant ownership, remaining quota, expiry, daily admission usage,
  assignment and the latest 20 request settlements. Queries are bounded.
- Added local Telegram hook, controller binding and lazy pipeline interface
  detection. These checks do not certify compatibility with vanilla AstrBot.
- API remains dashboard-admin-only; regression tests check denial before reading
  data and ensure the serialized status contains no credential fields.
- 44 tests passed. Playwright visited all four views at 1400x960 and 390x844,
  verified no document overflow, no JavaScript errors and unchanged runtime state.
- Mobile tables use labelled stacked fields instead of hiding essential quota
  and expiry columns off-screen.
- Reloaded only the main plugin, then reconnected the previously running collector
  transport through the native platform API. No production service restart.
- Tenant usage remains 1/30. No model previews, group messages or pause commands
  were issued by this UI verification.
- Script: verify_operations_page.py; screenshots: operations-{desktop,mobile}-*.png
  in the private deployment directory.

## Persistent Pipeline Fix and Boundary Regression

The local AstrBot EventBus now initializes missing schedulers for valid
hot-created profiles, using that profile's configuration and the existing
pipeline's plugin manager/database dependencies. Initialization is serialized;
failed initializations are not cached and never fall back to the default profile.
Dispatch continues after an initialization failure.

42 tests passed, including four new pipeline lifecycle regressions and two
additional expiry/resume-budget regressions. Existing pause-after-generation,
cross-tenant isolation and atomic budget tests also passed. These are isolated
tests, not live exhaustion/expiry demonstrations.

AstrBot was restarted once to load the core fix. After restart:

- Main control attached, one customer entry restored.
- Collector running with the same narrow group/sender policy.
- Keywords AI transport still disabled.
- Actual owner /start against VIP_dhbot and AIService94ce058bfdBot responded.
- Customer status remained enabled, usage 1/30, one account and one group.
- No group test messages or new AI calls were made.

Core backup: event_bus.before-lazy-pipeline.py under the private deployment
directory. This core change is not contained in the existing v0.3.0 plugin ZIP.
An unmodified AstrBot installation still needs compatibility/lifecycle validation.
Hot-profile tests use isolated scheduler doubles; real runtime hot-profile
creation under this new lazy path has not yet been exercised in production.

## Live Triggered Reply Confirmed

After runtime pipeline repair, the operator confirmed receiving the expected
AI-labelled Xiaoliao introduction in the authorized group. Read-only ledger
verification found message 1141 settled as sent, with one total charged request
and one daily admission. Earlier failed messages 1139 and 1140 remain released.
Trial usage is 1/30; today's bounded account admission usage is 1/3.

This verifies one real triggered receive-to-reply cycle and its request settlement.
Live quota exhaustion, pause/expiry enforcement, two-real-tenant isolation and
the permanent hot-created-profile lifecycle fix remain outstanding. No additional
test messages were sent while verifying settlement.

## Live Reply Failure and Runtime Repair

The administrator's messages 1139 and 1140 passed the transport and tenant gate,
but the production journal explicitly reported a missing PipelineScheduler for
the collector profile. Neither request reached a model through the event bus.
Native create_conf persists a profile without initializing its runtime pipeline;
the plugin's hot-created profiles exposed this integration gap.

repair_trial_pipeline.py re-saved both AI profiles through AstrBot's native
configuration update service, which calls reload_pipeline_scheduler. No service
restart or group message replay was performed. Both confirmed failed reservations
were released, their daily admission entries removed, and correction audit rows
written atomically. A private timestamped backup precedes the correction.

Collector remains running. A fresh human-triggered group message is still needed
to verify the full pipeline. This repair does not yet change plugin provisioning
for future hot-created profiles; clean-install/hot-install lifecycle coverage
remains a release gap. Do not declare live reply acceptance passed.

## Follow-Up: Approved Group and Connected Transport

- Verified request 30609f7ca609f6b7 approved, and the group authorization stored.
- After the operator's confirmation in response to the authorization/disclosure
  prerequisite, configured collector for this group only, sender 6206764051 only,
  three admissions per UTC day, 30-second cooldown, AI disclosure enabled.
- Platform API reports collector running; keywords remains disabled.
- Tenant remains paused. Administrator must separately enable the tenant through
  Telegram before a reply can be generated.
- Managed account senders are excluded to prevent account-to-account loops.
  Therefore the keywords account cannot trigger the collector AI; the allowlisted
  human administrator must send the live test mention.
- No live group reply has yet been verified. No group message was sent by these
  preparation steps.
- prepare_live_trial.py saved the previous collector platform configuration to a
  private timestamped backup before updating the native platform.

## Follow-Up: Account Assignment and Group Request

- Verified collector exclusively leased to tenant 73d632ccb950d05862e71cf3.
- Read-only Telegram checks confirmed @wdhihji1 is megagroup -1004304140268.
- Both authorized test accounts are existing members, neither is a group admin
  or creator. Membership does not establish group authorization or disclosure.
- Owner submitted /bind -1004304140268 to the new customer Bot and received
  pending request 30609f7ca609f6b7.
- Administrator approval, group authorization/disclosure and service activation
  remain pending. No group message, join or transport enable was performed.
- Script: check_trial_group.py; private evidence: trial-group-check.json under
  /root/Projects/agents/telethon-ai-deployment.

## Follow-Up: Live Enrollment Passed

After the administrator confirmed the one-day grant, the authorized keywords
session sent /create and received the Managed Bot creation link. The user-side
Telegram bots.createBot method created exactly one Bot under VIP_dhbot:

- Username: AIService94ce058bfdBot
- Bot ID: 8910402926
- Owner: 8695244435
- Tenant: 73d632ccb950d05862e71cf3
- Native platform: AIClient_8910402926

The real managed event enrolled the Bot. Owner /start returned the customer
keyboard and paused service status, budget 0/30, no assigned accounts and 0/1
authorized groups. VIP /bots listed this Bot as active. That label refers to
enrollment, not enabled AI service. /status is not implemented as a status
alias; it currently returns support guidance. Use /start or /service.

The other authorized session (collector) tested /start, /service, /resume and
/bind -1004304140268 against the new Bot. All returned the owner-only denial.
No unauthorized group request or account activation was performed.

Runtime reports one tenant and one customer entry. Both Telethon transports
remain disabled with empty chat/sender allowlists and disclosure unconfirmed.
Live group execution, quota exhaustion and two-real-tenant isolation remain
pending. Existing isolated test results below are not substitutes for those.

Reproducible script: accept_enrollment.py. It records a creation intent before
the external operation and refuses automatic recreation on uncertain outcomes.
Private evidence: /root/Projects/agents/telethon-ai-deployment/live-enrollment.json.

## Result

Partial acceptance passed. Live customer enrollment and group execution remain
pending; this is not a production launch approval.

## Verified This Run

- Production status: controller attached, zero tenants, zero customer entries.
- Both Telethon transports disabled, with no authorized chats or senders and
  disclosure unconfirmed. No group messages were sent.
- 36 automated tests passed (one upstream audioop deprecation warning).
  Coverage includes ownership, cross-tenant quotas/groups, atomic reservations,
  expiry, suspend-after-generation, confirmation actor/expiry/replay, enrollment
  retry/idempotence and customer request authorization. These are isolated tests,
  not two live customer deployments.
- Both authorized user sessions privately tested VIP_dhbot:
  /start, /bots, /plans and /create responded.
  /create correctly denied absent trial eligibility.
  /tgai returned no administrative information to either non-admin user.
- The installed native Plugin Page loaded at desktop and mobile sizes;
  its document-level mobile overflow assertion passed.
- A real model preview returned the original AI persona's introduction.
  Preview did not send to Telegram and does not exercise tenant billing.
- Page pause/unpause controls passed. Both transports remained disabled and
  unpaused afterward.
- Only the installed main plugin was reloaded for Page acceptance; the
  production AstrBot service and other Bots were not restarted.

## Pending Live Acceptance

The two available user sessions are not platform administrator 6206764051.
Do not impersonate that administrator or directly insert trial eligibility to
bypass the Telegram approval flow.

The administrator can privately send VIP_dhbot:

    /tgaigrant 8695244435 1

Then click the actor-bound confirmation button within five minutes.
This grants a one-day trial to the existing keywords test account, not a
payment or permission to post to any group.

After approval:

1. Run /create as the test owner and complete Telegram's creation confirmation.
2. Verify received Managed Bot identity, native profile routing, private owner
   menus, actual responses and unauthorized-user denial.
3. Allocate one AI account and explicitly verify group authorization and AI
   disclosure before enabling any group transport.
4. Test bounded triggered replies, quota exhaustion, pause and expiry.
5. Enroll a second real customer to validate live cross-tenant isolation.

Formal payments, proactive group messaging, account auto-joining and the legacy
independent tenant were not enabled or changed.
