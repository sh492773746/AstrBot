"""Plain-text presentation for a platform without rich-text support."""

from markdown_it import MarkdownIt

from astrbot.utils.text_redaction import (
    redact_reply as redact_reply,
)
from astrbot.utils.text_redaction import (
    strip_reasoning_markup as strip_reasoning_markup,
)

PLAIN_TEXT_INSTRUCTION = (
    "旺商聊只支持纯文本。请用空行、空格、简短的【标题】和编号组织回复。"
    "不要使用 Markdown 标题、加粗、代码围栏、表格或 HTML。"
    "表格信息改为逐项说明；链接保留完整地址；代码内容保持完整。"
    "段落简短，不要为了排版重复内容。"
    "不要在回复中泄露密码、验证码、API Key、Token 或其他认证凭证；"
    "手机号和邮箱使用掩码展示。不要复述工具结果中的认证信息。"
)


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
