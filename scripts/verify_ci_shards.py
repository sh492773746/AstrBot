"""Reject incomplete or overlapping shard evidence before combining coverage."""

import json
import sys
from pathlib import Path


def verify(root: Path) -> int:
    """Validate all shards cover the full collected suite exactly once.

    Args:
        root: Downloaded artifact directory containing collection manifests.

    Returns:
        The complete collected test count.

    Raises:
        ValueError: A shard is missing, overlapping, empty or inconsistent.
    """
    manifests = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(root.rglob("collection-*.json"))
    ]
    if not manifests:
        raise ValueError("Missing CI collection evidence")
    count = manifests[0]["count"]
    total = manifests[0]["total_collected"]
    if len(manifests) != count or {manifest["index"] for manifest in manifests} != set(
        range(count)
    ):
        raise ValueError("Missing or duplicate CI shards")
    selected = []
    for manifest in manifests:
        if (
            manifest["count"] != count
            or manifest["total_collected"] != total
            or not manifest["selected"]
        ):
            raise ValueError("Inconsistent or empty CI collection")
        selected.extend(manifest["selected"])
    if len(selected) != total or len(set(selected)) != total:
        raise ValueError("Incomplete or overlapping CI collection")
    return total


if __name__ == "__main__":
    print(f"Verified {verify(Path(sys.argv[1]))} collected cases across all shards")
