"""Exact-scope deletion and linked-row guards for the retired group."""

import copy
import json
import sqlite3
from contextlib import closing

import pytest

from scripts.retire_wsl_dahai_group import (
    GROUP,
    deletion_plan,
    group_origin,
    scrub_policy,
)


def test_policy_cleanup_keeps_other_rules_and_literal_keyword_numbers():
    before = {
        "enabled_groups": [GROUP, "1516387"],
        "moderation": {
            "permissions": {GROUP: ["mute"], "1516387": ["recall"]},
            "mute_keywords": [GROUP, "literal"],
        },
        "developer_test": {"groups": [GROUP]},
        "proactive_send": {"targets": [GROUP, "1516387"]},
    }
    original = copy.deepcopy(before)
    cleaned = scrub_policy(before)
    assert cleaned["enabled_groups"] == ["1516387"]
    assert cleaned["moderation"]["permissions"] == {"1516387": ["recall"]}
    assert cleaned["moderation"]["mute_keywords"] == [GROUP, "literal"]
    assert cleaned["developer_test"]["groups"] == []
    assert cleaned["proactive_send"]["targets"] == ["1516387"]
    assert before == original


@pytest.mark.parametrize(
    "value,expected",
    [
        (f"wangshangliao_bot:GroupMessage:99/{GROUP}", True),
        (f"wangshangliao_bot:GroupMessage:99/{GROUP}/wsl-ai/customer", True),
        (f"wangshangliao_bot:GroupMessage:99/{GROUP}1", False),
        (f"telegram:GroupMessage:99/{GROUP}", False),
        (f"wangshangliao_bot:FriendMessage:99/{GROUP}", False),
        (f"other text mentions {GROUP}", False),
    ],
)
def test_origins_require_exact_platform_group_and_boundary(value, expected):
    assert group_origin(value) is expected


def test_deletion_plan_handles_children_but_never_matches_message_text():
    with closing(sqlite3.connect(":memory:")) as db:
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE groups(id TEXT,name TEXT);
            CREATE TABLE entries(group_id TEXT,member TEXT);
            CREATE TABLE lotteries(id TEXT,group_id TEXT);
            CREATE TABLE lottery_entries(lottery TEXT,member TEXT);
            CREATE TABLE outbox(key TEXT,fingerprint TEXT);
            CREATE TABLE operations(id TEXT,result TEXT);
            CREATE TABLE card_seen(identity TEXT);
        """)
        db.executemany(
            "INSERT INTO groups VALUES(?,?)",
            [(GROUP, "retired"), ("1516387", "keep")],
        )
        db.executemany(
            "INSERT INTO entries VALUES(?,?)",
            [(GROUP, "1"), ("1516387", GROUP)],
        )
        db.executemany(
            "INSERT INTO lotteries VALUES(?,?)",
            [("old", GROUP), ("keep", "1516387")],
        )
        db.executemany(
            "INSERT INTO lottery_entries VALUES(?,?)",
            [("old", "1"), ("keep", GROUP)],
        )
        db.executemany(
            "INSERT INTO outbox VALUES(?,?)",
            [
                (f"99/{GROUP}/reply", json.dumps([GROUP, "reply"])),
                ("99/1516387/reply", json.dumps(["1516387", GROUP])),
            ],
        )
        db.executemany(
            "INSERT INTO operations VALUES(?,?)",
            [
                ("old", json.dumps({"group": int(GROUP)})),
                ("keep", json.dumps({"group": 1516387, "text": GROUP})),
            ],
        )
        db.executemany(
            "INSERT INTO card_seen VALUES(?)",
            [
                (json.dumps(["99", GROUP, "1", "1001"]),),
                (json.dumps(["99", "1516387", GROUP, "1002"]),),
            ],
        )
        plan = deletion_plan(db)
        assert plan == {
            "groups": [1],
            "entries": [1],
            "lotteries": [1],
            "lottery_entries": [1],
            "outbox": [1],
            "operations": [1],
            "card_seen": [1],
        }
        for table, ids in plan.items():
            db.executemany(
                f"DELETE FROM {table} WHERE rowid=?", [(row_id,) for row_id in ids]
            )
        assert deletion_plan(db) == {}
        assert db.execute("SELECT group_id,member FROM entries").fetchone()[:] == (
            "1516387",
            GROUP,
        )


def test_shared_framework_history_does_not_delete_other_platforms():
    with closing(sqlite3.connect(":memory:")) as db:
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE conversations(user_id TEXT,content TEXT)")
        db.executemany(
            "INSERT INTO conversations VALUES(?,?)",
            [
                (f"wangshangliao_bot:GroupMessage:99/{GROUP}", "retired"),
                (f"telegram:GroupMessage:99/{GROUP}", "keep"),
                ("wangshangliao_bot:GroupMessage:99/1516387", GROUP),
            ],
        )
        assert deletion_plan(db, framework=True) == {"conversations": [1]}
