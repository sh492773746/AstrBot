"""Bounded Telegram link resolution and channel-origin classification."""

import asyncio
import html
import re
import unicodedata
from urllib.parse import parse_qs, unquote, urlsplit

TG_HOSTS = {"t.me", "telegram.me", "telegram.dog"}
LINKS = re.compile(
    r"(?:https?://|tg://)[^\s<>\"，。！？；]+|(?<![\w@])(?:[\w-]+\.)+[a-z]{2,}(?::\d{2,5})?(?:/[^\s<>\"，。！？；]*)?",
    re.I,
)


async def classify(message, chat, config, bot, cache, now):
    """Classify only verified public targets; never follow arbitrary web URLs.

    Args:
        message: Telegram message including caption entities and forward origin.
        chat: Receiving supergroup.
        config: Confirmed group policy.
        bot: Existing Telegram bot client.
        cache: Bounded target-resolution cache owned by this plugin instance.
        now: Current timestamp.

    Returns:
        Detection flags for links, platform links, Telegram links and forwarding.
    """
    from .ad_killer import normalize

    result = dict.fromkeys(
        ("links", "telegram", "group_links", "platform_links", "channel_forward"), False
    )
    origin = getattr(message, "forward_origin", None)
    source = getattr(origin, "chat", None)
    allowed = config["telegram_allow"]
    own_names = {
        str(name).lower()
        for name in (
            getattr(chat, "username", None),
            *(getattr(chat, "active_usernames", None) or ()),
        )
        if name
    }
    source_allowed = source is not None and (
        str(source.id) == str(chat.id)
        or str(source.id) in allowed
        or (
            getattr(source, "username", None)
            and "@" + source.username.lower() in allowed
        )
    )
    result["channel_forward"] = bool(
        origin
        and origin.type == "channel"
        and not source_allowed
        and not getattr(message, "is_automatic_forward", False)
    )
    text = normalize(
        getattr(message, "text", None) or getattr(message, "caption", None) or ""
    )
    entities = tuple(getattr(message, "entities", None) or ()) + tuple(
        getattr(message, "caption_entities", None) or ()
    )
    # Preserve case-sensitive invite hashes while normalizing visible text.
    raw_text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
    inputs = [raw_text] + [
        e.url
        for e in entities
        if getattr(e, "type", "") == "text_link" and getattr(e, "url", None)
    ]
    normalized = unicodedata.normalize("NFKC", html.unescape(" ".join(inputs)))
    normalized = normalized.translate(
        dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"))
    )
    candidates = list(dict.fromkeys(LINKS.findall(normalized)))
    if config["rules"]["telegram"]["enabled"]:
        from .ad_killer import CALL_TO_ACTION, HANDLE_RE

        result["telegram"] = bool(
            any(
                match.group().lower() not in allowed
                and match.group()[1:].lower() not in own_names
                for match in HANDLE_RE.finditer(text)
            )
            and any(word in text for word in CALL_TO_ACTION)
        )
    resolutions = 0
    for raw in candidates:
        raw = raw.rstrip(").,，。")
        try:
            url = urlsplit(raw if "://" in raw else "https://" + raw)
            host = (url.hostname or "").lower()
            port = url.port
        except ValueError:
            continue
        is_tg = url.scheme == "tg" or host in TG_HOSTS or host.endswith(".t.me")
        if url.scheme != "tg" and any(
            host == domain or host.endswith("." + domain)
            for domain in config["domains"]
        ):
            continue
        if not is_tg:
            result["links"] = result["platform_links"] = True
            continue
        path = unquote(url.path).strip("/")
        if url.scheme == "tg":
            query = parse_qs(url.query)
            path = (
                "+" + query.get("invite", [""])[0]
                if host == "join"
                else query.get("domain", [""])[0]
                if host == "resolve"
                else ""
            )
        if host.endswith(".t.me"):
            path = host.removesuffix(".t.me") + ("/" + path if path else "")
        if path.startswith("s/"):
            path = path[2:]
        first = path.split("/", 1)[0]
        canonical = "https://t.me/" + path
        own_invite = getattr(chat, "invite_link", None)
        if (
            canonical in allowed
            or raw == own_invite
            or first.lower() in own_names
            or "@" + first.lower() in allowed
        ):
            continue
        if first == "c":
            parts = path.split("/")
            peer = "-100" + parts[1] if len(parts) > 1 and parts[1].isdigit() else ""
            if peer and (peer == str(chat.id) or peer in allowed):
                continue
            result["group_links"] |= bool(peer)
        elif first.startswith("+") or first == "joinchat":
            result["group_links"] = len(path) > len(first) or len(first) > 1
        elif (
            (
                config["rules"]["group_links"]["enabled"]
                or any(value.startswith("-") for value in allowed)
            )
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", first)
            and first.lower()
            not in {"share", "addstickers", "addemoji", "proxy", "socks", "login"}
            and port in (None, 443, 80)
        ):
            key = first.lower()
            entry = cache.get(key)
            if (not entry or entry[0] <= now) and resolutions < 3:
                resolutions += 1
                try:
                    target = await asyncio.wait_for(bot.get_chat("@" + first), 3)
                    entry = (now + 300, str(target.id), target.type)
                except Exception:
                    entry = (now + 30, "", "")
                if len(cache) >= 512:
                    cache.clear()
                cache[key] = entry
            if entry and entry[0] > now:
                if entry[1] == str(chat.id) or entry[1] in allowed:
                    continue
                result["group_links"] |= entry[2] in {"group", "supergroup", "channel"}
        result["links"] = result["telegram"] = True
    return result
