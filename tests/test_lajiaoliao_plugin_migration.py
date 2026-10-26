import json

import pytest

from data.plugins.astrbot_plugin_lajiaoliao.migration import (
    prepare_bundle,
    rollback_bundle,
)
from data.plugins.astrbot_plugin_lajiaoliao.storage import (
    SessionError,
    SessionStore,
    atomic_json,
)


def fixtures():
    return (
        {"platform": [
            {"id": "unrelated", "type": "other"},
            {"id": "test", "type": "lajiaoliao", "enable": False,
             "account_id": "u", "enabled_groups": ["g", "disabled"],
             "reply_groups": {"disabled": False}, "reply_private": False},
        ]},
        {"user_id": "u", "chat_token": "synthetic-chat", "im_token": "synthetic-im"},
        {"user_id": "u", "sequences": {"group:g": 12}},
    )


def test_bundle_preserves_policy_and_current_rollback_token(tmp_path):
    config, token, cursor = fixtures()
    manifest = prepare_bundle(config, token, cursor, owner="owner", instance_id="test",
                              output=tmp_path / "prepared")
    assert config["platform"][1]["enabled_groups"] == ["g", "disabled"]
    after = json.loads((tmp_path / "prepared/bot.after.json").read_text())
    assert "account_id" not in after
    assert after["message_policy"] == {
        "group_mode": "allowlist", "group_ids": ["g"], "private_enabled": False,
    }
    assert not after["enable"]
    store = SessionStore(tmp_path / "prepared/sessions")
    ref = manifest["session_ref"]
    token["im_token"] = "synthetic-new-current"
    atomic_json(store.directory(ref) / "token.json", token)
    rollback_bundle(store, ref, config["platform"][1], output=tmp_path / "rollback")
    assert json.loads((tmp_path / "rollback/token.json").read_text())["im_token"] == "synthetic-new-current"
    assert ((tmp_path / "rollback/token.json").stat().st_mode & 0o777) == 0o600


def test_wrong_account_is_never_implicitly_adopted(tmp_path):
    config, token, cursor = fixtures()
    config["platform"][1]["account_id"] = "old-placeholder"
    with pytest.raises(SessionError, match="confirmation_required"):
        prepare_bundle(config, token, cursor, owner="owner", instance_id="test", output=tmp_path / "new")
    assert not (tmp_path / "new").exists()


def test_wrong_cursor_is_rejected(tmp_path):
    config, token, cursor = fixtures()
    cursor["user_id"] = "another-user"
    with pytest.raises(SessionError, match="cursor_account_mismatch"):
        prepare_bundle(config, token, cursor, owner="owner", instance_id="test", output=tmp_path / "new")


def test_no_cursor_preserves_first_subscription_baseline(tmp_path):
    config, token, _ = fixtures()
    manifest = prepare_bundle(config, token, None, owner="owner", instance_id="test", output=tmp_path / "new")
    assert manifest["history_baseline"] is True
    assert not list((tmp_path / "new").rglob("cursor.json"))


def test_deferred_binding_never_adopts_mismatched_account(tmp_path):
    config, token, cursor = fixtures()
    config["platform"][1].update(account_id="legacy-placeholder", enable=True)
    manifest = prepare_bundle(
        config, token, cursor, owner="owner", instance_id="test",
        output=tmp_path / "deferred", defer_binding=True,
    )
    store = SessionStore(tmp_path / "deferred/sessions")
    row = store.get(manifest["session_ref"], "owner")
    assert row["state"] == "authorized" and row["instance_id"] == ""
    assert not json.loads((tmp_path / "deferred/bot.after.json").read_text())["enable"]
    with pytest.raises(SessionError, match="binding_mismatch"):
        store.for_instance(row["session_ref"], "test")
