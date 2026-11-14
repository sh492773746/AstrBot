"""Retained group-image witnesses use saved rule assets, not deleted guide menus."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from astrbot.builtin_stars.wangshangliao_moderation.feature_image import ImageMessage
from astrbot.core.platform.sources.wangshangliao import wire
from tests.test_wangshangliao_advertisement_images import (
    configuration,
    upload,
)
from tests.test_wangshangliao_business_rules import rules as rules
from tests.test_wangshangliao_business_rules import save_rules
from tests.test_wangshangliao_first_use import decode_image
from tests.test_wangshangliao_group_ai import tenant
from tests.test_wangshangliao_test_witness_revocation import witness as witness


@pytest_asyncio.fixture
async def rule_image(rules, witness, monkeypatch):
    """Send a saved rule image through fake native IO and retain actual ciphertext.

    Args:
        rules: Temporary group rules and media store.
        witness: Isolated native observer, ledger and developer window.
        monkeypatch: Scoped sender authorization override.

    Returns:
        Witness scope, decoded group image, native envelope and saved image row.
    """
    account, group = "43506527", "1459282"
    window, _, outer = witness.reply_window(witness)
    witness.service.rule_replies = rules
    tenant(rules.store, account, group)
    reference = upload(rules, account=account, group=group)
    config = configuration([reference])
    config["ads"][0]["id"] = "WSL_TEST_ACTIVITY_image_witness"
    save_rules(rules, config, account=account, group=group)
    operation = f"advertisement/{account}/{group}/{config['ads'][0]['id']}/123"
    with rules.store.connect(write=True) as db:
        db.execute(
            "UPDATE business_rule_images SET asset='12345',state='ready' WHERE id=?",
            (reference,),
        )
        image = dict(
            db.execute(
                "SELECT * FROM business_rule_images WHERE id=?", (reference,)
            ).fetchone()
        )
        db.execute(
            "INSERT INTO business_rule_sends VALUES(?,?,?,'advertisement',?,?,'dispatched',?,'')",
            (
                operation,
                account,
                group,
                config["ads"][0]["id"],
                time.time(),
                time.time(),
            ),
        )
    monkeypatch.setattr(rules, "send_guard", lambda *args, **kwargs: None)
    receiver = witness.receiver
    receiver.business = SimpleNamespace(
        deployment=SimpleNamespace(key=lambda name: bytes(32))
    )
    receiver.ledger = witness.sender.ledger
    receiver.send_lock = asyncio.Lock()
    receiver.nim = SimpleNamespace(
        request=AsyncMock(
            return_value=(200, wire.properties([(12, b"12345"), (7, b"1000")]))
        )
    )
    assert (
        await rules.media.send(
            receiver,
            group,
            reference,
            operation + "/image",
            lambda: None,
            proactive=False,
        )
        == "accepted"
    )
    props, _ = wire.read_properties(receiver.nim.request.call_args.args[2])
    outer[10], outer[11] = props[10].decode(), props[11].decode()
    decoded = decode_image(props[10], bytes(32))
    assert decoded.created_at >= window._witness_since
    return witness, decoded, outer, image


@pytest.mark.asyncio
async def test_native_image_witness_records_only_verified_rule_metadata(rule_image):
    scope, image, outer, saved = rule_image
    native = wire.ApplicationMessage.FromString(image.SerializeToString())
    assert scope.probe.observe_reply(
        scope.service, scope.sender, "1459282", outer, native
    )
    await asyncio.gather(*scope.service.candidate_tasks)
    async with scope.sender.ledger.db.execute(
        "SELECT state,payload FROM inbox WHERE account=? AND message=?",
        ("23691273", "12345"),
    ) as cursor:
        state, payload = await cursor.fetchone()
    data = json.loads(payload)
    assert state == "ignored_bot" and data["text"] == ""
    assert data["image"]["digest"] == saved["digest"]
    assert data["image"]["asset"] == saved["asset"]
    assert data["_test_window"] == "fixed-test-window"
    scope.sender.nim.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_rule_image_witness_uses_native_decode_and_inactive_group_ingress(
    rule_image, monkeypatch
):
    from astrbot.core.platform.sources.wangshangliao import runtime_policy
    from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter

    scope, _, outer, saved = rule_image
    observer = scope.sender
    observer.groups.clear()
    observer.receive_health = {"decoded_messages": 0}
    observer.business = SimpleNamespace(
        deployment=SimpleNamespace(key=lambda name: bytes(32))
    )
    observer.diagnostics = SimpleNamespace(emit=Mock(), observe=Mock())
    observer.nim.acknowledge = AsyncMock()
    scope.service.admit_candidate = lambda adapter, group, envelope, decoded: (
        scope.probe.observe_reply(scope.service, adapter, group, envelope, decoded)
    )
    monkeypatch.setattr(
        runtime_policy, "runtime", SimpleNamespace(open_service=scope.service)
    )
    await WangshangliaoAdapter.ingest(observer, outer)
    await asyncio.gather(*scope.service.candidate_tasks)
    observer.nim.acknowledge.assert_awaited_once_with(1, "12345")
    async with observer.ledger.db.execute(
        "SELECT state,payload FROM inbox WHERE account=? AND message=?",
        ("23691273", "12345"),
    ) as cursor:
        state, payload = await cursor.fetchone()
    assert (
        state == "ignored_bot"
        and json.loads(payload)["image"]["digest"] == saved["digest"]
    )
    assert observer.groups == {} and observer.receive_health["decoded_messages"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ("asset", "width", "size", "sticker", "text", "empty")
)
async def test_rule_image_witness_rejects_unverified_or_ambiguous_images(
    rule_image, fault
):
    scope, sent, outer, _ = rule_image
    image = ImageMessage.FromString(sent.SerializeToString())
    if fault == "asset":
        image.image.asset.uri = "https://untrusted.invalid/image"
    elif fault == "width":
        image.image.width = 1
    elif fault == "size":
        image.image.size = 0
    elif fault == "sticker":
        image.image.is_sticker = True
    elif fault == "text":
        image.content.data = "unexpected text"
    else:
        image.ClearField("image")
    native = wire.ApplicationMessage.FromString(image.SerializeToString())
    assert not scope.probe.observe_reply(
        scope.service, scope.sender, "1459282", outer, native
    )
    assert not scope.service.candidate_tasks
