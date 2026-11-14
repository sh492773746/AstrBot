# Wangshangliao Command Tree

## Current Group Workflows

All private menus, group selection, personal knowledge, support, private AI/admin
guidance, finance chat and image intake are retired, together with automatic
friend approval/welcomes. Historical records remain.
Loaded by a full restart at **01:48 Asia/Shanghai, November 2, 2026**.
Mute-first is loaded; all six production raid switches remain off pending full live acceptance.

| Purpose | Group command or WebUI |
| --- | --- |
| Help and authority | `帮助`, `我的权限`, `规则` in the target group |
| Add a speech exemption | Real client mention of one member plus `白名单`, immediately; view with `白名单` |
| Remove an exemption | `移除白名单 CURRENT_NAME/SAVED_OLD_NAME/EXACT_UID` → `选择白名单 NUMBER` → personal `确认白名单 TOKEN` |
| Blacklist and mute | Real mention or verified native reply to one member plus `黑名单`; immediately mute, never kick; view with `黑名单列表` |
| Remove and unmute | Real mention/reply plus `解除黑名单` (alias `移除黑名单`); inspect the two outcomes separately |
| Blacklist duration | `黑名单禁言时长 N分钟/N小时/N天` → personal `确认黑名单 TOKEN`; default 41,760 minutes/29 days, range 1..41760 whole minutes |
| Unmute and end blacklist enrollment | `解禁 @MEMBER` / `解除禁言 @MEMBER`, using a real native mention; remove this group's entry only after verification, never on an unknown receipt (loaded November 2, 2026; direct phone unmute is not synchronized) |
| Daily group mute schedule | `定时禁言 23:00 08:00` → personal `确认定时 TOKEN`; Asia/Shanghai times |
| Text replies/schedules | `关键词回复`, `定时发言`; existing fixed add/edit/enable/disable/delete preview-confirm flows |
| Images, activities and finance | Existing authorized Dashboard operators use Groups → Rules or Activities & Finance; no global login grant for ordinary group admins |

Group whitelist addition needs no search, selection or confirmation. Dashboard
list addition still searches a real complete current roster by name and confirms,
never blindly by UID. Dashboard blacklist addition only records; removal only
removes, without unmuting. Group additions and real rejoins use the confirmed
mute duration; changes do not reset existing mutes or punish historical entries.
Names, UIDs, typed mentions, copied quotes and multiple targets are not native
targets. Owners, admins and bots remain protected. New bot/group/UID entries
are exclusive; explicitly remove the other entry first. Preserve historical
overlaps with blacklist priority and separate removals. Unknown receipts pause
without retry. Ordinary mutes retain their 24-hour cap.

### Short List Aliases

Added November 2, 2026. Original commands, authority, native-target checks,
durations and confirmation flows remain unchanged:

| Alias | Existing command and behavior |
| --- | --- |
| `加黑` | `黑名单`: real single-member mention or verified same-group reply; enroll and mute for the configured duration |
| `删黑` | `解除黑名单` / `移除黑名单`: real mention or verified reply; remove and unmute with separate outcomes |
| `加白` | `白名单`: real single-member mention; add immediately without unmuting |
| `删白` | `移除白名单 NAME/SAVED_NAME/EXACT_UID`: search, `选择白名单 NUMBER`, then personal `确认白名单 TOKEN` |

Examples: `@MEMBER 加黑`, `@MEMBER 删黑`, `@MEMBER 加白`, or `删白 NAME`.
Typed mentions are not identity evidence. `删白` does not add direct-mention removal
or bypass the existing confirmation.

## Old-To-New Mapping

| Retired entrance | Current disposition |
| --- | --- |
| Private/numeric menus, group selection and administration | Use fixed commands in the target group; no cross-group selector |
| Private advertising images | Groups → Rules → Keyword Replies & Scheduled Messages; one image per keyword, up to three per schedule, one rotated per occurrence; static JPG/PNG/WebP, each original ≤5MB |
| Groups → Friend Requests and automatic friend/welcome processing | Removed, without a friend-automation replacement |
| AI → Customer Service / Admin Guidance, personal knowledge/private AI | Removed, not migrated to group AI or hidden private routes |
| Activities & Finance → Settlement Grants and private finance chat | Remove private grants; retain Invitation Settlement for existing authorized Dashboard operators; never publish private cash/payee details |

See [the beginner guide](wangshangliao-start.md) for mappings and bilingual isolated
browser screenshots. October22/23 menu/list-loading evidence remains in
[maintenance history](../../wangshangliao-maintenance.md), not this batch's
deployment or acceptance. AI cannot execute or confirm.

[Beginner guide](wangshangliao-start.md) ·
[Architecture and permissions](wangshangliao-architecture-permissions.md) ·
[Chinese tree](../../zh/platform/wangshangliao-command-tree.md).

## Activities, Cards and Navigation

Lottery limits: 1-100 winners, 1-10080 minutes, and 0-10000 for capacity/invitation
threshold; winners cannot exceed a nonzero capacity. Settings freeze during an
active lottery. Join in the activity group only. Drawing rechecks current membership,
persists random winners before one native-@ notification, and never redraws because
delivery is unknown. Proactive start/result/cancellation notices are retained;
they are not twelve-second command-reply cleanup.
Invitation tracking starts with a roster baseline and verifies later recruits
against official business attribution, not merely the native join operator.
Activity points never transfer money or pay repeatedly for rejoining.
Rankings use retained, trusted text for the Beijing day, twenty members per page:
at most one count per member per thirty seconds, no repeated text within five
minutes, excluding recorded sanctioned or recalled messages. They are not complete platform totals.
`排名` and `发言排行榜` still show today's ranking and accept a page number.
Automatic speech exemptions use yesterday's Beijing-day ranking: only its top twenty
with more than 100 eligible texts (at least 101) newly qualify; never backfill rank21.
Refresh runs every thirty seconds with a thirty-second settling delay.
Existing eligibility survives day changes; each eligible text renews seven days,
and seven inactive days expire it. Retained and new members may exceed twenty;
this stores activity timestamps, not seven days of chat bodies. Manual grants
remain unchanged. Explicit removal blocks historical readdition until a later
date qualifies anew; blacklist wins and existing mutes stay in place.

Integrated mode hides the old Groups & Runtime / Moderation tabs; per-group ad
and flood settings are under Activation & Rules.
Card edits and banned/cancelled-account cleanup use dashboard preview jobs,
complete rosters and separate action grants, never AI execution.
Currently the manual Card tasks controls are mounted only in the hidden
Groups & Runtime tab, with no visible integrated-mode replacement.
Group Grants still exposes suffix/automatic-card settings, not manual preview,
execution or stop controls.
An accepted-but-unverified or unknown card result stops further writes; reconcile
receipts/current membership rather than resend.
Warning delivery and original-message recall are separate audit fields.
The legacy `N / 3` count display is neither a removal threshold nor an automatic reset.
See [content rules](../../wangshangliao-content-rules.md) and
[daily schedules](../../wangshangliao-schedules.md).
