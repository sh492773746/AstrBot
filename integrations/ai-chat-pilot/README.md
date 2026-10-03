# AI group chat pilot

This is an isolated, **dry-run** prototype. The optional `probe` operation
briefly connects the two existing Telethon sessions to verify authorization;
it does not read dialogs or messages, join a group, send a message, or use
the supplied Bot Token. No production AstrBot configuration is changed.
Never restart the retired collector while probing or using these sessions.

## First trial

Run from this directory with Python 3.10 or later:

```sh
/opt/telegram-chat-collector-prod/.venv/bin/python -m chat_pilot probe --collector-config /opt/telegram-chat-collector-prod/config.json
python3 -m chat_pilot subscribe demo-tenant "$(($(date +%s) + 30 * 86400))"
python3 -m chat_pilot allocate demo-tenant
python3 -m chat_pilot group demo-tenant -1001000000002
python3 -m unittest discover -s tests
```

Use a future Unix timestamp for the subscription, and a group ID whose
administrator authorized this use. `var/pilot.db` is local, permission `0600`,
and ignored by Git. `probe` registers labels only after both accounts
authenticate; it does not keep sessions open or bind a Telegram event listener.
Allocation is stable and exclusive: one account per tenant until an operator
explicitly changes that assignment. There is no automatic account rotation.
`python3 -m chat_pilot pause tenant demo-tenant` or
`python3 -m chat_pilot pause account operator-a` blocks new drafts without
deleting history or subscription data.

To preview an AI reply, provide an explicitly authorized HTTPS,
OpenAI-compatible chat-completions endpoint via environment variables:

```sh
export CHAT_PILOT_MODEL_URL=https://your-provider.example/v1/chat/completions
export CHAT_PILOT_MODEL_KEY=your-key
export CHAT_PILOT_MODEL=your-model
printf '%s\n' '{"group_id":-1001000000002,"message_id":1,"sender_id":42,"text":"Hi","sender_is_bot":false,"sender_is_self":false}' | python3 -m chat_pilot draft demo-tenant
```

This sends the provided message text to that model provider, so only use
messages for which processing has been approved. It prints a candidate reply
locally; it never sends the reply to Telegram. A group has a 120-second draft
cooldown, and each Telegram message ID can create at most one successful draft.
The pilot uses the operator-supplied sender flags; production integration must
derive them from authenticated Telethon events instead.

## Follow-on milestones

1. Add a separate account-login and health service with dedicated sessions,
   after freeing accounts from existing services or provisioning new ones.
   Credentials and sessions must remain outside tenant data and never enter
   the community template.
2. Add an owner-authorized group onboarding flow in the control bot and connect
   confirmed subscriptions to account allocation. Verify payments and renewals
   server-side; do not treat chat commands as proof of payment.
3. Feed real inbound group events into the draft path. Add human review,
   retention limits, audit, per-account rate limits and an immediate stop
   switch before considering any live sending.
