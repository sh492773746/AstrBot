"""Independent bounded PlayNow reader with a dedicated optional proxy."""

import asyncio
import math
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import httpx

from .rules import canada_balls
from .store import Rejected

URL = "https://www.playnow.com/services2/keno/draw/latest/100/0"


def poll_interval(latest_at, now):
    """Choose a bounded fast window around the next expected draw.

    Args:
        latest_at: Last verified source draw timestamp, or None.
        now: Current wall-clock timestamp.
    """
    return (
        3.0 if latest_at is not None and -10 <= now - (latest_at + 210) <= 90 else 10.0
    )


class KenoDeferred(Exception):
    """A shared source cooldown prohibits both live and historical requests."""

    def __init__(self, seconds):
        self.seconds = seconds
        super().__init__("Keno source cooldown")


def parse(payload):
    """Validate source evidence; never fabricate an unparseable draw time.

    Args:
        payload: Decoded PlayNow response.

    Returns:
        Strict canonical records in source order.
    """
    rows = payload.get("draws") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise Rejected("Keno 数据结构无效")
    output = []
    for row in rows:
        if not isinstance(row, dict):
            raise Rejected("Keno 数据结构无效")
        raw = row.get("drawNbrs", [])
        canada_balls(raw)
        stamp = f"{row.get('drawDate', '')} {row.get('drawTime', '')}"
        point = None
        for fmt in ("%b %d, %Y %I:%M:%S %p", "%B %d, %Y %I:%M:%S %p"):
            try:
                parsed = datetime.strptime(stamp, fmt)
                zone = ZoneInfo("America/Vancouver")
                early, late = (
                    parsed.replace(tzinfo=zone, fold=0),
                    parsed.replace(tzinfo=zone, fold=1),
                )
                if early.utcoffset() != late.utcoffset():
                    raise Rejected("夏令时切换的开奖时间不明确，暂停核查")
                point = early.timestamp()
                break
            except ValueError:
                continue
        if point is None:
            raise Rejected("Keno 开奖时间无法核实")
        output.append(
            {"issue": row.get("drawNbr"), "at": point, "raw": raw, "evidence": row}
        )
    return output


class Keno:
    def __init__(self, proxy=""):
        self.lock = asyncio.Lock()
        self.retry_at = 0.0
        self.failures = 0
        self.client = httpx.AsyncClient(
            proxy=proxy or None,
            timeout=15,
            follow_redirects=False,
            trust_env=False,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://www.playnow.com/lottery/keno/",
            },
        )

    async def fetch(self, offset=0):
        """Read a bounded source page; offset counts draws before the latest page.

        Args:
            offset: Nonnegative historical draw offset, verified against returned IDs.
        """
        if type(offset) is not int or not 0 <= offset <= 10_000_000:
            raise Rejected("开奖补采范围无效")
        url = URL if offset == 0 else URL.rsplit("/", 1)[0] + f"/{offset}"
        async with self.lock:
            if self.retry_at > time.time():
                raise KenoDeferred(self.retry_at - time.time())
            try:
                # Bound the complete response, not just each individual socket read.
                async with asyncio.timeout(15):
                    async with self.client.stream("GET", url) as response:
                        if response.status_code in {429, 503}:
                            delay = 30.0
                            header = response.headers.get("Retry-After", "")
                            try:
                                parsed = float(header)
                                if math.isfinite(parsed) and parsed >= 0:
                                    delay = max(delay, parsed)
                            except ValueError:
                                try:
                                    target = parsedate_to_datetime(header)
                                    if target.tzinfo is None:
                                        target = target.replace(tzinfo=timezone.utc)
                                    delay = max(delay, target.timestamp() - time.time())
                                except (ValueError, TypeError, OverflowError):
                                    pass
                            self.retry_at = time.time() + delay
                            raise KenoDeferred(delay)
                        response.raise_for_status()
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > 2_000_000:
                                raise Rejected("Keno 响应过大")
                import json

                draws = parse(json.loads(body))
                self.failures = 0
                return draws
            except KenoDeferred:
                raise
            except Exception:
                self.failures = min(self.failures + 1, 5)
                self.retry_at = time.time() + min(120, 10 * 2 ** (self.failures - 1))
                raise

    async def close(self):
        await self.client.aclose()
