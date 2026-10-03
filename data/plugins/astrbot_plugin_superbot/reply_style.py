"""Telegram entity-based styling without interpreting user-provided markup."""

import re

from telegram import MessageEntity


def style_reply(text, result, prefix="", fold_lines=()):
    """Style business headings and amounts with UTF-16 entity coordinates.

    Args:
        text: Plain business reply without the recipient line.
        result: Durable outbox result type.
        prefix: Optional recipient line already formatted by the caller.
        fold_lines: Explicit settlement detail line indexes eligible for folding.

    Returns:
        Plain display text and non-overlapping bold entities.
    """
    icons = {
        "accepted": "✅",
        "rejected": "⚠️",
        "activated": "🎲",
        "history": "📜",
        "settlement": "🏁",
        "bets": "🧾",
        "profit": "💎",
        "flow": "🧾",
        "duel": "🤝",
        "duel_result": "🏁",
    }
    lines = text.splitlines()
    if not lines:
        return prefix + text, []
    if result in icons:
        lines[0] = f"{icons[result]} {lines[0]}"
    # Keep the entire heading together; separate detail from the recipient.
    body = "\n".join(lines)
    rendered = prefix + body
    spans = [(len(prefix), len(prefix) + len(lines[0]))]
    for match in re.finditer(
        r"(?:合计扣分|剩余积分|本群积分|返还|投注|净变动)[：:]?\s*([+\-]?\d+)",
        body,
    ):
        start, end = match.span(1)
        start, end = start + len(prefix), end + len(prefix)
        if start >= spans[0][1]:
            spans.append((start, end))
    entities = [
        MessageEntity(
            type=MessageEntity.BOLD,
            offset=len(rendered[:start].encode("utf-16-le")) // 2,
            length=len(rendered[start:end].encode("utf-16-le")) // 2,
        )
        for start, end in spans
    ]
    if result == "profit" and len(lines) > 10 and lines[2] == "积分输赢":
        fold_lines = (3, 4, 7, 10)
        for index in (2, 6, 9):
            start = len(prefix) + sum(len(line) + 1 for line in lines[:index])
            entities.append(
                MessageEntity(
                    type=MessageEntity.BOLD,
                    offset=len(rendered[:start].encode("utf-16-le")) // 2,
                    length=len(lines[index].encode("utf-16-le")) // 2,
                )
            )
    if result in {"flow", "bets"}:
        fold_lines = [
            index
            for index, line in enumerate(lines)
            if index > 0 and line.startswith("第 ") and " 期 · " in line
        ]
    if result == "history":
        fold_lines = [
            index
            for index, line in enumerate(lines)
            if index > 0 and line.startswith("第 ") and " 期：" in line
        ]
    if result in {"duel", "duel_result"}:
        fold_lines = [
            index
            for index, line in enumerate(lines)
            if index > 0 and (line.startswith("🎁") or line.startswith("仅供娱乐"))
        ]
        for index, line in enumerate(lines):
            if index > 0 and line.startswith(("🏆", "⏳", "🎯")):
                start = len(prefix) + sum(len(part) + 1 for part in lines[:index])
                entities.append(
                    MessageEntity(
                        type=MessageEntity.BOLD,
                        offset=len(rendered[:start].encode("utf-16-le")) // 2,
                        length=len(line.encode("utf-16-le")) // 2,
                    )
                )
    if (
        result
        in {"settlement", "profit", "flow", "bets", "history", "duel", "duel_result"}
        and fold_lines
    ):
        selected = set(fold_lines)
        offset, start, end = len(prefix), None, None
        for index, line in enumerate(lines):
            if index in selected:
                if start is None:
                    start = offset
                end = offset + len(line)
            elif start is not None:
                entities.append(
                    MessageEntity(
                        type="expandable_blockquote",
                        offset=len(rendered[:start].encode("utf-16-le")) // 2,
                        length=len(rendered[start:end].encode("utf-16-le")) // 2,
                    )
                )
                start = None
            offset += len(line) + 1
        if start is not None:
            entities.append(
                MessageEntity(
                    type="expandable_blockquote",
                    offset=len(rendered[:start].encode("utf-16-le")) // 2,
                    length=len(rendered[start:end].encode("utf-16-le")) // 2,
                )
            )
    return rendered, entities
