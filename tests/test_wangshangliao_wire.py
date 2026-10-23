import json
from pathlib import Path

import nacl.bindings
import pytest
from nacl.public import PrivateKey, SealedBox

from astrbot.core.platform.sources.wangshangliao import wire

FIXTURES = Path(__file__).parent / "fixtures" / "wangshangliao"


def test_native_metadata_corpus():
    corpus = json.loads((FIXTURES / "request_metadata_280.json").read_text())
    for vector in corpus["vectors"]:
        assert wire.metadata(vector["fields"]).hex() == vector["wire_hex"]


def test_sealed_box_corpus():
    corpus = json.loads((FIXTURES / "sealed_box_280.json").read_text())
    key = PrivateKey(bytes.fromhex(corpus["synthetic_private_key_hex"]))
    assert bytes(key.public_key).hex() == corpus["public_key_hex"]
    for vector in corpus["vectors"]:
        ciphertext = bytes.fromhex(vector["ciphertext_hex"])
        assert SealedBox(key).decrypt(ciphertext).hex() == vector["plaintext_hex"]
        with pytest.raises(Exception):
            SealedBox(key).decrypt(bytes([ciphertext[0] ^ 1]) + ciphertext[1:])


def test_independent_business_vector():
    body = bytes.fromhex("2cb4326bbc156bf266de89f03baf3d668bae2141b46cf95e39f54a7b9dfc9774f814ac191a984faca4160b4fbce0a4d3c199b112c0df126ede8ff0")
    plain = b'{"synthetic":true}'
    digest, aad = wire.binding(42, plain, 99)
    assert digest == 1991075964100698326
    assert aad.hex() == "81a175cbdd38b3b2f20c00678eacef89"
    assert wire.open_business(bytes(range(32)), aad, body) == plain
    for index in (0, 16, 32, len(body) - 1):
        damaged = bytearray(body)
        damaged[index] ^= 1
        with pytest.raises(wire.ProtocolError):
            wire.open_business(bytes(range(32)), aad, bytes(damaged))


@pytest.mark.parametrize("size", [0, 1, 255, 256, 131072, 131073, wire.LIMIT])
def test_business_boundaries(size):
    plain = b"x" * size
    body = wire.seal_business(bytes(32), 42, plain, 99)
    assert wire.open_business(bytes(32), wire.binding(42, plain, 99)[1], body) == plain


def test_independent_message_vector():
    encoded = "CSkCAAAwAAAAGQgAAAAAAAAAIh_VEC3xEchgKPYWqI6m77TaZXw2G3gjR8N4hEuLa_UdMAk6EN5fuQIJLALDoABHUjpguQA"
    message = wire.open_message(bytes([7]) * 32, encoded)
    assert (message.sender.id, message.target.id, message.session) == (12,34,2)
    assert message.content.data == "@DH test"
    message.device, message.version, message.client_id = 1, 2, "synthetic"
    sealed = wire.seal_message(bytes([7]) * 32, message, 8, 9, 1700000000)
    assert wire.open_message(bytes([7]) * 32, sealed) == message
    with pytest.raises(wire.ProtocolError):
        wire.open_message(bytes(32), sealed)


def test_siphash_vectors():
    expected = [0x77cda97233d8f304,0x80113eb1e3e30e2e,0x4e032477ec15d740,0x1d50511edbb70f6c,0xbfbdd8c1b9b8b951,0x1fea8e2c439acb3d,0x5242244124713f97,0xa16895fb40a26c3d]
    for variant, value in enumerate(expected):
        data = bytes((i * 17 + variant * 31) & 255 for i in range(24))
        key = bytes((i * 13 + variant * 29) & 255 for i in range(16))
        assert int.from_bytes(nacl.bindings.crypto_shorthash_siphash24(data,key), "little") == value


def test_nim_bounds_and_duplicate_fields():
    expected = bytes([2,19,4,100,101,109,111,232,7,9,115,121,110,116,104,101,116,105,99])
    assert wire.properties([(19,b"demo"),(1000,b"synthetic")]) == expected
    assert wire.read_properties(expected) == ({19:b"demo",1000:b"synthetic"},len(expected))
    for length in range(len(expected)):
        with pytest.raises(wire.ProtocolError):
            wire.read_properties(expected[:length])
    with pytest.raises(wire.ProtocolError):
        wire.read_properties(wire.properties([(1,b"a"),(1,b"b")]))
    assert wire.packet(4,10,4660) == bytes([0,4,10,52,18,0])
    assert wire.read_packet(bytes([172,2,4,10,52,18,2,200,0])) == (4,10,4660,200,b"")


@pytest.mark.parametrize("damage", ["truncated", "trailing", "oversize"])
def test_authenticated_invalid_compression(damage):
    import zstandard

    compressed = zstandard.ZstdCompressor().compress(b"fixture" if damage != "oversize" else b"x" * (wire.LIMIT + 1))
    if damage == "truncated":
        compressed = compressed[:-1]
    if damage == "trailing":
        compressed += b"untrusted trailing frame"
    sealed = nacl.bindings.crypto_aead_chacha20poly1305_encrypt(compressed, bytes(16), bytes(8), bytes(32))
    envelope = sealed[-16:] + bytes(16) + sealed[:-16]
    with pytest.raises(wire.ProtocolError):
        wire.open_business(bytes(32), bytes(16), envelope)
