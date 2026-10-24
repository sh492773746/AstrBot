"""Exact speech exemptions never grant commands, model quotas or native roles."""

import sqlite3

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import (
    member_exemptions as exemptions,
)
from astrbot.builtin_stars.wangshangliao_moderation import policy
from tests.test_wangshangliao_content_rules import fixture


def grant():
    exemptions.set_exemption(
        "bot",
        "1",
        "5",
        "2",
        "22",
        enabled=True,
        actor="server/test",
        reason="Explicit operator request",
        guard=lambda: None,
    )


@pytest.mark.parametrize(
    "account,group,member,peer",
    [
        ("1", "5", "2", "22"),
        ("9", "5", "2", "22"),
        ("1", "6", "2", "22"),
        ("1", "5", "3", "22"),
        ("1", "5", "2", "23"),
        ("1", "5", "2", ""),
    ],
)
def test_exact_identity_scope_and_revocation(
    tmp_path, monkeypatch, account, group, member, peer
):
    monkeypatch.setattr(exemptions, "instance_dir", lambda _: tmp_path)
    assert not exemptions.is_exempt("bot", account, group, member, peer)
    grant()
    assert exemptions.is_exempt("bot", account, group, member, peer) is (
        (account, group, member, peer) == ("1", "5", "2", "22")
    )
    exemptions.set_exemption(
        "bot",
        "1",
        "5",
        "2",
        "22",
        enabled=False,
        actor="server/test",
        reason="Revoked",
        guard=lambda: None,
    )
    assert not exemptions.is_exempt("bot", "1", "5", "2", "22")
    with sqlite3.connect(tmp_path / "member_exemptions.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM exemption_audit").fetchone()[0] == 2


def test_changed_authority_rolls_back_exemption(tmp_path, monkeypatch):
    monkeypatch.setattr(exemptions, "instance_dir", lambda _: tmp_path)
    calls = 0

    def guard():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("revoked")

    with pytest.raises(ValueError, match="revoked"):
        exemptions.set_exemption(
            "bot",
            "1",
            "5",
            "2",
            "22",
            enabled=True,
            actor="server/test",
            reason="Test",
            guard=guard,
        )
    assert not exemptions.is_exempt("bot", "1", "5", "2", "22")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["text", "image", "video", "forward", "flood", "keyword"]
)
async def test_exempt_member_has_no_automatic_side_effects(tmp_path, monkeypatch, kind):
    from astrbot.builtin_stars.wangshangliao_moderation import content_rules

    monkeypatch.setattr(exemptions, "instance_dir", lambda _: tmp_path)
    adapter, _ = fixture(tmp_path, monkeypatch)
    grant()
    before = dict(adapter.config)
    payload = {
        "sender": "2",
        "text": "加我vx:abc123456",
        "kind": kind,
        "recall_route": {"peer": "22"},
    }
    assessment = (
        {"decision": "violation", "message_id": "123", "category": "message_flood"}
        if kind == "flood"
        else None
    )
    assert not await content_rules.enforce(
        adapter, "5", "123", payload, assessment=assessment
    )
    assert not await policy.handle(adapter, "5", "123", payload)
    adapter.execute_moderation.assert_not_awaited()
    adapter.recall_violation.assert_not_awaited()
    adapter.send_text.assert_not_awaited()
    adapter.get_moderation_members.assert_not_awaited()
    assert adapter.config == before


@pytest.mark.asyncio
async def test_grant_during_roster_wait_prevents_inflight_sanction(
    tmp_path, monkeypatch
):
    from astrbot.builtin_stars.wangshangliao_moderation import content_rules

    monkeypatch.setattr(exemptions, "instance_dir", lambda _: tmp_path)
    adapter, member = fixture(tmp_path, monkeypatch)

    async def roster(group):
        grant()
        return {"complete": True, "groupMemberInfo": [member]}

    adapter.get_moderation_members.side_effect = roster
    await content_rules.enforce(
        adapter,
        "5",
        "123",
        {
            "sender": "2",
            "text": "加我vx:abc123456",
            "recall_route": {"peer": "22", "time": str(content_rules.time.time())},
        },
    )
    adapter.execute_moderation.assert_not_awaited()
    adapter.recall_violation.assert_not_awaited()
    adapter.send_text.assert_not_awaited()
