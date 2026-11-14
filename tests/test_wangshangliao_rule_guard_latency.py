"""Exact-scope, fresh authority checks without blocking SQLite lock waits."""

import sqlite3
import time
from unittest.mock import Mock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from tests.test_wangshangliao_business_rules import (
    keyword,
    save_rules,
)
from tests.test_wangshangliao_business_rules import (
    rules as rules,
)


@pytest.fixture
def guarded(rules):
    """Prepare persisted owner authority and count effective-policy reads."""
    save_rules(rules, {"keywords": [keyword()]}, owner="1", peer="1001")
    service = rules.service
    service.active = True
    service.settings = {"paused": False}
    service.capacity_paused = False
    service.suspended_groups = set()
    service.membership_receiving = {}
    service.reconciling_groups = {}
    service.retired_groups = set()
    service.capabilities[("99", "5")] = {"checked_at": time.time()}
    service.member_evidence[("99", "5")] = {
        "99": {"nimId": "1099", "groupRole": "GROUP_ROLE_ADMIN"},
        "1": {"nimId": "1001", "groupRole": "GROUP_ROLE_OWNER"},
    }

    class Adapter:
        account = "99"
        nim_account = "1099"
        connection_state = "online"
        reads = 0
        policy = {
            "enable": True,
            "_business_active": True,
            "enabled_groups": ["5"],
            "reply_groups": {"5": True},
            "proactive_send": {"enabled": True, "targets": ["5"]},
        }

        @property
        def config(self):
            self.reads += 1
            return self.policy

    return rules, Adapter(), rules.read("99", "5")


def test_tenant_lookup_does_not_decode_other_groups(rules, monkeypatch):
    expected = rules.store.tenant("99", "5")
    rules.store.discover(
        "99",
        {group: {"name": "offline", "team": "100" + group} for group in ("5", "6")},
    )
    with rules.store.connect(write=True) as db:
        db.execute("UPDATE tenants SET rules='invalid-json' WHERE group_id='6'")
    monkeypatch.setattr(
        rules.store, "tenants", Mock(side_effect=AssertionError("account-wide scan"))
    )
    assert rules.store.tenant("99", "5") == expected
    assert rules.store.tenant("199", "5") is None
    assert rules.store.tenant("99", "7") is None


def test_send_guard_reads_one_policy_and_only_exact_persisted_versions(
    guarded, monkeypatch
):
    rules, adapter, row = guarded
    rules.store.discover(
        "99",
        {group: {"name": "offline", "team": "100" + group} for group in ("5", "6")},
    )
    with rules.store.connect(write=True) as db:
        db.execute("UPDATE tenants SET managers='invalid-json' WHERE group_id='6'")
        db.execute("UPDATE business_rules SET config='invalid-json'")
    monkeypatch.setattr(
        rules, "read", Mock(side_effect=AssertionError("rule body parsed"))
    )
    monkeypatch.setattr(
        rules.store, "tenant", Mock(side_effect=AssertionError("tenant body parsed"))
    )
    rules.send_guard(adapter, row, posting=True)
    assert adapter.reads == 1


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ("tenant_version", "business_rules_inactive"),
        ("rule_version", "business_rules_inactive"),
        ("tenant_paused", "business_rules_inactive"),
        ("manager_removed", "business_owner_revoked"),
        ("owner_demoted", "business_owner_revoked"),
        ("bot_demoted", "business_native_permission"),
        ("posting_revoked", "proactive_not_authorized"),
        ("reply_revoked", "business_rules_inactive"),
    ],
)
def test_guard_never_caches_authority_between_calls(guarded, change, error):
    rules, adapter, row = guarded
    rules.send_guard(adapter, row, posting=True)
    with rules.store.connect(write=True) as db:
        if change == "tenant_version":
            db.execute("UPDATE tenants SET version=version+1 WHERE group_id='5'")
        elif change == "rule_version":
            db.execute("UPDATE business_rules SET version=version+1")
        elif change == "tenant_paused":
            db.execute("UPDATE tenants SET paused=1 WHERE group_id='5'")
        elif change == "manager_removed":
            db.execute("UPDATE tenants SET managers='[]' WHERE group_id='5'")
    if change in {"owner_demoted", "bot_demoted"}:
        uid = "1" if change == "owner_demoted" else "99"
        rules.service.member_evidence[("99", "5")][uid]["groupRole"] = (
            "GROUP_ROLE_MEMBER"
        )
    elif change == "posting_revoked":
        adapter.policy["proactive_send"]["enabled"] = False
    elif change == "reply_revoked":
        adapter.policy["reply_groups"]["5"] = False
    with pytest.raises(OpenError, match=error):
        rules.send_guard(adapter, row, posting=True)
    assert adapter.reads == 2


def test_guard_fails_closed_without_waiting_for_exclusive_lock(guarded):
    rules, adapter, row = guarded
    # DELETE journal plus an exclusive lock exercises reader contention that a
    # normal WAL writer would not cause. No production database is opened.
    with rules.store.connect() as db:
        db.execute("PRAGMA journal_mode=DELETE")
    blocker = sqlite3.connect(rules.store.path)
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        started = time.perf_counter()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            rules.send_guard(adapter, row, posting=True)
        assert time.perf_counter() - started < 0.15
        assert adapter.reads == 0
    finally:
        blocker.rollback()
        blocker.close()
    rules.send_guard(adapter, row, posting=True)
    assert adapter.reads == 1
