"""Native execution evidence is separate from complete business acceptance."""

import json
from pathlib import Path


def load_capabilities():
    """Read the trusted, packaged capability matrix.

    Returns:
        The versioned native and business evidence declarations.
    """
    return json.loads(Path(__file__).with_suffix(".json").read_text())


def execution_verified(feature):
    """Respect explicit execution gates without claiming full workflow acceptance.

    Args:
        feature: One packaged capability declaration.

    Returns:
        Whether native execution evidence permits the configured action.
    """
    if "execution_verified" in feature:
        return feature["execution_verified"] is True
    return feature.get("live_acceptance") is True
