"""Keep opt-in cost checks bounded and separate from production AI admission."""

import json
import sqlite3
import sys
from contextlib import contextmanager
from json import dumps

import httpx
import pytest

from scripts import check_wsl_model_cost as cost


@pytest.fixture
def lab(tmp_path, monkeypatch):
    """Build a disposable model config and quota store.

    Args:
        tmp_path: Isolated pytest directory.
        monkeypatch: Pytest patch manager.

    Returns:
        Config path, output directory and recorded request payloads.
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
    sent = []
    monkeypatch.setattr(cost, "ROOT", root)
    monkeypatch.setattr(cost, "EVIDENCE_ROOT", output_root)
    monkeypatch.setattr(cost.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(sys, "argv", ["cost", "--apply", "--evidence", str(output)])
    return config, output, sent


@pytest.fixture
def install_http(monkeypatch):
    """Install a no-network HTTP fixture.

    Args:
        monkeypatch: Pytest patch manager.

    Returns:
        Fixture installer accepting a request list and a failure mode.
    """

    def install(sent, mode="ok", *, base=None, model=None):
        @contextmanager
        def stream(client, method, url, *, headers, json):
            sent.append(json)
            assert method == "POST"
            assert url == (base or cost.BASE) + "/chat/completions"
            assert headers["Authorization"] == "Bearer fixture-secret-never-persist"
            if mode == "timeout":
                raise httpx.ReadTimeout("fixture")
            message = {"content": "Read-only fixture reply."}
            if mode == "tools":
                message["tool_calls"] = [{"id": "fixture"}]
            if mode == "empty":
                message["content"] = ""
            payload = {
                "id": "fixture-completion",
                "model": model or cost.MODEL,
                "choices": [{"message": message, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 3,
                    "total_tokens": 13,
                },
            }
            response = httpx.Response(
                503 if mode == "http" else 200,
                content=b"x" * 131073
                if mode == "oversized"
                else dumps(payload).encode(),
            )
            yield response

        monkeypatch.setattr(cost.httpx.Client, "stream", stream)

    return install


def test_dry_run_never_opens_http(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        sys, "argv", ["cost", "--evidence", str(tmp_path / "not-created")]
    )
    cost.main()
    result = json.loads(capsys.readouterr().out)
    assert result["dry_run"] is True
    assert result["max_calls"] == 4
    assert result["max_output_tokens_per_call"] == 1024
    assert all(n <= 4096 for n in result["question_characters"])
    assert not (tmp_path / "not-created").exists()


def test_four_calls_use_real_history_without_opening_gate(lab, install_http):
    config, output, sent = lab
    original = config.read_bytes()
    install_http(sent)
    cost.main()
    record = json.loads((output / "evidence.json").read_text())
    assert len(sent) == 4
    assert [len(p["messages"]) for p in sent] == [2, 2, 4, 6]
    assert sent[2]["messages"][2]["content"] == "Read-only fixture reply."
    assert all(p["max_completion_tokens"] == 1024 for p in sent)
    assert all(
        "仅获授权群管理：群列表、选择群" in p["messages"][0]["content"] for p in sent
    )
    assert all(set(p) == {"model", "messages", "max_completion_tokens"} for p in sent)
    assert config.read_bytes() == original
    assert record["completed"] is True
    assert record["public_budget_unchanged"] is True
    assert record["public_call_count_unchanged"] is True
    assert record["production_config_unchanged"] is True
    assert record["finance_opened"] is False
    assert "fixture-secret" not in (output / "evidence.json").read_text()
    assert (output / "evidence.json").stat().st_mode & 0o777 == 0o600
    assert output.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    ("mode", "state"),
    [
        ("timeout", "unknown"),
        ("http", "http_rejected"),
        ("tools", "response_rejected"),
        ("empty", "response_rejected"),
        ("oversized", "unknown"),
    ],
)
def test_uncertain_or_invalid_call_stops_without_retry(lab, install_http, mode, state):
    _, output, sent = lab
    install_http(sent, mode)
    with pytest.raises(RuntimeError, match="do not replay"):
        cost.main()
    record = json.loads((output / "evidence.json").read_text())
    assert len(sent) == 1
    assert record["cases"][0]["state"] == state
    assert record["completed"] is False
    assert record["public_budget_unchanged"] is True
    assert record["public_call_count_unchanged"] is True


def test_existing_journal_cannot_be_replayed(lab, install_http):
    _, output, sent = lab
    install_http(sent)
    output.mkdir()
    with pytest.raises(FileExistsError):
        cost.main()
    assert sent == []


@pytest.fixture
def accounted_lab(lab, monkeypatch):
    """Install a confirmed fixture price without enabling the public service.

    Args:
        lab: Disposable provider and public-gate fixture.
        monkeypatch: Clock and CLI replacement.

    Returns:
        Existing model fixture with a separate opt-in accounting flag.
    """
    from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenStore
    from tests.test_wangshangliao_ai_plans import PRICE

    config, output, sent = lab
    store = config.parent / "plugin_data/wangshangliao_open/open-service.sqlite3"
    model = {
        "base_url": cost.BASE,
        "model": cost.MODEL,
        "api_key": "fixture-secret-never-persist",
    }
    with sqlite3.connect(store) as db:
        db.execute(
            "CREATE TABLE ai_prices(binding TEXT,provider TEXT,config TEXT,blocked TEXT)"
        )
        db.execute(
            "INSERT INTO ai_prices VALUES(?,?,?,'')",
            (OpenStore.model_binding(model), cost.PROVIDER, dumps(PRICE)),
        )
    clock = [1_800_000_000]
    monkeypatch.setattr(cost.time, "time", lambda: clock[0])
    monkeypatch.setattr(
        cost.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    monkeypatch.setattr(sys, "argv", [*sys.argv, "--accounted", "--max-output", "768"])
    return config, output, sent


def test_accounted_probe_settles_four_calls_without_opening_public_ai(
    accounted_lab, install_http
):
    config, output, sent = accounted_lab
    original = config.read_bytes()
    install_http(sent)
    cost.main()
    record = json.loads((output / "evidence.json").read_text())
    assert record["completed"] is True
    assert record["isolated_accounting"] is True
    assert record["isolated_monthly_budget_micro"] == 1_000_000
    assert len(sent) == 4
    assert all(p["max_completion_tokens"] == 768 for p in sent)
    assert all(c["charged_micro"] == 1 for c in record["cases"])
    isolated = output / "isolated-accounting/open-service.sqlite3"
    with sqlite3.connect(isolated) as db:
        assert db.execute(
            "SELECT COUNT(*),SUM(charged_micro) FROM ai_charges WHERE state='settled'"
        ).fetchone() == (4, 4)
    assert record["public_budget_unchanged"] is True
    assert record["public_call_count_unchanged"] is True
    assert config.read_bytes() == original
    assert "fixture-secret" not in (output / "evidence.json").read_text()


def test_accounted_probe_keeps_timeout_hold_without_retry(accounted_lab, install_http):
    _, output, sent = accounted_lab
    install_http(sent, "timeout")
    with pytest.raises(RuntimeError, match="do not replay"):
        cost.main()
    assert len(sent) == 1
    with sqlite3.connect(output / "isolated-accounting/open-service.sqlite3") as db:
        assert db.execute("SELECT state FROM ai_charges").fetchone()[0] == "unknown"


def test_accounted_probe_requires_existing_current_price(accounted_lab, install_http):
    config, _, sent = accounted_lab
    install_http(sent)
    store = config.parent / "plugin_data/wangshangliao_open/open-service.sqlite3"
    with sqlite3.connect(store) as db:
        db.execute("UPDATE ai_prices SET blocked='fixture-review-required'")
    with pytest.raises(RuntimeError, match="confirmed pricing"):
        cost.main()
    assert sent == []


@pytest.mark.parametrize("limit", [0, 63, 1025])
def test_invalid_output_limit_never_sends(lab, install_http, monkeypatch, limit):
    _, _, sent = lab
    install_http(sent)
    monkeypatch.setattr(sys, "argv", [*sys.argv, "--max-output", str(limit)])
    with pytest.raises(SystemExit):
        cost.main()
    assert sent == []


def test_unexpected_model_source_never_sends(lab, install_http):
    config, _, sent = lab
    install_http(sent)
    data = json.loads(config.read_text())
    data["provider_sources"][0]["api_base"] = "https://untrusted.invalid/v1"
    config.write_text(json.dumps(data))
    with pytest.raises(RuntimeError, match="does not match"):
        cost.main()
    assert sent == []


def test_evidence_cannot_target_application_data(lab, install_http, monkeypatch):
    config, _, sent = lab
    install_http(sent)
    monkeypatch.setattr(
        sys, "argv", ["cost", "--apply", "--evidence", str(config.parent / "run")]
    )
    with pytest.raises(RuntimeError, match="Evidence must be isolated"):
        cost.main()
    assert sent == []


@pytest.fixture
def deepseek_lab(lab, monkeypatch, install_http):
    config, output, sent = lab
    provider = "deepseek/deepseek-v4.1-flash"
    base, model, _ = cost.SOURCES[provider]
    data = json.loads(config.read_text())
    data["provider"][0].update(id=provider, model=model)
    data["provider_sources"][0]["api_base"] = base
    config.write_text(json.dumps(data))
    monkeypatch.setattr(sys, "argv", [*sys.argv, "--provider", provider])
    install_http(sent, base=base, model=model)

    def get(client, url, *, headers):
        assert url == base + "/v1/usage"
        assert headers["Authorization"] == "Bearer fixture-secret-never-persist"
        count = len(sent)
        return httpx.Response(
            200,
            json={
                "unit": "USD",
                "mode": "unrestricted",
                "secret_account_field": "never-persist-this-field",
                "model_stats": [
                    {
                        "model": model,
                        "requests": 1 + count,
                        "input_tokens": 10 + count * 10,
                        "output_tokens": 3 + count * 3,
                        "total_tokens": 13 + count * 13,
                        "cost": str((1 + count) / 1000),
                        "actual_cost": str((1 + count) / 10000),
                    }
                ],
            },
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(cost.httpx.Client, "get", get)
    return config, output, sent


def test_deepseek_real_probe_receipts_are_scoped_and_never_open_public_ai(deepseek_lab):
    config, output, sent = deepseek_lab
    original = config.read_bytes()
    cost.main()
    record = json.loads((output / "evidence.json").read_text())
    assert len(sent) == 4
    assert all(p["model"] == "deepseek-v4.1-flash" for p in sent)
    assert record["supplier_rate_user_reported"] == 0.07
    assert record["cost_probe_only"] is True
    assert record["supplier_hard_cap_verified"] is False
    assert record["receipt_attributed"] is True
    assert record["receipt_delta"]["actual_cost"] == "0.0004"
    assert record["public_budget_unchanged"] is True
    assert record["public_call_count_unchanged"] is True
    assert record["cost_currency_verified"] is False
    assert "never-persist-this-field" not in (output / "evidence.json").read_text()
    assert config.read_bytes() == original


def test_deepseek_stops_before_calls_without_preflight_receipt(
    deepseek_lab, monkeypatch
):
    _, output, sent = deepseek_lab

    def failed_get(*args, **kwargs):
        raise httpx.ReadTimeout("fixture receipt timeout")

    monkeypatch.setattr(cost.httpx.Client, "get", failed_get)
    with pytest.raises(httpx.ReadTimeout):
        cost.main()
    assert sent == []
    record = json.loads((output / "evidence.json").read_text())
    assert record["completed"] is False
    assert record["public_budget_unchanged"] is True


def test_deepseek_unknown_call_never_retries_or_claims_attribution(
    deepseek_lab, install_http
):
    _, output, sent = deepseek_lab
    base, model, _ = cost.SOURCES["deepseek/deepseek-v4.1-flash"]
    install_http(sent, "timeout", base=base, model=model)
    with pytest.raises(RuntimeError, match="do not replay"):
        cost.main()
    record = json.loads((output / "evidence.json").read_text())
    assert len(sent) == 1
    assert record["cases"][0]["state"] == "unknown"
    assert record["receipt_attributed"] is False
    assert record["completed"] is False


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
