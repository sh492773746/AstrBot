"""Offline contracts for the digest-pinned native guide uploader."""

import asyncio
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest

from astrbot.builtin_stars.wangshangliao_moderation import (
    feature_image,
)
from astrbot.builtin_stars.wangshangliao_moderation import (
    native_guide_upload as upload,
)
from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.business import BusinessError

ORIGIN = "https://business.invalid"
UPLOAD = "https://media.invalid/official-upload"
DOWNLOAD = "https://download.invalid/native/"
ASSET = "1234567890123456789"


class Response:
    def __init__(self, body=b"", *, status=200, error=None, after_read=None):
        self.status = status
        self.body = body
        self.error = error
        self.after_read = after_read
        self.content = self

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *exc):
        pass

    async def iter_chunked(self, size):
        for start in range(0, len(self.body), size):
            yield self.body[start : start + size]
        if self.after_read:
            self.after_read()


class Session:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.responses.pop(0)


@pytest.fixture
def transport(monkeypatch):
    _, data, _ = upload.load_fixed_guide(public=True)
    session = Session(
        [
            Response(json.dumps({"code": 0, "id": ASSET}).encode()),
            Response(data),
            Response(data),
        ]
    )
    factory = Mock(return_value=session)
    monkeypatch.setattr(upload.aiohttp, "ClientSession", factory)
    monkeypatch.setattr(
        upload, "REVIEWED_MEDIA_ENDPOINTS", {ORIGIN: (UPLOAD, DOWNLOAD)}
    )
    client = SimpleNamespace(
        deployment=SimpleNamespace(origin=ORIGIN),
        fields=[0, 0, 0, 43506527],
        jwt="fixture-not-a-real-token",
        lock=asyncio.Lock(),
        http=Mock(),
        request=AsyncMock(),
        login=AsyncMock(),
    )
    return client, session, factory


@pytest.mark.parametrize("public", [False, True])
def test_current_artwork_matches_fixed_digest_and_dimensions(public):
    path, data, digest = upload.load_fixed_guide(public=public)
    assert hashlib.sha256(data).hexdigest() == digest
    image = feature_image.FeatureImage(public=public)
    assert image.path == path
    assert image.digest == digest
    assert image.size == len(data) <= upload.IMAGE_LIMIT


@pytest.mark.parametrize("value", [None, 0, 1, "public", "/tmp/image.jpg", b"image"])
def test_artwork_selector_cannot_be_a_path_or_media_payload(value):
    with pytest.raises(wire.ProtocolError, match="guide_image_selection"):
        upload.load_fixed_guide(public=value)


@pytest.mark.parametrize("failure", ["digest", "oversize", "symlink"])
def test_changed_fixed_artwork_is_rejected_before_upload(
    tmp_path, monkeypatch, failure
):
    path = tmp_path / "fixed.jpg"
    if failure == "symlink":
        path.symlink_to(upload.OPEN_GUIDE_PATH)
    elif failure == "oversize":
        path.write_bytes(b"x" * (upload.IMAGE_LIMIT + 1))
    else:
        path.write_bytes(b"replacement image")
    monkeypatch.setattr(upload, "OPEN_GUIDE_PATH", path)
    with pytest.raises(wire.ProtocolError, match="guide_image_"):
        feature_image.FeatureImage(public=True)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "0",
        "01",
        "-1",
        "1.0",
        "1e2",
        "１２３",
        "https://download.invalid/native/1",
        "../1",
        "18446744073709551616",
        "1" * 100000,
        1,
        True,
        None,
    ],
)
def test_native_asset_id_is_bounded_and_not_a_url(value):
    assert not upload.is_native_asset_id(value)


@pytest.mark.asyncio
async def test_unreviewed_endpoint_is_fail_closed_without_session(
    transport, monkeypatch
):
    client, session, factory = transport
    monkeypatch.setattr(upload, "REVIEWED_MEDIA_ENDPOINTS", {})
    with pytest.raises(
        BusinessError, match="guide_upload_endpoint_unreviewed"
    ) as caught:
        await upload.upload_fixed_guide(client, public=True, authorize=Mock())
    assert caught.value.submitted is False
    factory.assert_not_called()
    assert not session.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoints",
    [
        ("http://media.invalid/", DOWNLOAD),
        ("https://user:pass@media.invalid/", DOWNLOAD),
        ("https://media.invalid:8443/", DOWNLOAD),
        ("https://media.invalid/path?token=x", DOWNLOAD),
        ("https://media.invalid/path#fragment", DOWNLOAD),
        ("https://media.invalid\\@other.invalid/", DOWNLOAD),
        ("https://media.invalid/%2e%2e", DOWNLOAD),
        ("https://media.invalid/\r\nx: y", DOWNLOAD),
        ("https://media.invalid/\x7f", DOWNLOAD),
        (UPLOAD, "http://download.invalid/"),
        (UPLOAD, "https://download.invalid/no-slash"),
        (UPLOAD, "https://user@download.invalid/"),
        ("", DOWNLOAD),
        [UPLOAD, DOWNLOAD],
    ],
)
async def test_even_reviewed_addresses_cannot_downgrade_or_leak_credentials(
    transport, monkeypatch, endpoints
):
    client, session, factory = transport
    monkeypatch.setattr(upload, "REVIEWED_MEDIA_ENDPOINTS", {ORIGIN: endpoints})
    with pytest.raises(BusinessError) as caught:
        await upload.upload_fixed_guide(client, public=True, authorize=Mock())
    assert caught.value.submitted is False
    factory.assert_not_called()
    assert not session.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("public", [False, True])
@pytest.mark.parametrize("asset", [ASSET, 18446744073709551615])
async def test_official_multipart_and_two_readbacks_are_fixed(
    transport, monkeypatch, public, asset
):
    client, session, factory = transport
    path, data, digest = upload.load_fixed_guide(public=public)
    session.responses = [
        Response(json.dumps({"code": 0, "id": asset}).encode()),
        Response(data),
        Response(data),
    ]
    monkeypatch.setattr(feature_image, "REVIEWED_ASSETS", {})
    guard = AsyncMock()
    result = await upload.upload_fixed_guide(client, public=public, authorize=guard)
    assert result == upload.VerifiedGuideAsset(
        "43506527", digest, str(asset), len(data)
    )
    assert feature_image.REVIEWED_ASSETS == {}
    assert [(method, url) for method, url, _ in session.calls] == [
        ("POST", UPLOAD),
        ("GET", DOWNLOAD + str(asset)),
        ("GET", DOWNLOAD + str(asset) + ".jpg"),
    ]
    options = factory.call_args.kwargs
    assert options["trust_env"] is False
    assert isinstance(options["cookie_jar"], aiohttp.DummyCookieJar)
    assert options["timeout"].total == 30
    assert "headers" not in options
    posted = session.calls[0][2]
    assert posted["headers"] == {"token": client.jwt}
    fields = {
        params["name"]: (params, headers, value)
        for params, headers, value in posted["data"]._fields
    }
    assert set(fields) == {
        "asset_type",
        "use_for",
        "height",
        "width",
        "file",
        "thumbnail",
    }
    for key, value in {
        "asset_type": "1",
        "use_for": "1",
        "height": "1024",
        "width": "1024",
    }.items():
        assert fields[key][2] == value
    for key in ("file", "thumbnail"):
        assert fields[key][0]["filename"] == path.name
        assert fields[key][1]["Content-Type"] == "image/jpeg"
        assert fields[key][2] == data
    for method, _, kwargs in session.calls:
        assert kwargs["allow_redirects"] is False
        assert kwargs["ssl"] is True
        if method == "GET":
            assert kwargs["headers"] == {}
    client.request.assert_not_awaited()
    client.login.assert_not_awaited()
    assert not client.http.mock_calls
    assert guard.await_count == 5
    assert client._authorization_task is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b'{"code":true,"id":"123"}',
        b'{"code":1,"id":"123"}',
        b'{"code":0,"data":{"id":"123"}}',
        b'{"code":0,"id":true}',
        b'{"code":0,"id":1.5}',
        b'{"code":0,"id":0}',
        b'{"code":0,"id":"01"}',
        b'{"code":0,"id":18446744073709551616}',
        b'{"code":0,"id":"https://other.invalid/file.jpg"}',
        b'{"code":1,"code":0,"id":"123"}',
        b'{"code":0,"id":"999","id":"123"}',
        b"x" * 65537,
    ],
)
async def test_untrusted_upload_response_cannot_produce_a_native_id(transport, body):
    client, session, _ = transport
    session.responses[0] = Response(body)
    with pytest.raises(BusinessError) as caught:
        await upload.upload_fixed_guide(client, public=True, authorize=Mock())
    assert caught.value.submitted is True
    assert len(session.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", [0, 1, 2])
@pytest.mark.parametrize("status", [301, 302, 307, 401, 500])
async def test_upload_and_readbacks_never_follow_redirects_or_retry(
    transport, phase, status
):
    client, session, _ = transport
    session.responses[phase] = Response(status=status)
    with pytest.raises(BusinessError) as caught:
        await upload.upload_fixed_guide(client, public=True, authorize=Mock())
    assert caught.value.submitted is True
    assert len(session.calls) == phase + 1
    assert all(not kwargs["allow_redirects"] for _, _, kwargs in session.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", [1, 2])
@pytest.mark.parametrize("body", [b"", b"wrong picture", b"x" * (1024 * 1024 + 1)])
async def test_both_original_and_thumbnail_must_match_fixed_digest(
    transport, phase, body
):
    client, session, _ = transport
    session.responses[phase] = Response(body)
    with pytest.raises(BusinessError, match="guide_upload_") as caught:
        await upload.upload_fixed_guide(client, public=True, authorize=Mock())
    assert caught.value.submitted is True
    assert len(session.calls) == phase + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", [0, 1, 2])
@pytest.mark.parametrize(
    "error_type", [TimeoutError, aiohttp.ClientError, asyncio.CancelledError]
)
async def test_unknown_outcome_is_not_retried_or_registered(
    transport, monkeypatch, phase, error_type
):
    client, session, _ = transport
    monkeypatch.setattr(feature_image, "REVIEWED_ASSETS", {})
    session.responses[phase] = Response(error=error_type())
    expected = (
        asyncio.CancelledError
        if error_type is asyncio.CancelledError
        else BusinessError
    )
    with pytest.raises(expected) as caught:
        await upload.upload_fixed_guide(client, public=True, authorize=Mock())
    assert caught.value.submitted is True
    assert len(session.calls) == phase + 1
    assert feature_image.REVIEWED_ASSETS == {}
    assert not client.lock.locked()


@pytest.mark.asyncio
async def test_missing_authentication_never_constructs_media_session(transport):
    client, session, factory = transport
    client.jwt = ""
    with pytest.raises(BusinessError, match="reauth_required") as caught:
        await upload.upload_fixed_guide(client, public=True, authorize=Mock())
    assert caught.value.submitted is False
    factory.assert_not_called()
    assert not session.calls


@pytest.mark.asyncio
async def test_guard_runs_after_lock_admission_before_any_network(transport):
    client, session, factory = transport
    guard = Mock(side_effect=wire.ProtocolError("revoked"))
    await client.lock.acquire()
    task = asyncio.create_task(
        upload.upload_fixed_guide(client, public=True, authorize=guard)
    )
    try:
        await asyncio.sleep(0)
        guard.assert_not_called()
        client.lock.release()
        with pytest.raises(wire.ProtocolError, match="revoked") as caught:
            await task
        assert caught.value.submitted is False
    finally:
        if client.lock.locked():
            client.lock.release()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    factory.assert_not_called()
    assert not session.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", [0, 1, 2])
@pytest.mark.parametrize("change", ["account", "token", "deployment", "endpoint"])
async def test_changed_account_or_authority_never_publishes_receipt(
    transport, monkeypatch, phase, change
):
    client, session, _ = transport

    def mutate():
        if change == "account":
            client.fields[3] = 31846829
        elif change == "token":
            client.jwt = "another-fixture-token"
        elif change == "deployment":
            client.deployment = SimpleNamespace(origin=ORIGIN)
        else:
            monkeypatch.setattr(upload, "REVIEWED_MEDIA_ENDPOINTS", {})

    session.responses[phase].after_read = mutate
    with pytest.raises(BusinessError, match="guide_upload_scope_changed") as caught:
        await upload.upload_fixed_guide(client, public=True, authorize=Mock())
    assert caught.value.submitted is True
    assert len(session.calls) == phase + 1
