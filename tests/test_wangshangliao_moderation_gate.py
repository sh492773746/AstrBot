import pytest

@pytest.mark.asyncio
async def test_plugin_managed_test_reaches_rule_once(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from astrbot.builtin_stars.wangshangliao_moderation import main
    from astrbot.core.platform.sources.wangshangliao import event

    monkeypatch.setattr(event, "is_managed_account", lambda _: True)
    handler = AsyncMock(return_value=True)
    monkeypatch.setattr(main, "handle", handler)
    e = SimpleNamespace(is_admin=lambda: False, get_platform_name=lambda: "wangshangliao", is_private_chat=lambda: False,
        get_sender_id=lambda: "2", get_group_id=lambda: "5",
        get_extra=lambda _: {"text": "exact-test-message"}, platform=SimpleNamespace(account="1"),
        message_obj=SimpleNamespace(message_id="m"), stop_event=Mock())
    await main.Main.moderate(None, e)
    handler.assert_not_awaited()
    e.get_extra = lambda key: (
        "group_rules" if key == "wsl_test_scope" else {"text": "exact-test-message"}
    )
    await main.Main.moderate(None, e)
    assert handler.await_count == 1
