import importlib.util
from pathlib import Path


def test_actual_bot_evidence_and_ordinary_reward_projection(tmp_path, monkeypatch):
    directory = Path(__file__).resolve().parents[1] / "services/lajiaoliao-bridge"
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location(
        "lajiaoliao_rejoin_reward_acceptance",
        directory / "rejoin_reward_acceptance.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stamp = 1791708255
    roster = {
        "9680209": {"groupID": "72567012", "userID": "9680209"},
        "3129316": {
            "groupID": "72567012",
            "userID": "3129316",
            "joinSource": 2,
            "inviterUserID": "9680209",
            "joinTime": stamp * 1000,
        },
    }
    note = {
        "timestamp": stamp,
        "message_id": "test-only",
        "inviter_id": "9680209",
        "member_ids": ["3129316"],
    }
    results = module.calculate_cases(tmp_path, roster, note, set(roster))
    assert len(results) == 5
    assert all(row["pass"] for row in results.values())
    assert results["actual_evidence_with_bot_protection"]["points"] == 0
    ordinary = results["ordinary_first_duplicate_rejoin_restart"]
    assert ordinary["first_credited"] == 1
    assert ordinary["rejoin_credited"] == 0
    assert ordinary["restart_credited"] == 0
