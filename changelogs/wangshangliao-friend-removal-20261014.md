# Wangshangliao Friend Deletion Removal

- Removed the Friend Cleanup page, `friends` API routes, deletion implementation
  and SDK deletion wrapper. Historical audit and financial data are retained.
- Navigation mapping: Friend Cleanup -> Friend Requests for automatic acceptance
  settings only. No replacement deletion entry exists.
- Automatic acceptance remains enabled by default, handles one incoming request
  at a time and retains its independent persistent 30-second cooldown.
- Chat, AI and MCP do not gain any friendship mutation capability.
- Added fixed test-group removal/reinvitation acceptance with native identity
  checks, one-shot durable intent and restoration. The invitation route remains
  excluded from the production request whitelist.
- Pending group invitations require exact recipient-side evidence and a separate
  one-shot acceptance. An accepted invitation request alone is not counted as a rejoin.
- Real rejoin exposed an NIM inviter query retaining the old source. Membership
  acceptance verifies the current signed business invitation, as activation does,
  and records SDK attribution conflicts separately without modifying cashback.
