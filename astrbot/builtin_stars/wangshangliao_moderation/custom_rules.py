"""Bounded literal combinations shared by tenant validation and matching."""

import unicodedata


def normalize(text):
    """Normalize literal matching without compiling user expressions.

    Args:
        text: Plain message text or a configured literal term.

    Returns:
        Case-folded text without whitespace or invisible format characters.
    """
    return "".join(
        c
        for c in unicodedata.normalize("NFKC", text).casefold()
        if unicodedata.category(c) != "Cf" and not c.isspace() and c != "\ufe0f"
    )


def validate(rules):
    """Validate optional per-group combinations without changing legacy data.

    Args:
        rules: At most twenty named, independently enabled literal combinations.

    Raises:
        ValueError: If names, terms, types or size limits are invalid.
    """
    if not isinstance(rules, list) or len(rules) > 20:
        raise ValueError("custom_rules_invalid")
    names = set()
    for rule in rules:
        if (
            not isinstance(rule, dict)
            or set(rule) != {"name", "terms", "enabled"}
            or type(rule["enabled"]) is not bool
            or not isinstance(rule["name"], str)
            or not 1 <= len(rule["name"]) <= 24
            or not normalize(rule["name"])
            or normalize(rule["name"]) in names
            or not isinstance(rule["terms"], list)
            or not 2 <= len(rule["terms"]) <= 5
        ):
            raise ValueError("custom_rules_invalid")
        names.add(normalize(rule["name"]))
        terms = set()
        for text in [rule["name"], *rule["terms"]]:
            if not isinstance(text, str) or any(
                unicodedata.category(c) in {"Cc", "Zl", "Zp"} for c in text
            ):
                raise ValueError("custom_rules_invalid")
        for term in rule["terms"]:
            normalized = normalize(term)
            if (
                not 2 <= len(term) <= 40
                or len(normalized) < 2
                or not any(c.isalnum() for c in normalized)
                or normalized in terms
            ):
                raise ValueError("custom_rules_invalid")
            terms.add(normalized)
        if any(a != b and a in b for a in terms for b in terms):
            raise ValueError("custom_rules_redundant")


def parse(text):
    """Parse a slash- or pipe-separated command into one enabled rule.

    Args:
        text: Rule name followed by two to five literal terms. A pipe takes
            precedence so slash-containing terms, such as URLs, remain literal.

    Returns:
        A validated new rule; callers preserve an existing rule's enabled state.

    Raises:
        ValueError: If the command does not contain a valid bounded combination.
    """
    separator = "|" if "|" in text else "/"
    name, *terms = (part.strip() for part in text.split(separator))
    rule = {"name": name, "terms": terms, "enabled": True}
    validate([rule])
    return rule
