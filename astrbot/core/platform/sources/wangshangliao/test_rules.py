"""Narrow rule-test authority for the three existing controlled accounts."""

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .group_scope import GroupAdapter
from .storage import instance_dir
from .test_window import TestWindow
from .wire import ProtocolError


@dataclass(frozen=True)
class AdvertisingFixture:
    """Bind fresh sanction storage to one authenticated controlled test window."""

    window: TestWindow
    operation: str
    path: Path
    authorize: Callable[[], None]


def advertising_path(adapter, group, member, message):
    """Resolve only a dashboard-bound test ledger, never a caller-selected path.

    Args:
        adapter: Original or group-scoped native receiver.
        group: Actual incoming business group.
        member: Authenticated incoming business member.
        message: Actual admitted message identifier.

    Returns:
        Fixed isolated sanction ledger path, or None for ordinary processing.

    Raises:
        ProtocolError: If a bound fixture loses its original window or authority.
    """
    receiver = adapter.adapter if isinstance(adapter, GroupAdapter) else adapter
    window = getattr(receiver, "test_window", None)
    fixture = getattr(window, "_advertising_fixture", None)
    if fixture is None or (group, member) != ("1459282", "23691273"):
        return None
    if (
        type(fixture) is not AdvertisingFixture
        or fixture.window is not window
        or not re.fullmatch(r"WSL_TEST_AD_[0-9a-f]{32}", fixture.operation)
        or not rule_target(adapter, group, member, message)
        or window.scopes != ["group_rules"]
        or window.keywords != [fixture.operation]
        or fixture.path
        != instance_dir(receiver.config["id"])
        / "test-advertising"
        / fixture.operation
        / "moderation.sqlite3"
    ):
        raise ProtocolError("test_advertising_scope")
    fixture.authorize()
    return fixture.path


def rule_target(adapter, group, member, message):
    """Allow only an exact native message admitted by the original test window.

    This does not grant native permissions or authorize manual member operations.
    Every caller must still run its normal identity, policy and execution guards.
    """
    receiver = adapter.adapter if isinstance(adapter, GroupAdapter) else adapter
    window = getattr(receiver, "test_window", None)
    return bool(
        isinstance(window, TestWindow)
        and window.receiver is receiver
        and receiver.account == "43506527"
        and group == "1459282"
        and window.groups == [group]
        and member == "23691273"
        and window.sender.account == member
        and receiver.members.get(group, {}).get(member)
        == str(window.sender.nim_account)
        and window.accepted.get((group, str(message))) == "group_rules"
        and window.active()
    )


def admit_rule_target(receiver, group, message, payload):
    """Reserve native ingress evidence without admitting unrelated bot traffic."""
    window = getattr(receiver, "test_window", None)
    member = str(payload.get("sender", ""))
    if (
        not isinstance(window, TestWindow)
        or receiver.account != "43506527"
        or group != "1459282"
        or window.groups != [group]
        or member != "23691273"
        or str(payload.get("recall_route", {}).get("peer", ""))
        != str(window.sender.nim_account)
        or receiver.members.get(group, {}).get(member) != str(window.sender.nim_account)
        or "group_rules" not in window.scopes
        or not any(word in str(payload.get("text", "")) for word in window.keywords)
    ):
        return False
    return window.admit(group, str(message), payload) == "group_rules" and rule_target(
        receiver, group, member, message
    )


def rule_message(adapter, group, member, operation):
    """Recover only an admitted message bound to this exact progressive intent."""
    receiver = adapter.adapter if isinstance(adapter, GroupAdapter) else adapter
    window = getattr(receiver, "test_window", None)
    if not isinstance(window, TestWindow) or not window.active():
        return ""
    for (origin, message), scope in window.accepted.items():
        if origin != group or scope != "group_rules":
            continue
        expected = (
            "progressive/"
            + hashlib.sha256(
                json.dumps([receiver.account, group, member, message]).encode()
            ).hexdigest()
        )
        if operation == expected and rule_target(receiver, group, member, message):
            return message
    return ""
