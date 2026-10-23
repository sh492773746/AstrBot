"""Optional instance policy, shared by ingress and transport send boundaries."""

from collections.abc import Mapping


class MessagePolicyDenied(PermissionError):
    """The instance policy rejected a message before transport submission."""


def validate_message_policy(config: dict) -> None:
    """Validate the fixed-allowlist contract before saving a LajiaoLiao bot.

    Args:
        config: Bot configuration being created or updated.

    Raises:
        ValueError: If the policy would allow broad or malformed access.
    """
    if config.get("type") != "lajiaoliao":
        return
    policy = config.get("message_policy")
    if policy is None:
        config["message_policy"] = {
            "group_mode": "allowlist",
            "group_ids": [],
            "private_enabled": False,
        }
        return
    if not isinstance(policy, dict):
        raise ValueError("invalid_message_policy")
    policy = dict(policy)
    if policy.get("group_mode") == "deny_all":
        policy.update(group_mode="allowlist", group_ids=[])
    if policy.get("group_mode") != "allowlist":
        raise ValueError("lajiaoliao_allowlist_required")
    groups = policy.get("group_ids", [])
    if not isinstance(groups, list) or any(
        not isinstance(group, str)
        or not group.strip()
        or group != group.strip()
        or len(group) > 128
        for group in groups
    ):
        raise ValueError("invalid_group_ids")
    if type(policy.get("private_enabled", False)) is not bool:
        raise ValueError("invalid_private_policy")
    policy.update(
        group_ids=list(dict.fromkeys(groups)),
        private_enabled=policy.get("private_enabled", False),
    )
    config["message_policy"] = policy


def allows_message(config: Mapping, kind: str, target: str) -> bool:
    policy = config.get("message_policy")
    fixed_allowlist = config.get("type") == "lajiaoliao"
    if policy is None:
        return not fixed_allowlist
    if not isinstance(policy, Mapping):
        return False
    if kind == "private":
        return policy.get("private_enabled") is True
    if kind != "group":
        return False
    mode = policy.get("group_mode")
    if fixed_allowlist and mode != "allowlist":
        return False
    if mode == "all":
        return True
    groups = policy.get("group_ids", [])
    return (
        mode == "allowlist"
        and isinstance(groups, list)
        and str(target) in {str(group) for group in groups}
    )


def require_message(config: Mapping, kind: str, target: str) -> None:
    if not allows_message(config, kind, target):
        raise MessagePolicyDenied("message_policy_denied")


def migrate_legacy_policy(config: Mapping) -> dict:
    """Preserve the intersection of legacy group allowlist and reply switches."""
    groups = config.get("enabled_groups", [])
    overrides = config.get("reply_groups", {})
    if not isinstance(groups, list) or not isinstance(overrides, Mapping):
        raise ValueError("invalid_legacy_message_policy")
    allowed = sorted(
        {str(group) for group in groups if bool(overrides.get(str(group), True))}
    )
    return {
        "group_mode": "allowlist" if allowed else "deny_all",
        "group_ids": allowed,
        "private_enabled": bool(config.get("reply_private", True)),
    }
