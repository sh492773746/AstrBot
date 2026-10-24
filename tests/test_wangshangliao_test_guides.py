"""Offline tests for the fixed three-account native guide rehearsal."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from test_wangshangliao_three_live_probe import reply_window
from test_wangshangliao_three_live_probe import scope as scope

from astrbot.builtin_stars.wangshangliao_moderation import test_probe
from astrbot.builtin_stars.wangshangliao_moderation.feature_image import (
    REVIEWED_ASSETS,
    ImageMessage,
    seal_guide,
)
from astrbot.builtin_stars.wangshangliao_moderation.native_guide_upload import (
    ONBOARDING_GUIDES,
)
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from astrbot.core.platform.sources.wangshangliao import wire


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("instructions", "examples"))
async def test_native_image_witness_records_only_reviewed_asset_metadata(scope, kind):
    _, inner, outer = reply_window(scope)
    image = ImageMessage.FromString(inner.SerializeToString())
    image.ClearField("content")
    image.format = 1
    digest = ONBOARDING_GUIDES[kind][1]
    image.image.asset.uri = REVIEWED_ASSETS[digest]
    image.image.width = image.image.height = 1024
    image.image.size = 200000
    native = wire.ApplicationMessage.FromString(image.SerializeToString())
    assert test_probe.observe_reply(
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
    assert data["image"]["digest"] == digest
    assert data["image"]["asset"] == REVIEWED_ASSETS[digest]
    assert data["_test_window"] == "fixed-test-window"
    scope.sender.nim.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_image_witness_uses_actual_native_decode_and_inactive_group_ingress(
    scope, monkeypatch
):
    from astrbot.core.platform.sources.wangshangliao import runtime_policy
    from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter

    _, inner, outer = reply_window(scope)
    image = ImageMessage.FromString(inner.SerializeToString())
    image.ClearField("content")
    image.format = 1
    digest = ONBOARDING_GUIDES["instructions"][1]
    image.image.asset.uri = REVIEWED_ASSETS[digest]
    image.image.width = image.image.height = 1024
    image.image.size = 215056
    observer = scope.sender
    observer.groups.clear()
    observer.receive_health = {"decoded_messages": 0}
    observer.business = SimpleNamespace(
        deployment=SimpleNamespace(key=lambda _: bytes(32))
    )
    observer.diagnostics = SimpleNamespace(emit=Mock())
    observer.nim.acknowledge = AsyncMock()
    scope.service.admit_candidate = lambda adapter, group, envelope, decoded: (
        test_probe.observe_reply(scope.service, adapter, group, envelope, decoded)
    )
    monkeypatch.setattr(
        runtime_policy, "runtime", SimpleNamespace(open_service=scope.service)
    )
    outer[10] = seal_guide(bytes(32), image, 123, 456, 1000)
    await WangshangliaoAdapter.ingest(observer, outer)
    await asyncio.gather(*scope.service.candidate_tasks)
    observer.nim.acknowledge.assert_awaited_once_with(1, "12345")
    async with observer.ledger.db.execute(
        "SELECT state,payload FROM inbox WHERE account=? AND message=?",
        ("23691273", "12345"),
    ) as cursor:
        state, payload = await cursor.fetchone()
    assert state == "ignored_bot" and json.loads(payload)["image"]["digest"] == digest
    assert observer.groups == {} and observer.receive_health["decoded_messages"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ("asset", "width", "size", "sticker", "text", "empty")
)
async def test_native_image_witness_rejects_unreviewed_or_ambiguous_images(
    scope, fault
):
    _, inner, outer = reply_window(scope)
    image = ImageMessage.FromString(inner.SerializeToString())
    image.ClearField("content")
    image.format = 1
    image.image.asset.uri = REVIEWED_ASSETS[ONBOARDING_GUIDES["instructions"][1]]
    image.image.width = image.image.height = 1024
    image.image.size = 200000
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
    assert not test_probe.observe_reply(
        scope.service, scope.sender, "1459282", outer, native
    )
    assert not scope.service.candidate_tasks


@pytest.fixture
def guides(scope, monkeypatch):
    """Bind a temporary real ledger and mocked transport, without live credentials.

    Args:
        scope: Existing native test fixture.
        monkeypatch: Scoped module replacements.

    Returns:
        Scope with a fixed operation and an observable two-image sender.
    """
    reply_window(scope)
    receiver, service = scope.receiver, scope.service
    receiver.ledger = scope.sender.ledger
    receiver.nim = object()
    receiver.group_catalog = {"1459282": {"team": "555"}}
    service.suspended_groups = set()
    service.tenants = {
        ("43506527", "1459282"): {
            "active": True,
            "present": True,
            "paused": False,
            "epoch": 1,
            "version": 2,
        }
    }
    service.store = SimpleNamespace(
        tenant=lambda account, group: dict(service.tenants[(account, group)])
    )
    for member in scope.members:
        member["accountState"] = "ACCOUNT_STATE_GOOD"
    scope.payload = {"operation_id": "WSL_TEST_GUIDES_abcdefgh"}
    monkeypatch.setattr(
        "astrbot.core.platform.sources.wangshangliao.database.account_guard",
        lambda adapter: lambda: None,
    )

    async def send(adapter, group, team, key, guard):
        for kind in ("instructions", "examples"):
            guard()
            _, state = await adapter.ledger.reserve(key + "/guide-" + kind, kind)
            if state == "new":
                await adapter.ledger.finish(key + "/guide-" + kind, "accepted", kind)
        return "accepted"

    scope.send_guides = AsyncMock(side_effect=send)
    monkeypatch.setattr(
        "astrbot.builtin_stars.wangshangliao_moderation.feature_image.FeatureGuides.send_group",
        scope.send_guides,
    )
    return scope


@pytest.mark.asyncio
async def test_guide_rehearsal_is_bounded_idempotent_and_not_first_welcome(guides):
    window = guides.receiver.test_window
    first = await test_probe.send_guides(
        guides.service, "dashboard/test", guides.payload
    )
    second = await test_probe.send_guides(
        guides.service, "dashboard/test", guides.payload
    )
    assert first == second and first["status"] == "accepted"
    assert window.outbound_remaining == 8
    assert all(r["status"] == "accepted" for r in first["receipts"].values())
    assert not first["first_welcome"] and not first["phone_display_verified"]
    async with guides.sender.ledger.db.execute("SELECT COUNT(*) FROM outbox") as cursor:
        assert (await cursor.fetchone())[0] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    ("chat", "extra", "expired", "replaced", "paused", "departed", "budget", "roster"),
)
async def test_guide_rehearsal_refuses_changed_scope_before_sending(guides, fault):
    actor = "dashboard/test"
    if fault == "chat":
        actor = "nim/333"
    elif fault == "extra":
        guides.payload["url"] = "https://untrusted.invalid"
    elif fault == "expired":
        guides.receiver.test_window.expires = 0
    elif fault == "replaced":
        guides.receiver.test_window = None
    elif fault == "paused":
        guides.service.tenants[("43506527", "1459282")]["paused"] = True
    elif fault == "departed":
        guides.service.tenants[("43506527", "1459282")]["present"] = False
    elif fault == "budget":
        guides.receiver.test_window.outbound_remaining = 1
    else:
        guides.members.append(
            {"userId": "999", "nimId": "999", "groupRole": "GROUP_ROLE_MEMBER"}
        )
    with pytest.raises(OpenError):
        await test_probe.send_guides(guides.service, actor, guides.payload)
    guides.send_guides.assert_not_awaited()


@pytest.mark.asyncio
async def test_guide_rehearsal_rechecks_after_roster_wait(guides):
    async def revoke(*args):
        guides.receiver.test_window.expires = 0
        return {"complete": True, "groupMemberInfo": guides.members}

    guides.directory.side_effect = revoke
    with pytest.raises(OpenError, match="test_guides_window"):
        await test_probe.send_guides(guides.service, "dashboard/test", guides.payload)
    guides.send_guides.assert_not_awaited()


@pytest.mark.asyncio
async def test_guide_rehearsal_never_adopts_unknown_previous_window_send(guides):
    key = "test-guides/" + guides.payload["operation_id"] + "/guide-instructions"
    await guides.receiver.ledger.reserve(key, "prior attempt")
    await guides.receiver.ledger.finish(key, "unknown")
    with pytest.raises(OpenError, match="test_guides_historical"):
        await test_probe.send_guides(guides.service, "dashboard/test", guides.payload)
    guides.send_guides.assert_not_awaited()
