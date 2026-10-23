"""Conservative cleanup and query-plan tests using isolated databases only."""

# ruff: noqa: F811
import sqlite3
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from test_superbot_community import community  # noqa: F401
from test_superbot_group_points import scoped  # noqa: F401
from test_superbot_moderation import setup  # noqa: F401
from test_superbot_text_game import text_service  # noqa: F401

from data.plugins.astrbot_plugin_superbot.maintenance import cleanup, main


def test_preview_and_cleanup_preserve_business_and_frozen_archive(scoped):
    s = scoped
    now = s.clock[0]
    old = (
        datetime.fromtimestamp(now - 8 * 86400, ZoneInfo("Asia/Shanghai"))
        .date()
        .isoformat()
    )
    today = datetime.fromtimestamp(now, ZoneInfo("Asia/Shanghai")).date().isoformat()
    for day in (old, today):
        s.store.db.execute(
            "INSERT INTO group_chat_seen VALUES('-1001','2',?,'fixture')", (day,)
        )
    s.store.db.execute(
        "INSERT INTO callbacks VALUES('expired','2','-1001','{}',?,0)", (now - 1,)
    )
    s.store.db.execute(
        "INSERT INTO callbacks VALUES('active','2','-1001','{}',?,0)", (now + 60,)
    )
    protected = {
        name: [tuple(r) for r in s.store.db.execute(f"SELECT * FROM {name}")]
        for name in ("group_wallets", "group_ledger", "ledger", "wallets", "chat_seen")
    }
    preview = cleanup(s.store.db, now)
    assert preview["group_chat_seen"] == {"eligible": 1, "affected": 0}
    assert s.store.db.execute("SELECT COUNT(*) FROM callbacks").fetchone()[0] == 2
    with s.store.tx() as db:
        applied = cleanup(db, now, apply=True)
    assert applied["group_chat_seen"]["affected"] == 1
    assert applied["callbacks"]["affected"] == 1
    assert s.store.db.execute("SELECT token FROM callbacks").fetchone()[0] == "active"
    for name, before in protected.items():
        assert before == [tuple(r) for r in s.store.db.execute(f"SELECT * FROM {name}")]
    assert all(
        r["affected"] == 0 for r in cleanup(s.store.db, now, apply=True).values()
    )


def test_panel_text_requires_confirmed_deletion_and_preserves_ids(scoped):
    s = scoped
    now = s.clock[0]
    for i, state in enumerate(("deleted", "review", "pending", "blocked"), 1):
        s.store.db.execute(
            "INSERT INTO game_panels(id,chat,source,kind,message,status,rendered,created) "
            "VALUES(?,'-1001',?,'hub',?,'done','old caption',?)",
            (i, i, i, now - 8 * 86400),
        )
        s.store.db.execute(
            "INSERT INTO gt_delete(chat,message,source,due,next,status) VALUES('-1001',?,?,?,?,?)",
            (i, i, now - 8 * 86400, 0, state),
        )
    with s.store.tx() as db:
        result = cleanup(db, now, apply=True)
    assert result["game_panels"]["affected"] == 1
    assert (
        s.store.db.execute("SELECT rendered FROM game_panels WHERE id=1").fetchone()[0]
        == ""
    )
    assert s.store.db.execute("SELECT COUNT(*) FROM game_panels").fetchone()[0] == 4
    assert s.store.db.execute("SELECT COUNT(*) FROM gt_delete").fetchone()[0] == 4
    assert (
        s.store.db.execute(
            "SELECT COUNT(*) FROM game_panels WHERE rendered<>''"
        ).fetchone()[0]
        == 3
    )


def test_batch_bound_and_transaction_rollback(scoped):
    s = scoped
    s.store.db.executemany(
        "INSERT INTO dialogs VALUES(?,'{}',0)", [(str(i),) for i in range(4)]
    )
    with pytest.raises(RuntimeError), s.store.tx() as db:
        assert cleanup(db, s.clock[0], apply=True, limit=2)["dialogs"]["affected"] == 2
        raise RuntimeError("rollback")
    assert s.store.db.execute("SELECT COUNT(*) FROM dialogs").fetchone()[0] == 4
    with pytest.raises(ValueError):
        cleanup(s.store.db, s.clock[0], apply=True, limit=501)


def test_cli_requires_backup_and_verifies_snapshot(
    scoped, tmp_path, monkeypatch, capsys
):
    s = scoped
    s.store.db.execute("INSERT INTO dialogs VALUES('2','{}',0)")
    monkeypatch.setattr(
        sys, "argv", ["maintenance", "cleanup", str(s.store.path), "--apply"]
    )
    with pytest.raises(SystemExit, match="backup"):
        main()
    assert s.store.db.execute("SELECT COUNT(*) FROM dialogs").fetchone()[0] == 1
    backup = tmp_path / "before.sqlite3"
    monkeypatch.setattr(sys, "argv", sys.argv + ["--destination", str(backup)])
    main()
    assert '"applied": true' in capsys.readouterr().out
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT COUNT(*) FROM dialogs").fetchone()[0] == 1
    assert s.store.db.execute("SELECT COUNT(*) FROM dialogs").fetchone()[0] == 0


@pytest.mark.parametrize(
    "query,index",
    [
        ("SELECT * FROM notices WHERE status='pending' LIMIT 1", "notices_pending"),
        ("SELECT rowid FROM dialogs WHERE expires<100 LIMIT 500", "dialogs_expiry"),
        (
            "SELECT rowid FROM group_chat_seen WHERE day<'2026-01-01' LIMIT 500",
            "group_chat_seen_day",
        ),
        ("SELECT rowid FROM ak_seen WHERE at<100 LIMIT 500", "ak_seen_age"),
        (
            "SELECT rowid FROM cm_tickets WHERE expires<100 LIMIT 500",
            "cm_ticket_expiry",
        ),
        (
            "SELECT job FROM gb_cleanup WHERE status='pending' AND next<100",
            "gb_cleanup_due",
        ),
        (
            "SELECT rowid FROM game_panels WHERE status='done' AND created<100",
            "game_panels_retention",
        ),
    ],
)
def test_targeted_retention_queries_use_indexes(scoped, query, index):
    plan = " ".join(
        str(tuple(r)) for r in scoped.store.db.execute("EXPLAIN QUERY PLAN " + query)
    )
    assert index in plan


def test_pending_index_survives_analyzed_completed_notice_history(scoped):
    db = scoped.store.db
    db.executemany(
        "INSERT INTO notices(id,chat,text,status) VALUES(?,'2','fixture','sent')",
        [(f"history-{i}",) for i in range(1000)],
    )
    db.execute("ANALYZE notices")
    plan = " ".join(
        str(tuple(r))
        for r in db.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM notices WHERE status='pending' LIMIT 1"
        )
    )
    assert "notices_pending" in plan
    assert "SCAN notices" not in plan
