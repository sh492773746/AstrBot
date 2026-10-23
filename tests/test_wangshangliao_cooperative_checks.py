"""Admission guards for local native cooperation runners without network IO."""

import json
from unittest.mock import Mock

import pytest

from scripts import check_wsl_cooperative_live as cooperative
from scripts import check_wsl_quotes_live as quotes


@pytest.mark.asyncio
@pytest.mark.parametrize("phase,count", [("all", 3), ("permissions", 2)])
async def test_cooperative_plan_never_opens_a_client(
    monkeypatch, capsys, tmp_path, phase, count
):
    monkeypatch.setattr("sys.argv", ["cooperative", "--phase", phase])
    monkeypatch.setattr(cooperative, "ROOT", tmp_path / "missing")
    client = Mock(side_effect=AssertionError("Native IO is forbidden"))
    monkeypatch.setattr(cooperative.httpx, "AsyncClient", client)
    await cooperative.main()
    result = json.loads(capsys.readouterr().out)
    assert result["dry_run"] is True
    assert result["new_roles_or_send_grants"] is False
    assert result["model_calls"] == 0
    assert len(result["private_commands"]) == count
    client.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("runner", [cooperative, quotes])
@pytest.mark.parametrize(
    "invalid", ["missing", "duplicate_account", "duplicate_instance"]
)
async def test_native_runners_reject_ambiguous_bindings_before_network(
    monkeypatch, tmp_path, runner, invalid
):
    platforms = [
        {
            "id": "fixture-" + uid,
            "type": "wangshangliao",
            "account_id": uid,
            "enable": True,
        }
        for uid in ("31846829", "43506527", "23691273")
    ]
    if invalid == "missing":
        platforms.pop()
    elif invalid == "duplicate_account":
        platforms[-1]["account_id"] = platforms[0]["account_id"]
    else:
        platforms[-1]["id"] = platforms[0]["id"]
    data = tmp_path / "data"
    data.mkdir()
    (data / "cmd_config.json").write_text(
        json.dumps(
            {
                "platform": platforms,
                "dashboard": {
                    "username": "offline",
                    "jwt_secret": "offline-only-test-secret-" * 2,
                },
            }
        )
    )
    monkeypatch.setattr("sys.argv", ["runner", "--apply"])
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    client = Mock(side_effect=AssertionError("Native IO is forbidden"))
    monkeypatch.setattr(runner.httpx, "AsyncClient", client)
    with pytest.raises(RuntimeError, match="Exact three configured"):
        await runner.main()
    client.assert_not_called()
