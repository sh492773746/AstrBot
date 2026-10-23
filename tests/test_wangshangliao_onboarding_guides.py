"""Two fixed native guides, exact-group activation and durable welcome safety."""

import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import (
    feature_image,
    native_guide_upload,
    private_interaction,
)
from astrbot.builtin_stars.wangshangliao_moderation.open_service import (
    ActivationContext,
)
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from astrbot.core.platform.sources.wangshangliao import wire
from tests.test_wangshangliao_first_use import decode_image
from tests.test_wangshangliao_first_use import image_event as image_event
from tests.test_wangshangliao_native_guide_upload import (
    Response,
)
from tests.test_wangshangliao_native_guide_upload import transport as transport
from tests.test_wangshangliao_public_hardening import invitation
from tests.test_wangshangliao_public_hardening import public as public


@pytest.fixture
def pair(image_event, monkeypatch, tmp_path):
    event, ledger, calls = image_event
    images = feature_image.FeatureGuides()
    monkeypatch.setattr(
        feature_image,
        "REVIEWED_ASSETS",
        {image.digest: str(301 + i) for i, image in enumerate(images.images)},
    )
    monkeypatch.setattr(
        private_interaction, "instance_dir", lambda instance: tmp_path / instance
    )
    event.platform.group_catalog = {"5": {"team": "1005"}}
    return private_interaction.PrivateInteraction(images), event, ledger, calls


@pytest.mark.parametrize("kind", ["instructions", "examples"])
def test_new_artwork_has_fixed_dimensions_digest_and_reviewed_native_asset(kind):
    path, data, digest = native_guide_upload.load_fixed_guide(kind=kind)
    assert path.name == kind + ".jpg"
    assert len(data) < 256 * 1024
    assert hashlib.sha256(data).hexdigest() == digest
    assert native_guide_upload.is_native_asset_id(feature_image.REVIEWED_ASSETS[digest])


@pytest.mark.parametrize("kind", [1, [], {}, "", "https://image.invalid/x", "../x"])
def test_artwork_selection_never_accepts_paths_urls_or_arbitrary_payloads(kind):
    with pytest.raises(wire.ProtocolError, match="guide_image_selection"):
        native_guide_upload.load_fixed_guide(kind=kind)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["instructions", "examples"])
async def test_two_new_fixed_images_use_existing_verified_upload_contract(
    transport, kind
):
    client, session, _ = transport
    _, data, digest = native_guide_upload.load_fixed_guide(kind=kind)
    session.responses = [
        Response(b'{"code":0,"id":"987654321"}'),
        Response(data),
        Response(data),
    ]
    asset = await native_guide_upload.upload_fixed_guide(
        client, kind=kind, authorize=Mock()
    )
    assert asset.digest == digest and asset.asset_id == "987654321"
    assert len(session.calls) == 3


@pytest.mark.asyncio
async def test_friend_pair_is_ordered_native_and_idempotent(pair):
    worker, event, _, calls = pair
    for _ in range(2):
        assert (
            await worker.images.send_friend(
                event.platform, "2", "902", "friend/once", lambda: None
            )
            == "accepted"
        )
    assert len(calls) == 2
    for index, (service, command, props) in enumerate(calls):
        assert (service, command) == (7, 1)
        image = decode_image(props[10].decode(), b"a" * 32)
        assert image.session == 1 and image.target.id == 2
        assert image.image.asset.uri == str(301 + index)


@pytest.mark.asyncio
async def test_first_friend_acceptance_sends_menu_and_both_images_once(
    pair, monkeypatch
):
    worker, event, _, calls = pair
    monkeypatch.setattr(private_interaction, "is_managed_account", lambda uid: False)
    assert (
        await worker.welcome_friend(event.platform, "2", "902", "request", lambda: None)
        == "accepted"
    )
    messages = [decode_image(props[10].decode(), b"a" * 32) for _, _, props in calls]
    assert [message.format for message in messages] == [0, 1, 1]
    assert all(message.session == 1 and message.target.id == 2 for message in messages)
    assert "①使用说明 ②对话示例" in messages[0].content.data
    assert [message.image.asset.uri for message in messages[1:]] == ["301", "302"]
    reloaded = private_interaction.PrivateInteraction(feature_image.FeatureGuides())
    assert (
        await reloaded.welcome_friend(
            event.platform, "2", "902", "different-request", lambda: None
        )
        == "already_sent"
    )
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_shutdown_during_friend_welcome_stops_second_image(pair, monkeypatch):
    worker, event, _, calls = pair
    monkeypatch.setattr(private_interaction, "is_managed_account", lambda uid: False)
    original = event.platform.nim.request.side_effect

    async def stop(service, command, props):
        result = await original(service, command, props)
        if len(calls) == 2:
            await worker.close()
        return result

    event.platform.nim.request.side_effect = stop
    assert (
        await worker.welcome_friend(event.platform, "2", "902", "request", lambda: None)
        == "delivery_unconfirmed"
    )
    assert len(calls) == 2 and worker.closed


@pytest.mark.asyncio
async def test_group_welcome_sends_menu_then_both_images_once_per_generation(pair):
    worker, event, ledger, calls = pair
    adapter = event.platform
    assert (
        await worker.welcome_group(
            adapter, "5", "1005", 1, "本群菜单\n3 审核规则", lambda: None
        )
        == "accepted"
    )
    assert len(calls) == 3
    messages = []
    for service, command, props in calls:
        assert (service, command) == (8, 2)
        assert props[0] == b"1" and props[1] == b"1005"
        messages.append(decode_image(props[10].decode(), b"a" * 32))
    assert [message.session for message in messages] == [2, 2, 2]
    assert [message.format for message in messages] == [0, 1, 1]
    assert messages[0].content.data == "本群菜单\n3 审核规则"
    assert [message.image.asset.uri for message in messages[1:]] == ["301", "302"]
    reloaded = private_interaction.PrivateInteraction(feature_image.FeatureGuides())
    assert (
        await reloaded.welcome_group(
            adapter, "5", "1005", 1, "本群菜单\n3 审核规则", lambda: None
        )
        == "already_sent"
    )
    assert len(calls) == 3
    receipt = await ledger.receipt("group-welcome/1/5/1/guide-examples")
    assert receipt["status"] == "accepted"


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["friend", "group"])
async def test_unknown_first_image_stops_second_and_never_replays(pair, route):
    worker, event, ledger, _ = pair
    adapter = event.platform
    adapter.nim.request.side_effect = TimeoutError
    if route == "friend":
        with pytest.raises(TimeoutError):
            await worker.images.send_friend(
                adapter, "2", "902", "friend/unknown", lambda: None
            )
        result = await worker.images.send_friend(
            adapter, "2", "902", "friend/unknown", lambda: None
        )
        assert result == "unknown"
        assert await ledger.receipt("friend/unknown/guide-examples") is None
    else:
        assert (
            await worker.welcome_group(
                adapter, "5", "1005", 1, "本群菜单", lambda: None
            )
            == "delivery_unconfirmed"
        )
        assert (
            await worker.welcome_group(
                adapter, "5", "1005", 1, "本群菜单", lambda: None
            )
            == "already_sent"
        )
    assert adapter.nim.request.await_count == 1


@pytest.mark.asyncio
async def test_pause_between_images_stops_remaining_send_and_is_not_retried(pair):
    worker, event, _, calls = pair
    paused = False
    original = event.platform.nim.request.side_effect

    async def pause(service, command, props):
        nonlocal paused
        result = await original(service, command, props)
        if len(calls) == 2:
            paused = True
        return result

    def guard():
        if paused:
            raise OpenError("operation_revoked")

    event.platform.nim.request.side_effect = pause
    assert (
        await worker.welcome_group(event.platform, "5", "1005", 1, "本群菜单", guard)
        == "delivery_unconfirmed"
    )
    assert len(calls) == 2
    assert (
        await worker.welcome_group(
            event.platform, "5", "1005", 1, "本群菜单", lambda: None
        )
        == "already_sent"
    )
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_wrong_group_team_or_unreviewed_asset_never_sends(pair, monkeypatch):
    worker, event, _, calls = pair
    with pytest.raises(wire.ProtocolError, match="guide_send_scope_changed"):
        await worker.images.send_group(
            event.platform, "5", "9999", "wrong", lambda: None
        )
    monkeypatch.setattr(feature_image, "REVIEWED_ASSETS", {})
    with pytest.raises(wire.ProtocolError, match="guide_native_asset_unreviewed"):
        await worker.images.send_friend(
            event.platform, "2", "902", "unreviewed", lambda: None
        )
    assert not calls


@pytest.mark.asyncio
async def test_activation_queues_only_verified_group_and_late_revoke_stops_it(public):
    service, adapter, rows = public
    queue = Mock()
    service.plugin.private_interaction = SimpleNamespace(queue_group=queue)

    async def request(route, _, **kwargs):
        return (
            {"list": [invitation()]}
            if route.endswith("get-apply-logs")
            else {"groupMemberInfo": rows}
        )

    adapter.business.request.side_effect = request
    await service.refresh()
    await service.activate(ActivationContext(adapter, "1", 1), "5")
    assert queue.call_count == 2
    first = queue.call_args_list[0].args
    assert first[:4] == (adapter, "5", "1005", 1)
    assert "请拉我进群的群主或管理员发送「开通」" in first[4]
    first[5]()
    args = queue.call_args.args
    assert args[:4] == (adapter, "5", "1005", 1)
    assert "常用操作" in args[4] and "插件设置" in args[4]
    assert "①使用说明 ②对话示例" in args[4]
    args[5]()
    with service.store.connect(write=True) as db:
        db.execute(
            "UPDATE tenants SET managers='[]',version=version+1 "
            "WHERE account='99' AND group_id='5'"
        )
    with pytest.raises(OpenError, match="operation_revoked"):
        args[5]()
    with pytest.raises(OpenError, match="operation_revoked"):
        first[5]()


@pytest.mark.asyncio
@pytest.mark.parametrize("pause", [False, True])
async def test_verified_administrator_invitation_gets_guide_before_activation(
    public, pause
):
    service, adapter, rows = public
    queue = Mock()
    service.plugin.private_interaction = SimpleNamespace(queue_group=queue)

    async def request(route, _, **kwargs):
        return (
            {"list": [invitation()]}
            if route.endswith("get-apply-logs")
            else {"groupMemberInfo": rows}
        )

    adapter.business.request.side_effect = request
    await service.refresh()
    await service.check_inviter(adapter, "5")
    queue.assert_called_once()
    args = queue.call_args.args
    assert "大家好，我是大海机器人" in args[4]
    tenant = service.store.tenant("99", "5")
    assert not tenant["active"] and not tenant["managers"]
    args[5]()
    if pause:
        with service.store.connect(write=True) as db:
            db.execute(
                "UPDATE tenants SET paused=1 WHERE account='99' AND group_id='5'"
            )
    else:
        service.member_evidence[("99", "5")]["1"]["groupRole"] = "GROUP_ROLE_MEMBER"
    with pytest.raises(OpenError, match="operation_revoked"):
        args[5]()


@pytest.mark.asyncio
async def test_non_administrator_invitation_never_gets_menu_or_images(public):
    service, adapter, rows = public
    queue = Mock()
    service.plugin.private_interaction = SimpleNamespace(queue_group=queue)
    rows[1]["groupRole"] = "GROUP_ROLE_MEMBER"

    async def request(route, _, **kwargs):
        return (
            {"list": [invitation()]}
            if route.endswith("get-apply-logs")
            else {"groupMemberInfo": rows}
        )

    adapter.business.request.side_effect = request
    await service.refresh()
    with pytest.raises(OpenError, match="inviter_not_native_admin"):
        await service.check_inviter(adapter, "5")
    queue.assert_not_called()


@pytest.mark.asyncio
async def test_failed_activation_never_queues_group_guidance(public):
    service, adapter, _ = public
    queue = Mock()
    service.plugin.private_interaction = SimpleNamespace(queue_group=queue)
    await service.refresh()
    with pytest.raises(OpenError):
        await service.activate(ActivationContext(adapter, "2", 1), "5")
    queue.assert_not_called()


@pytest.mark.asyncio
async def test_welcome_queue_is_bounded_and_shutdown_cancels_pending_items(pair):
    worker, event, _, _ = pair
    started = asyncio.Event()

    async def blocked(*args):
        started.set()
        await asyncio.Event().wait()

    worker.welcome_group = blocked
    for _ in range(20):
        assert worker.queue_group(event.platform, "5", "1005", 1, "菜单", lambda: None)
    assert not worker.queue_group(event.platform, "5", "1005", 1, "菜单", lambda: None)
    await asyncio.wait_for(started.wait(), 1)
    await worker.close()
    assert worker.closed and worker.group_queue.empty()
    assert worker.group_task.done()
    assert not worker.queue_group(event.platform, "5", "1005", 1, "菜单", lambda: None)
