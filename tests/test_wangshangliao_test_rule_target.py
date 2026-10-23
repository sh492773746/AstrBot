"""Managed-account exceptions require a live, exact, native test admission."""

import asyncio
import hashlib
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot.core.platform.sources.wangshangliao.group_scope import scoped
from astrbot.core.platform.sources.wangshangliao.test_rules import (
    rule_message,
    rule_target,
)
from astrbot.core.platform.sources.wangshangliao.test_window import TestWindow as Window


@pytest.fixture
def rule_window():
    sender = SimpleNamespace(
        account="23691273", nim_account="111", stopping=asyncio.Event()
    )
    receiver = SimpleNamespace(
        account="43506527",
        nim_account="222",
        enabled_groups=["1459282"],
        members={"1459282": {"23691273": "111"}},
        config={"_business_epoch": 7, "_open_groups": {}},
        stopping=asyncio.Event(),
    )
    window = Window(
        receiver, sender, ["1459282"], ["group_rules"], ["WSL_TEST_"], 60, 10
    )
    receiver.test_window = window
    window._rule_generation = "controlled-rule-window"
    assert (
        window.admit(
            "1459282", "100", {"sender": "23691273", "text": "WSL_TEST_candidate"}
        )
        == "group_rules"
    )
    return receiver, sender, window


def test_exact_admitted_message_and_scoped_adapter_are_allowed(rule_window):
    receiver, _, _ = rule_window
    assert rule_target(receiver, "1459282", "23691273", "100")
    assert rule_target(scoped(receiver, "1459282"), "1459282", "23691273", "100")


def test_progressive_native_boundary_recovers_only_exact_admitted_intent(rule_window):
    receiver, _, window = rule_window
    operation = (
        "progressive/"
        + hashlib.sha256(
            json.dumps(["43506527", "1459282", "23691273", "100"]).encode()
        ).hexdigest()
    )
    assert rule_message(receiver, "1459282", "23691273", operation) == "100"
    assert rule_message(receiver, "1516387", "23691273", operation) == ""
    assert rule_message(receiver, "1459282", "31846829", operation) == ""
    assert rule_message(receiver, "1459282", "23691273", "progressive/fake") == ""
    window.expires = 0
    assert rule_message(receiver, "1459282", "23691273", operation) == ""


@pytest.mark.parametrize(
    "change",
    (
        "missing",
        "expired",
        "closed",
        "epoch",
        "peer",
        "sender_account",
        "sender_nim",
        "receiver_account",
        "receiver_nim",
        "sender_stopping",
        "receiver_stopping",
        "scope",
        "group",
        "member",
        "message",
    ),
)
def test_rule_exception_cannot_outlive_or_escape_its_admission(rule_window, change):
    receiver, sender, window = rule_window
    group, member, message = "1459282", "23691273", "100"
    if change == "missing":
        receiver.test_window = None
    elif change == "expired":
        window.expires = 0
    elif change == "closed":
        receiver.test_window = SimpleNamespace()
    elif change == "epoch":
        receiver.config["_business_epoch"] += 1
    elif change == "peer":
        receiver.members[group][member] = "999"
    elif change == "sender_account":
        sender.account = "999"
    elif change == "sender_nim":
        sender.nim_account = "999"
    elif change == "receiver_account":
        receiver.account = "999"
    elif change == "receiver_nim":
        receiver.nim_account = "999"
    elif change == "sender_stopping":
        sender.stopping.set()
    elif change == "receiver_stopping":
        receiver.stopping.set()
    elif change == "scope":
        window.accepted[(group, message)] = "group_commands"
    elif change == "group":
        group = "1516387"
    elif change == "member":
        member = "31846829"
    elif change == "message":
        message = "101"
    assert not rule_target(receiver, group, member, message)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("expired", "closed", "replaced"))
async def test_warning_cannot_follow_a_revoked_rule_window(
    rule_window, tmp_path, monkeypatch, change
):
    from astrbot.builtin_stars.wangshangliao_moderation import content_rules
    from astrbot.core.platform.sources.wangshangliao import progressive

    receiver, _, window = rule_window
    receiver.config.update(
        id="offline-rule-warning",
        enabled_groups=["1459282"],
        moderation={
            "enabled": True,
            "automation_enabled": True,
            "progressive_mute": True,
            "recall_enabled": True,
            "content_rules_since": time.time() - 60,
            "mute_keywords": ["WSL_TEST_"],
            "permissions": {"1459282": ["recall", "mute"]},
        },
    )
    receiver.diagnostics = SimpleNamespace(emit=Mock())
    receiver.execute_action = AsyncMock()
    receiver.get_moderation_members = AsyncMock(
        return_value={
            "complete": True,
            "groupMemberInfo": [
                {
                    "userId": "23691273",
                    "nimId": "111",
                    "groupRole": "GROUP_ROLE_MEMBER",
                }
            ],
        }
    )

    async def revoke(*args, **kwargs):
        if change == "expired":
            window.expires = 0
        elif change == "closed":
            receiver.test_window = None
        else:
            receiver.test_window = SimpleNamespace()
        return {
            "status": "accepted",
            "action": "recall",
            "warning_number": 1,
            "recall_status": "accepted",
        }

    monkeypatch.setattr(content_rules, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(progressive, "enforce", AsyncMock(side_effect=revoke))
    assert await content_rules.enforce(
        receiver,
        "1459282",
        "100",
        {
            "sender": "23691273",
            "text": "WSL_TEST_candidate",
            "_test_rule_window": window._rule_generation,
            "recall_route": {"time": str(int(time.time() * 1000))},
        },
    )
    progressive.enforce.assert_awaited_once()
    receiver.execute_action.assert_not_awaited()
