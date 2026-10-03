import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telethon import errors
from telethon.crypto import AuthKey
from telethon.sessions import MemorySession, SQLiteSession

from data.plugins.astrbot_plugin_telethon_ai import account_login as mod
from data.plugins.astrbot_plugin_telethon_ai.main import TelethonAI


@pytest.fixture
def login(tmp_path, monkeypatch):
    monkeypatch.setattr(mod.registry, "private_root", lambda: tmp_path)
    session = MemorySession()
    session.set_dc(2, "149.154.167.51", 443)
    session.auth_key = AuthKey(bytes(range(256)))
    client = SimpleNamespace(
        session=session,
        connect=AsyncMock(),
        disconnect=AsyncMock(),
        send_code_request=AsyncMock(
            return_value=SimpleNamespace(phone_code_hash="hash")
        ),
        sign_in=AsyncMock(),
        get_me=AsyncMock(return_value=SimpleNamespace(id=1234, bot=False)),
        log_out=AsyncMock(),
    )
    factory = Mock(return_value=client)
    monkeypatch.setattr(mod, "TelegramClient", factory)
    body = {
        "alias": "test",
        "phone": "+12025550123",
        "api_id": 123,
        "api_hash": "a" * 32,
        "consent": True,
    }
    return mod.AccountLogin(), body, client, tmp_path, factory


@pytest.mark.asyncio
async def test_login_persists_only_authorized_session(login):
    subject, body, client, root, factory = login
    result = await subject.start("admin", body)
    assert list(root.iterdir()) == []
    assert factory.call_args.kwargs["receive_updates"] is False
    result = await subject.confirm(
        "admin", {"ticket": result["ticket"], "code": "12345"}
    )
    assert result == {"state": "registered", "account": "test"}
    record = mod.registry.load()["test"]
    assert record["user_id"] == 1234
    assert "phone" not in record and "code" not in record and "password" not in record
    assert (root / "accounts.json").stat().st_mode & 0o777 == 0o600
    assert (root / "session_test.session").stat().st_mode & 0o777 == 0o600
    stored = SQLiteSession(record["session"])
    assert stored.auth_key.key == client.session.auth_key.key
    stored.close()
    client.disconnect.assert_awaited_once()
    client.log_out.assert_not_awaited()
    assert not subject.pending


@pytest.mark.asyncio
async def test_login_owner_2fa_cancel_and_expiry(login):
    subject, body, client, root, _ = login
    result = await subject.start("admin", body)
    ticket = result["ticket"]
    with pytest.raises(mod.LoginError):
        await subject.start("admin", body)
    for actor, token in [("other", ticket), ("admin", "错误"), ("admin", "wrong")]:
        with pytest.raises(mod.LoginError):
            await subject.confirm(actor, {"ticket": token, "code": "12345"})
    client.sign_in.side_effect = errors.SessionPasswordNeededError(None)
    assert await subject.confirm("admin", {"ticket": ticket, "code": "12345"}) == {
        "state": "password"
    }
    client.sign_in.side_effect = errors.PasswordHashInvalidError(None)
    with pytest.raises(mod.LoginError):
        await subject.confirm("admin", {"ticket": ticket, "password": "bad"})
    subject.pending["expires"] = time.monotonic() - 1
    with pytest.raises(mod.LoginError):
        await subject.confirm("admin", {"ticket": ticket, "password": "bad"})
    assert not subject.pending and list(root.iterdir()) == []
    client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_and_attempt_limit(login):
    subject, body, client, _, _ = login
    ticket = (await subject.start("admin", body))["ticket"]
    assert (await subject.confirm("admin", {"ticket": ticket, "cancel": True}))[
        "state"
    ] == "cancelled"
    subject.next_start = 0
    ticket = (await subject.start("admin", body))["ticket"]
    client.sign_in.side_effect = errors.PhoneCodeInvalidError(None)
    for _ in range(6):
        with pytest.raises(mod.LoginError):
            await subject.confirm("admin", {"ticket": ticket, "code": "12345"})
    assert not subject.pending


@pytest.mark.asyncio
async def test_duplicate_user_not_registered_or_left_connected(login):
    subject, body, client, root, _ = login
    ticket = (await subject.start("admin", body))["ticket"]
    await subject.confirm("admin", {"ticket": ticket, "code": "12345"})
    original = (root / "accounts.json").read_bytes()
    subject.next_start = 0
    ticket = (await subject.start("admin", {**body, "alias": "second"}))["ticket"]
    with pytest.raises(mod.LoginError):
        await subject.confirm("admin", {"ticket": ticket, "code": "12345"})
    await subject.close()
    client.log_out.assert_awaited_once()
    assert (root / "accounts.json").read_bytes() == original
    assert not (root / "session_second.session").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method", ["page_account_login", "page_account_confirm", "page_account_sync"]
)
async def test_account_mutations_require_dashboard_admin(method):
    response = await getattr(TelethonAI, method)(
        SimpleNamespace(dashboard_admin=lambda: False)
    )
    assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [{"alias": "../bad"}, {"consent": False}, {"api_id": True}, {"phone": "123"}],
)
async def test_invalid_input_never_connects(login, change):
    subject, body, _, _, factory = login
    with pytest.raises(mod.LoginError):
        await subject.start("admin", {**body, **change})
    factory.assert_not_called()
