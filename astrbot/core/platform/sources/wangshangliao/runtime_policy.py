"""Plugin-owned policy snapshots consumed by the native transport."""

import copy
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BUSINESS_FIELDS = frozenset(
    {
        "enabled_groups",
        "reply_private",
        "reply_groups",
        "ai_routes",
        "proactive_send",
        "moderation",
        "developer_test",
    }
)


@dataclass(frozen=True)
class PolicySnapshot:
    """Keep an immutable serialized policy and its account-bound revision."""

    account: str
    revision: str
    payload: str
    effective_at: float

    def values(self) -> dict:
        """Return a detached policy suitable for existing dict consumers."""
        return json.loads(self.payload)


class PolicyRuntime:
    """Bridge transport and plugin without importing plugin implementation."""

    def __init__(self):
        self.service: Any = None
        self.classifier = None
        self.observer = None
        self.help_responder = None
        self.snapshots: dict[str, PolicySnapshot] = {}
        self.generation = 0
        self.observation_groups: dict[tuple[str, str], tuple[str, ...]] = {}
        self.open_service = None

    def restore_observation(self, config: dict, path: Path) -> None:
        """Read only group IDs from the committed store without enabling business.

        Args:
            config: Transport identity whose observation scope is being restored.
            path: Plugin-owned policy database, opened read-only and never created.
        """
        identity = (str(config.get("id", "")), str(config.get("account_id", "")))
        self.observation_groups.pop(identity, None)
        if not path.is_file():
            return
        try:
            with closing(
                sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
            ) as db:
                row = db.execute(
                    "SELECT payload FROM policies WHERE instance=? AND account=?",
                    identity,
                ).fetchone()
            if not row:
                return
            groups = json.loads(row[0]).get("enabled_groups", [])
            if (
                not isinstance(groups, list)
                or len(groups) > 100
                or any(
                    not isinstance(g, str)
                    or not g.isascii()
                    or not g.isdigit()
                    or not 0 < int(g) < 1 << 30
                    for g in groups
                )
            ):
                return
            self.observation_groups[identity] = tuple(groups)
        except (sqlite3.Error, ValueError, TypeError, AttributeError):
            from astrbot.api import logger

            logger.warning(
                "Wangshangliao observation scope unavailable for %s", identity[0]
            )

    def publish(self, instance: str, snapshot: PolicySnapshot) -> None:
        """Publish one committed, immutable snapshot.

        Args:
            instance: Native bot instance ID.
            snapshot: Account-bound committed policy.
        """
        self.snapshots[instance] = snapshot

    def snapshot(self, config: dict) -> PolicySnapshot | None:
        """Resolve a snapshot only for its original authenticated account."""
        item = self.snapshots.get(str(config.get("id", "")))
        return (
            item if item and item.account == str(config.get("account_id", "")) else None
        )

    def view(self, config: dict, *, public: bool = False) -> dict:
        """Merge transport settings with a detached policy, failing closed.

        Args:
            config: Transport configuration, never modified.
            public: Show saved values even when execution is suspended.

        Returns:
            Effective or public configuration without changing either store.
        """
        result = copy.deepcopy(config)
        item = self.snapshot(config)
        if item:
            for field in BUSINESS_FIELDS:
                result.pop(field, None)
            result.update(item.values())
            result["_business_revision"] = item.revision
        elif "enabled_groups" not in result:
            result["enabled_groups"] = list(
                self.observation_groups.get(
                    (str(config.get("id", "")), str(config.get("account_id", ""))), ()
                )
            )
        active = bool(self.service and self.service.active and item)
        if (
            self.open_service is not None
            and getattr(self.open_service, "integrated", False)
            and item
        ):
            result = self.open_service.policy_values(item.account, result)
        if (
            self.open_service is not None
            and (not self.open_service.active or self.open_service.settings["paused"])
            and not getattr(self.open_service, "integrated", False)
        ):
            active = False
        result["_business_active"] = active
        result["_business_epoch"] = self.generation
        if not public and not active:
            result.update(
                reply_private=False,
                reply_groups=dict.fromkeys(result.get("enabled_groups", []), False),
                proactive_send={"enabled": False, "targets": []},
                moderation={"enabled": False},
                ai_routes={},
                developer_test={},
            )
        return result

    def recognize(self, text: str):
        """Classify commands only through the active plugin's registered hook."""
        if self.service and self.service.active and self.classifier:
            return self.classifier(text)
        return None

    def observe(self, config, account, group, message, payload, received_at):
        """Call the active business observer for a newly committed envelope.

        Args:
            config: Native transport configuration.
            account: Authenticated account.
            group: Business group ID.
            message: Stable upstream message ID.
            payload: Detached, trusted adapter envelope.
            received_at: Local ingress timestamp.
        """
        snapshot = self.snapshot(config)
        if (
            self.service
            and self.service.active
            and snapshot
            and snapshot.account == account
            and self.observer
        ):
            if self.open_service is not None and getattr(
                self.open_service, "integrated", False
            ):
                snapshot = PolicySnapshot(
                    snapshot.account,
                    snapshot.revision,
                    json.dumps(
                        self.open_service.policy_values(account, snapshot.values())
                    ),
                    snapshot.effective_at,
                )
            self.observer(snapshot, account, group, message, payload, received_at)

    def detach(self, service) -> None:
        """Revoke execution while retaining read-only observation snapshots."""
        if self.service is service:
            self.service = None
            self.classifier = None
            self.observer = None
            self.help_responder = None
            self.generation += 1


runtime = PolicyRuntime()
