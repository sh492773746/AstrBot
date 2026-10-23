"""Human-readable group identity for private administration pages."""


def group_label(info, fallback=""):
    """Display a group name and its public handle when one exists.

    Args:
        info: Telegram chat object, or None when it cannot be fetched.
        fallback: Previously verified group title.

    Returns:
        A readable label; never use the public handle as a stable identifier.
    """
    title = getattr(info, "title", None) or fallback
    username = getattr(info, "username", None)
    if username:
        return f"{title} (@{username})"
    if info and getattr(info, "type", None) in {"supergroup", "group", "channel"}:
        return f"{title} (私密群)" if title else "私密群"
    return title or "目标群"
