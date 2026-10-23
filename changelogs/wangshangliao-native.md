# Native Wangshangliao integration (fork)

- Add a built-in asynchronous Python adapter, signed business authentication, encrypted per-instance sessions, NIM transport and persistent group text processing.
- Replace the unshipped HTTP/SSE prototype flow with **Create robot → Wangshangliao → Account login**. Administrators supply server-side protocol deployment configuration; users complete human verification and device SMS in AstrBot.
- Add owner-bound login transactions, save rollback and duplicate-save handling, disable/logout/relogin controls, and explicit connection states.
- Keep ordinary messages in built-in group context without waking commands or models. Wait for each group pipeline to complete before dispatching the next event.
- Preserve inbox records across lost ACKs and quarantine uncertain sends or interrupted generation. Server acceptance is not peer delivery.
- Add synthetic wire vectors, local HTTP/WebSocket and lifecycle tests, browser fixtures, and Chinese/English setup guides with screenshots.

Validation on macOS ARM64 / Python 3.12: the combined Dashboard, platform-registration, Telegram, personal-WeChat, EventBus and native suites passed 342 tests. The final native-only run passed 41 tests. Dashboard tests passed 55 tests; generated API client, production build, Ruff, mocked browser creation/SMS/save retry/cancellation, and isolated startup passed. No real Wangshangliao account was connected. Live account/group acceptance and other operating systems remain pending.

- Use the supplied Wangshangliao logo and support native private text conversations with separate peer sessions, private ACK/send routes and persistent deduplication.
