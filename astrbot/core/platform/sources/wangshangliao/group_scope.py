"""Detached per-group views without mutating a shared transport adapter."""


def group_config(config, group):
    """Resolve public tenant settings while leaving legacy configurations unchanged."""
    tenants = config.get("_open_groups")
    if tenants is None:
        return config
    rules = tenants.get(group)
    if rules is None:
        return {**config, "moderation": {"enabled": False}}
    policy = {
        **config.get("moderation", {}),
        "enabled": True,
        "automation_enabled": rules["advertising"] or rules["flood"],
        "advertising_enabled": rules["advertising"],
        "flood_enabled": rules["flood"],
        "mute_keywords": list(rules["keywords"]),
        "kick_keywords": [],
        "semantic": {"enabled": False},
        "progressive_mute": True,
        "recall_enabled": True,
        "manual_kick_only": True,
    }
    if config.get("_open_native_roles", {}).get(group) == "GROUP_ROLE_MEMBER":
        policy.update(
            automation_enabled=False,
            recall_enabled=False,
            progressive_mute=False,
        )
    return {**config, "moderation": policy}


def group_revision(config, group):
    """Keep unrelated public tenant changes out of an active flood window."""
    return config.get("_open_revisions", {}).get(
        group, config.get("_business_revision")
    )


class GroupAdapter:
    """Delegate transport operations while resolving current tenant rules on access."""

    def __init__(self, adapter, group):
        object.__setattr__(self, "adapter", adapter)
        object.__setattr__(self, "group", group)

    @property
    def config(self):
        return group_config(self.adapter.config, self.group)

    def __getattr__(self, name):
        return getattr(self.adapter, name)

    def __setattr__(self, name, value):
        setattr(self.adapter, name, value)


def scoped(adapter, group):
    """Keep adapter identity unchanged for all existing production instances."""
    if isinstance(adapter, GroupAdapter):
        adapter = adapter.adapter
    return GroupAdapter(adapter, group) if "_open_groups" in adapter.config else adapter
