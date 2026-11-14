"""Private TMZYZ parse-gateway credentials and bounded playlist resolution."""

import hashlib
import ipaddress
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

GATEWAY_ORIGIN = "https://parse.tmzyz.com"
CARRIERS = ("移动", "电信", "联通")
_DOCUMENTATION_IP = "1.2.3.4"
_PLAYER_CODE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_TOKEN = re.compile(r"[A-Za-z0-9._~-]{1,128}")
_MANIFEST = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True)
class GatewayConfig:
    """One instance-owned gateway credential set; never exposed by the API."""

    player_code: str
    token: str
    carrier: str = "移动"
    client_ip: str = ""
    prefer: bool = True


def load(path: Path) -> GatewayConfig | None:
    """Read an optional private gateway file without accepting loose permissions.

    Args:
        path: Instance-owned ``tmzyz_gateway.json`` path.

    Returns:
        Validated credentials, or None when the file does not exist.

    Raises:
        ProtocolError: If the file is a symlink, too large, readable by another
            user, owned by another account or structurally invalid.
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise ProtocolError("tmzyz_gateway_config_unreadable") from None
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_size > 4096
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise ProtocolError("tmzyz_gateway_config_insecure")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise ProtocolError("tmzyz_gateway_config_invalid") from None
    if not isinstance(value, dict) or not set(value) <= {
        "player_code",
        "token",
        "carrier",
        "client_ip",
        "prefer",
    }:
        raise ProtocolError("tmzyz_gateway_config_invalid")
    player_code = value.get("player_code")
    token = value.get("token")
    carrier = value.get("carrier", "移动")
    client_ip = value.get("client_ip", "")
    prefer = value.get("prefer", True)
    if (
        type(player_code) is not str
        or not _PLAYER_CODE.fullmatch(player_code)
        or type(token) is not str
        or not _TOKEN.fullmatch(token)
        or carrier not in CARRIERS
        or type(client_ip) is not str
        or client_ip
        and not _public_ip(client_ip)
        or type(prefer) is not bool
    ):
        raise ProtocolError("tmzyz_gateway_config_invalid")
    return GatewayConfig(player_code, token, carrier, client_ip, prefer)


def _public_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


async def _read_bounded(response, limit: int) -> bytes:
    data = bytearray()
    async for chunk in response.content.iter_chunked(8192):
        data.extend(chunk)
        if len(data) > limit:
            raise ProtocolError("tmzyz_gateway_response_limit")
    return bytes(data)


async def _document(
    http, method: str, url: str, guard, *, limit=65536, **kwargs
) -> dict:
    guard()
    async with getattr(http, method)(
        url, allow_redirects=False, ssl=True, **kwargs
    ) as response:
        if response.status != 200:
            raise ProtocolError("tmzyz_gateway_rejected")
        raw = await _read_bounded(response, limit)
    guard()
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ProtocolError("tmzyz_gateway_response_invalid") from None
    if not isinstance(value, dict):
        raise ProtocolError("tmzyz_gateway_response_invalid")
    return value


async def resolve(
    http, resource_id: str, config: GatewayConfig, guard
) -> tuple[str, str]:
    """Resolve one reviewed resource ID through the private parse gateway.

    Args:
        http: Cookie-free, public-address-only HTTP client.
        resource_id: 32-character lowercase ``bkplayer`` resource identifier.
        config: Private gateway credentials.
        guard: Current account, group and operation authority fence.

    Returns:
        Bounded playlist text and its 32-character manifest identifier.

    Raises:
        ProtocolError: On invalid input, provider rejection, quota exhaustion,
            malformed receipts, or an invalid playlist.
    """
    if type(resource_id) is not str or not _MANIFEST.fullmatch(resource_id):
        raise ProtocolError("video_manifest_invalid")
    sign = hashlib.md5(
        f"{config.player_code}{config.token}{resource_id}".encode()
    ).hexdigest()
    receipt = await _document(
        http,
        "post",
        GATEWAY_ORIGIN + "/api/getPlayerUrl",
        guard,
        json={
            "name": resource_id,
            "sign": sign,
            "player_code": config.player_code,
            "ip": config.client_ip or _DOCUMENTATION_IP,
            "carrier": config.carrier,
        },
    )
    if type(receipt.get("code")) is not int:
        raise ProtocolError("tmzyz_gateway_response_invalid")
    if receipt["code"] != 0:
        message = receipt.get("message")
        if isinstance(message, str) and "点数不足" in message:
            raise ProtocolError("tmzyz_gateway_insufficient")
        if isinstance(message, str) and "网络渠道" in message:
            raise ProtocolError("tmzyz_gateway_carrier")
        raise ProtocolError("tmzyz_gateway_rejected")
    data = receipt.get("data")
    manifest = data.get("mpd") if isinstance(data, dict) else None
    if type(manifest) is not str or not _MANIFEST.fullmatch(manifest):
        raise ProtocolError("tmzyz_gateway_response_invalid")
    receipt = await _document(
        http,
        "get",
        GATEWAY_ORIGIN + "/api/mpd/" + manifest,
        guard,
        limit=1_048_576,
    )
    if type(receipt.get("code")) is not int or receipt["code"] != 0:
        raise ProtocolError("tmzyz_gateway_response_invalid")
    data = receipt.get("data")
    playlist = data.get("mpd") if isinstance(data, dict) else None
    if (
        type(playlist) is not str
        or not 0 < len(playlist.encode("utf-8")) <= 1_048_576
        or not playlist.startswith("#EXTM3U")
        or not playlist.rstrip().endswith("#EXT-X-ENDLIST")
    ):
        raise ProtocolError("tmzyz_gateway_response_invalid")
    guard()
    return playlist, manifest
