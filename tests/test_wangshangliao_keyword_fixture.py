"""Neutral keyword acceptance never bypasses production managed-account guards."""

import asyncio
import copy
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.open_store import (
    OpenError,
    OpenStore,
)
from astrbot.builtin_stars.wangshangliao_moderation.rule_replies import RuleReplies
from astrbot.builtin_stars.wangshangliao_moderation.test_probe import (
    keyword_probe_guard,
)
from astrbot.core.platform.sources.wangshangliao.identity import ActorIdentity
from astrbot.core.platform.sources.wangshangliao.test_window import TestWindow as Window

MARKER = "WSL_TEST_ACTIVITY_" + "a" * 32
R, OWNER, T, G = "43506527", "31846829", "23691273", "1459282"


@pytest.fixture
def lab(tmp_path, monkeypatch):
    bots = {
        uid: SimpleNamespace(
            account=uid,
            nim_account=str(1000 + index),
            config={
                "id": "instance-" + uid,
                "_business_epoch": 1,
                "_business_active": True,
            },
            connection_state="online",
            stopping=asyncio.Event(),
            enabled_groups=[G],
        )
        for index, uid in enumerate((R, OWNER, T))
    }
    receiver, sender = bots[R], bots[T]
    receiver.members = {G: {T: sender.nim_account}}
    receiver.send_reply_text = AsyncMock(return_value="accepted")
    window = receiver.test_window = Window(
        receiver, sender, [G], ["group_rules"], [MARKER], 300, 10
    )
    key = "test-probe/" + MARKER + "_trigger_A"
    window.outbound_keys.add(key)
    window.accepted[(G, "100")] = "group_rules"
    window._rule_generation = "original-generation"
    payload = {
        "text": MARKER + "_A",
        "sender": T,
        "_test_rule_window": window._rule_generation,
        "recall_route": {
            "peer": sender.nim_account,
            "client": str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"{sender.config['id']}/{key}")
            ),
        },
    }
    extras = {"wangshangliao_payload": payload, "wsl_test_scope": "group_rules"}
    event = SimpleNamespace(
        platform=receiver,
        actor_identity=ActorIdentity(R, T, sender.nim_account, T),
        message_str=payload["text"],
        message_obj=SimpleNamespace(message_id="100"),
        get_group_id=lambda: G,
        get_sender_id=lambda: T,
        is_private_chat=lambda: False,
        get_extra=extras.get,
        set_extra=extras.__setitem__,
    )
    rule = {
        "id": MARKER + "_keyword_A",
        "enabled": True,
        "match": "exact",
        "terms": [payload["text"]],
        "text": MARKER + "_reply_A",
        "images": ["b" * 32],
        "cooldown_seconds": 30,
    }
    service = SimpleNamespace(
        active=True,
        integrated=True,
        settings={"paused": 0},
        adapters=lambda: list(bots.values()),
        store=OpenStore(tmp_path),
        group_ai=SimpleNamespace(activity={}),
        capabilities={(R, G): {"checked_at": time.time()}},
    )
    rules = RuleReplies(service)
    monkeypatch.setattr(rules, "read", lambda *_: {"config": {"keywords": [rule]}})
    monkeypatch.setattr(rules, "send_guard", lambda *_: None)
    monkeypatch.setattr(
        "astrbot.builtin_stars.wangshangliao_moderation.rule_replies.is_managed_account",
        lambda uid: uid in bots,
    )
    rules.media.send = AsyncMock(return_value="accepted")
    return SimpleNamespace(
        service=service,
        event=event,
        rule=rule,
        bots=bots,
        payload=payload,
        extras=extras,
        rules=rules,
        window=window,
        key=key,
    )


@pytest.mark.parametrize(
    "fault",
    [
        "window",
        "scope",
        "marker",
        "outbound",
        "ingress",
        "peer",
        "client",
        "generation",
        "other_group",
        "sender",
        "identity",
        "account_set",
        "rule_id",
        "rule_text",
        "rule_terms",
        "disabled",
        "without_image",
        "paused",
        "offline",
    ],
)
def test_keyword_fixture_rejects_every_broadened_or_unproven_scope(lab, fault):
    r, w, rule = lab.bots[R], lab.window, copy.deepcopy(lab.rule)
    if fault == "window":
        r.test_window = None
    elif fault == "scope":
        w.scopes = ["group_rules", "group_commands"]
    elif fault == "marker":
        w.keywords = ["ordinary"]
    elif fault == "outbound":
        w.outbound_keys.clear()
    elif fault == "ingress":
        w.accepted.clear()
    elif fault in {"peer", "client"}:
        lab.payload["recall_route"][fault] = "different"
    elif fault == "generation":
        lab.payload["_test_rule_window"] = "old-generation"
    elif fault == "other_group":
        lab.event.get_group_id = lambda: "1516387"
    elif fault == "sender":
        lab.event.get_sender_id = lambda: OWNER
    elif fault == "identity":
        r.account = "other"
    elif fault == "account_set":
        lab.service.adapters = lambda: [r, lab.bots[T]]
    elif fault == "rule_id":
        rule["id"] = "production"
    elif fault == "rule_text":
        rule["text"] = "other content"
    elif fault == "rule_terms":
        rule["terms"].append("other")
    elif fault == "disabled":
        rule["enabled"] = False
    elif fault == "without_image":
        rule["images"] = []
    elif fault == "paused":
        lab.service.settings["paused"] = 1
    elif fault == "offline":
        lab.bots[T].connection_state = "stopped"
    with pytest.raises(OpenError, match="test_keyword"):
        keyword_probe_guard(lab.service, lab.event, rule)


def test_keyword_fixture_rechecks_window_after_binding(lab):
    guard = keyword_probe_guard(lab.service, lab.event, lab.rule)
    guard()
    lab.bots[R].test_window = None
    with pytest.raises(OpenError, match="revoked"):
        guard()


@pytest.mark.asyncio
async def test_exact_neutral_fixture_sends_one_image_then_one_text(lab):
    order = []

    async def image(*args, **kwargs):
        args[4]()
        order.append("image")
        return "accepted"

    async def text(*args, **kwargs):
        kwargs["authorize"]()
        order.append("text")
        return "accepted"

    lab.rules.media.send.side_effect = image
    lab.bots[R].send_reply_text.side_effect = text
    assert await lab.rules.observe(lab.event)
    assert order == ["image", "text"]


@pytest.mark.asyncio
async def test_closed_window_after_image_blocks_text(lab):
    async def image(*args, **kwargs):
        args[4]()
        lab.bots[R].test_window = None
        return "accepted"

    lab.rules.media.send.side_effect = image
    assert await lab.rules.observe(lab.event)
    lab.bots[R].send_reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_ordinary_managed_traffic_stays_ignored(lab):
    lab.extras["wsl_test_scope"] = ""
    assert not await lab.rules.observe(lab.event)
    lab.rules.media.send.assert_not_awaited()
    lab.bots[R].send_reply_text.assert_not_awaited()
