"""Fixed module registration contract without dynamic code uploads."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Module:
    key: str
    label: str
    scope: str
    interval: float
    worker: str


MODULES = (
    Module("ads", "📢 广告发布", "ads", 5, "ads"),
    Module("game", "🎮 玩法中心", "game", 10, "game"),
    Module("points", "🎁 积分获取", "points", 0, ""),
    Module("moderation", "群管理", "moderation", 5, "moderation"),
    Module("payments", "广告充值", "ads", 60, "payments"),
)
