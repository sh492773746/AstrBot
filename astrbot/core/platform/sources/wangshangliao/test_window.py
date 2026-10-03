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
            or any(not isinstance(g, str) for g in groups)
            or not set(groups) <= set(receiver.enabled_groups)
            or not isinstance(scopes, list)
            or not scopes
            or any(not isinstance(s, str) for s in scopes)
            or not set(scopes)
            <= {
                "private_commands",
                "private_ai",
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
            or (any(s.startswith("group_") for s in scopes) and not groups)
        ):
            raise ProtocolError("test_window_arguments")
        self.receiver, self.sender = receiver, sender
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
            group: Native session key or business group ID.
            mid: Stable inbound message ID.
            payload: Authenticated native payload.

        Returns:
            Accepted scope or an empty string.
        """
        key = (group, mid)
        if not self.active() or str(payload.get("sender")) != self.sender.account:
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
        from astrbot.builtin_stars.wangshangliao_moderation.syntax import (
            recognize_command,
        )

        command = bool(recognize_command(text))
        if group.startswith("private/") and text in {"/help", "/帮助"}:
            command = True
        scope = "private_commands" if group.startswith("private/") else "group_commands"
        if not group.startswith("private/") and group not in self.groups:
            return ""
        if not command:
            if group.startswith("private/"):
                if not text or text.startswith("/"):
                    return ""
                scope = "private_ai"
            elif (
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
            not self.active()
            or (
                key in self.replies
                and (operation is None or self.replies[key] != operation)
            )
            or self.accepted.get(key)
            not in {"private_commands", "private_ai", "group_commands", "group_ai"}
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

    def send_command(self, sender, target, text, key, *, consume=False):
        """Allow bounded private commands toward the window's receiver only.

        Args:
            sender: Original sending adapter instance.
            target: Native private session key including verified NIM identity.
            text: Complete outgoing command.
            key: Persistent outbox operation key.
            consume: Reserve one attempt before transport.

        Returns:
            Whether this exact command attempt is permitted.
        """
        from astrbot.builtin_stars.wangshangliao_moderation.syntax import (
            recognize_command,
        )

        from .wire import b64

        expected = f"private/{self.receiver.account}/{b64(str(self.receiver.nim_account).encode())}"
        command = text.strip()
        if (
            sender is not self.sender
            or getattr(self.receiver, "test_window", None) is not self
            or not self.active()
            or target != expected
            or not (
                (
                    "private_commands" in self.scopes
                    and (
                        recognize_command(command)
                        or command in {"/群管", "/群管帮助", "/help", "/帮助"}
                        or command.startswith("/群管 ")
                    )
                )
                or (
                    "private_ai" in self.scopes
                    and command
                    and not command.startswith("/")
                    and not recognize_command(command)
                )
            )
            or self.remaining <= 0
            or (key not in self.outbound_keys and self.outbound_remaining <= 0)
        ):
            return False
        if consume and key not in self.outbound_keys:
            self.outbound_keys.add(key)
            self.outbound_remaining -= 1
        return True
