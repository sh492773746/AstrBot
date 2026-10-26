"""Bounded wire codecs ported from DH BOT's synthetic-tested protocol.

The AEAD uses libsodium's original 64-bit-nonce format, not the IETF format.
"""

import base64
import json
import struct

import lz4.block
import nacl.bindings
import xxhash
import zstandard
from google.protobuf import (
    descriptor_pb2,
    descriptor_pool,
    message_factory,
    unknown_fields,
)
from google.protobuf.message import DecodeError

LIMIT = 1024 * 1024
INVITATION_LOG_LIMIT = 3 * LIMIT


class ProtocolError(ValueError):
    """A fixed protocol error code, never an upstream response or credential."""


def b64(data: bytes) -> str:
    """Return canonical unpadded Base64URL."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64(value: str, limit: int = LIMIT) -> bytes:
    """Decode bounded canonical Base64URL.

    Args:
        value: Unpadded Base64URL input.
        limit: Maximum decoded bytes.

    Returns:
        Decoded bytes.

    Raises:
        ProtocolError: On size or noncanonical encoding.
    """
    if not isinstance(value, str) or len(value) > (limit + 2) // 3 * 4:
        raise ProtocolError("encoding_limit")
    try:
        result = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, UnicodeError):
        raise ProtocolError("encoding") from None
    if len(result) > limit or b64(result) != value:
        raise ProtocolError("encoding")
    return result


def varint(value: int) -> bytes:
    """Encode an unsigned 64-bit integer."""
    if type(value) is not int or not 0 <= value < 1 << 64:
        raise ProtocolError("integer_range")
    out = bytearray()
    while value >= 128:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def read_varint(data: bytes, offset: int, bits: int = 32) -> tuple[int, int]:
    """Read a bounded varint without allocating from its declared value."""
    value = 0
    for shift in range(0, bits, 7):
        if offset >= len(data):
            raise ProtocolError("truncated")
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if value >= 1 << bits:
            raise ProtocolError("integer_range")
        if byte < 128:
            return value, offset
    raise ProtocolError("integer_range")


def metadata(fields: list[int]) -> bytes:
    """Encode the fourteen native request metadata scalars."""
    if len(fields) != 14:
        raise ProtocolError("metadata")
    out = bytearray()
    for number, value in enumerate(fields, 1):
        if number in (2, 3, 4, 5, 7, 12, 13) and not 0 <= value < 1 << 32:
            raise ProtocolError("metadata_range")
        if number in (2, 3, 4, 5, 7, 12) and value >= 1 << 31:
            value = value - (1 << 32) + (1 << 64)
        if value:
            out.extend(varint(number << 3) + varint(value))
    return bytes(out)


def properties(fields: list[tuple[int, bytes]]) -> bytes:
    """Encode ordered NIM properties, preserving binary values."""
    if len(fields) > 128:
        raise ProtocolError("property_limit")
    out = varint(len(fields)) + b"".join(
        varint(k) + varint(len(v)) + v for k, v in fields
    )
    if len(out) > LIMIT:
        raise ProtocolError("property_limit")
    return out


def read_properties(data: bytes, offset: int = 0) -> tuple[dict[int, bytes], int]:
    """Decode bounded properties, rejecting ambiguous duplicate fields."""
    if len(data) > LIMIT:
        raise ProtocolError("property_limit")
    count, offset = read_varint(data, offset)
    if count > 128:
        raise ProtocolError("property_limit")
    fields = {}
    for _ in range(count):
        key, offset = read_varint(data, offset)
        size, offset = read_varint(data, offset)
        if key in fields or offset + size > len(data):
            raise ProtocolError("property_shape")
        fields[key] = data[offset : offset + size]
        offset += size
    return fields, offset


def packet(service: int, command: int, serial: int, body: bytes = b"") -> bytes:
    """Encode a complete WebSocket packet; its length prefix is zero."""
    return struct.pack("<BB BHB", 0, service, command, serial, 0) + body


def read_packet(data: bytes) -> tuple[int, int, int, int, bytes]:
    """Parse one binary WebSocket message with optional response code."""
    if len(data) > LIMIT:
        raise ProtocolError("frame_limit")
    _, offset = read_varint(data, 0)
    if len(data) < offset + 5:
        raise ProtocolError("frame_truncated")
    service, command, serial, tag = struct.unpack_from("<BBHB", data, offset)
    offset += 5
    code = 200
    if tag & 2:
        if len(data) < offset + 2:
            raise ProtocolError("frame_truncated")
        code = int.from_bytes(data[offset : offset + 2], "little")
        offset += 2
    return service, command, serial, code, data[offset:]


def binding(
    request_id: int, plain: bytes, device: int, *, version: int = 0x019C411FDEAF
) -> tuple[int, bytes]:
    """Build the request-bound AAD for the reviewed client protocol version."""
    digest = xxhash.xxh3_64_intdigest(plain) if plain else 0
    aad = xxhash.xxh3_128_intdigest(
        struct.pack("<QQQQ", request_id, digest, device, version)
    ).to_bytes(16, "little")
    return digest, aad


def seal_business(
    key: bytes,
    request_id: int,
    plain: bytes,
    device: int,
    *,
    version: int = 0x019C411FDEAF,
) -> bytes:
    """Encrypt a raw-block Zstandard envelope identical to the Rust encoder."""
    if len(plain) > LIMIT:
        raise ProtocolError("body_limit")
    compressed = bytearray(b"\x28\xb5\x2f\xfd\xa0" + struct.pack("<I", len(plain)))
    if not plain:
        compressed.extend(b"\x01\x00\x00")
    for start in range(0, len(plain), 131072):
        chunk = plain[start : start + 131072]
        compressed.extend(
            ((len(chunk) << 3) | int(start + len(chunk) == len(plain))).to_bytes(
                3, "little"
            )
        )
        compressed.extend(chunk)
    material = xxhash.xxh3_128_intdigest(
        struct.pack("<Q", request_id) + compressed
    ).to_bytes(16, "little")
    sealed = nacl.bindings.crypto_aead_chacha20poly1305_encrypt(
        bytes(compressed),
        binding(request_id, plain, device, version=version)[1],
        material[:8],
        key,
    )
    return sealed[-16:] + material + sealed[:-16]


def open_business(key: bytes, aad: bytes, body: bytes, *, limit: int = LIMIT) -> bytes:
    """Authenticate before bounded decompression of the response.

    Args:
        key: Server-only symmetric authentication key.
        aad: Request-bound authentication data.
        body: Encrypted response envelope.
        limit: Reviewed byte limit for compressed input, output and window.

    Returns:
        Authenticated, bounded response bytes.

    Raises:
        ProtocolError: If authentication, framing or any size bound fails.
    """
    if (
        type(limit) is not int
        or not 0 < limit <= INVITATION_LOG_LIMIT
        or not 33 <= len(body) <= limit + 4096
    ):
        raise ProtocolError("body_limit")
    try:
        compressed = nacl.bindings.crypto_aead_chacha20poly1305_decrypt(
            body[32:] + body[:16], aad, body[16:24], key
        )
        frame = zstandard.get_frame_parameters(compressed)
        size = frame.content_size
        if frame.window_size > limit or (
            size
            not in (
                zstandard.CONTENTSIZE_UNKNOWN,
                zstandard.CONTENTSIZE_ERROR,
            )
            and size > limit
        ):
            raise ProtocolError("body_limit")
        plain = zstandard.ZstdDecompressor(max_window_size=limit).decompress(
            compressed, max_output_size=limit, allow_extra_data=False
        )
        if len(plain) > limit:
            raise ProtocolError("body_limit")
        return plain
    except ProtocolError:
        raise
    except Exception:
        raise ProtocolError("body_authentication_or_compression") from None


# Only reviewed fields are constructed; protobuf retains unknown incoming fields.
_schema = descriptor_pb2.FileDescriptorProto(
    name="wangshangliao.proto", package="wsl", syntax="proto3"
)
_definitions = {
    "Exchange": [
        (1, "device", 4, None),
        (2, "kind", 5, None),
        (3, "public_key", 12, None),
        (4, "seconds", 3, None),
        (5, "correlation", 3, None),
        (6, "version", 5, None),
    ],
    "LoginToken": [
        (1, "seconds", 3, None),
        (2, "correlation", 3, None),
        (3, "config", 12, None),
        (4, "mac", 12, None),
        (5, "kind", 5, None),
        (6, "uid", 5, None),
    ],
    "Source": [(1, "id", 5, None), (2, "name", 9, None)],
    "Content": [(1, "data", 9, None)],
    "IncomingAsset": [(1, "uri", 9, None)],
    "IncomingImage": [
        (1, "asset", 11, "IncomingAsset"),
        (2, "width", 5, None),
        (3, "height", 5, None),
        (4, "size", 5, None),
        (5, "is_sticker", 8, None),
    ],
    "Mention": [
        (1, "start", 5, None),
        (2, "end", 5, None),
        (3, "uid", 13, None),
        (4, "nick", 9, None),
    ],
    "Mentions": [(1, "content", 11, "Content"), (2, "people", 11, "Mention")],
    # Official desktop 2.8.3 api.common.MessageReply; quote is separate from payload.
    "MessageReply": [
        (1, "client_id", 9, None),
        (2, "server_id", 9, None),
        (100, "content", 11, "Content"),
    ],
    "ApplicationMessage": [
        (1, "sender", 11, "Source"),
        (2, "target", 11, "Source"),
        (3, "created_at", 3, None),
        (4, "device", 5, None),
        (5, "session", 5, None),
        (6, "version", 5, None),
        (11, "format", 5, None),
        (14, "appoint", 5, None),
        (50, "client_id", 9, None),
        (100, "content", 11, "Content"),
        (105, "mentions", 11, "Mentions"),
        (301, "reply", 11, "MessageReply"),
    ],
    "Envelope": [
        (1, "header", 6, None),
        (2, "timestamp", 6, None),
        (3, "nonce", 6, None),
        (4, "body", 12, None),
        (6, "aad", 4, None),
        (7, "tag", 12, None),
    ],
    "MessagePacket": [
        (1, "msg", 9, None),
        (2, "sign", 9, None),
        (3, "version", 5, None),
    ],
    "MessageSign": [
        (1, "id", 4, None),
        (2, "nid", 5, None),
        (3, "version", 4, None),
        (4, "client", 5, None),
        (5, "expired_at", 4, None),
        (6, "public_key", 9, None),
        (10, "key_sign", 9, None),
        (11, "msg_sign", 9, None),
    ],
}
for _name, _fields in _definitions.items():
    _message = _schema.message_type.add(name=_name)
    for _number, _field, _type, _reference in _fields:
        _item = _message.field.add(
            name=_field,
            number=_number,
            type=_type,
            label=3 if _field == "people" else 1,
        )
        if _reference:
            _item.type_name = f".wsl.{_reference}"
_pool = descriptor_pool.DescriptorPool()
_pool.Add(_schema)
Exchange = message_factory.GetMessageClass(_pool.FindMessageTypeByName("wsl.Exchange"))
LoginToken = message_factory.GetMessageClass(
    _pool.FindMessageTypeByName("wsl.LoginToken")
)
ApplicationMessage = message_factory.GetMessageClass(
    _pool.FindMessageTypeByName("wsl.ApplicationMessage")
)
IncomingImage = message_factory.GetMessageClass(
    _pool.FindMessageTypeByName("wsl.IncomingImage")
)
Envelope = message_factory.GetMessageClass(_pool.FindMessageTypeByName("wsl.Envelope"))
MessagePacket = message_factory.GetMessageClass(
    _pool.FindMessageTypeByName("wsl.MessagePacket")
)
MessageSign = message_factory.GetMessageClass(
    _pool.FindMessageTypeByName("wsl.MessageSign")
)
Source = message_factory.GetMessageClass(_pool.FindMessageTypeByName("wsl.Source"))
Content = message_factory.GetMessageClass(_pool.FindMessageTypeByName("wsl.Content"))
Mention = message_factory.GetMessageClass(_pool.FindMessageTypeByName("wsl.Mention"))
Mentions = message_factory.GetMessageClass(_pool.FindMessageTypeByName("wsl.Mentions"))


def image_metadata(message):
    """Read bounded native image metadata without changing the text codec.

    Args:
        message: Already authenticated ApplicationMessage.

    Returns:
        Reviewed image asset metadata, or None for unsupported image shapes.
    """
    fields = [
        field
        for field in unknown_fields.UnknownFieldSet(message)
        if field.field_number == 101
    ]
    if (
        message.format != 1
        or message.HasField("content")
        or message.HasField("mentions")
        or len(fields) != 1
        or fields[0].wire_type != 2
        or len(fields[0].data) > 4096
    ):
        return None
    try:
        image = IncomingImage.FromString(fields[0].data)
        asset = image.asset.uri
        if (
            not asset.isascii()
            or not asset.isdecimal()
            or not 1 <= len(asset) <= 20
            or asset.startswith("0")
            or not 0 < int(asset) < 1 << 64
            or not 1 <= image.width <= 16384
            or not 1 <= image.height <= 16384
            or image.width * image.height > 16_000_000
            or not 0 < image.size <= 5 * 1024 * 1024
            or image.is_sticker
        ):
            return None
        return {
            "asset": asset,
            "width": image.width,
            "height": image.height,
            "size": image.size,
        }
    except Exception:
        return None


def _message_envelope(encoded):
    """Normalize the scalar wire variants accepted by the official message codec."""
    envelope = Envelope.FromString(encoded)
    for field in unknown_fields.UnknownFieldSet(envelope):
        name = {1: "header", 3: "nonce", 6: "aad"}.get(field.field_number)
        if name and field.wire_type in (0, 1):
            current = getattr(envelope, name)
            if current and current != field.data:
                raise ProtocolError("message_envelope_conflict")
            setattr(envelope, name, field.data)
    return envelope


def open_message(key: bytes, text: str):
    """Authenticate a custom message and verify its inner routing header.

    Args:
        key: Deployment-owned message authentication key.
        text: Bounded encoded application envelope.

    Returns:
        Authenticated application message.

    Raises:
        ProtocolError: Fixed decode failure with a payload-free stage in ``reason``.
            ``unverified`` contains only untrusted format metadata, never message
            content or an identity suitable for authorization.
    """
    phase = "wrapper"
    shape = ""
    unverified = {}
    try:
        if len(text) > LIMIT * 2:
            raise ProtocolError("message_limit")
        if text.startswith("{"):
            wrapper = json.loads(text)
            if set(wrapper) != {"b"}:
                raise ProtocolError("message_wrapper")
            text = wrapper["b"]
        phase = "encoding"
        encoded = unb64(text)
        envelope = _message_envelope(encoded)
        phase = "envelope"
        if len(envelope.tag) != 16:
            fields = {
                (field.number, {4: 0, 6: 1, 12: 2}[field.type])
                for field, _ in envelope.ListFields()
            }
            for index, field in enumerate(unknown_fields.UnknownFieldSet(envelope)):
                if index >= 16:
                    break
                fields.add((min(field.field_number, 17), field.wire_type))
            shape = "_".join(f"f{number}w{kind}" for number, kind in sorted(fields))
            if not envelope.aad and not envelope.tag and envelope.body:
                # Official 3.7.3 has a plaintext branch. Inspect only its type;
                # neither its content nor its claimed identity is authenticated.
                try:
                    candidate = ApplicationMessage.FromString(envelope.body)
                    if candidate.ListFields():
                        unverified = {
                            "reason": "unsigned_plaintext",
                            "format": candidate.format
                            if 0 <= candidate.format <= 65535
                            else -1,
                        }
                except DecodeError:
                    pass
            packet = MessagePacket.FromString(encoded)
            if not packet.msg or not packet.sign:
                raise ProtocolError("message_unsigned")
            phase = "packet_version"
            if packet.version != 1:
                raise ProtocolError("message_packet_version")
            phase = "packet_signature"
            signature = unb64(packet.sign)
            if not 24 < len(signature) <= 65536:
                raise ProtocolError("message_packet_signature")
            # Official 3.7.3 uses the same project key for both AEAD layers.
            # The metadata is parsed, never used as a sender or permission source.
            signed_metadata = nacl.bindings.crypto_aead_chacha20poly1305_decrypt(
                signature[24:] + signature[8:24],
                struct.pack("<Q", packet.version),
                signature[:8],
                key,
            )
            MessageSign.FromString(signed_metadata)
            phase = "packet_message"
            text = packet.msg
            if text.startswith("{"):
                wrapper = json.loads(text)
                if set(wrapper) != {"b"}:
                    raise ProtocolError("message_wrapper")
                text = wrapper["b"]
            envelope = _message_envelope(unb64(text))
            if len(envelope.tag) != 16:
                raise ProtocolError("message_unsigned")
        phase = "authentication"
        compressed = nacl.bindings.crypto_aead_chacha20poly1305_decrypt(
            envelope.body + envelope.tag,
            struct.pack("<Q", envelope.aad),
            struct.pack("<Q", envelope.nonce),
            key,
        )
        phase = "compression"
        if envelope.aad:
            if len(compressed) < 4 or int.from_bytes(compressed[:4], "little") > LIMIT:
                raise ProtocolError("message_limit")
            plain = lz4.block.decompress(compressed)
            if len(plain) != int.from_bytes(compressed[:4], "little"):
                raise ProtocolError("message_size")
        else:
            if len(compressed) > LIMIT:
                raise ProtocolError("message_limit")
            plain = compressed
        phase = "schema"
        message = ApplicationMessage.FromString(plain)
        phase = "routing"
        if not (
            0 < message.sender.id < 1 << 30
            and 0 < message.target.id < 1 << 30
            and 0 <= message.device <= 3
            and message.session >= 0
        ):
            raise ProtocolError("message_binding")
        header = (
            message.sender.id << 34
            | message.target.id << 4
            | min(message.session, 3) << 2
            | message.device
        )
        if envelope.header != header:
            raise ProtocolError("message_binding")
        return message
    except Exception:
        error = ProtocolError("message_decode")
        error.reason = phase
        error.shape = shape if phase == "envelope" else ""
        error.unverified = unverified if phase == "envelope" else {}
        raise error from None


def seal_message(key: bytes, message, nonce: int, aad: int, timestamp: int) -> str:
    """Encode a new text message with a caller-reserved nonce."""
    if not (
        0 < message.sender.id < 1 << 30
        and 0 < message.target.id < 1 << 30
        and message.session in (1, 2)
        and message.device == 1
        and message.format == 0
        and (message.content.data.strip() or message.mentions.content.data.strip())
        and aad
    ):
        raise ProtocolError("message_binding")
    if message.HasField("reply") and (
        message.session != 2
        or message.appoint != 1
        or not 0 < len(message.reply.client_id.encode()) <= 128
        or not message.reply.server_id.isascii()
        or not message.reply.server_id.isdigit()
        or len(message.reply.server_id) > 20
        or not 0 < int(message.reply.server_id) < 1 << 64
        or not message.reply.content.data.strip()
        or len(message.reply.content.data.encode()) > 1024
    ):
        raise ProtocolError("quote_binding")
    if message.HasField("mentions"):
        if (
            message.session != 2
            or message.HasField("content")
            or not message.mentions.people
        ):
            raise ProtocolError("mention_binding")
        text = message.mentions.content.data
        previous = 0
        encoded = text.encode("utf-16-le")
        for person in message.mentions.people:
            if not (
                person.uid
                and previous <= person.start < person.end <= len(encoded) // 2
            ):
                raise ProtocolError("mention_range")
            if encoded[person.start * 2 : person.end * 2] != f"@{person.nick} ".encode(
                "utf-16-le"
            ):
                raise ProtocolError("mention_text")
            previous = person.end
    plain = message.SerializeToString()
    if len(plain) > 4096:
        raise ProtocolError("message_limit")
    compressed = lz4.block.compress(plain, store_size=True)
    sealed = nacl.bindings.crypto_aead_chacha20poly1305_encrypt(
        compressed, struct.pack("<Q", aad), struct.pack("<Q", nonce), key
    )
    envelope = Envelope(
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
    return json.dumps({"b": b64(envelope.SerializeToString())}, separators=(",", ":"))
