"""Retired private cost probes stay offline; dry runs and receipt checks remain."""

import json
import sqlite3
import sys
from unittest.mock import Mock

import httpx
import pytest

from astrbot.builtin_stars.wangshangliao_moderation import (
    command_menu,
    conversation_guidance,
)
from scripts import check_wsl_model_cost as cost


@pytest.fixture
def lab(tmp_path, monkeypatch):
    """Build a disposable model config and quota store.

    Args:
        tmp_path: Isolated pytest directory.
        monkeypatch: Pytest patch manager.

    Returns:
        Config path, quota store and evidence directory.
    """
    root = tmp_path / "app"
    data = root / "data"
    data.mkdir(parents=True)
    config = data / "cmd_config.json"
    config.write_text(
        json.dumps(
            {
                "provider": [
                    {
                        "id": cost.PROVIDER,
                        "provider_source_id": "Gemini",
                        "model": cost.MODEL,
                    }
                ],
                "provider_sources": [
                    {
                        "id": "Gemini",
                        "api_base": cost.BASE,
                        "key": ["fixture-secret-never-persist"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    store = data / "plugin_data/wangshangliao_open/open-service.sqlite3"
    store.parent.mkdir(parents=True)
    with sqlite3.connect(store) as db:
        db.execute("CREATE TABLE ai_budget(id INTEGER, confirmed INTEGER)")
        db.execute("INSERT INTO ai_budget VALUES(1,0)")
        db.execute("CREATE TABLE calls(id TEXT)")
    output_root = tmp_path / "evidence"
    output_root.mkdir()
    output = output_root / "run"
    monkeypatch.setattr(cost, "ROOT", root)
    monkeypatch.setattr(cost, "EVIDENCE_ROOT", output_root)
    monkeypatch.setattr(sys, "argv", ["cost", "--apply", "--evidence", str(output)])
    return config, store, output


def test_dry_run_never_opens_http(tmp_path, monkeypatch, capsys):
    client = Mock(side_effect=AssertionError("dry run must remain offline"))
    monkeypatch.setattr(cost.httpx, "Client", client)
    monkeypatch.setattr(
        sys, "argv", ["cost", "--evidence", str(tmp_path / "not-created")]
    )
    cost.main()
    result = json.loads(capsys.readouterr().out)
    assert result["dry_run"] is True
    assert result["max_calls"] == 4
    assert result["max_output_tokens_per_call"] == 1024
    assert all(n <= 4096 for n in result["question_characters"])
    assert result["public_ai_gate_modified"] is False
    client.assert_not_called()
    assert not (tmp_path / "not-created").exists()


@pytest.mark.parametrize(
    "flags",
    [
        [],
        ["--accounted"],
        ["--provider", "deepseek/deepseek-v4.1-flash"],
        ["--provider", "deepseek/deepseek-v4.1-flash", "--accounted"],
    ],
)
def test_private_cost_probe_cannot_apply_after_prompt_retirement(
    lab, monkeypatch, flags
):
    config, store, output = lab
    before_config, before_store = config.read_bytes(), store.read_bytes()
    client = Mock(side_effect=AssertionError("retired probe must remain offline"))
    connect = Mock(side_effect=AssertionError("retired probe must not open a store"))
    monkeypatch.setattr(cost.httpx, "Client", client)
    monkeypatch.setattr(cost.sqlite3, "connect", connect)
    monkeypatch.setattr(sys, "argv", [*sys.argv, *flags])
    assert not hasattr(command_menu, "overview")
    assert not hasattr(conversation_guidance, "consultation_prompt")
    with pytest.raises(ImportError, match="cannot import name 'overview'"):
        cost.main()
    client.assert_not_called()
    connect.assert_not_called()
    assert config.read_bytes() == before_config
    assert store.read_bytes() == before_store
    assert not output.exists()


@pytest.mark.parametrize("limit", [0, 63, 1025])
def test_invalid_output_limit_never_sends(lab, monkeypatch, limit):
    _, _, output = lab
    client = Mock(side_effect=AssertionError("invalid probe must remain offline"))
    monkeypatch.setattr(cost.httpx, "Client", client)
    monkeypatch.setattr(sys, "argv", [*sys.argv, "--max-output", str(limit)])
    with pytest.raises(SystemExit):
        cost.main()
    client.assert_not_called()
    assert not output.exists()


def test_usage_receipts_expose_only_selected_model_aggregates():
    row = {
        "model": cost.MODEL,
        "requests": 1,
        "input_tokens": 10,
        "output_tokens": 3,
        "total_tokens": 13,
    }
    client = Mock()
    client.get.return_value = httpx.Response(
        200,
        json={
            "unit": "USD",
            "mode": "unrestricted",
            "secret_account_field": "account-secret",
            "model_stats": [
                {**row, "api_key": "model-secret"},
                {"model": "other-model", "requests": 999},
            ],
        },
        request=httpx.Request("GET", cost.BASE + "/usage"),
    )
    receipt = cost.usage_receipt(client, cost.BASE, "fixture-key", cost.MODEL)
    assert receipt == {"unit": "USD", "mode": "unrestricted", "model": row}
    client.get.assert_called_once_with(
        cost.BASE + "/usage", headers={"Authorization": "Bearer fixture-key"}
    )


@pytest.mark.parametrize(
    ("cached", "extra_requests", "unit", "valid"),
    [
        (4, 0, "USD", True),
        (3, 0, "USD", False),
        (4, 1, "USD", False),
        (4, 0, "CNY", False),
    ],
)
def test_receipt_cache_reconciliation_requires_exact_tokens_and_request_count(
    cached, extra_requests, unit, valid
):
    before = {
        "unit": "USD",
        "model": {
            "requests": 1,
            "input_tokens": 10,
            "output_tokens": 3,
            "cache_read_tokens": 0,
            "total_tokens": 13,
            "actual_cost": "0.0001",
        },
    }
    after = {
        "unit": unit,
        "model": {
            "requests": 2 + extra_requests,
            "input_tokens": 16,
            "output_tokens": 6,
            "cache_read_tokens": cached,
            "total_tokens": 26,
            "actual_cost": "0.0002",
        },
    }
    cases = [
        {
            "state": "completed",
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 3,
                "total_tokens": 13,
                "prompt_tokens_details": {"cached_tokens": 4},
            },
        }
    ]
    result = cost.reconcile_receipts(before, after, cases)
    assert result["receipt_attributed"] is valid
    if valid:
        assert result["receipt_input_convention"] == "uncached_plus_cache"
