"""The three-account runner reads real roles and never grants implicitly."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("fcntl", reason="POSIX-only maintenance CLI")

from scripts import check_wsl_three_live as live


@pytest.mark.asyncio
async def test_stale_cached_admin_cannot_trigger_or_pass_native_role_setup(
    tmp_path, monkeypatch
):
    root = tmp_path / "root"
    (root / "data").mkdir(parents=True)
    (root / "data/cmd_config.json").write_text(
        json.dumps({"dashboard": {"username": "fixture", "jwt_secret": "x" * 64}})
    )
    backups = tmp_path / "backups"
    backups.mkdir()
    monkeypatch.setattr(live, "ROOT", root)
    monkeypatch.setattr(live, "BACKUPS", backups)
    monkeypatch.setattr(live.sys, "argv", ["test", "--apply", "--phase", "lottery"])
    monkeypatch.setattr(live, "protected", lambda: "untouched")
    context = SimpleNamespace()
    context.__aenter__ = AsyncMock(return_value=context)
    context.__aexit__ = AsyncMock()

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(live.httpx, "AsyncClient", lambda **kw: Client())
    monkeypatch.setattr(live.aiohttp, "ClientSession", Client)
    accounts = [
        {"account": uid, "instance_id": instance, "state": "online"}
        for uid, instance in (
            (live.OWNER, live.SENDER),
            (live.PEER, live.RECEIVER),
            (live.ORDINARY, live.FIXTURE),
        )
    ]
    api = AsyncMock(
        return_value={
            "accounts": accounts,
            "groups": [
                {
                    "account": live.PEER,
                    "group_id": live.GROUP,
                    "native_role": "GROUP_ROLE_ADMIN",
                }
            ],
        }
    )
    monkeypatch.setattr(live, "MenuCheck", lambda *args: SimpleNamespace(api=api))
    pins = {live.OWNER: "111", live.PEER: "222", live.ORDINARY: "333"}

    class Vault:
        def __init__(self, instance):
            self.uid = next(
                a["account"] for a in accounts if a["instance_id"] == instance
            )

        def load(self):
            return {"business": {"uid": self.uid}, "nim_id": pins[self.uid]}

    monkeypatch.setattr(live, "Vault", Vault)
    monkeypatch.setattr(live.Deployment, "load", lambda: object())
    monkeypatch.setattr(
        live,
        "BusinessClient",
        lambda *args: SimpleNamespace(restore=lambda saved: None),
    )
    directory = AsyncMock(
        return_value={
            "complete": True,
            "groupMemberInfo": [
                {
                    "userId": uid,
                    "nimId": nim,
                    "accountState": "ACCOUNT_STATE_GOOD",
                    "groupRole": "GROUP_ROLE_OWNER"
                    if uid == live.OWNER
                    else "GROUP_ROLE_MEMBER",
                }
                for uid, nim in pins.items()
            ],
        }
    )
    monkeypatch.setattr(live, "member_directory", directory)
    lab = Mock(
        side_effect=AssertionError("No native write controller should be opened")
    )
    monkeypatch.setattr(live, "TestLab", lab)
    with pytest.raises(RuntimeError, match="Set Dahu as administrator"):
        await live.main()
    directory.assert_awaited_once()
    lab.assert_not_called()
    assert api.await_count == 1
    report = json.loads(next(backups.rglob("summary.json")).read_text())
    assert report["native_roles"][live.PEER] == "GROUP_ROLE_MEMBER"
    assert not report["automatic_role_changes"] and report["phases"] == []
