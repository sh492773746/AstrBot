"""Group image codecs and guarded downloads survive private intake retirement."""

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
from astrbot.builtin_stars.wangshangliao_moderation.open_service import (
    OpenService,
    recognize_integrated,
)
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from astrbot.builtin_stars.wangshangliao_moderation.rule_replies import RuleReplies
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
        target={"id": 5},
        session=2,
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
def test_group_image_metadata_is_bounded_native_not_a_url(change):
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


@pytest.mark.parametrize("placeholder", [{"width": 0}, {"height": 0}, {"size": 0}])
def test_group_inspection_can_recover_placeholders_without_relaxing_strict_metadata(
    placeholder,
):
    message = wire.ApplicationMessage.FromString(
        image_message(**placeholder).SerializeToString()
    )
    assert wire.image_metadata(message) is None
    result = wire.image_metadata(message, allow_sticker=True, allow_incomplete=True)
    assert result["asset"] == "123456789"
    for field, value in placeholder.items():
        assert result[field] == value


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"asset": {"uri": "https://invalid.test"}}, "asset"),
        ({"width": 20000}, "dimensions"),
        ({"size": 6 * 1024 * 1024}, "size"),
    ],
)
def test_image_metadata_errors_remain_specific_and_bounded(change, reason):
    message = wire.ApplicationMessage.FromString(
        image_message(**change).SerializeToString()
    )
    with pytest.raises(wire.ProtocolError, match="image_metadata_" + reason):
        wire.image_metadata(message, allow_incomplete=True, explain=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("group_image", [False, True])
async def test_native_images_retain_only_group_payloads(
    tmp_path, monkeypatch, group_image
):
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
        if group_image:
            adapter.groups = {"5": "1005"}
            adapter.members = {"5": {"1": "1001"}}
        else:
            message.session = 1
            message.target.id = 99
        decoder = Mock(wraps=wire.open_message)
        monkeypatch.setattr(wire, "open_message", decoder)
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
                        header=1 << 34
                        | (5 if group_image else 99) << 4
                        | (2 if group_image else 1) << 2
                        | 1,
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
                0: "1" if group_image else "0",
                1: "1005" if group_image else "1099",
                2: "1001",
                7: str(int(time.time() * 1000)),
                8: "100",
                10: attachment,
                12: "native-image",
            }
        )
        async with adapter.ledger.db.execute(
            "SELECT payload,state FROM inbox WHERE message='native-image'"
        ) as cursor:
            row = await cursor.fetchone()
        if group_image:
            payload, state = row
            payload = json.loads(payload)
            assert payload["kind"] == "image" and not payload["text"]
            assert payload["image"]["asset"] == "123456789"
            assert payload["sender"] == "1"
            assert state == "business_suspended"
            decoder.assert_called_once()
        else:
            assert row is None
            decoder.assert_not_called()
            assert not adapter.dispatch_wakeup.is_set()
        assert adapter._event_queue.empty()
        adapter.nim.acknowledge.assert_awaited_once_with(
            1 if group_image else 0, "native-image"
        )
    finally:
        await adapter.ledger.close()


def test_private_image_intake_api_and_command_are_absent():
    assert not hasattr(RuleReplies, "receive_image")
    assert recognize_command("广告图片 1") == ""
    assert recognize_integrated("广告图片 1") == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", [False, True])
async def test_private_image_ingress_cannot_reuse_historical_selection(
    rpc_scope, selected
):
    service, adapter, _ = rpc_scope
    rules = service.rule_replies
    reference = rules.media.upload("99", "5", bitmap(), guard=lambda: None)
    save_rules(
        rules,
        {
            "ads": [
                {
                    "id": "a",
                    "name": "通知",
                    "text": "正文",
                    "times": ["09:00"],
                    "images": [reference["id"]],
                }
            ]
        },
    )
    before = rules.read("99", "5")
    event = chat_event(adapter, "cached-image")
    event.stop_event = Mock()
    if selected:
        service.plugin.commands.selections[
            (
                id(adapter),
                adapter.account,
                event.get_sender_id(),
                event.unified_msg_origin,
            )
        ] = {"group": "5", "expires": time.monotonic() + 300}
    rules.media.receive = AsyncMock()
    service.reject_reply = AsyncMock()
    service.group_ai.observe = AsyncMock()
    event.message_str = ""
    event.set_extra(
        "wangshangliao_payload",
        {"kind": "image", "created_at": time.time() * 1000, "image": {}},
    )
    await service.on_event(event)
    rules.media.receive.assert_not_awaited()
    service.reject_reply.assert_not_awaited()
    service.group_ai.observe.assert_not_awaited()
    adapter.business.request.assert_not_awaited()
    event.send.assert_not_awaited()
    event.stop_event.assert_called_once()
    assert event.get_extra("_component_claimed") is True
    assert rules.read("99", "5") == before
    assert not hasattr(rules, "image_sessions")
    with rules.store.connect() as db:
        assert not db.execute(
            "SELECT 1 FROM business_rule_drafts WHERE used=0"
        ).fetchone()
    await rules.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["广告图片 1", "确认广告 old-token", "定时发言"])
async def test_private_rule_commands_reject_before_downloading_or_modifying(
    rpc_scope, command
):
    service, adapter, _ = rpc_scope
    rules = service.rule_replies
    config = {"ads": [{"id": "a", "name": "通知", "text": "正文", "times": ["09:00"]}]}
    save_rules(rules, config)
    before = rules.read("99", "5")
    event = chat_event(adapter, "cached-image-command")
    service.plugin.commands.selections[
        (id(adapter), adapter.account, event.get_sender_id(), event.unified_msg_origin)
    ] = {"group": "5", "expires": time.monotonic() + 300}
    rules.media.receive = AsyncMock()
    with pytest.raises(OpenError, match="^private_messages_retired$"):
        await rules.command(event, command)
    rules.media.receive.assert_not_awaited()
    adapter.business.request.assert_not_awaited()
    assert rules.read("99", "5") == before
    assert not hasattr(rules, "image_sessions")
    await rules.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["on_event", "_on_event"])
async def test_private_image_stops_before_rule_or_payload_access(entry):
    service = object.__new__(OpenService)
    event = SimpleNamespace(
        is_private_chat=lambda: True,
        message_str="",
        get_extra=Mock(side_effect=AssertionError("private image payload accessed")),
        set_extra=Mock(),
        stop_event=Mock(),
    )
    await getattr(service, entry)(event)
    event.set_extra.assert_called_once_with("_component_claimed", True)
    event.stop_event.assert_called_once_with()
    event.get_extra.assert_not_called()
    assert vars(service) == {}


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
