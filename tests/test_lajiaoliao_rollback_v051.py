import copy
import importlib.util
import json
from pathlib import Path

from data.plugins.astrbot_plugin_lajiaoliao.business import DEFAULTS, BusinessStore


def test_v051_conversion_retains_unknown_ledgers_and_original_settings(tmp_path):
    path = (
        Path(__file__).resolve().parents[1]
        / "services/lajiaoliao-bridge/rollback_v051.py"
    )
    spec = importlib.util.spec_from_file_location("lajiaoliao_v051_rollback", path)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    store = BusinessStore(tmp_path)
    value = copy.deepcopy(DEFAULTS)
    value["native_actions"] = ["mute", "reply_recall"]
    value["invitations_enabled"] = True
    value["invitation_rewards_enabled"] = False
    value["lottery"]["min_invitations"] = 2
    store.save_settings("g", 0, value, "dashboard:owner", [])
    identifier = store.reserve_effect("g", "card", "message", "u", {})
    with store.connect() as db:
        db.execute(
            "INSERT INTO scheduled_jobs VALUES(?,?,?,?,?,?,?,?,?)",
            ("unknown-job", "g", "quiet", 1, "dashboard:owner", 1, "unknown", "{}", ""),
        )
    old_defaults = {
        key: copy.deepcopy(value)
        for key, value in DEFAULTS.items()
        if key
        not in {
            "maintenance",
            "lottery_plan",
            "reminder_plan",
            "invitation_rewards_enabled",
        }
    }
    old_defaults["automation"].pop("punishment_mode")
    old_defaults["automation"].pop("flood_recall")

    def validator(result):
        assert set(result) == set(old_defaults)
        assert set(result["automation"]) == set(old_defaults["automation"])
        return result

    assert (
        tool.convert_database(
            tmp_path / "business.sqlite3", old_defaults, {"mute"}, validator
        )
        == 1
    )
    with store.connect() as db:
        row = db.execute(
            "SELECT version,value FROM settings WHERE group_id='g'"
        ).fetchone()
        result = json.loads(row["value"])
        assert row["version"] == 2
        assert result["native_actions"] == ["mute"]
        assert result["automation"]["enabled"] is False
        assert result["lottery_enabled"] is False
        assert result["reminders_enabled"] is False
        assert result["invitations_enabled"] is False
        assert result["lottery"]["min_invitations"] == 0
        original = db.execute("SELECT value FROM rollback_settings_v060").fetchone()[0]
        assert json.loads(original)["native_actions"] == ["mute", "reply_recall"]
        assert db.execute("SELECT state FROM scheduled_jobs").fetchone()[0] == "unknown"
        assert (
            db.execute(
                "SELECT state FROM maintenance_effects WHERE id=?", (identifier,)
            ).fetchone()[0]
            == "unknown"
        )
