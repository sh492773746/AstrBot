"""Central policy for group-visible bot message retention."""

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class RetentionPolicy:
    """Versioned defaults shared by every game and query."""

    feedback: int = 10
    query: int = 10
    history: int = 60
    canada_settlement: int = 30
    k3_settlement: int = 60
    game_result: int = 60
    panel_idle: int = 30
    admin: int = 30
    help: int = 60
    animation: int = 60
    activity_result: int = 86400

    @classmethod
    def from_mapping(cls, raw: Mapping | None) -> "RetentionPolicy":
        """Load only known platform fields; invalid values fall back safely."""
        if not isinstance(raw, Mapping):
            return cls()
        values = {}
        for field in cls.__dataclass_fields__:
            value = raw.get(field)
            if isinstance(value, bool):
                continue
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
            values[field] = max(1, min(value, 24 * 60 * 60))
        return cls(**values)

    def seconds(self, category: str) -> int:
        aliases = {
            "accepted": "feedback",
            "rejected": "feedback",
            "points": "query",
            "checkin": "query",
            "flow": "query",
            "profit": "query",
            "settlement": "canada_settlement",
            "duel": "feedback",
            "duel_result": "game_result",
            "panel": "panel_idle",
            "result": "game_result",
        }
        value = getattr(self, aliases.get(category, category), self.feedback)
        return max(1, min(int(value), 24 * 60 * 60))


DEFAULT_RETENTION = RetentionPolicy()


def retention(runtime, category: str) -> int:
    """Resolve the platform policy; group operators cannot override it."""
    return (getattr(runtime, "retention_policy", None) or DEFAULT_RETENTION).seconds(
        category
    )
