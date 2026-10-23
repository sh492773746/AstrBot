# LajiaoLiao / OpenIM compatibility report

Updated: 2026-10-09 (Asia/Shanghai)

## Login test

The previous test reached the wrong upstream host. On 2026-10-08, tracing
the official desktop bundle resolved the account client as follows:
`App.pretty.js` imports `g as Wt` from `index-7b352e9a.js`; that module
exports `p as g`, where `p` returns CHAT_URL (`https://api.lajiaoliao.com`).
The account client is `oz = WL(Wt(), false)`. Correct account routes are:

1. `POST https://api.lajiaoliao.com/client_config/get`
2. `POST https://api.lajiaoliao.com/pc/account/login`
3. `POST https://api.lajiaoliao.com/pc/account/get_im_token`
4. persist only `userID`, `imToken`, and `chatToken` with mode `0600`

The earlier `header must have token` response came from the OpenIM host
(`im-api.lajiaoliao.com`), not the business account host. It does NOT prove
that a prior desktop session token is required for first login. The previous
bootstrap-token requirement and corresponding dashboard instructions were
incorrect and have been removed.

The correct host's `/client_config/get` returns HTTP 200 and `errCode=0`
without a token. Live configuration enables Aliyun captcha for password
login (`usedFor=3`). The bridge checks this configuration and returns
`428 lajiaoliao_captcha_required` before sending a password unless a
user-completed official challenge is supplied. It never solves or bypasses
the challenge. The current Create/Edit Bot form embeds the official widget in
an opaque-origin frame. The independent plugin account page has been removed.
Standalone loopback documents remain isolated protocol diagnostics, not a
second Dashboard login entry point.

The user subsequently completed manual verification and login; the service
saved the resulting chatToken and imToken under mode 0600. A later read-only
probe verified the WebSocket handshake, Gob envelopes, the official
`v0.0.73-alpha.17` Protobuf definitions, and sequence and message-pull responses.
Only counts were logged, not tokens, user identifiers or message bodies.
Local mock tests cover fresh login without
a prior token, host separation, human-verification gating, new-device
confirmation, IM-only refresh responses, and preserving sessions on failure.

## Findings

The downloaded Android package contains a GoMobile OpenIM binding in
`lib/arm64-v8a/libgojni.so`. Its exported symbols include the complete SDK
surface needed by the bridge:

- SDK initialization and login/logout
- connection and token-expiry callbacks
- text, image, sound, file, quote and video message creation
- send, revoke and history APIs
- joined-group and group-member APIs
- upload callbacks

The embedded build metadata identifies these dependency versions:

```text
github.com/openimsdk/openim-sdk-core/v3 v3.0.0-lajiao.11
github.com/openimsdk/protocol v0.0.73-alpha.17
```

The public `v3.0.0` repository was inspected for API shape. Its
`InitSDK` configuration requires at least:

```json
{
  "apiAddr": "https://...",
  "wsAddr": "wss://...",
  "platformID": Android platform id,
  "dataDir": "...",
  "logLevel": 6,
  "objectStorage": "..."
}
```

The private `lajiao.11` suffix means the public module cannot be assumed to
match the production server. The Go module proxy and public repository do not
publish this tag.

## Addresses found

The Windows/Electron production bundle contains:

```text
API_URL:  https://im-api.lajiaoliao.com
CHAT_URL: https://api.lajiaoliao.com
WS_URL:   wss://ws.lajiaoliao.com
```

Static strings reveal `www.lajiao.chat`, `www-test.lajiao.chat`,
`www.lajiaoliao.com`, and Android-local ports `10001`, `10002`, and `10008`.
The desktop bundle also contains these account routes:

```text
POST /pc/account/login
POST /pc/account/login_or_signup
POST /pc/account/get_im_token
POST /account/code/send
POST /account/code/verify
```

The OpenIM websocket address is `wss://ws.lajiaoliao.com`. The OpenIM SDK
still requires `userID` plus the issued `imToken`; credentials cannot be
passed directly to OpenIM.

## Decision

Do not bind the Android ARM64 library into the Linux bridge. The Go transport
now supports sequence polling, ordered text events with consumer acknowledgments,
reconnection and text sends with explicit server receipts. It reports the real
upstream connection state independently of saved-token presence.

The private Go SDK itself is still unavailable, but the necessary public
Protobuf module is published and its read paths are compatible with the live
server. Text receive/model/reply and a text quote send have now been verified
in the explicitly selected test group; see the plugin acceptance record for
IDs, scope and remaining gates. An initial directory probe returned zero
groups; the selected account now has the uniquely resolved test group.
Media and mentions remain disabled. v0.2.2 adds fixed self-only recall and
group-card changes; recalling or renaming other members remains disabled.
Live acceptance is recorded in the plugin's ACCEPTANCE.md.
Explicit-expiry IM renewal and staged same-account reauthorization are
implemented and mock-tested, not live-accepted. Exactly-once model execution
is not promised. See README for delivery semantics.
No production Wangshangliao instance was changed.

## Text quotes

The live HTTP read route `/msg/pull_msg_by_seq` exposed group seq 8 as
`contentType=114`, with `content` encoding `text` and `quoteMessage`. The
desktop calls `createQuoteMessage({text, message: JSON.stringify(original)})`
and then the usual `sendMessage`.

The worker now maps this into a neutral quote component carrying the original
message ID, text, sender and timestamp. AstrBot receives a populated `Reply`.
Outbound `reply_to` or a quote component resolves the original through the
existing upstream connection (RPC 1001/1002), scoped to the current account,
conversation and latest 200 messages; it does not advance the cursor, replay
history, create a second connection or persist additional message bodies.
Only text originals and the own text of an existing quote can be quoted.
Nested quotes are flattened according to the SDK's behavior.

A controlled live send produced group seq 9, type 114, an exact reference
to seq 8 and a server receipt. The other existing account's adapter received
the quote with its original text. Private quotes and a fresh user-quote/model
roundtrip are still pending live acceptance; they are not declared complete.
Missing/foreign/unsupported originals fail before sending; unknown send
receipts are never retried or downgraded to plain text.

## Remaining feature gates

The packaged `capabilities.json` tracks SDK evidence separately from verified
wire compatibility. Desktop source has quote creation, upload, image creation,
revoke, member kick and group-info methods. The public SDK has mute/unmute
routes. None establishes the private service's role checks, receipts or upload
safety requirements.

| Wangshangliao module | LajiaoLiao decision |
| --- | --- |
| Group allowlist / private control | Implemented through AstrBot; no permission expansion |
| Content review / anti-spam | Business logic feasible; actions remain gated |
| Mute / unmute / kick / announce | Verify protocol and native roles; no action enabled |
| Member card / batch maintenance | Native identity, role protection and APIs unresolved |
| Rankings | Fresh accepted message counts feasible; implementation pending |
| Scheduled notifications | Text acceptance, persisted tasks and creator authorization required |
| Lottery | Nonfinancial state machine feasible; frozen enrollment and one-time publication required |
| Invitation points | Disabled pending trusted inviter provenance; join events alone insufficient |
| AI management | Fixed approved actions with preview/confirmation; implementation pending |
| Cashback / cashier | Assessment only; no payments or financial migration |

Do not directly import Wangshangliao business modules: they reference its
storage, native IDs, action services and event filters. No evidence proves the
private platform fundamentally lacks the pending capabilities; the bridge
rejects them because implementation and acceptance are incomplete.

Renewal recognizes OpenIM TokenExpiredError=1501, following official tools
error definitions. Invalid/kicked tokens do not trigger login or takeover.
The private fork's real expiry behavior has not been tested. Real acceptance
requires explicit test account/contact/group targets. The selected text/quote
test group is authorized; no private contact or moderation target is supplied.
