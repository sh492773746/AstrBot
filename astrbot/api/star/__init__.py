from astrbot.core.star import Context, Star, StarTools
from astrbot.core.star.component_runtime import ExecutionContext, component_runtime
from astrbot.core.star.components import (
    CapabilitySpec,
    CommandSpec,
    PageSpec,
    components,
)
from astrbot.core.star.config import *
from astrbot.core.star.register import (
    register_star as register,  # 注册插件（Star）
)

__all__ = [
    "Context",
    "Star",
    "StarTools",
    "register",
    "CommandSpec",
    "CapabilitySpec",
    "PageSpec",
    "components",
    "ExecutionContext",
    "component_runtime",
]
