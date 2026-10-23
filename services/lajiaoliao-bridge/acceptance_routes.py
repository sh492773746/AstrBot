"""Temporary exact test routes without changing other conversations' priority."""


def install_routes(original, exact, profile_id):
    return {**dict.fromkeys(exact, profile_id),
            **{key: value for key, value in original.items() if key not in exact}}


def restore_routes(original, current, exact, profile_id):
    expected = install_routes(original, exact, profile_id)
    if list(current.items()) != list(expected.items()):
        raise RuntimeError("test_routes_changed_externally")
    return dict(original)


class ScopedTestRoutes:
    def __init__(self, api, exact, original):
        self.api, self.exact, self.original = api, exact, original
        self.profile_id = None

    def install(self, profile_id):
        current = self.api("/api/v1/config-routes")["routing"]
        if list(current.items()) != list(self.original.items()):
            raise RuntimeError("test_routes_changed_before_install")
        self.profile_id = profile_id
        expected = install_routes(self.original, self.exact, profile_id)
        self.api("/api/v1/config-routes", {"routing": expected}, method="PUT")
        if list(self.api("/api/v1/config-routes")["routing"].items()) != list(expected.items()):
            raise RuntimeError("test_routes_not_installed")

    def restore(self):
        current = self.api("/api/v1/config-routes")["routing"]
        if list(current.items()) == list(self.original.items()):
            return
        if not self.profile_id:
            raise RuntimeError("test_routes_changed_without_install")
        expected = restore_routes(self.original, current, self.exact, self.profile_id)
        self.api("/api/v1/config-routes", {"routing": expected}, method="PUT")
        if list(self.api("/api/v1/config-routes")["routing"].items()) != list(expected.items()):
            raise RuntimeError("test_routes_not_restored")
