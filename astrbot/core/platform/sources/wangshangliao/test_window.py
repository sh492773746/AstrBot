"""Instance-bound, bounded developer windows shared by ingestion and plugins."""

import time

from .wire import ProtocolError


class TestWindow:
    """Hold volatile test permissions; never grant administrator capabilities."""

    def __init__(self, receiver, sender, groups, scopes, keywords, seconds, budget):
        if (
            receiver is sender
            or receiver.account == sender.account
            or type(seconds) is not int
            or not 1 <= seconds <= 300
            or type(budget) is not int
            or not 1 <= budget <= 10
            or not isinstance(groups, list)
            or not groups
            or any(not isinstance(g, str) or g.startswith("private/") for g in groups)
            or not set(groups) <= set(receiver.enabled_groups)
            or not isinstance(scopes, list)
            or not scopes
            or any(not isinstance(s, str) for s in scopes)
            or not set(scopes)
            <= {
                "group_commands",
                "group_ai",
                "group_rules",
            }
            or not isinstance(keywords, list)
            or len(keywords) > 10
            or any(
                not isinstance(w, str) or not w.startswith("WSL_TEST_") or len(w) > 100
                for w in keywords
            )
            or ("group_rules" in scopes and not keywords)
        ):
            raise ProtocolError("test_window_arguments")
        self.receiver, self.sender = receiver, sender
        self.policy_epoch = getattr(receiver, "config", {}).get("_business_epoch")
        self.identities = tuple(
            (p.account, getattr(p, "nim_account", None)) for p in (receiver, sender)
        )
        self.groups, self.scopes, self.keywords = (
            list(groups),
            list(scopes),
            list(keywords),
        )
        self.expires = time.monotonic() + seconds
        self.remaining = budget
        self.outbound_remaining = budget
        self.outbound_keys = set()
        self.accepted = {}
        self.replies = {}

    def active(self):
        """Return whether both original instances and the deadline remain valid."""
        return (
            time.monotonic() < self.expires
            and getattr(self.receiver, "config", {}).get("_business_active")
            is not False
            and self.policy_epoch
            == getattr(self.receiver, "config", {}).get("_business_epoch")
            and (
                self.policy_epoch is None
                or getattr(self.receiver, "test_window", None) is self
            )
            and not self.receiver.stopping.is_set()
            and not self.sender.stopping.is_set()
            and self.identities
            == tuple(
                (p.account, getattr(p, "nim_account", None))
                for p in (self.receiver, self.sender)
            )
        )

    def admit(self, group, mid, payload):
        """Reserve one ingress budget for an exact native message.

        Args:
            group: Business group ID.
            mid: Stable inbound message ID.
            payload: Authenticated native payload.

        Returns:
            Accepted scope or an empty string.
        """
        key = (group, mid)
        if (
            group.startswith("private/")
            or group not in self.groups
            or not self.active()
            or str(payload.get("sender")) != self.sender.account
        ):
            return ""
        if key in self.accepted:
            return self.accepted[key]
        if self.remaining <= 0:
            return ""
        text = payload.get("text", "").strip()
        encoded = payload.get("text", "").encode("utf-16-le")
        for span in payload.get("mention_spans", []):
            end = span.get("end", 0)
            if (
                span.get("uid") == str(getattr(self.receiver, "nim_account", ""))
                and span.get("start") == 0
                and type(end) is int
                and 0 < end * 2 <= len(encoded)
            ):
                prefix = encoded[: end * 2].decode("utf-16-le", errors="replace")
                if prefix.rstrip() == "@" + span.get("nick", ""):
                    text = (
                        encoded[end * 2 :].decode("utf-16-le", errors="replace").strip()
                    )
                    break
        from .runtime_policy import runtime

        command = bool(runtime.recognize(text)) or self.lottery_command(group, text)
        scope = "group_commands"
        if not command:
            if (
                "group_ai" in self.scopes
                and str(getattr(self.receiver, "nim_account", ""))
                in payload.get("mentions", [])
                and text
                and not text.startswith("/")
            ):
                scope = "group_ai"
            else:
                scope = "group_rules"
                if not any(w in text for w in self.keywords):
                    return ""
        if scope not in self.scopes:
            return ""
        self.remaining -= 1
        self.accepted[key] = scope
        return scope

    def lottery_command(self, group, text):
        """Match only the phrase frozen by the dedicated controlled draw.

        Args:
            group: Native business group ID.
            text: Complete message text.

        Returns:
            Whether this exact phrase belongs to the original bounded fixture.
        """
        binding = getattr(self, "_lottery_entry", None)
        return bool(
            self.active()
            and self.receiver.account == "43506527"
            and self.sender.account == "23691273"
            and group == "1459282"
            and self.groups == [group]
            and self.scopes == ["group_commands"]
            and getattr(self.receiver, "test_window", None) is self
            and isinstance(binding, tuple)
            and len(binding) == 2
            and isinstance(binding[0], str)
            and binding[0]
            and isinstance(binding[1], str)
            and binding[1].startswith("WSL_TEST_")
            and 1 <= len(binding[1]) <= 32
            and isinstance(text, str)
            and text.strip() == binding[1]
            and getattr(self.receiver, "_lottery_test_fixture", None)
            == (binding[0], self)
        )

    def reply(self, group, mid, operation=None):
        """Consume the single result reply associated with an admitted command.

        Args:
            group: Original incoming native session key.
            mid: Original stable message ID.

        Returns:
            Whether one command result may be transmitted.
        """
        key = (group, mid)
        if (
            group.startswith("private/")
            or group not in self.groups
            or not self.active()
            or (
                key in self.replies
                and (operation is None or self.replies[key] != operation)
            )
            or self.accepted.get(key) not in {"group_commands", "group_ai"}
        ):
            return False
        self.replies[key] = operation
        return True

    def status(self):
        """Return public window status without authentication data."""
        return {
            "active": self.active() and self.remaining > 0,
            "remaining": self.remaining if self.active() else 0,
            "seconds": max(0, int(self.expires - time.monotonic())),
            "sender_instance": self.sender.config["id"],
            "groups": self.groups,
            "scopes": self.scopes,
        }
