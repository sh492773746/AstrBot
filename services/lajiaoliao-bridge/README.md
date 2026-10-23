# LajiaoLiao local bridge

This service is the local transport boundary for the AstrBot `lajiaoliao`
adapter. It listens on `127.0.0.1:17890` by default and exposes:

- `POST /v1/session/login`
- `POST /v1/session/logout`
- `GET /v1/status`
- `GET /v1/groups`
- `GET /v1/groups/{id}/members`
- `POST /v1/messages`
- `GET /v1/events` as WebSocket
- `GET /login` as a local manual-authorization page
- `GET /v1/session/challenge` as public captcha scene settings
- `GET /verify` as an isolated verification frame (no account inputs)

The bridge implements the observed LajiaoLiao desktop login/token exchange.
On 2026-10-08 the saved user-authorized session passed real WebSocket, Gob,
Protobuf sequence and read-only message-pull validation. The bridge now runs
a Go text transport using `github.com/openimsdk/protocol v0.0.73-alpha.17`.
It does not load Android libraries or run an Android client.

## Run

Install Go 1.22 or newer, then:

```bash
cd /opt/astrbot-prod/services/lajiaoliao-bridge
go mod download
go run .
```

Set `LAJIAOLIAO_BRIDGE_TOKEN_FILE` to an absolute path in the service user's
private directory for persistent credentials. The bridge creates the directory
with mode `0700` and the token file with mode `0600`.

Account routes use `https://api.lajiaoliao.com` (`LAJIAOLIAO_CHAT_ADDR`).
OpenIM routes use `https://im-api.lajiaoliao.com` (`LAJIAOLIAO_API_ADDR`).
These hosts are not interchangeable. The earlier bootstrap-token requirement
was based on the wrong host and has been removed.

Before sending a password, the bridge reads official client configuration.
If the password-login captcha scene is enabled, the local API requires
`captcha_verify_param` from a user-completed official challenge. Missing proof
returns `428 lajiaoliao_captcha_required` without sending a password upstream.
The upstream service validates the proof; the bridge never manufactures one.
The dashboard does not yet embed the official verification widget.
The separate `/login` page does: the user completes the official Aliyun
challenge manually. Password submission happens only afterward. The widget
runs on the opposite loopback hostname (`localhost` versus `127.0.0.1`),
with frame-source, origin and per-attempt request-ID checks. This isolates
third-party verification scripts from the password form.
Device-switch confirmation returns `409 lajiaoliao_device_confirmation_required`
and must be completed through the official verification process.

Do not paste passwords, challenge proofs or session tokens into chat or logs.
Real login, IM-token exchange, connection and read-only pull have been verified.
Real outgoing messages and the complete model-reply workflow still require
a dedicated test peer/group acceptance test.

## Linux manual authorization

No Windows client, Android emulator, desktop session extraction, or ARM library
is needed for the local authorization page. Open
`http://127.0.0.1:17890/login` in a browser on the bridge host. On a remote
Linux server, forward the port from the user's computer with SSH:

```bash
ssh -N -L 127.0.0.1:17891:127.0.0.1:17890 USER@SERVER
```

Then open `http://127.0.0.1:17891/login` on that computer. Both loopback
hostnames must resolve locally. Enter the dedicated test account and complete
the official verification; do not send passwords, challenge proofs or tokens
to the assistant. Tokens are written only after a complete successful login
and IM-token exchange. They never appear in page responses. Successful
authorization alone does not mean the AstrBot adapter is enabled.

The test service `lajiaoliao-bridge-login.service` was started as a transient
systemd unit with a dynamic service user, private state directory and a
loopback-only listener. It is separate from AstrBot and does not restart it.
Its token path is `/var/lib/lajiaoliao-bridge/token.json`; the containing
directory has mode `0700` and token files have mode `0600`. It is not enabled
for boot; stop it with `systemctl stop lajiaoliao-bridge-login`.

`go test -race ./...` covers authentication and loopback-origin restrictions.
`browser_smoke_test.py` covers desktop/mobile rendering, spoofed messages,
manual-verification gating, password clearing and cancellation using mocked
provider callbacks and synthetic credentials only. Real account login still
requires the user; no automated captcha solving is implemented.

## Text transport

The bridge verifies `1001` (sequence metadata) before considering the account
online, and uses `1002` (ordered range pull), `1003` (text send), and server
receipts. Sequence polling every three seconds recovers missed push hints;
the idle connection heartbeat is fifteen seconds. There is one event consumer
per bridge account. First attachment starts at the current head, so history
does not cause surprise replies. `cursor.json` is atomically persisted beside
the session with private permissions. After accepting or filtering an event,
the adapter sends `{"type":"ack","message_id":"..."}`; an unacknowledged event
is replayed on reconnect.

Acknowledgment means queued into AstrBot, not model execution completed.
There is a crash window between queueing, acknowledgement and cursor save:
this is not a transactional exactly-once reply guarantee.

`POST /v1/messages` requires `kind` (`private` or `group`), `target_id`, and
text `parts`, optionally a `{"type":"quote","message_id":"..."}` part or
`reply_to` (not both). Quote targets must be textual messages in the same
account/conversation's latest 200 messages. The worker fetches original SDK
fields using the existing connection; the lookup never advances the cursor,
adds a consumer or persists message bodies. Type 114 carries
`{"text":"...","quoteMessage":{...}}`; incoming quotes include original text,
sender and timestamp. Missing or unsupported originals are rejected, never
downgraded to plain text. Media, mentions and revocation remain unsupported.
Lost send receipts are returned as unknown and never
automatically retried. Sending text has mock-server coverage; real
peer/group sending is pending explicit test-target selection.

The adapter defaults to an empty group allowlist (no groups are processed).
Private replies are configurable. Saved credentials are never copied to bot
configuration. `connection_state` is separate from saved authentication.
Chat-session expiration currently requires user reauthorization; automatic
background token refresh has not yet been implemented.
