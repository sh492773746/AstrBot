"""Bounded image materialization; remote sources never access internal hosts."""

from __future__ import annotations

import asyncio
import base64
import io
import ipaddress
import os
import stat
import tempfile
import time
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import aiohttp
from PIL import Image as PILImage

from astrbot.api.message_components import Image
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

from .protocol import LajiaoLiaoProtocolError

MAX_BYTES = 20 << 20
MAX_PIXELS = 40000000
MAX_GIF_FRAMES = 120
MAX_GIF_TOTAL_PIXELS = 100000000
CDN_HOSTS = {
    "lj-im-chat-01.lajiaoliao.com",
    "lj-chat-im-01.lajiaoliao.com",
    "im-api.lajiaoliao.com",
    "lj-chat-im-01.oss-accelerate.aliyuncs.com",
}
SLOTS = asyncio.Semaphore(2)


class PublicResolver(aiohttp.abc.AbstractResolver):
    """Validate every DNS result before handing it to the HTTP connector."""

    def __init__(self):
        self.delegate = aiohttp.resolver.ThreadedResolver()

    async def resolve(self, host, port=0, family=0):
        rows = await self.delegate.resolve(host, port, family)
        if not rows or any(
            not ipaddress.ip_address(row["host"]).is_global for row in rows
        ):
            raise LajiaoLiaoProtocolError(
                "image_address_rejected", delivery_status="rejected"
            )
        return rows

    async def close(self):
        await self.delegate.close()


def validate_url(value, *, inbound=False):
    """Validate a remote image address without following redirects.

    Args:
        value: Remote URL.
        inbound: Restrict upstream downloads to verified official hosts.

    Raises:
        LajiaoLiaoProtocolError: If the URL is unsafe.
    """
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise LajiaoLiaoProtocolError(
            "image_url_rejected", delivery_status="rejected"
        ) from None
    try:
        invalid = parsed.port not in {None, 443}
    except ValueError:
        invalid = True
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or invalid
        or (inbound and parsed.hostname not in CDN_HOSTS)
    ):
        raise LajiaoLiaoProtocolError("image_url_rejected", delivery_status="rejected")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return
    if not address.is_global:
        raise LajiaoLiaoProtocolError(
            "image_address_rejected", delivery_status="rejected"
        )


def validate_bytes(raw):
    """Validate size, format and dimensions without allocating decoded pixels.

    Args:
        raw: Encoded JPEG or PNG bytes.

    Returns:
        MIME type of the verified image.

    Raises:
        LajiaoLiaoProtocolError: If the image is invalid or exceeds limits.
    """
    if not 0 < len(raw) <= MAX_BYTES:
        raise LajiaoLiaoProtocolError("image_size_exceeded", delivery_status="rejected")
    try:
        with PILImage.open(io.BytesIO(raw)) as image:
            if image.format not in {"JPEG", "PNG", "GIF"}:
                raise ValueError("invalid_image")
            if image.width * image.height > MAX_PIXELS:
                raise ValueError("image_dimensions_exceeded")
            if image.format == "GIF":
                if image.n_frames > MAX_GIF_FRAMES:
                    raise ValueError("gif_frame_limit_exceeded")
                if image.width * image.height * image.n_frames > MAX_GIF_TOTAL_PIXELS:
                    raise ValueError("gif_decode_budget_exceeded")
                for frame in range(image.n_frames):
                    image.seek(frame)
                    image.load()
                mime = "image/gif"
            else:
                if getattr(image, "is_animated", False):
                    raise ValueError("animated_png_not_supported")
                mime = "image/jpeg" if image.format == "JPEG" else "image/png"
                image.verify()
            return mime
    except Exception:
        raise LajiaoLiaoProtocolError(
            "image_format_or_dimensions_rejected", delivery_status="rejected"
        ) from None


class Images:
    """Account-private images with a fixed two-task global concurrency limit."""

    def __init__(self, directory):
        self.directory = Path(directory) / "media"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)

    async def materialize(self, value, *, inbound=False):
        """Use AstrBot's parser on validated private files, never remote URLs.

        Args:
            value: Image source from a component or verified upstream message.
            inbound: Whether to require an official CDN URL.

        Returns:
            A private temporary path and verified MIME type.

        Raises:
            LajiaoLiaoProtocolError: For invalid sources or failed downloads.
        """
        async with SLOTS, asyncio.timeout(50):
            value = str(value)
            raw = None
            if value.startswith("https://"):
                validate_url(value, inbound=True)
                resolver = PublicResolver()
                connector = aiohttp.TCPConnector(
                    resolver=resolver, limit=2, ttl_dns_cache=0
                )
                try:
                    async with aiohttp.ClientSession(
                        connector=connector,
                        trust_env=False,
                        auto_decompress=False,
                        timeout=aiohttp.ClientTimeout(total=40, connect=5),
                    ) as http:
                        for hop in range(4):
                            validate_url(value, inbound=True)
                            async with http.get(
                                value, allow_redirects=False
                            ) as response:
                                if response.status in {301, 302, 303, 307, 308}:
                                    if hop == 3:
                                        raise LajiaoLiaoProtocolError(
                                            "image_redirect_limit",
                                            delivery_status="rejected",
                                        )
                                    value = urljoin(
                                        value, response.headers.get("Location", "")
                                    )
                                    continue
                                if (response.content_length or 0) > MAX_BYTES:
                                    raise LajiaoLiaoProtocolError(
                                        "image_size_exceeded",
                                        delivery_status="rejected",
                                    )
                                if response.status != 200:
                                    if (
                                        400 <= response.status < 500
                                        and response.status != 429
                                    ):
                                        raise LajiaoLiaoProtocolError(
                                            "image_access_rejected",
                                            delivery_status="rejected",
                                        )
                                    raise ValueError("image_response_failed")
                                content = bytearray()
                                async for chunk in response.content.iter_chunked(65536):
                                    content.extend(chunk)
                                    if len(content) > MAX_BYTES:
                                        raise LajiaoLiaoProtocolError(
                                            "image_size_exceeded",
                                            delivery_status="rejected",
                                        )
                                raw = bytes(content)
                                break
                except LajiaoLiaoProtocolError:
                    raise
                except Exception:
                    raise LajiaoLiaoProtocolError(
                        "image_download_failed", delivery_status="failed"
                    ) from None
                finally:
                    await resolver.close()
            elif inbound:
                raise LajiaoLiaoProtocolError(
                    "image_url_rejected", delivery_status="rejected"
                )
            elif value.startswith(("base64://", "data:image/")):
                if value.startswith("data:") and "," not in value:
                    raise LajiaoLiaoProtocolError(
                        "image_base64_invalid", delivery_status="rejected"
                    )
                encoded = (
                    value.split(",", 1)[1] if value.startswith("data:") else value[9:]
                )
                if len(encoded) > (MAX_BYTES + 2) // 3 * 4:
                    raise LajiaoLiaoProtocolError(
                        "image_size_exceeded", delivery_status="rejected"
                    )
                try:
                    raw = base64.b64decode(encoded, validate=True)
                except ValueError:
                    raise LajiaoLiaoProtocolError(
                        "image_base64_invalid", delivery_status="rejected"
                    ) from None
            else:
                parsed = urlsplit(value)
                if parsed.scheme not in {"", "file"} or (
                    parsed.scheme == "file" and parsed.netloc
                ):
                    raise LajiaoLiaoProtocolError(
                        "image_source_rejected", delivery_status="rejected"
                    )
                path = Path(
                    unquote(parsed.path) if parsed.scheme == "file" else value
                ).resolve()
                roots = [
                    Path(get_astrbot_temp_path()).resolve(),
                    self.directory.resolve(),
                ]
                if not any(path.is_relative_to(root) for root in roots):
                    raise LajiaoLiaoProtocolError(
                        "image_local_path_rejected", delivery_status="rejected"
                    )
                raw = await asyncio.to_thread(self.read_local, path)
            mime = await asyncio.to_thread(validate_bytes, raw)
            await asyncio.to_thread(self.cleanup)
            fd, name = tempfile.mkstemp(
                prefix="image-",
                suffix={
                    "image/png": ".png",
                    "image/gif": ".gif",
                }.get(mime, ".jpg"),
                dir=self.directory,
            )
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                path = await Image(file=name).convert_to_file_path()
                if Path(path).resolve() != Path(name).resolve():
                    raise LajiaoLiaoProtocolError(
                        "image_resolver_path_changed", delivery_status="rejected"
                    )
                return Path(path), mime
            except BaseException:
                Path(name).unlink(missing_ok=True)
                raise

    def cleanup(self):
        """Remove abandoned media after at most one day."""
        for path in self.directory.glob("image-*"):
            try:
                if path.lstat().st_mtime < time.time() - (86400 - 300):
                    path.unlink(missing_ok=True)
            except FileNotFoundError:
                continue

    @staticmethod
    def read_local(path):
        """Read a bounded regular file without following a replaced symlink."""
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                    raise ValueError("invalid_local_image")
                raw = stream.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ValueError("invalid_local_image")
                return raw
        except (OSError, ValueError):
            raise LajiaoLiaoProtocolError(
                "image_local_path_rejected", delivery_status="rejected"
            ) from None
