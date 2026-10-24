"""Offline behavior contracts for native adapter duplicate-code removal."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain, Reply
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.platform.sources.wangshangliao import event as native_event
from astrbot.core.platform.sources.wangshangliao.policy import validate_policy
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError


@pytest.fixture
def reply_event(monkeypatch):
    """Build isolated native events without configuration or network access.

    Args:
        monkeypatch: Test-scoped method and managed-account replacements.

    Returns:
        Factory returning an event and its framework bookkeeping mock.
    """
    bookkeeping = AsyncMock()
    monkeypatch.setattr(
        "astrbot.core.platform.astr_message_event.AstrMessageEvent.send", bookkeeping
    )
    monkeypatch.setattr(native_event, "is_managed_account", lambda _: False)

    def create(private, command, ranking=False, test_scope=None):
        message = AstrBotMessage()
        message.type = (
            MessageType.FRIEND_MESSAGE if private else MessageType.GROUP_MESSAGE
        )
        message.self_id = "1"
        message.session_id = "1/private/2/OTAy" if private else "1/5"
        message.group_id = "" if private else "5"
        message.message_id = "incoming"
        message.sender = MessageMember("2", "Fixture Member")
        message.message_str = "fixture"
        message.message = [Plain("fixture")]
        message.raw_message = {"_transport_peer": "902"}
        platform = SimpleNamespace(
            account="1",
            config={},
            members={"5": {"2": "902"}},
            meta=lambda: PlatformMetadata("wangshangliao", "fixture", id="cleanup"),
            send_reply_text=AsyncMock(return_value="accepted"),
        )
        event = native_event.WangshangliaoEvent(message, platform, not command)
        event._log_reply = Mock()
        if command:
            event.set_extra("wsl_plain_command", True)
            event.set_extra("wsl_command_result", True)
        if ranking:
            event.set_extra("wsl_ranking_result", True)
        if test_scope:
            event.set_extra("wsl_test_scope", test_scope)
        return event, bookkeeping

    return create


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "private,command,ranking,test_scope",
    [
        (True, False, False, None),
        (True, True, False, None),
        (True, False, False, "private_ai"),
        (False, False, False, None),
        (False, True, False, None),
        (False, True, True, None),
    ],
)
@pytest.mark.parametrize("quoted", [False, True])
@pytest.mark.parametrize(
    "receipt", ["accepted", "verified", "rejected", "unknown", None]
)
async def test_reply_call_preserves_optional_arguments_and_receipt(
    reply_event, private, command, ranking, test_scope, quoted, receipt
):
    """Keep the exact native call across all merged reply branches.

    Args:
        reply_event: Isolated native event factory.
        private: Whether the reply uses a private route.
        command: Whether the incoming event is a command.
        ranking: Whether ranking whitespace and mention omission apply.
        test_scope: Optional private test admission scope.
        quoted: Whether a native group quote is requested.
        receipt: Synthetic native result to normalize.
    """
    event, bookkeeping = reply_event(private, command, ranking, test_scope)
    event.platform.send_reply_text.return_value = receipt
    chain = [Plain("Reply")]
    if quoted:
        chain.insert(0, Reply(id="quoted"))
    await event.send(MessageChain(chain))

    target = "private/2/OTAy" if private else "5"
    text = "Reply" if private or ranking else "@Fixture Member Reply"
    expected = {}
    if quoted and not private:
        expected["reply_to"] = "quoted"
    if not private and not ranking:
        expected["mentions"] = [
            {"uid": 902, "nick": "Fixture Member", "start": 0, "end": 16}
        ]
    elif command or test_scope == "private_ai":
        expected["test_mid"] = "incoming"
    if command and not private:
        expected["auto_recall"] = True
    event.platform.send_reply_text.assert_awaited_once_with(
        target, f"{event.session_id}/incoming/0", text, **expected
    )
    assert event.reply_index == 1
    assert event.get_extra("_wsl_native_reply_result") == (
        receipt if receipt in {"accepted", "verified", "rejected"} else "unknown"
    )
    bookkeeping.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("ranking", [False, True])
@pytest.mark.parametrize("revoked", [False, True])
async def test_merged_call_keeps_management_authorization(
    reply_event, ranking, revoked
):
    """Retain both early denial and the original pre-submission guard.

    Args:
        reply_event: Isolated native event factory.
        ranking: Whether the reply takes the unmentioned command path.
        revoked: Whether management authority was already revoked.
    """
    event, bookkeeping = reply_event(False, True, ranking)
    guard = Mock(
        side_effect=ProtocolError("management_reply_scope_changed") if revoked else None
    )
    event.set_extra("_wsl_management_delivery_guard", guard)
    await event.send(MessageChain([Plain("Reply")]))
    if revoked:
        event.platform.send_reply_text.assert_not_awaited()
        bookkeeping.assert_not_awaited()
        assert event.reply_index == 0
        return
    guard.assert_called_once_with()
    authorization = event.platform.send_reply_text.await_args.kwargs["authorize"]
    await authorization()
    assert guard.call_count == 2
    guard.side_effect = ProtocolError("management_reply_scope_changed")
    with pytest.raises(ProtocolError, match="management_reply_scope_changed"):
        await authorization()


@pytest.mark.parametrize("field", ["keywords", "mute_keywords", "kick_keywords"])
@pytest.mark.parametrize(
    "words",
    [
        None,
        True,
        "word",
        {},
        ("word",),
        [None],
        [True],
        [""],
        [" "],
        ["x" * 101],
        ["x"] * 51,
    ],
)
def test_all_keyword_lists_keep_identical_validation(field, words):
    """Keep every legacy keyword field under the same strict bounds.

    Args:
        field: Existing keyword policy key.
        words: Invalid field value.
    """
    with pytest.raises(ValueError, match="^moderation_keywords$"):
        validate_policy({field: words})


@pytest.mark.parametrize("words", [[], ["x"], ["x" * 100] * 50])
def test_all_keyword_lists_accept_original_boundaries(words):
    """Keep empty lists and inclusive original size limits valid.

    Args:
        words: Valid list shared by all three existing keyword fields.
    """
    validate_policy({"keywords": words, "mute_keywords": words, "kick_keywords": words})


def test_keyword_validation_preserves_other_error_precedence():
    """Reject invalid keywords before checking the unrelated cooldown value."""
    with pytest.raises(ValueError, match="^moderation_keywords$"):
        validate_policy({"keywords": [""], "cooldown_seconds": 0})
