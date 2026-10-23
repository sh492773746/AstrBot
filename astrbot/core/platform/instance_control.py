"""Instance-scoped serialization and optimistic Dashboard configuration versions."""

import asyncio
import hashlib
import json
from weakref import WeakValueDictionary

_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


def instance_lock(instance_id: str) -> asyncio.Lock:
    """Return the shared lock for an instance.

    Args:
        instance_id: Stable platform instance ID.

    Returns:
        Lock retained while an operation is running or waiting.
    """
    return _locks.setdefault(instance_id, asyncio.Lock())


def instance_revision(config: dict) -> str:
    """Version persistent fields without including the version itself.

    Args:
        config: Persistent or public instance configuration.

    Returns:
        Opaque SHA-256 comparison value.
    """
    values = {
        key: value for key, value in config.items() if key != "_instance_revision"
    }
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, ensure_ascii=True).encode()
    ).hexdigest()
