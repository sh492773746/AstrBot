"""Safe formatting for plugin-owned business text, never advertiser documents."""

from html import escape
from html.parser import HTMLParser

from telegram.error import BadRequest


def cards(text, *, fold_sections=False):
    """Split plain business text before escaping, preserving every character.

    Args:
        text: Plain text containing untrusted names or descriptions.
        fold_sections: Render rule sections as bold headings and native quotes.

    Returns:
        Plain/HTML pairs, each below the conservative transport size bound.
    """
    if fold_sections:
        pages = []
        for section in str(text).strip().split("\n\n"):
            title, _, body = section.partition("\n")
            rendered = "<b>" + escape(title, quote=False) + "</b>"
            if body:
                rendered += (
                    "\n<blockquote expandable>"
                    + escape(body, quote=False)
                    + "</blockquote>"
                )
            if len(rendered.encode("utf-16-le")) // 2 > 3800:
                pages.extend(cards(section))
            elif (
                pages
                and len((pages[-1][1] + rendered).encode("utf-16-le")) // 2 + 2 <= 3800
            ):
                plain, previous = pages.pop()
                pages.append((plain + "\n\n" + section, previous + "\n\n" + rendered))
            else:
                pages.append((section, rendered))
        return pages or [(" ", " ")]
    pages, plain, rendered = [], "", ""
    for line in str(text).splitlines(keepends=True):
        parts = (
            [line[i : i + 500] for i in range(0, len(line), 500)]
            if len(escape(line).encode("utf-16-le")) // 2 > 3500
            else [line]
        )
        for part in parts:
            escaped = escape(part, quote=False)
            if (
                rendered
                and len((rendered + escaped).encode("utf-16-le")) // 2 + 7 > 3800
            ):
                pages.append((plain, rendered))
                plain, rendered = "", ""
            if not rendered:
                rendered = (
                    "<b>"
                    + escaped.rstrip("\n")
                    + "</b>"
                    + ("\n" if part.endswith("\n") else "")
                )
            else:
                rendered += escaped
            plain += part
    if plain:
        pages.append((plain, rendered))
    return pages or [(" ", " ")]


class PlainHTML(HTMLParser):
    """Recover plain text from plugin-owned markup for an explicit parse failure."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


async def send_html(call, text, **kwargs):
    """Send trusted escaped markup; only explicit entity rejection permits fallback.

    Args:
        call: Telegram send or edit coroutine.
        text: Plugin-generated, escaped HTML.
        **kwargs: Destination, buttons and timeout arguments.

    Returns:
        Telegram result; uncertain transport errors propagate without a second send.
    """
    try:
        return await call(text=text, parse_mode="HTML", **kwargs)
    except BadRequest as exc:
        if not any(
            term in str(exc).lower()
            for term in (
                "parse entities",
                "unsupported start tag",
                "can't find end tag",
            )
        ):
            raise
        parser = PlainHTML()
        parser.feed(text)
        return await call(text="".join(parser.parts), parse_mode=None, **kwargs)
