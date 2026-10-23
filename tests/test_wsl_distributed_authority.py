"""Offline regressions for scoped authority and pause-revoked confirmations."""

import socket
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.command_menu import CommandMenu
from astrbot.builtin_stars.wangshangliao_moderation.open_store import (
    DEFAULT_RULES,
    OpenError,
    OpenStore,
)
from astrbot.core.platform.sources.wangshangliao.identity import ActorIdentity


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    """Reject network access during every synthetic authority test.

    Args:
        monkeypatch: Test-scoped environment and socket overrides.
    """
    monkeypatch.setenv("ASTRBOT_DISABLE_FILE_LOGGING", "1")
    monkeypatch.setenv("ASTRBOT_DISABLE_METRICS", "1")

    def reject_connection(*args, **kwargs):
        raise AssertionError("distributed_authority_network_forbidden")

    monkeypatch.setattr(socket.socket, "connect", reject_connection)
    monkeypatch.setattr(socket.socket, "connect_ex", reject_connection)


@pytest.fixture
def authority_store(tmp_path):
    """Create synthetic tenants only inside pytest's temporary directory.

    Args:
        tmp_path: Isolated database directory.

    Returns:
        Store containing two accounts and two groups per account.
    """
    store = OpenStore(tmp_path)
    for account in ("99", "199"):
        store.discover(
            account,
            {group: {"name": group, "team": "100" + group} for group in ("5", "6")},
        )
        for group in ("5", "6"):
            store.import_group(
                account, group, dict(DEFAULT_RULES), ["1", "3"], "offline:fixture"
            )
            row = store.tenant(account, group)
            store.native_role(account, group, row["epoch"], "GROUP_ROLE_ADMIN")
    return store


@pytest.mark.parametrize("session", ["private/1/peer", "group/5"])
@pytest.mark.parametrize("operation,target", [("add", "2"), ("remove", "3")])
def test_pause_resume_revokes_old_manager_confirmation(
    authority_store, session, operation, target
):
    store = authority_store
    row = store.tenant("99", "5")
    token = store.preview_manager(
        "99",
        "5",
        row["version"],
        "1",
        session,
        "preview",
        target,
        operation,
        "100" + target,
    )
    members = {"1": "1001", "2": "1002", "3": "1003"}
    settings = store.settings()
    store.configure(settings["version"], True, 20, 1000, "offline:pause")
    with pytest.raises(OpenError):
        store.confirm_manager(
            token,
            "1",
            session,
            "during-pause",
            "99",
            "5",
            members,
            eligible_managers={"2"},
        )
    store.configure(store.settings()["version"], False, 20, 1000, "offline:resume")

    # Reopening proves that revocation is durable, not just a menu/cache check.
    restored = OpenStore(store.path.parent)
    with pytest.raises(OpenError, match="confirmation_invalid"):
        restored.confirm_manager(
            token,
            "1",
            session,
            "after-resume",
            "99",
            "5",
            members,
            eligible_managers={"2"},
        )
    assert restored.tenant("99", "5") == row


def test_pause_revokes_manager_previews_across_accounts_and_groups(authority_store):
    store = authority_store
    previews = []
    for row in store.tenants():
        token = store.preview_manager(
            row["account"],
            row["group_id"],
            row["version"],
            "1",
            "private/1/peer",
            "preview",
            "2",
            "add",
            "1002",
        )
        previews.append((row, token))
    store.configure(store.settings()["version"], True, 20, 1000, "offline:pause")
    store.configure(store.settings()["version"], False, 20, 1000, "offline:resume")
    for row, token in previews:
        with pytest.raises(OpenError, match="confirmation_invalid"):
            store.confirm_manager(
                token,
                "1",
                "private/1/peer",
                "confirm",
                row["account"],
                row["group_id"],
                {"1": "1001", "2": "1002"},
                eligible_managers={"2"},
            )
        assert store.tenant(row["account"], row["group_id"]) == row


def test_limit_only_update_preserves_preview_and_fresh_post_resume_preview_works(
    authority_store,
):
    store = authority_store
    row = store.tenant("99", "5")
    token = store.preview_manager(
        "99",
        "5",
        row["version"],
        "1",
        "private/1/peer",
        "preview",
        "2",
        "add",
        "1002",
    )
    store.configure(store.settings()["version"], False, 20, 999, "offline:limits")
    store.confirm_manager(
        token,
        "1",
        "private/1/peer",
        "confirm",
        "99",
        "5",
        {"1": "1001", "2": "1002"},
        eligible_managers={"2"},
    )
    store.configure(store.settings()["version"], True, 20, 999, "offline:pause")
    store.configure(store.settings()["version"], False, 20, 999, "offline:resume")
    row = store.tenant("99", "5")
    fresh = store.preview_manager(
        "99",
        "5",
        row["version"],
        "1",
        "private/1/peer",
        "fresh-preview",
        "2",
        "remove",
        "1002",
    )
    store.confirm_manager(
        fresh,
        "1",
        "private/1/peer",
        "fresh-confirm",
        "99",
        "5",
        {"1": "1001", "2": "1002"},
    )
    assert store.tenant("99", "5")["managers"] == ["1", "3"]


def test_pause_preserves_independently_authorized_control_recovery(authority_store):
    store = authority_store
    row = store.tenant("99", "5")
    token = store.preview_control(
        "99",
        "5",
        row["version"],
        "offline:control",
        row["rules"],
        ["1", "2"],
        False,
    )
    store.configure(store.settings()["version"], True, 20, 1000, "offline:pause")
    store.change(
        "99",
        "5",
        row["version"],
        "offline:control",
        rules=row["rules"],
        managers=["1", "2"],
        paused=False,
        control=True,
        confirmation=token,
    )
    assert store.settings()["paused"]
    assert store.tenant("99", "5")["managers"] == ["1", "2"]


def test_failed_pause_rolls_back_settings_and_token_revocation(
    authority_store, monkeypatch
):
    store = authority_store
    row = store.tenant("99", "5")
    token = store.preview_manager(
        "99",
        "5",
        row["version"],
        "1",
        "private/1/peer",
        "preview",
        "2",
        "add",
        "1002",
    )
    settings = store.settings()

    def fail_audit(*args, **kwargs):
        raise RuntimeError("offline_audit_failure")

    with monkeypatch.context() as patch:
        patch.setattr(store, "log", fail_audit)
        with pytest.raises(RuntimeError, match="offline_audit_failure"):
            store.configure(settings["version"], True, 20, 1000, "offline:pause")
    assert store.settings() == settings
    store.confirm_manager(
        token,
        "1",
        "private/1/peer",
        "confirm",
        "99",
        "5",
        {"1": "1001", "2": "1002"},
        eligible_managers={"2"},
    )
    assert store.tenant("99", "5")["managers"] == ["1", "3", "2"]


@pytest.mark.parametrize(
    "change",
    ["actor", "session", "account", "group", "message", "peer", "revoked", "expired"],
)
def test_manager_confirmation_remains_exact_bound_and_revocable(
    authority_store, change
):
    store = authority_store
    row = store.tenant("99", "5")
    token = store.preview_manager(
        "99",
        "5",
        row["version"],
        "1",
        "private/1/peer",
        "preview",
        "2",
        "add",
        "1002",
    )
    actor, session, message, account, group = (
        "1",
        "private/1/peer",
        "confirm",
        "99",
        "5",
    )
    members = {"1": "1001", "2": "1002", "3": "1003"}
    if change == "actor":
        actor = "3"
    elif change == "session":
        session = "group/5"
    elif change == "account":
        account = "199"
    elif change == "group":
        group = "6"
    elif change == "message":
        message = "preview"
    elif change == "peer":
        members["2"] = "other-peer"
    elif change == "revoked":
        store.change("99", "5", row["version"], "3", managers=["3"])
    else:
        with store.connect(write=True) as db:
            db.execute("UPDATE capabilities SET expires=?", (time.time() - 1,))
    before = store.tenants()
    with pytest.raises(OpenError):
        store.confirm_manager(
            token,
            actor,
            session,
            message,
            account,
            group,
            members,
            eligible_managers={"2"},
        )
    assert store.tenants() == before


@pytest.fixture
def menu_event():
    """Build a transport-bound event without any live platform methods.

    Returns:
        Synthetic private event suitable for menu-key checks.
    """
    return SimpleNamespace(
        platform=SimpleNamespace(account="99"),
        actor_identity=ActorIdentity("99", "1", "1001", "1"),
        unified_msg_origin="fixture:private:1001",
        get_sender_id=lambda: "1",
        get_group_id=lambda: "",
    )


@pytest.mark.parametrize("scope", ["peer", "account", "group", "conversation"])
def test_menu_state_isolated_from_other_transport_scopes(menu_event, scope):
    menu = CommandMenu(SimpleNamespace())
    original = menu.key(menu_event)
    if scope == "peer":
        menu_event.actor_identity = replace(
            menu_event.actor_identity, peer="other-peer"
        )
    elif scope == "account":
        menu_event.platform.account = "199"
        menu_event.actor_identity = replace(menu_event.actor_identity, account="199")
    elif scope == "group":
        menu_event.get_group_id = lambda: "5"
    else:
        menu_event.unified_msg_origin = "fixture:group:5"
    assert menu.key(menu_event) is not None
    assert menu.key(menu_event) != original


@pytest.mark.parametrize("change", ["account", "sender", "missing_peer"])
def test_menu_rejects_mismatched_transport_identity(menu_event, change):
    if change == "account":
        menu_event.platform.account = "199"
    elif change == "sender":
        menu_event.get_sender_id = lambda: "2"
    else:
        menu_event.actor_identity = replace(menu_event.actor_identity, peer="")
    assert CommandMenu(SimpleNamespace()).key(menu_event) is None
