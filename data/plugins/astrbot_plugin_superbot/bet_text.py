"""Strict, fully consumed text orders; no model or permissive partial matching."""

import re

from .rules import LABELS
from .store import Rejected


class ItemLimit(Rejected):
    """An oversized valid batch must never be partially submitted."""


ALIASES = {
    "da": "大",
    "x": "小",
    "xiao": "小",
    "d": "单",
    "dan": "单",
    "s": "双",
    "shuang": "双",
    "dad": "大单",
    "dd": "大单",
    "das": "大双",
    "ds": "大双",
    "xd": "小单",
    "xid": "小单",
    "xs": "小双",
    "xis": "小双",
    "jd": "极大",
    "jx": "极小",
    "l": "龙",
    "h": "虎",
    "bz": "豹子",
    "sz": "顺子",
    "dz": "对子",
}
WORDS = {**LABELS, **{key: LABELS[value] for key, value in ALIASES.items()}}
TOKEN = re.compile(
    "(?:"
    + "|".join(re.escape(w) for w in sorted(WORDS, key=len, reverse=True))
    + r"|[0-9]+(?:点|/|\.|押))",
    re.IGNORECASE,
)
SEPARATOR = re.compile(r"[\s|｜]*")
AMOUNT = re.compile(r"[0-9]+")
NUMERIC_PAIRS = re.compile(r"[0-9]+(?:\s+[0-9]+)+")
PREFIX = re.compile(
    r"^(?:[a-zA-Z]+|[0-9]+(?:点|/|\.|押)|"
    + "|".join(re.escape(w) for w in sorted(LABELS, key=len, reverse=True))
    + r")\s*[+\-]?[0-9]",
    re.IGNORECASE,
)


def is_candidate(text):
    """Return whether text starts like an order, including malformed aliases."""
    return bool(PREFIX.match(text.lstrip()))


def parse_partial(text):
    """Keep independently delimited valid items without guessing numeric boundaries.

    Args:
        text: Original plain message, at most 512 characters.

    Returns:
        Valid ordered items and the number of skipped fragments.

    Raises:
        Rejected: No valid items, excessive length or more than fifty items.
    """
    if len(text) > 512:
        raise Rejected("下注消息最多512字符")
    text = re.sub(
        r"(?<![A-Za-z0-9])([abc])([0-9])(?:\s+|[/押.])(?=[0-9])",
        r"\1\2点",
        text,
        flags=re.IGNORECASE,
    )
    if NUMERIC_PAIRS.fullmatch(text.strip()):
        tokens = text.split()
        if len(tokens) > 100:
            raise ItemLimit("每条消息最多50项下注，请拆分发送")
        items, skipped = [], len(tokens) % 2
        for pos in range(0, len(tokens) - 1, 2):
            number, amount = tokens[pos : pos + 2]
            if int(number) > 27 or len(amount) > 12 or int(amount) <= 0:
                skipped += 1
                continue
            items.append((f"number_{int(number)}", int(amount)))
        if not items:
            raise Rejected("未识别到有效下注")
        return items, skipped
    # Keep whitespace between a complete play token and its numeric amount.
    compact = re.sub(
        r"(" + TOKEN.pattern + r")\s+(?=[+\-]?[0-9])",
        r"\1",
        text,
        flags=re.IGNORECASE,
    )
    # Only named plays can safely follow another amount without a separator.
    boundary = (
        r"(?<=[0-9])(?=(?:"
        + "|".join(re.escape(w) for w in sorted(WORDS, key=len, reverse=True))
        + r")\s*[+\-]?[0-9])"
    )
    fragments = re.split(r"[\s|｜]+|" + boundary, compact.strip(), flags=re.IGNORECASE)
    items, skipped = [], 0
    for fragment in fragments:
        if not fragment:
            continue
        try:
            items.extend(parse(fragment))
        except ItemLimit:
            raise
        except Rejected:
            skipped += 1
        if len(items) > 50:
            raise ItemLimit("每条消息最多50项下注，请拆分发送")
    if not items:
        raise Rejected("未识别到有效下注")
    return items, skipped


def parse(text):
    """Parse a complete ordered batch without discarding invalid fragments.

    Args:
        text: Original plain message text.

    Returns:
        Ordered (canonical play, integer points) pairs, preserving duplicates.

    Raises:
        Rejected: Any invalid, ambiguous, oversized or unconsumed input.
    """
    if len(text) > 512:
        raise Rejected("下注消息最多512字符")
    pos, items = 0, []
    while pos < len(text):
        start = pos
        pos = SEPARATOR.match(text, pos).end()
        separated = pos > start
        if pos == len(text):
            break
        token = TOKEN.match(text, pos)
        if not token or (items and text[pos].isdigit() and not separated):
            raise Rejected("下注格式无效；数字点玩法前须有空格或竖线")
        word = token.group().lower()
        pos = token.end()
        while pos < len(text) and text[pos].isspace():
            pos += 1
        amount = AMOUNT.match(text, pos)
        if not amount or len(amount.group()) > 12 or int(amount.group()) <= 0:
            raise Rejected("金额必须为正整数积分，最多12位")
        pos = amount.end()
        if word in WORDS:
            play = WORDS[word]
        elif word.endswith(("点", "/", ".", "押")):
            number = word[:-1]
            if (word.endswith("点") and str(int(number)) != number) or not 0 <= int(
                number
            ) <= 27:
                raise Rejected("数字玩法仅支持0点至27点")
            play = f"number_{int(number)}"
        else:
            play = WORDS[word]
        items.append((play, int(amount.group())))
        if len(items) > 50:
            raise ItemLimit("每条消息最多50项下注，请拆分发送")
    if not items:
        raise Rejected("请填写玩法和正整数金额")
    return items
