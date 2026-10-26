import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "acceptance_routes",
    Path(__file__).resolve().parents[1] / "services/lajiaoliao-bridge/acceptance_routes.py",
)
routes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(routes)


def test_exact_routes_precede_instance_wildcard_and_restore_order():
    before = {"telegram::": "tg", "lajiao::": "real", "::": "fallback"}
    exact = ["lajiao:FriendMessage:private:peer"]
    during = routes.install_routes(before, exact, "test")
    assert list(during) == [*exact, *before]
    assert list(routes.restore_routes(before, during, exact, "test").items()) == list(before.items())
    with pytest.raises(RuntimeError, match="externally"):
        routes.restore_routes(before, {**during, "other::": "new"}, exact, "test")


def test_existing_exact_route_value_and_order_restored_and_external_changes_rejected():
    exact = ["lajiao:FriendMessage:private:peer"]
    before = {"lajiao::": "real", exact[0]: "old", "telegram::": "tg"}
    during = routes.install_routes(before, exact, "test")
    assert len(during) == len(before)
    assert list(routes.restore_routes(before, during, exact, "test").items()) == list(before.items())
    with pytest.raises(RuntimeError):
        routes.restore_routes(before, dict(reversed(list(during.items()))), exact, "test")
