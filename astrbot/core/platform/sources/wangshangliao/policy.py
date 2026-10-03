"""Per-bot, per-group moderation capability policy."""

from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

ACTIONS = {
    "mute",
    "unmute",
    "announce",
    "mute_all",
    "unmute_all",
    "kick",
    "recall",
    "rename",
    "cleanup",
}


def validate_policy(policy: dict) -> None:
    """Validate per-bot group capabilities and automation settings.

    Args:
        policy: Per-instance moderation policy.

    Raises:
        ValueError: If policy fields are invalid.
    """
    if not isinstance(policy, dict) or set(policy) - {
        "enabled",
        "permissions",
        "automation_enabled",
        "keywords",
        "mute_keywords",
        "kick_keywords",
        "recall_enabled",
        "cooldown_seconds",
        "card_auto",
        "content_rules_since",
        "semantic",
        "auto_kick",
        "progressive_mute",
        "manual_kick_only",
    }:
        raise ValueError("moderation_config")
    if (
        type(policy.get("enabled", False)) is not bool
        or type(policy.get("automation_enabled", False)) is not bool
        or type(policy.get("recall_enabled", False)) is not bool
        or type(policy.get("progressive_mute", False)) is not bool
        or type(policy.get("manual_kick_only", False)) is not bool
    ):
        raise ValueError("moderation_config")
    if policy.get("progressive_mute") and not policy.get("recall_enabled"):
        raise ValueError("progressive_mute_requires_recall")
    semantic = policy.get("semantic", {})
    if (
        not isinstance(semantic, dict)
        or set(semantic)
        - {"enabled", "provider_id", "timeout_seconds", "context_limit"}
        or type(semantic.get("enabled", False)) is not bool
        or not isinstance(semantic.get("provider_id", ""), str)
        or len(semantic.get("provider_id", "")) > 256
        or type(semantic.get("timeout_seconds", 12)) is not int
        or not 1 <= semantic.get("timeout_seconds", 12) <= 30
        or type(semantic.get("context_limit", 20)) is not int
        or not 1 <= semantic.get("context_limit", 20) <= 50
    ):
        raise ValueError("moderation_semantic")
    auto_kick = policy.get("auto_kick", {})
    if (
        not isinstance(auto_kick, dict)
        or len(auto_kick) > 100
        or any(
            not isinstance(g, str)
            or not g.isascii()
            or not g.isdigit()
            or int(g) <= 0
            or type(enabled) is not bool
            for g, enabled in auto_kick.items()
        )
    ):
        raise ValueError("moderation_auto_kick")
    if policy.get("manual_kick_only") and any(auto_kick.values()):
        raise ValueError("automatic_kick_disabled")
    permissions = policy.get("permissions", {})
    since = policy.get("content_rules_since", 0)
    if type(since) not in {int, float} or not 0 <= since < 4102444800:
        raise ValueError("moderation_content_rules")
    if not isinstance(permissions, dict) or len(permissions) > 100:
        raise ValueError("moderation_permissions")
    for group, actions in permissions.items():
        if (
            not isinstance(group, str)
            or not group.isascii()
            or not group.isdigit()
            or int(group) <= 0
            or not isinstance(actions, list)
            or any(
                not isinstance(action, str) or action not in ACTIONS
                for action in actions
            )
            or len(set(actions)) != len(actions)
        ):
            raise ValueError("moderation_permissions")
    card_auto = policy.get("card_auto", {})
    if (
        not isinstance(card_auto, dict)
        or len(card_auto) > 100
        or any(
            not isinstance(g, str)
            or not g.isascii()
            or not g.isdigit()
            or int(g) <= 0
            or type(enabled) is not bool
            for g, enabled in card_auto.items()
        )
    ):
        raise ValueError("card_auto_config")
    keywords = policy.get("keywords", [])
    for field in ("mute_keywords", "kick_keywords"):
        words = policy.get(field, [])
        if (
            not isinstance(words, list)
            or len(words) > 50
            or any(
                not isinstance(word, str) or not word.strip() or len(word) > 100
                for word in words
            )
        ):
            raise ValueError("moderation_keywords")
    if (
        not isinstance(keywords, list)
        or len(keywords) > 50
        or any(
            not isinstance(word, str) or not word.strip() or len(word) > 100
            for word in keywords
        )
    ):
        raise ValueError("moderation_keywords")
    cooldown = policy.get("cooldown_seconds", 60)
    if type(cooldown) is not int or not 10 <= cooldown <= 86400:
        raise ValueError("moderation_cooldown")


def authorize_action(config: dict, group: str, action: str) -> None:
    """Enforce the same capability grant for every execution entry point.

    Args:
        config: Saved bot configuration.
        group: Business group ID.
        action: Fixed moderation action.

    Raises:
        ProtocolError: If the bot, group or action is not explicitly enabled.
    """
    policy = config.get("moderation", {})
    permissions = policy.get("permissions", {}).get(group, [])
    if (
        not config.get("enable", True)
        or not policy.get("enabled")
        or group not in config.get("enabled_groups", [])
        or not isinstance(permissions, list)
        or action not in ACTIONS
        or action not in permissions
    ):
        raise ProtocolError("moderation_permission")
