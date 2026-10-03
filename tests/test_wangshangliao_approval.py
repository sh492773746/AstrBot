import pytest

from astrbot.core.platform.sources.wangshangliao import approval as module
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError


def test_approval_single_use_and_binding(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "instance_dir", lambda _: tmp_path)
    session = {"business": {"uid": 123, "jwt": "test-session"}}
    request = {"action": "moderation_preview", "group": 5, "operation_action": "announce", "text": "test"}
    preview = module.approval("bot", "user", session, request)
    consume = {"action": "moderation_execute", "instance_id": "bot", "approval_token": preview["approval_token"]}
    with pytest.raises(ProtocolError):
        module.approval("bot", "other", session, consume)
    with pytest.raises(ProtocolError):
        module.approval("bot", "user", {"business": {"uid": 123, "jwt": "new"}}, consume)
    with pytest.raises(ProtocolError):
        module.approval("bot", "user", session, {**consume, "text": "changed"})
    assert module.approval("bot", "user", session, consume)["text"] == "test"
    with pytest.raises(ProtocolError):
        module.approval("bot", "user", session, consume)


def test_expired_approval(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(module.time, "time", lambda: 1000)
    session = {"business": {"uid": 123}}
    preview = module.approval("bot", "user", session, {"action": "moderation_preview", "group": 5, "operation_action": "mute_all"})
    monkeypatch.setattr(module.time, "time", lambda: 1601)
    with pytest.raises(ProtocolError):
        module.approval("bot", "user", session, {"action": "moderation_execute", "instance_id": "bot", "approval_token": preview["approval_token"]})
