"""Keyword image replies retain native scope, order and no-replay guarantees."""

import base64
import io
import json
import time
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from pydantic import ValidationError

from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from astrbot.builtin_stars.wangshangliao_moderation.rule_replies import KeywordReply
from tests.test_wangshangliao_business_rules import (
    event_for,
    keyword,
    save_rules,
)
from tests.test_wangshangliao_business_rules import (
    rules as rules,
)


@pytest.fixture
def image_id(rules):
    """Create one local bitmap in the isolated rule scope.

    Args:
        rules: Isolated business-rule fixture.

    Returns:
        Group-scoped opaque image identifier.
    """
    output = io.BytesIO()
    Image.new("RGB", (40, 30), "#176f62").save(output, "PNG")
    return rules.media.upload(
        "99", "5", base64.b64encode(output.getvalue()).decode(), guard=lambda: None
    )["id"]


def test_keyword_schema_supports_one_image_and_image_only_reply(image_id):
    parsed = KeywordReply.model_validate(keyword(images=[image_id], text=""))
    assert parsed.images == [image_id]
    assert KeywordReply.model_validate(keyword()).images == []


def test_keyword_image_scope_is_checked_at_preview(rules, image_id):
    with pytest.raises(OpenError, match="image_scope"):
        save_rules(rules, {"keywords": [keyword(images=[image_id])]}, group="6")


@pytest.mark.parametrize(
    "values",
    [
        {"images": ["a" * 32, "b" * 32]},
        {"images": ["https://invalid.test/picture.png"]},
        {"images": [], "text": "  "},
    ],
)
def test_keyword_image_limits_and_empty_content_are_rejected(values):
    with pytest.raises(ValidationError):
        KeywordReply.model_validate(keyword(**values))


def test_saved_keyword_image_is_previewed_and_retained_during_cleanup(rules, image_id):
    save_rules(rules, {"keywords": [keyword(images=[image_id])]})
    assert image_id in rules.media.previews("99", "5", rules.read("99", "5")["config"])
    with rules.store.connect(write=True) as db:
        db.execute("UPDATE business_rule_images SET created=?", (time.time() - 172800,))
    output = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(output, "PNG")
    rules.media.upload(
        "99", "5", base64.b64encode(output.getvalue()).decode(), guard=lambda: None
    )
    with rules.store.connect() as db:
        assert db.execute(
            "SELECT 1 FROM business_rule_images WHERE id=?", (image_id,)
        ).fetchone()


@pytest.mark.asyncio
@pytest.mark.parametrize("image_state", ["accepted", "unknown", "rejected"])
async def test_keyword_image_precedes_text_and_uncertain_image_is_not_replayed(
    rules, image_id, monkeypatch, image_state
):
    save_rules(rules, {"keywords": [keyword(images=[image_id])]})
    monkeypatch.setattr(rules, "send_guard", lambda *a, **kw: None)
    steps = []

    async def send_image(*args, **kwargs):
        steps.append("image")
        assert kwargs["proactive"] is False
        return image_state

    async def send_text(*args, **kwargs):
        steps.append("text")
        assert kwargs["auto_recall"] is False
        return "accepted"

    rules.media.send = AsyncMock(side_effect=send_image)
    event = event_for(rules.service)
    event.platform.send_reply_text = AsyncMock(side_effect=send_text)
    assert await rules.observe(event)
    assert steps == (["image", "text"] if image_state == "accepted" else ["image"])
    assert await rules.observe(event)
    assert rules.media.send.call_count == 1
    with rules.store.connect() as db:
        state = db.execute(
            "SELECT state FROM business_rule_sends WHERE operation=?",
            ("keyword/99/5/message",),
        ).fetchone()["state"]
    assert state == image_state


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "Notification"])
async def test_image_only_and_partial_keyword_replies_obey_cooldown(
    rules, image_id, monkeypatch, text
):
    save_rules(rules, {"keywords": [keyword(images=[image_id], text=text)]})
    monkeypatch.setattr(rules, "send_guard", lambda *a, **kw: None)
    rules.media.send = AsyncMock(return_value="accepted")
    event = event_for(rules.service)
    event.platform.send_reply_text = AsyncMock(return_value="rejected")
    assert await rules.observe(event)
    assert event.platform.send_reply_text.await_count == bool(text)
    event.message_obj.message_id = "message2"
    assert await rules.observe(event)
    assert rules.media.send.await_count == 1
    with rules.store.connect() as db:
        rows = {
            row["operation"]: row["state"]
            for row in db.execute("SELECT operation,state FROM business_rule_sends")
        }
    assert rows["keyword/99/5/message"] == ("partial" if text else "accepted")
    assert rows["keyword/99/5/message2"] == "cooldown"


@pytest.mark.asyncio
async def test_revocation_between_keyword_image_and_text_blocks_caption(
    rules, image_id, monkeypatch
):
    save_rules(rules, {"keywords": [keyword(images=[image_id])]})
    revoked = False

    def guard(*args, **kwargs):
        if revoked:
            raise OpenError("revoked")

    async def send_image(*args, **kwargs):
        nonlocal revoked
        revoked = True
        return "accepted"

    monkeypatch.setattr(rules, "send_guard", guard)
    rules.media.send = AsyncMock(side_effect=send_image)
    event = event_for(rules.service)
    assert await rules.observe(event)
    event.platform.send_reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_fixed_command_text_edit_preserves_keyword_image(
    rules, image_id, monkeypatch
):
    save_rules(rules, {"keywords": [keyword(images=[image_id])]})
    event = event_for(rules.service)
    event.unified_msg_origin = "isolated-rule-session"
    monkeypatch.setattr(
        rules,
        "context",
        AsyncMock(return_value=("5", rules.store.tenant("99", "5"), lambda: None)),
    )
    message = await rules.command(event, "修改回复 1 通知|精确|通知|更新后的通知")
    assert "配图：1张" in message
    with rules.store.connect() as db:
        draft = db.execute(
            "SELECT config FROM business_rule_drafts ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    edited = json.loads(draft["config"])["keywords"][0]
    assert edited["images"] == [image_id] and edited["text"] == "更新后的通知"
    assert rules.read("99", "5")["version"] == 1
