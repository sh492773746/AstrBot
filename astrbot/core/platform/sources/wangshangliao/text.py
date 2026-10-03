"""Plain-text presentation for a platform without rich-text support."""

import re

from markdown_it import MarkdownIt

PLAIN_TEXT_INSTRUCTION = (
    "旺商聊只支持纯文本。请用空行、空格、简短的【标题】和编号组织回复。"
    "不要使用 Markdown 标题、加粗、代码围栏、表格或 HTML。"
    "表格信息改为逐项说明；链接保留完整地址；代码内容保持完整。"
    "段落简短，不要为了排版重复内容。"
    "不要在回复中泄露密码、验证码、API Key、Token 或其他认证凭证；"
    "手机号和邮箱使用掩码展示。不要复述工具结果中的认证信息。"
)

_REASONING_TAGS = ("think", "thinking", "thought", "analysis")


def strip_reasoning_markup(text: str, *, strip: bool = True) -> str:
    """Remove model reasoning accidentally returned as visible plain text.

    Some providers expose reasoning as structured metadata, while others still
    wrap it in XML-like tags in the assistant text. Wangshangliao only sends
    plain text, so these blocks must be removed at the final platform boundary.
    """
    for tag in _REASONING_TAGS:
        paired = re.compile(
            rf"<{tag}\b[^>]*>.*?</{tag}\s*>",
            flags=re.IGNORECASE | re.DOTALL,
        )
        text = paired.sub("", text)
        # A truncated stream must not expose the remainder of a reasoning block.
        text = re.sub(
            rf"^\s*<{tag}\b[^>]*>.*$",
            "",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        text = re.sub(
            rf"</{tag}\s*>",
            "",
            text,
            flags=re.IGNORECASE,
        )
    return text.strip() if strip else text


def redact_reply(text: str, *, preserve_whitespace: bool = False) -> str:
    """Mask recognizable secrets and contact details in outgoing text.

    Args:
        text: Reply text, not the incoming message or persistent configuration.

    Returns:
        Redacted text. Unlabelled arbitrary secrets cannot be reliably detected.
    """
    text = strip_reasoning_markup(text, strip=not preserve_whitespace)
    text = re.sub(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        "[REDACTED PRIVATE KEY]",
        text,
    )
    text = re.sub(
        r"\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text, flags=re.I
    )
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[REDACTED]", text)
    text = re.sub(
        r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[REDACTED]", text
    )
    text = re.sub(
        r"""(?ix)((?<![\w])(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|token|password|passwd|secret|验证码|密码|密钥)["']?\s*[:=：]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s,，;；&<>]+)""",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(
        r"(?<![\w])(?:\+?86[- ]?)?(1[3-9]\d)(\d{4})(\d{4})(?!\d)", r"\1****\3", text
    )
    text = re.sub(
        r"(?<![\w.+-])([A-Za-z0-9])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})(?![\w])",
        r"\1***@\2",
        text,
    )
    return text


def plain_text(text: str) -> str:
    """Render Markdown tokens as readable text without altering code contents.

    Args:
        text: Model-generated text, possibly containing Markdown.

    Returns:
        Text using line breaks and ordinary characters only.
    """
    text = strip_reasoning_markup(text)
    parser = MarkdownIt("commonmark", {"html": False}).enable("table")
    output = []
    lists = []
    links = []
    in_header = False
    for token in parser.parse(text):
        kind = token.type
        if kind == "heading_open":
            output.append("【")
        elif kind == "heading_close":
            output.append("】\n\n")
        elif kind == "inline":
            for child in token.children or []:
                if child.type in {"text", "code_inline", "html_inline"}:
                    output.append(child.content)
                elif child.type in {"softbreak", "hardbreak"}:
                    output.append("\n")
                elif child.type == "link_open":
                    links.append((child.attrGet("href") or "", len(output)))
                elif child.type == "link_close" and links:
                    url, start = links.pop()
                    if url and "".join(output[start:]) != url:
                        output.append(f" ({url})")
                elif child.type == "image":
                    output.append(f"{child.content} ({child.attrGet('src') or ''})")
        elif kind in {"fence", "code_block"}:
            output.append(token.content + "\n")
        elif kind in {"bullet_list_open", "ordered_list_open"}:
            lists.append(
                int(token.attrGet("start") or 1)
                if kind == "ordered_list_open"
                else None
            )
        elif kind in {"bullet_list_close", "ordered_list_close"}:
            lists.pop()
            output.append("\n")
        elif kind == "list_item_open":
            number = lists[-1]
            output.append(
                "  " * (len(lists) - 1) + ("- " if number is None else f"{number}. ")
            )
            if number is not None:
                lists[-1] += 1
        elif kind == "paragraph_close":
            output.append("\n" if lists else "\n\n")
        elif kind == "thead_open":
            in_header = True
        elif kind == "thead_close":
            in_header = False
        elif kind in {"th_close", "td_close"}:
            output.append("  ")
        elif kind == "tr_close":
            output.append("\n\n" if in_header else "\n")
        elif kind in {"table_close", "blockquote_close", "hr"}:
            output.append("\n")
    return "".join(output).strip()
