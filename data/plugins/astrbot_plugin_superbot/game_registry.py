"""Small registry for group game entry points.

Game implementations remain responsible for their own state and settlement.
The registry only owns public aliases and dispatch metadata so the update
router does not grow another list of string comparisons for every game.
"""

from dataclasses import dataclass

# Shared by settings navigation, local switches and daily entertainment windows.
ENTERTAINMENT = {
    "canada": "加拿大28",
    "k3": "积分快三",
    "wheel": "积分转盘",
    "slots": "老虎机PvP",
    "mines": "扫雷接龙",
    "duel": "双人对赌",
}
LOCAL_SWITCHES = {**ENTERTAINMENT, "chat": "AI客服", "avatar": "制图"}


@dataclass(frozen=True)
class GameSpec:
    key: str
    aliases: frozenset[str]
    attribute: str


DEFAULT_GAMES = (
    GameSpec("mines", frozenset({"扫雷", "扫雷接龙", "💣 扫雷接龙"}), "mines"),
    GameSpec(
        "slots",
        frozenset({"老虎机", "老虎机PvP", "🎰老虎机PvP", "🎰 老虎機PvP"}),
        "slots",
    ),
    GameSpec("wheel", frozenset({"转盘", "积分转盘"}), "wheel"),
)


class GameRegistry:
    """Resolve an entry alias without inspecting or mutating game state."""

    def __init__(self, specs=DEFAULT_GAMES):
        self.specs = tuple(specs)
        self._aliases = {
            alias.casefold(): spec for spec in self.specs for alias in spec.aliases
        }

    def match(self, text: str, command: str = ""):
        value = (text or "").strip()
        if command == "/wheel":
            return self._aliases.get("转盘")
        return self._aliases.get(value.casefold())
