"""Validate explicit Beijing lottery publication times independently of host time."""

import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")
MAX_SCHEDULE_DAYS = 30
START_GRACE_SECONDS = 60


def parse_start_at(value, *, future=False):
    """Parse an optional exact Beijing calendar minute.

    Args:
        value: Empty text for immediate publication or YYYY-MM-DDTHH:mm.
        future: Require a future instant within thirty days.

    Returns:
        A Unix timestamp, or zero for immediate publication.

    Raises:
        ValueError: If the input is invalid, past, or too far in the future.
    """
    if type(value) is not str:
        raise ValueError("开始时间须为北京时间日期和时刻，留空表示立即发送")
    if not value:
        return 0.0
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}", value):
        raise ValueError("开始时间格式：YYYY-MM-DD HH:mm（北京时间）")
    try:
        stamp = datetime.strptime(value.replace("T", " "), "%Y-%m-%d %H:%M")
        result = stamp.replace(tzinfo=BEIJING).timestamp()
    except (ValueError, OverflowError, OSError):
        raise ValueError("开始时间不是有效的北京时间日期和时刻") from None
    now = time.time()
    if future and not now < result <= now + MAX_SCHEDULE_DAYS * 86400:
        raise ValueError("开始时间须晚于现在，且在未来30天内（北京时间）")
    return result


def beijing_time(stamp):
    """Format a frozen instant explicitly in Beijing time.

    Args:
        stamp: Unix timestamp from the frozen lottery record.

    Returns:
        Calendar date, time and the explicit Beijing timezone label.
    """
    return (
        datetime.fromtimestamp(stamp, BEIJING).strftime("%Y-%m-%d %H:%M:%S")
        + "（北京时间）"
    )
