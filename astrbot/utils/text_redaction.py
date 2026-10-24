"""Dependency-free text redaction shared by transport and standalone services."""

import re

_REASONING_TAGS = ("think", "thinking", "thought", "analysis")


def strip_reasoning_markup(text: str, *, strip: bool = True) -> str:
    """Remove paired and truncated model reasoning tags.

    Args:
        text: Potentially model-generated text.
        strip: Whether to remove surrounding whitespace.

    Returns:
        Visible text without reasoning blocks.
    """
    for tag in _REASONING_TAGS:
        paired = re.compile(
            rf"<{tag}\b[^>]*>.*?</{tag}\s*>",
            flags=re.IGNORECASE | re.DOTALL,
        )
        text = paired.sub("", text)
        text = re.sub(
            rf"^\s*<{tag}\b[^>]*>.*$",
            "",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        text = re.sub(rf"</{tag}\s*>", "", text, flags=re.IGNORECASE)
    return text.strip() if strip else text


def redact_reply(text: str, *, preserve_whitespace: bool = False) -> str:
    """Mask recognizable secrets and contact details.

    Args:
        text: Outgoing or audit text, not persistent configuration.
        preserve_whitespace: Preserve surrounding whitespace when requested.

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
    return re.sub(
        r"(?<![\w.+-])([A-Za-z0-9])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})(?![\w])",
        r"\1***@\2",
        text,
    )
