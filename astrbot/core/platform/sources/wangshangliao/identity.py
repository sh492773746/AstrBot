"""Read account labels without changing transport or authorization identities."""

import unicodedata
from dataclasses import dataclass

from .wire import ProtocolError


@dataclass(frozen=True)
class ActorIdentity:
    """Keep transport identity separate from an untrusted application UID."""

    account: str
    claimed_uid: str
    peer: str
    verified_uid: str = ""

    @property
    def principal(self) -> str:
        """Return a transport-bound key for non-authorizing private services."""
        if not self.peer or len(self.peer.encode()) > 1024:
            raise ProtocolError("private_identity_unverified")
        return "nim/" + self.peer

    def require_verified(self) -> str:
        """Reject application identities not matched to signed member evidence."""
        if not self.verified_uid or self.verified_uid != self.claimed_uid:
            raise ProtocolError("private_identity_unverified")
        return self.verified_uid


def account_nickname(adapter, config: dict | None = None) -> str:
    """Read only the signed-in account's global nickname from verified snapshots.

    Args:
        adapter: Native adapter, or None for a disconnected instance.
        config: Saved instance configuration; defaults to the adapter policy.

    Returns:
        A bounded plain-text nickname, or an empty string when unavailable.
    """
    config = config if config is not None else getattr(adapter, "config", {})
    account = str(config.get("account_id", ""))
    live_account = str(getattr(adapter, "account", ""))
    nickname = config.get("nickname", "")
    cached = getattr(adapter, "verified_nickname", "")
    if isinstance(cached, str) and cached.strip():
        nickname = cached
    if adapter is not None and account != live_account:
        return ""
    snapshots = getattr(adapter, "member_snapshots", {})
    if isinstance(snapshots, dict):
        latest = -float("inf")
        for owner, checked, roster in snapshots.values():
            if owner != account or checked < latest or not roster.get("complete"):
                continue
            for member in roster.get("groupMemberInfo", []):
                if str(member.get("userId", "")) != account:
                    continue
                peer = str(getattr(adapter, "nim_account", ""))
                if peer and str(member.get("nimId", "")) != peer:
                    continue
                value = member.get("userNick")
                if isinstance(value, str) and value.strip():
                    nickname, latest = value, checked
    if not isinstance(nickname, str):
        return ""
    return " ".join(
        "".join(
            c for c in nickname if not unicodedata.category(c).startswith("C")
        ).split()
    )[:128]
