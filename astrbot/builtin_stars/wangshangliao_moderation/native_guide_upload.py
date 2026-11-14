"""Bounded group-media uploads using the official native protocol."""

import asyncio
import hashlib
import inspect
import io
import json
from dataclasses import dataclass
from urllib.parse import urlsplit

import aiohttp
from PIL import Image

from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.business import BusinessError

IMAGE_LIMIT = 1024 * 1024

# APK 3.7.1 and the verified 2026-10-16 17:00:15 UTC official LineEntry agree.
# Bind the reviewed pair to this deployment; never infer a host from caller data.
REVIEWED_MEDIA_ENDPOINTS: dict[str, tuple[str, str]] = {
    "https://47.86.91.23:5015": (
        "https://upapi01.hfjtqy.com/v1/assets/userupload",
        "https://images-03003.hfjtqy.com/assets/",
    )
}


@dataclass(frozen=True)
class VerifiedGuideAsset:
    """Receipt returned only after the fixed original and thumbnail match."""

    account: str
    digest: str
    asset_id: str
    size: int


def is_native_asset_id(value):
    """Check the positive decimal uint64 shape supported by the guide codec.

    Args:
        value: Candidate native asset identifier, never a resource URL.

    Returns:
        Whether the value is a canonical, bounded native identifier string.
    """
    return (
        type(value) is str
        and 0 < len(value) <= 20
        and value.isascii()
        and value.isdecimal()
        and not value.startswith("0")
        and int(value) < 1 << 64
    )


async def _read_bounded(response, limit):
    """Read one bounded media response without trusting Content-Length.

    Args:
        response: Upload, original-image or thumbnail HTTP response.
        limit: Maximum decoded bytes accepted for this response.

    Returns:
        Complete bounded response bytes.

    Raises:
        BusinessError: If the response exceeds the fixed bound.
    """
    data = bytearray()
    async for chunk in response.content.iter_chunked(8192):
        data.extend(chunk)
        if len(data) > limit:
            raise BusinessError("guide_upload_response_limit")
    return bytes(data)


async def upload_verified_image(
    client, image, *, width, height, filename="scheduled-image.jpg", authorize
):
    """Upload server-normalized JPEG bytes through the reviewed native endpoint.

    Args:
        client: Existing authenticated business connection.
        image: Verified JPEG bytes, never a browser URL or path.
        width: Verified pixel width.
        height: Verified pixel height.
        filename: Server-owned multipart filename.
        authorize: Scope and lifecycle check before and after native IO.

    Returns:
        Account-bound native asset with byte-identical original and thumbnail.

    Raises:
        BusinessError: If delivery or readback cannot be established.
    """
    submitted, uploaded_asset = False, ""
    try:
        if (
            type(image) is not bytes
            or not 0 < len(image) <= IMAGE_LIMIT
            or type(width) is not int
            or type(height) is not int
            or not 1 <= width <= 2048
            or not 1 <= height <= 2048
        ):
            raise BusinessError("guide_image_shape")
        with Image.open(io.BytesIO(image)) as bitmap:
            if bitmap.format != "JPEG" or bitmap.size != (width, height):
                raise BusinessError("guide_image_shape")
            bitmap.verify()
        digest = hashlib.sha256(image).hexdigest()
        deployment = client.deployment
        origin = deployment.origin.rstrip("/")
        endpoints = REVIEWED_MEDIA_ENDPOINTS.get(origin)
        if type(endpoints) is not tuple or len(endpoints) != 2:
            raise BusinessError("guide_upload_endpoint_unreviewed")
        upload_url, download_prefix = endpoints
        for address in (upload_url, download_prefix):
            if (
                type(address) is not str
                or not 0 < len(address) <= 512
                or not address.isascii()
                or any(ord(char) <= 32 or ord(char) == 127 for char in address)
                or "\\" in address
                or "%" in address
            ):
                raise BusinessError("guide_upload_endpoint_invalid")
            parsed = urlsplit(address)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.port not in (None, 443)
                or parsed.query
                or parsed.fragment
            ):
                raise BusinessError("guide_upload_endpoint_invalid")
        if not download_prefix.endswith("/"):
            raise BusinessError("guide_upload_endpoint_invalid")
        account = client.fields[3]
        if type(account) is not int or not 0 < account < 1 << 30:
            raise BusinessError("reauth_required", 401)
        async with client.lock:
            token = client.jwt
            if (
                type(token) is not str
                or not 0 < len(token) <= 65536
                or not token.isascii()
                or any(ord(char) <= 32 or ord(char) == 127 for char in token)
            ):
                raise BusinessError("reauth_required", 401)

            async def check():
                previous = getattr(client, "_authorization_task", None)
                client._authorization_task = asyncio.current_task()
                try:
                    checked = authorize()
                    if inspect.isawaitable(checked):
                        await checked
                finally:
                    client._authorization_task = previous
                if (
                    client.deployment is not deployment
                    or deployment.origin.rstrip("/") != origin
                    or client.fields[3] != account
                    or client.jwt != token
                    or REVIEWED_MEDIA_ENDPOINTS.get(origin) != endpoints
                ):
                    raise BusinessError("guide_upload_scope_changed")

            await check()
            form = aiohttp.FormData()
            form.add_field("asset_type", "1")
            form.add_field("use_for", "1")
            form.add_field("height", str(height))
            form.add_field("width", str(width))
            # Official MediaUploadWorker includes a thumbnail for MESSAGE images.
            for field in ("file", "thumbnail"):
                form.add_field(
                    field, image, filename=filename, content_type="image/jpeg"
                )
            # A separate cookie-free session prevents business default headers,
            # cookies or environment proxies from reaching a media/download host.
            async with aiohttp.ClientSession(
                cookie_jar=aiohttp.DummyCookieJar(),
                trust_env=False,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as http:
                await check()
                submitted = True
                async with http.post(
                    upload_url,
                    data=form,
                    headers={"token": token},
                    allow_redirects=False,
                    ssl=True,
                ) as response:
                    if response.status != 200:
                        raise BusinessError("guide_upload_rejected", response.status)
                    raw = await _read_bounded(response, 65536)

                def unique_object(pairs):
                    """Reject ambiguous duplicate keys in the upload receipt."""
                    value = {}
                    for key, item in pairs:
                        if key in value:
                            raise ValueError
                        value[key] = item
                    return value

                try:
                    value = json.loads(raw, object_pairs_hook=unique_object)
                except (ValueError, UnicodeError):
                    raise BusinessError("guide_upload_response_invalid") from None
                if (
                    type(value) is not dict
                    or type(value.get("code")) is not int
                    or value["code"] != 0
                ):
                    raise BusinessError("guide_upload_response_invalid")
                asset = value.get("id")
                if type(asset) is int:
                    asset = str(asset)
                if not is_native_asset_id(asset):
                    raise BusinessError("guide_upload_asset_invalid")
                uploaded_asset = asset
                # XClientUtils/MessageHelper use prefix + id; thumbnails add .jpg.
                # Require byte identity even for thumbnails, failing closed if a
                # provider transforms our supplied thumbnail instead of storing it.
                for suffix in ("", ".jpg"):
                    await check()
                    async with http.get(
                        download_prefix + asset + suffix,
                        headers={},
                        allow_redirects=False,
                        ssl=True,
                    ) as response:
                        if response.status != 200:
                            raise BusinessError(
                                "guide_upload_readback_rejected", response.status
                            )
                        downloaded = await _read_bounded(response, IMAGE_LIMIT)
                    if hashlib.sha256(downloaded).hexdigest() != digest:
                        raise BusinessError("guide_upload_readback_mismatch")
                await check()
                return VerifiedGuideAsset(str(account), digest, asset, len(image))
    except (asyncio.CancelledError, wire.ProtocolError) as exc:
        exc.submitted = submitted
        raise
    except Exception as cause:
        error = BusinessError(
            "guide_upload_unknown" if submitted else "guide_upload_invalid"
        )
        error.submitted = submitted
        error.asset_id = uploaded_asset
        error.cause_code = (
            str(cause) if isinstance(cause, BusinessError) else type(cause).__name__
        )
        error.number = getattr(cause, "number", 0)
        raise error from None
