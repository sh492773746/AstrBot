"""Exercise Telegram migration behavior without sending real login codes."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import TelegramClient, errors, functions, types
from telethon.sessions import MemorySession

from collector import __main__ as cli


async def test_login_retries_after_data_center_migration(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"api_id": 12345, "api_hash": "a" * 32}))
    clients = []

    def factory(session_path, api_id, api_hash, **kwargs):
        client = TelegramClient(MemorySession(), api_id, api_hash, **kwargs)
        client.is_user_authorized = AsyncMock(return_value=False)
        client._switch_dc = AsyncMock()
        client.disconnect = AsyncMock()
        result = types.auth.SentCode(types.auth.SentCodeTypeApp(5), "test-code-hash")
        sender = SimpleNamespace(
            send=AsyncMock(
                side_effect=[errors.PhoneMigrateError(None, capture=4), result]
            )
        )
        request = functions.auth.SendCodeRequest(
            "15550000000", api_id, api_hash, types.CodeSettings()
        )

        async def start(**callbacks):
            assert callable(callbacks["phone"])
            assert callable(callbacks["password"])
            assert callable(callbacks["code_callback"])
            assert await client._call(sender, request) is result

        client.start = start
        clients.append((client, sender))
        return client

    monkeypatch.setattr(cli, "TelegramClient", factory)
    await cli.execute(SimpleNamespace(config=config, command="login"))
    client, sender = clients[0]
    assert sender.send.await_count == 2
    client._switch_dc.assert_awaited_once_with(4)
    client.disconnect.assert_awaited_once()
    assert client.flood_sleep_threshold == 0


async def test_old_zero_retry_setting_reproduces_generic_value_error():
    client = TelegramClient(MemorySession(), 12345, "a" * 32, request_retries=0)
    client.is_user_authorized = AsyncMock(return_value=False)
    client._switch_dc = AsyncMock()
    sender = SimpleNamespace(
        send=AsyncMock(side_effect=errors.PhoneMigrateError(None, capture=4))
    )
    request = functions.auth.SendCodeRequest(
        "15550000000", 12345, "a" * 32, types.CodeSettings()
    )
    with pytest.raises(ValueError, match="Request was unsuccessful"):
        await client._call(sender, request)


@pytest.mark.parametrize(
    "error,expected",
    [
        (errors.ApiIdInvalidError(None), "developer credentials"),
        (errors.FloodWaitError(None, capture=42), "42 seconds"),
        (
            ValueError("sensitive-content-must-not-be-printed"),
            "Invalid configuration or input",
        ),
    ],
)
def test_cli_error_hints_do_not_expose_exception_payload(
    monkeypatch, capsys, error, expected
):
    monkeypatch.setattr("sys.argv", ["tg-collector", "login"])
    monkeypatch.setattr(cli, "execute", AsyncMock(side_effect=error))
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 1
    text = capsys.readouterr().err
    assert expected in text
    assert "sensitive-content" not in text
