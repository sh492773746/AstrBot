"""Audit candidates are advisory and cannot mutate financial state."""

import sqlite3

from scripts.audit_wsl_finance import inspect


def test_audit_reports_old_debt_without_rewriting_it():
    with sqlite3.connect(":memory:") as db:
        db.executescript(
            "CREATE TABLE groups(id,enabled,rate,agent_rate,qualification_seconds,"
            "require_speech,agent_only,started);"
            "INSERT INTO groups VALUES('10',0,500,1500,10800,0,1,2000);"
            "CREATE TABLE entries(id,group_id,member,inviter,amount,state,"
            "verification,eligible,present,ever_left,paid_at,paid_by,"
            "member_class,first_seen);"
            "INSERT INTO entries VALUES('entry','10','2','3',500,'unpaid',"
            "'verified',1,1,0,0,'','ordinary',1000);"
        )
        before = db.execute("SELECT * FROM entries").fetchall()
        result = inspect(db)
        assert result["read_only"] and not result["automatic_repairs"]
        assert result["review_count"] == 1
        assert result["review_candidates"][0]["reasons"] == [
            "debt_predates_current_collection_baseline"
        ]
        assert [tuple(r) for r in db.execute("SELECT * FROM entries")] == before
