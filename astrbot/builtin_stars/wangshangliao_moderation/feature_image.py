"""Deliver the reviewed guide without treating a web URL as a native asset."""

import asyncio
import json
import secrets
import struct
import time
import uuid

import lz4.block
import nacl.bindings
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.event import is_managed_account

from .native_guide_upload import GUIDE_PATH as GUIDE_PATH
from .native_guide_upload import ONBOARDING_GUIDES, is_native_asset_id, load_fixed_guide
from .observability import record_event
from .onboarding_delivery import send_onboarding_text

GUIDE_URL = "https://wsl-feature-guide.atlouptxlu.workers.dev/feature-guide-v1.jpg"
OPEN_GUIDE_URL = "https://wsl-open-guide.atlouptxlu.workers.dev/feature-guide-v2.jpg"
PRIVATE_GUIDE_URL = (
    "https://wsl-feature-guide.atlouptxlu.workers.dev/feature-guide-v1.jpg"
)
GUIDE_LINKS = (
    "菜单图片（点击查看）：\n"
    "① 使用说明：https://images-03003.hfjtqy.com/assets/7493665398455374383\n"
    "② 对话示例：https://images-03003.hfjtqy.com/assets/1915106862424622935"
)
# Populate only after verifying a native upload against the exact bitmap digest.
# Official clients prepend their asset host to uri; external URLs produce grey tiles.
REVIEWED_ASSETS: dict[str, str] = {
    # 2026-10-17: exact original and thumbnail readback match the public guide.
    "259c6ea63b348335251e8b156e60d1135b91312f801f9f57320100b5ce4ed547": (
        "3678505596171634031"
    ),
    "0baf6b6479abe935a09294992c17c068d8be13d27a6576b013909fb8c87da4ff": (
        "7493665398455374383"
    ),
    "1a4f32e0888ea3dbf8784ec19f2a648a1729bc6d46a04ae81cc818dbd6fd78ab": (
        "1915106862424622935"
    ),
}

# Official 3.7.1: Message.image=101, format=1; Image.asset=1 and Asset.uri=1.
# An independent descriptor keeps the existing live text codec unchanged.
_schema = descriptor_pb2.FileDescriptorProto()
_schema.ParseFromString(wire.ApplicationMessage.DESCRIPTOR.file.serialized_pb)
_schema.name = "wangshangliao-feature-image.proto"
_asset = _schema.message_type.add(name="GuideAsset")
_asset.field.add(name="uri", number=1, type=9, label=1)
_image = _schema.message_type.add(name="GuideImage")
for _name, _number, _type in (
    ("asset", 1, 11),
    ("width", 2, 5),
    ("height", 3, 5),
    ("size", 4, 5),
    ("is_sticker", 5, 8),
):
    _field = _image.field.add(name=_name, number=_number, type=_type, label=1)
    if _name == "asset":
        _field.type_name = ".wsl.GuideAsset"
for _message in _schema.message_type:
    if _message.name == "ApplicationMessage":
        _message.field.add(
            name="image", number=101, type=11, label=1, type_name=".wsl.GuideImage"
        )
_pool = descriptor_pool.DescriptorPool()
_pool.Add(_schema)
ImageMessage = message_factory.GetMessageClass(
    _pool.FindMessageTypeByName("wsl.ApplicationMessage")
)


def seal_guide(key, message, nonce, aad, timestamp):
    """Seal only the fixed guide using the native authenticated message envelope.

    Args:
        key: Server-only message encryption key.
        message: Reviewed image protobuf with account-bound routing.
        nonce: Native-ledger-reserved nonce.
        aad: Unpredictable authenticated material.
        timestamp: Current Unix time.

    Returns:
        Native custom attachment, without granting general media capabilities.

    Raises:
        ProtocolError: If image or account binding is not the reviewed shape.
    """
    if not (
        0 < message.sender.id < 1 << 30
        and 0 < message.target.id < 1 << 30
        and message.session in (1, 2)
        and message.device == 1
        and message.format == 1
        and message.image.asset.uri in REVIEWED_ASSETS.values()
        and is_native_asset_id(message.image.asset.uri)
        and message.image.width == 1024
        and message.image.height == 1024
        and 0 < message.image.size <= 1024 * 1024
        and not message.image.is_sticker
        and not message.HasField("content")
        and not message.HasField("mentions")
        and 0 < aad < 1 << 64
        and 0 <= nonce < 1 << 64
    ):
        raise wire.ProtocolError("guide_image_binding")
    plain = message.SerializeToString()
    if len(plain) > 4096:
        raise wire.ProtocolError("message_limit")
    sealed = nacl.bindings.crypto_aead_chacha20poly1305_encrypt(
        lz4.block.compress(plain, store_size=True),
        struct.pack("<Q", aad),
        struct.pack("<Q", nonce),
        key,
    )
    envelope = wire.Envelope(
        header=message.sender.id << 34
        | message.target.id << 4
        | message.session << 2
        | 1,
        timestamp=timestamp,
        nonce=nonce,
        aad=aad,
        body=sealed[:-16],
        tag=sealed[-16:],
    )
    return json.dumps(
        {"b": wire.b64(envelope.SerializeToString())}, separators=(",", ":")
    )


class FeatureImage:
    """Use a verified native asset or a fixed picture link, never arbitrary media."""

    def __init__(self, *, public=False, kind=None):
        self.url = OPEN_GUIDE_URL if public else GUIDE_URL
        self.path, data, self.digest = load_fixed_guide(public=public, kind=kind)
        self.kind = kind
        self.size = len(data)
        self.last_sent = {}

    async def send(self, event, authorize):
        """Send the fixed bitmap only as a reply to this authenticated event.

        Args:
            event: Private event whose command text was already accepted.
            authorize: Frozen account, sender, epoch and reply-policy check.

        Returns:
            Durable transport status. Unknown sends are never retried.

        Raises:
            ProtocolError: If connection, recipient or authorization changed.
        """
        group = event.session_id.split("/", 1)[1]
        logical_key = f"{event.session_id}/{event.message_obj.message_id}/0"
        return await self._send(
            event.platform, group, logical_key, authorize, event=event
        )

    async def send_friend(self, adapter, member, peer, key, authorize):
        """Send the fixed menu after a signed, confirmed incoming friend acceptance.

        Args:
            adapter: Native adapter that accepted the application.
            member: Verified business UID.
            peer: NIM identity returned in the same signed acceptance record.
            key: Durable welcome key, shared with its text receipt.
            authorize: Frozen acceptance, account and plugin ownership guard.

        Returns:
            Durable image receipt status.

        Raises:
            ProtocolError: If the recipient or account is invalid or revoked.
        """
        if (
            not member.isascii()
            or not member.isdecimal()
            or not 0 < int(member) < 1 << 30
            or not peer.isascii()
            or not peer.isdecimal()
            or not 0 < int(peer) < 1 << 32
        ):
            raise wire.ProtocolError("friend_welcome_identity")
        group = f"private/{member}/{wire.b64(peer.encode())}"
        return await self._send(adapter, group, key, authorize)

    async def send_group(self, adapter, group, team, key, authorize):
        """Send only fixed onboarding artwork to a verified admitted group.

        Args:
            adapter: Current authenticated native transport.
            group: Business group verified by the activation transaction.
            team: Independent NIM team from the signed directory.
            key: Durable activation-generation welcome key.
            authorize: Live and durable exact-group activation guard.

        Returns:
            Durable native image receipt, without enabling other media operations.
        """
        if not (
            self.kind in ONBOARDING_GUIDES
            and type(group) is str
            and group.isascii()
            and group.isdecimal()
            and 0 < int(group) < 1 << 30
            and type(team) is str
            and team.isascii()
            and team.isdecimal()
            and 0 < int(team) < 1 << 63
        ):
            raise wire.ProtocolError("guide_group_identity")
        return await self._send(adapter, group, key, authorize, team=team)

    async def _send(
        self, adapter, group, logical_key, authorize, *, event=None, team=None
    ):
        """Share native encoding while retaining separate event and friend proofs.

        Args:
            adapter: Current native account transport.
            group: Fixed private route bound to its business and NIM identities.
            logical_key: Stable text operation key for this welcome or event.
            authorize: Recipient-specific proof checked again before transport.
            event: Fresh inbound event, or None for a confirmed friend acceptance.
            team: Verified NIM team for the fixed activation-welcome route only.

        Returns:
            Durable native receipt status.

        Raises:
            ProtocolError: On changed identity, transport or proof.
        """
        account, nim, business, ledger = (
            str(adapter.account),
            adapter.nim,
            adapter.business,
            adapter.ledger,
        )
        nim_account = adapter.nim_account
        key = logical_key + (
            "/feature-guide" if self.kind is None else "/guide-" + self.kind
        )
        private = team is None
        target = group.split("/", 2)[1] if private else group

        def check():
            authorize()
            if (
                private
                and not group.startswith("private/")
                or not private
                and (
                    self.kind not in ONBOARDING_GUIDES
                    or event is not None
                    or adapter.group_catalog.get(group, {}).get("team") != team
                )
                or event is not None
                and (
                    not event.is_private_chat()
                    or target != str(event.get_sender_id())
                    or event.processing_completion.cancelled()
                )
                or str(adapter.account) != account
                or adapter.nim is not nim
                or adapter.nim_account != nim_account
                or adapter.business is not business
                or adapter.ledger is not ledger
                or adapter.stopping.is_set()
                or adapter.connection_state != "online"
                or not adapter.config.get("_business_active")
                or not adapter.config.get("enable", True)
                or private
                and not adapter.config.get("reply_private", True)
            ):
                raise wire.ProtocolError("guide_send_scope_changed")
            if (
                private
                and is_managed_account(target)
                and not (
                    event is not None
                    and adapter.test_window
                    and adapter.test_window.reply(
                        group, str(event.message_obj.message_id), logical_key
                    )
                )
            ):
                raise wire.ProtocolError("managed_bot_peer")

        check()
        if event is not None:
            async with ledger.db.execute(
                "SELECT 1 FROM inbox WHERE account=? AND team=? AND message=?",
                (account, group, str(event.message_obj.message_id)),
            ) as cursor:
                if await cursor.fetchone() is None:
                    raise wire.ProtocolError("private_peer_unknown")
        if private:
            _, target, encoded_peer = group.split("/", 2)
            transport_target = wire.unb64(encoded_peer, 1024).decode()
        else:
            transport_target = team
        asset = REVIEWED_ASSETS.get(self.digest)
        if asset is None:
            if self.kind is not None:
                raise wire.ProtocolError("guide_native_asset_unreviewed")
            if self.url not in {OPEN_GUIDE_URL, PRIVATE_GUIDE_URL}:
                raise wire.ProtocolError("guide_image_binding")
            state = await send_onboarding_text(
                adapter,
                target,
                transport_target,
                logical_key + "/feature-guide-link-v1",
                "菜单图片（点击查看）：\n" + self.url,
                check,
            )
            if event is not None:
                if state == "accepted":
                    event.reply_index += 1
                record_event(event, "guide_link", state)
            return state
        # Separate the text and image, including concurrent first-use replies.
        await asyncio.sleep(3)
        while True:
            check()
            previous = max(
                self.last_sent.get((adapter.config["id"], account), 0),
                getattr(adapter, "_onboarding_sent_at", 0),
            )
            await asyncio.sleep(max(0, 3 - (time.monotonic() - previous)))
            async with adapter.send_lock:
                check()
                if (
                    max(
                        self.last_sent.get((adapter.config["id"], account), 0),
                        getattr(adapter, "_onboarding_sent_at", 0),
                    )
                    != previous
                ):
                    continue
                fingerprint = json.dumps([group, asset, self.digest])
                nonce, state = await ledger.reserve(key, fingerprint)
                if state != "new":
                    return "unknown" if state == "sending" else state
                client_id = str(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"{adapter.config['id']}/{key}")
                )
                try:
                    check()
                    inner = ImageMessage(
                        sender={"id": int(account)},
                        target={"id": int(target)},
                        created_at=int(time.time() * 1000),
                        device=1,
                        session=1 if private else 2,
                        version=2,
                        format=1,
                        client_id=client_id,
                        image={
                            "asset": {"uri": asset},
                            "width": 1024,
                            "height": 1024,
                            "size": self.size,
                        },
                    )
                    attachment = seal_guide(
                        business.deployment.key("message_key"),
                        inner,
                        int(nonce),
                        secrets.randbits(64) or 1,
                        int(time.time()),
                    )
                    properties = wire.properties(
                        [
                            (0, b"0" if private else b"1"),
                            (1, transport_target.encode()),
                            (8, b"100"),
                            (10, attachment.encode()),
                            (11, client_id.encode()),
                        ]
                    )
                except BaseException:
                    await ledger.finish(key, "rejected")
                    if event is not None:
                        record_event(event, "guide_image", "rejected")
                    raise
                sent_at = time.monotonic()
                self.last_sent[(adapter.config["id"], account)] = sent_at
                adapter._onboarding_sent_at = sent_at
                try:
                    code, body = await nim.request(
                        7 if private else 8,
                        1 if private else 2,
                        properties,
                    )
                    if code != 200:
                        await ledger.finish(key, "rejected")
                        if event is not None:
                            record_event(event, "guide_image", "rejected")
                        return "rejected"
                    receipt, end = wire.read_properties(body)
                    if (
                        end != len(body)
                        or not receipt.get(12)
                        or not receipt.get(7, b"").isdigit()
                        or receipt.get(11, client_id.encode()) != client_id.encode()
                    ):
                        raise wire.ProtocolError("receipt_shape")
                    await ledger.finish(key, "accepted", receipt[12].decode())
                    if event is not None:
                        event.reply_index += 1
                        record_event(event, "guide_image", "accepted")
                    return "accepted"
                except BaseException:
                    await ledger.finish(key, "unknown")
                    if event is not None:
                        record_event(
                            event, "guide_image", "unknown", error="result_unknown"
                        )
                    raise


class FeatureGuides:
    """Deliver the reviewed instructions and examples in order, stopping on doubt."""

    def __init__(self):
        self.images = tuple(FeatureImage(kind=kind) for kind in ONBOARDING_GUIDES)

    async def send(self, event, authorize):
        """Send both images after confirmed first-use text.

        Args:
            event: Authenticated private inbound event.
            authorize: Frozen recipient and account authority.

        Returns:
            Last receipt, or the first non-success result without sending more.
        """
        for image in self.images:
            result = await image.send(event, authorize)
            if result not in {"accepted", "verified"}:
                return result
        return result

    async def send_friend(self, adapter, member, peer, key, authorize):
        """Send both images after a verified first friend welcome.

        Args:
            adapter: Current native transport.
            member: Verified business UID.
            peer: Independent verified NIM peer.
            key: Durable first-welcome key.
            authorize: Active approval and recipient guard.

        Returns:
            Last receipt, stopping immediately on rejection or uncertainty.
        """
        for image in self.images:
            result = await image.send_friend(adapter, member, peer, key, authorize)
            if result not in {"accepted", "verified"}:
                return result
        return result

    async def send_group(self, adapter, group, team, key, authorize):
        """Send both images after a claimed, exact-generation group welcome.

        Args:
            adapter: Current authenticated transport.
            group: Activated business group.
            team: Independently verified NIM team.
            key: Durable activation welcome key.
            authorize: Activation, membership and ownership guard.

        Returns:
            Last receipt, stopping immediately on rejection or uncertainty.
        """
        for image in self.images:
            result = await image.send_group(adapter, group, team, key, authorize)
            if result not in {"accepted", "verified"}:
                return result
        return result
