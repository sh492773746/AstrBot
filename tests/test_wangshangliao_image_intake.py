"""Private scheduled-image intake stays explicit, scoped and preview-only."""

import asyncio
import base64
import json
import struct
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import lz4.block
import nacl.bindings
import pytest

from astrbot.builtin_stars.wangshangliao_moderation import advertisement_media
from astrbot.builtin_stars.wangshangliao_moderation.feature_image import ImageMessage
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from astrbot.builtin_stars.wangshangliao_moderation.syntax import recognize_command
from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
from astrbot.core.platform.sources.wangshangliao.storage import Ledger
from tests.test_wangshangliao_advertisement_images import bitmap
from tests.test_wangshangliao_business_rules import rules as rules
from tests.test_wangshangliao_business_rules import save_rules
from tests.test_wangshangliao_open_rpc_guards import rpc_scope as rpc_scope
from tests.test_wangshangliao_public_hardening import chat_event


def image_message(**changes):
    """Construct the reviewed native image field independently of the decoder."""
    metadata = {
        "asset": {"uri": "123456789"},
        "width": 320,
        "height": 180,
        "size": 200,
        **changes,
    }
    return ImageMessage(
        sender={"id": 1},
        target={"id": 99},
        session=1,
        device=1,
        format=1,
        image=metadata,
    )


@pytest.mark.parametrize(
    "change",
    [
        {},
        {"asset": {"uri": "https://example.invalid/image.jpg"}},
        {"asset": {"uri": "00123"}},
        {"width": 0},
        {"height": 20000},
        {"size": 5 * 1024 * 1024 + 1},
        {"is_sticker": True},
    ],
)
def test_private_image_metadata_is_bounded_native_not_a_url(change):
    message = wire.ApplicationMessage.FromString(
        image_message(**change).SerializeToString()
    )
    result = wire.image_metadata(message)
    assert bool(result) == (change == {})
    if result:
        assert result == {
            "asset": "123456789",
            "width": 320,
            "height": 180,
            "size": 200,
        }


def test_image_cannot_hide_command_text_or_duplicate_attachments():
    message = image_message()
    message.content.data = "确认广告 hidden"
    assert (
        wire.image_metadata(
            wire.ApplicationMessage.FromString(message.SerializeToString())
        )
        is None
    )
    message.ClearField("content")
    raw = message.SerializeToString()
    attachment = message.image.SerializeToString()
    raw += wire.varint(101 << 3 | 2) + wire.varint(len(attachment)) + attachment
    assert wire.image_metadata(wire.ApplicationMessage.FromString(raw)) is None


@pytest.mark.asyncio
async def test_private_native_image_is_preserved_without_ai_or_management(tmp_path):
    adapter = WangshangliaoAdapter(
        {"id": "image-ingress", "account_id": "99"}, {}, asyncio.Queue()
    )
    adapter.nim_account = "1099"
    adapter.ledger = Ledger(tmp_path / "images.sqlite3")
    await adapter.ledger.open()
    adapter.business = SimpleNamespace(
        deployment=SimpleNamespace(key=lambda _: bytes(32))
    )
    adapter.nim = SimpleNamespace(acknowledge=AsyncMock())
    adapter.diagnostics.emit = Mock()
    try:
        message = image_message()
        sealed = nacl.bindings.crypto_aead_chacha20poly1305_encrypt(
            lz4.block.compress(message.SerializeToString(), store_size=True),
            struct.pack("<Q", 321),
            struct.pack("<Q", 123),
            bytes(32),
        )
        attachment = json.dumps(
            {
                "b": wire.b64(
                    wire.Envelope(
                        header=1 << 34 | 99 << 4 | 1 << 2 | 1,
                        timestamp=int(time.time()),
                        nonce=123,
                        aad=321,
                        body=sealed[:-16],
                        tag=sealed[-16:],
                    ).SerializeToString()
                )
            }
        )
        await adapter.ingest(
            {
                0: "0",
                1: "1099",
                2: "1001",
                7: str(int(time.time() * 1000)),
                8: "100",
                10: attachment,
                12: "private-image",
            }
        )
        async with adapter.ledger.db.execute(
            "SELECT payload,state FROM inbox WHERE message='private-image'"
        ) as cursor:
            payload, state = await cursor.fetchone()
        payload = json.loads(payload)
        assert payload["kind"] == "image" and not payload["text"]
        assert payload["image"]["asset"] == "123456789"
        assert payload["sender"] == "1" and payload["transport_peer"] == "1001"
        assert state == "business_suspended"
        assert adapter._event_queue.empty()
        adapter.nim.acknowledge.assert_awaited_once_with(0, "private-image")
    finally:
        await adapter.ledger.close()


def selected_event(service, adapter, *, message="image-command"):
    """Bind three private workflow tests to the real selection and native actor."""
    event = chat_event(adapter, message)
    service.plugin.commands.selections[
        (id(adapter), adapter.account, event.get_sender_id(), event.unified_msg_origin)
    ] = {"group": "5", "expires": time.monotonic() + 300}
    event.stop_event = Mock()
    event.eligible = True
    return event


@pytest.mark.asyncio
async def test_private_image_requires_plan_and_explicit_confirmation(rpc_scope):
    service, adapter, _ = rpc_scope
    rules = service.rule_replies
    save_rules(
        rules,
        {"ads": [{"id": "a", "name": "通知", "text": "正文", "times": ["09:00"]}]},
    )
    event = selected_event(service, adapter)
    assert recognize_command("广告图片 1") == "广告图片 1"
    assert await rules.receive_image(event) == ""
    reply = await rules.command(event, "广告图片 1")
    assert "5分钟" in reply and "尚未保存" not in reply
    reference = rules.media.upload("99", "5", bitmap(), guard=lambda: None)
    rules.media.receive = AsyncMock(return_value=reference)
    event.set_extra(
        "wangshangliao_payload",
        {"kind": "image", "created_at": time.time() * 1000, "image": {}},
    )
    reply = await rules.receive_image(event)
    assert "尚未保存" in reply and "共1/3张" in reply
    assert not rules.read("99", "5")["config"]["ads"][0]["images"]
    with rules.store.connect() as db:
        draft = dict(
            db.execute(
                "SELECT * FROM business_rule_drafts WHERE used=0 ORDER BY expires DESC"
            ).fetchone()
        )
    assert reference["id"] in json.loads(draft["config"])["ads"][0]["images"]
    await rules.command(event, "确认广告 " + draft["token"])
    assert rules.read("99", "5")["config"]["ads"][0]["images"] == [reference["id"]]
    assert not rules.image_sessions
    await rules.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", [False, True])
async def test_private_image_ingress_never_falls_through_to_chat(rpc_scope, requested):
    service, adapter, _ = rpc_scope
    rules = service.rule_replies
    save_rules(
        rules,
        {"ads": [{"id": "a", "name": "通知", "text": "正文", "times": ["09:00"]}]},
    )
    event = selected_event(service, adapter)
    if requested:
        await rules.command(event, "广告图片 1")
    reference = rules.media.upload("99", "5", bitmap(), guard=lambda: None)
    rules.media.receive = AsyncMock(return_value=reference)
    service.reject_reply = AsyncMock()
    service.chat = AsyncMock()
    event.message_str = ""
    event.set_extra(
        "wangshangliao_payload",
        {"kind": "image", "created_at": time.time() * 1000, "image": {}},
    )
    await service.on_event(event)
    assert rules.media.receive.await_count == int(requested)
    assert service.reject_reply.await_count == int(requested)
    if requested:
        assert "尚未保存" in service.reject_reply.await_args.args[1]
    service.chat.assert_not_awaited()
    event.stop_event.assert_called()
    assert not rules.read("99", "5")["config"]["ads"][0]["images"]
    await rules.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["expired", "selection", "version", "demoted", "cancelled"]
)
async def test_private_image_scope_changes_never_download_or_modify(rpc_scope, change):
    service, adapter, members = rpc_scope
    rules = service.rule_replies
    config = {"ads": [{"id": "a", "name": "通知", "text": "正文", "times": ["09:00"]}]}
    save_rules(rules, config)
    event = selected_event(service, adapter)
    await rules.command(event, "广告图片 1")
    rules.media.receive = AsyncMock()
    if change == "expired":
        rules.image_sessions[service.navigation.key(event)]["expires"] = 0
    elif change == "selection":
        service.plugin.commands.selections[
            (
                id(adapter),
                adapter.account,
                event.get_sender_id(),
                event.unified_msg_origin,
            )
        ]["group"] = "6"
    elif change == "version":
        save_rules(rules, config)
    elif change == "demoted":
        members[1]["groupRole"] = "GROUP_ROLE_MEMBER"
    else:
        event.message_str = "取消"
        event.set_extra("wangshangliao_payload", {"created_at": time.time() * 1000})
        await service.on_event(event)
    event.set_extra(
        "wangshangliao_payload",
        {"kind": "image", "created_at": time.time() * 1000, "image": {}},
    )
    if change == "cancelled":
        assert await rules.receive_image(event) == ""
    else:
        with pytest.raises((OpenError, wire.ProtocolError)):
            await rules.receive_image(event)
    rules.media.receive.assert_not_awaited()
    assert not rules.read("99", "5")["config"]["ads"][0]["images"]
    await rules.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["none", "cancelled", "selection", "demoted"])
async def test_private_image_rechecks_during_download_before_creating_draft(
    rpc_scope, change
):
    service, adapter, _ = rpc_scope
    rules = service.rule_replies
    save_rules(
        rules,
        {"ads": [{"id": "a", "name": "通知", "text": "正文", "times": ["09:00"]}]},
    )
    event = selected_event(service, adapter)
    await rules.command(event, "广告图片 1")
    reference = rules.media.upload("99", "5", bitmap(), guard=lambda: None)

    async def receive(*args, guard):
        if change == "cancelled":
            rules.image_sessions.clear()
        elif change == "selection":
            service.plugin.commands.selections[
                (
                    id(adapter),
                    adapter.account,
                    event.get_sender_id(),
                    event.unified_msg_origin,
                )
            ]["group"] = "6"
        elif change == "demoted":
            service.member_evidence[("99", "5")]["1"]["groupRole"] = "GROUP_ROLE_MEMBER"
        guard()
        return reference

    rules.media.receive = AsyncMock(side_effect=receive)
    event.set_extra(
        "wangshangliao_payload",
        {"kind": "image", "created_at": time.time() * 1000, "image": {}},
    )
    if change == "none":
        assert "尚未保存" in await rules.receive_image(event)
    else:
        with pytest.raises(OpenError):
            await rules.receive_image(event)
    assert not rules.read("99", "5")["config"]["ads"][0]["images"]
    assert not rules.image_sessions
    await rules.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["ok", "redirect", "size", "timeout", "revoked"])
async def test_native_download_is_fixed_host_bounded_cookie_free_and_guarded(
    rules, monkeypatch, case
):
    data = base64.b64decode(bitmap())
    event_thread = threading.get_ident()
    validation_threads = []
    image_open = advertisement_media.Image.open

    def tracked_open(*args, **kwargs):
        validation_threads.append(threading.get_ident())
        return image_open(*args, **kwargs)

    monkeypatch.setattr(advertisement_media.Image, "open", tracked_open)
    response = SimpleNamespace(status=302 if case == "redirect" else 200)
    request = MagicMock()
    request.__aenter__ = AsyncMock(return_value=response)
    request.__aexit__ = AsyncMock(return_value=False)
    http = SimpleNamespace(get=Mock(return_value=request))
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=http)
    session.__aexit__ = AsyncMock(return_value=False)
    factory = Mock(return_value=session)
    monkeypatch.setattr(advertisement_media.aiohttp, "ClientSession", factory)
    reader = AsyncMock(return_value=data)
    if case == "timeout":
        reader.side_effect = TimeoutError
    monkeypatch.setattr(advertisement_media, "_read_bounded", reader)
    adapter = SimpleNamespace(
        account="99",
        business=SimpleNamespace(
            deployment=SimpleNamespace(origin="https://47.86.91.23:5015")
        ),
    )
    checks = []

    def guard():
        checks.append(True)
        if case == "revoked" and len(checks) >= 3:
            raise OpenError("revoked")

    metadata = {
        "asset": "123456789",
        "width": 320,
        "height": 180,
        "size": len(data) + (case == "size"),
    }
    if case == "ok":
        result = await rules.media.receive(adapter, "5", metadata, guard=guard)
        assert rules.media.previews("99", "5", {"ads": [{"images": [result["id"]]}]})
        assert validation_threads and all(
            thread != event_thread for thread in validation_threads
        )
    else:
        with pytest.raises(OpenError):
            await rules.media.receive(adapter, "5", metadata, guard=guard)
        with rules.store.connect() as db:
            assert not db.execute("SELECT * FROM business_rule_images").fetchall()
    assert http.get.call_args.args == (
        "https://images-03003.hfjtqy.com/assets/123456789",
    )
    assert http.get.call_args.kwargs == {
        "headers": {},
        "allow_redirects": False,
        "ssl": True,
    }
    assert factory.call_args.kwargs["trust_env"] is False
    assert factory.call_args.kwargs["timeout"].total == 15
