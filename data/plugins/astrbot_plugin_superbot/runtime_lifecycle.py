"""Recover only unregistered, partially initialized runtimes during hot reload."""

import asyncio
from pathlib import Path

from astrbot.core.star.star import star_registry

from .store import Rejected


async def recover_failed_startup(runtime):
    """Drain an orphaned startup before attempting the real file lock again.

    Args:
        runtime: Replacement plugin instance whose lock acquisition failed.

    Raises:
        Rejected: The lock holder is live, external or cannot be uniquely identified.
    """
    path = Path(runtime.instance_lock.lock_file).resolve()
    candidates = {}
    for task in asyncio.all_tasks():
        frame = getattr(task.get_coro(), "cr_frame", None)
        candidate = frame.f_locals.get("self") if frame is not None else None
        if candidate is None or candidate is runtime:
            continue
        if (type(candidate).__module__, type(candidate).__qualname__) != (
            type(runtime).__module__,
            type(runtime).__qualname__,
        ):
            continue
        lock = getattr(candidate, "instance_lock", None)
        if (
            getattr(candidate, "context", None) is not runtime.context
            or task not in getattr(candidate, "tasks", ())
            or getattr(candidate, "application", None) is not None
            or getattr(candidate, "platform", None) is not None
            or not lock
            or not lock.is_locked
            or Path(lock.lock_file).resolve() != path
            or any(metadata.star_cls is candidate for metadata in star_registry)
        ):
            continue
        candidates[id(candidate)] = candidate
    if len(candidates) != 1:
        raise Rejected(
            "运行锁仍被其他实例持有，未找到可安全回收的失败启动实例；未强制释放。"
        )
    candidate = next(iter(candidates.values()))
    await type(runtime).terminate(candidate)
    if candidate.instance_lock.is_locked:
        raise Rejected("失败实例清理后仍持有运行锁，已停止加载。")
    runtime.logger.warning(
        "Recovered an orphaned Superbot startup after draining its tasks"
    )
