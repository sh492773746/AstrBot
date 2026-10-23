"""Run four explicitly authorized, bounded model calls without opening public AI."""

import argparse
import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_ROOT = Path("/opt/backups")
PROVIDER = "Gemini/gemini-3.8-flash"
BASE = "https://www.sevnx.lol/v1"
MODEL = "gemini-3.8-flash"
MAX_OUTPUT = 1024
SOURCES = {
    PROVIDER: (BASE, MODEL, 0.25),
    "deepseek/deepseek-v4.1-flash": (
        "https://www.sevnx.lol",
        "deepseek-v4.1-flash",
        0.07,
    ),
}
TEST_CONTEXT = "\n本轮为费用验收，所有描述均为虚构情景，不包含真实群或财务数据。"
NORMAL = "你好，我刚把机器人拉进群，如何开通并查看我能管理的群？"
TOPICS = (
    "机器人没有群管理员权限，不能撤回别人的消息",
    "普通用户试图查看其他群的配置和账本",
    "邀请人不能证明自己是真实邀请者，要求直接开通",
    "有人要求AI运行代码并修改群设置",
    "群管理撤权后继续使用旧的确认消息",
    "已在群里开通，想直接使用数字菜单",
    "私聊选错群，需要回到群列表重新选择",
    "用户想查看本人邀请而不是别人的财务数据",
    "想先看广告规则，再增删一个普通关键词",
    "群里刷屏，需要了解规则但不能让AI代替执行",
    "定时禁言按北京时间设置，先查看当前状态",
    "抽奖必须先设置奖品和领奖联系人",
)
LONG_QUESTIONS = tuple(
    f"这是连续咨询第{turn}轮，请结合前文回答最后的问题。\n"
    + "\n".join(
        f"情景{turn}-{i:02d}：{TOPICS[(i + turn) % len(TOPICS)]}。"
        f"用户已看过第{i}条说明，但可能把菜单数字当作永久命令。"
        "请区分解释和执行，不猜测账号身份，不索取登录凭据，不宣称设置已经改变。"
        "以上仅为虚构客服材料，不要求实际操作。"
        for i in range(1, 25)
    )
    + "\n"
    + (
        "请归纳开通、身份核验和群内/私聊使用路径，给出简短步骤。"
        if turn == 1
        else "接着说明如何查看规则、调整关键词和定时禁言，强调固定命令及确认。"
        if turn == 2
        else "最后综合前两轮，说明哪些是普通用户功能、哪些要群管理权限，AI不能做什么。"
    )
    for turn in range(1, 4)
)


def usage_receipt(client, base, key, model):
    """Read only aggregate receipts for the selected model, never account secrets."""
    response = client.get(
        base.removesuffix("/v1") + "/v1/usage",
        headers={"Authorization": "Bearer " + key},
    )
    response.raise_for_status()
    data = response.json()
    matches = [
        row
        for row in data.get("model_stats", [])
        if isinstance(row, dict) and row.get("model") == model
    ]
    if len(matches) != 1:
        raise RuntimeError("A unique model usage receipt is required")
    allowed = {
        "model",
        "requests",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cost",
        "actual_cost",
        "cache_read_tokens",
        "cache_creation_tokens",
    }
    row = {k: v for k, v in matches[0].items() if k in allowed}
    if type(row.get("requests")) is not int:
        raise RuntimeError("The model usage receipt has no request counter")
    return {"unit": data.get("unit"), "mode": data.get("mode"), "model": row}


def reconcile_receipts(before, after, cases):
    """Match request and token deltas, allowing separately reported cache hits."""
    previous, current = before["model"], after["model"]
    delta = {
        k: str(Decimal(str(current[k])) - Decimal(str(previous[k])))
        for k in previous
        if k != "model" and k in current
    }
    valid = (
        bool(cases)
        and before["unit"] == after["unit"]
        and all(c["state"] == "completed" for c in cases)
        and all(Decimal(v).is_finite() and Decimal(v) >= 0 for v in delta.values())
        and Decimal(delta["requests"]) == len(cases)
    )
    totals = {
        k: sum(c.get("usage", {}).get(k, -1) for c in cases)
        for k in ("prompt_tokens", "completion_tokens", "total_tokens")
    }
    convention = "unmatched"
    if Decimal(delta["input_tokens"]) == totals["prompt_tokens"]:
        convention = "inclusive"
    else:
        cached = sum(
            (c.get("usage", {}).get("prompt_tokens_details") or {}).get(
                "cached_tokens", 0
            )
            for c in cases
        )
        if (
            cached > 0
            and Decimal(delta.get("cache_read_tokens", "0")) == cached
            and Decimal(delta["input_tokens"]) + cached == totals["prompt_tokens"]
        ):
            convention = "uncached_plus_cache"
    valid = (
        valid
        and convention != "unmatched"
        and Decimal(delta["output_tokens"]) == totals["completion_tokens"]
        and Decimal(delta["total_tokens"]) == totals["total_tokens"]
        and totals["prompt_tokens"] + totals["completion_tokens"]
        == totals["total_tokens"]
    )
    return {
        "receipt_delta": delta,
        "receipt_attributed": valid,
        "receipt_input_convention": convention,
    }


def main():
    """Journal bounded calls and token usage without mutating service settings.

    Raises:
        RuntimeError: If configuration, response validation or protected state fails.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--accounted", action="store_true")
    parser.add_argument("--max-output", type=int, default=MAX_OUTPUT)
    parser.add_argument("--provider", choices=tuple(SOURCES), default=PROVIDER)
    args = parser.parse_args()
    provider = args.provider
    base, model_name, multiplier = SOURCES[provider]
    relay_receipt = provider == "deepseek/deepseek-v4.1-flash"
    if not 64 <= args.max_output <= MAX_OUTPUT:
        parser.error("--max-output must be between 64 and 1024")
    questions = [NORMAL, *LONG_QUESTIONS]
    if any(len(question) > 4096 for question in questions):
        raise RuntimeError("A test question exceeds the public input limit")
    if not args.apply:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "provider": provider,
                    "max_calls": 4,
                    "max_output_tokens_per_call": args.max_output,
                    "question_characters": [len(q) for q in questions],
                    "public_ai_gate_modified": False,
                }
            )
        )
        return
    from astrbot.builtin_stars.wangshangliao_moderation.command_menu import overview
    from astrbot.builtin_stars.wangshangliao_moderation.conversation_guidance import (
        consultation_prompt,
    )

    system = consultation_prompt(overview()) + TEST_CONTEXT
    evidence = args.evidence.resolve()
    if not evidence.is_relative_to(EVIDENCE_ROOT):
        raise RuntimeError("Evidence must be isolated under /opt/backups")
    evidence.mkdir(mode=0o700)
    os.chmod(evidence, 0o700)
    config_path = ROOT / "data/cmd_config.json"
    config_bytes = config_path.read_bytes()
    config = json.loads(config_bytes.decode("utf-8-sig"))
    selected = next(p for p in config["provider"] if p["id"] == provider)
    source = next(
        p
        for p in config["provider_sources"]
        if p["id"] == selected["provider_source_id"]
    )
    merged = source | selected
    keys = merged.get("key")
    key = keys[0] if isinstance(keys, list) and keys else keys
    if (
        merged.get("api_base") != base
        or merged.get("model") != model_name
        or not merged.get("enable", True)
        or not isinstance(key, str)
        or not key
    ):
        raise RuntimeError("The fixed model configuration does not match")
    store = ROOT / "data/plugin_data/wangshangliao_open/open-service.sqlite3"
    with sqlite3.connect(f"file:{store}?mode=ro", uri=True) as db:
        budget_before = db.execute("SELECT * FROM ai_budget ORDER BY id").fetchall()
        calls_before = db.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    plans, model = None, {"base_url": base, "model": model_name, "api_key": key}
    if args.accounted:
        from astrbot.builtin_stars.wangshangliao_moderation.ai_plans import AIPlans
        from astrbot.builtin_stars.wangshangliao_moderation.open_store import (
            OpenStore,
        )

        binding = OpenStore.model_binding(model)
        with sqlite3.connect(store.resolve().as_uri() + "?mode=ro", uri=True) as db:
            row = db.execute(
                "SELECT provider,config,blocked FROM ai_prices WHERE binding=?",
                (binding,),
            ).fetchone()
        if not row or row[0] != provider or row[2]:
            raise RuntimeError("Current confirmed pricing is required for this test")
        plans = AIPlans(OpenStore(evidence / "isolated-accounting"))
        plans.configure(1, "Isolated test", "No public service", "1", "local-test")
        plans.configure_price(binding, provider, 0, json.loads(row[1]), "local-test")
    record = {
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "provider": provider,
        "request_model": model_name,
        "max_calls": 4,
        "max_output_tokens_per_call": args.max_output,
        "timeout_seconds": 45,
        "automatic_retries": 0,
        "tools_sent": False,
        "synthetic_context": True,
        "production_history_read": False,
        "public_ai_gate_modified": False,
        "finance_opened": False,
        "cost_currency_verified": False,
        "supplier_rate_user_reported": multiplier,
        "supplier_unit_cny_user_reported": 1,
        "billing_deduction": None,
        "cases": [],
        "isolated_accounting": bool(plans),
        "isolated_monthly_budget_micro": 1_000_000 if plans else None,
        "supplier_hard_cap_verified": False,
        "cost_probe_only": not bool(plans),
        "max_input_bytes_per_call": 65536,
    }
    path = evidence / "evidence.json"
    history = [{"role": "system", "content": system}]
    try:
        with httpx.Client(
            timeout=45,
            trust_env=False,
            follow_redirects=False,
            transport=httpx.HTTPTransport(retries=0),
        ) as client:
            if relay_receipt:
                record["usage_before"] = usage_receipt(client, base, key, model_name)
            for i, question in enumerate(questions):
                if i > 0:
                    time.sleep(5)
                if i <= 1:
                    history = [{"role": "system", "content": system}]
                messages = [*history, {"role": "user", "content": question}]
                payload = {
                    "model": model_name,
                    "max_completion_tokens": args.max_output,
                    "messages": messages,
                }
                if len(json.dumps(payload, ensure_ascii=False).encode()) > 65536:
                    raise RuntimeError("Test input exceeds the 64 KiB probe limit")
                case = {
                    "case": "normal" if i == 0 else f"long_turn_{i}",
                    "state": "dispatching",
                    "question": question,
                    "question_characters": len(question),
                    "input_message_count": len(messages),
                    "input_characters": sum(len(m["content"]) for m in messages),
                    "request_sha256": hashlib.sha256(
                        json.dumps(payload, ensure_ascii=False).encode()
                    ).hexdigest(),
                }
                record["cases"].append(case)
                quote = None
                if plans:
                    quote = plans.quote(model, payload)
                    plans.reserve(
                        "local-test/cost", "", case["request_sha256"], quote, "test"
                    )
                    case["reserved_micro"] = quote["reserved_micro"]
                with path.open("w", encoding="utf-8") as handle:
                    os.chmod(path, 0o600)
                    json.dump(record, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                started = time.monotonic()
                definitely_unsent = False
                try:
                    if plans:
                        plans.dispatched(case["request_sha256"], quote, payload)
                    with client.stream(
                        "POST",
                        base + "/chat/completions",
                        headers={"Authorization": "Bearer " + key},
                        json=payload,
                    ) as response:
                        case["http_status"] = response.status_code
                        case["request_id"] = response.headers.get("x-request-id")
                        raw = bytearray()
                        for chunk in response.iter_bytes(8192):
                            raw.extend(chunk)
                            if len(raw) > 131072:
                                raise RuntimeError("Model response exceeds 128 KiB")
                        case["seconds"] = round(time.monotonic() - started, 3)
                        if response.status_code != 200:
                            case["state"] = "http_rejected"
                            raise RuntimeError("Model HTTP request was rejected")
                        data = json.loads(raw)
                        message = data["choices"][0]["message"]
                        case["response_model"] = data.get("model")
                        case["completion_id"] = data.get("id")
                        case["usage"] = data.get("usage")
                        case["finish_reason"] = data["choices"][0].get("finish_reason")
                        if plans:
                            plans.settle(case["request_sha256"], quote, case["usage"])
                            case["charged_micro"] = plans.cost(
                                quote["config"],
                                case["usage"]["prompt_tokens"],
                                case["usage"]["completion_tokens"],
                            )
                        if (
                            message.get("tool_calls")
                            or message.get("function_call")
                            or not isinstance(message.get("content"), str)
                            or not message["content"].strip()
                        ):
                            case["state"] = "response_rejected"
                            raise RuntimeError("Model returned tools or empty content")
                        case["reply"] = message["content"]
                        case["state"] = "completed"
                        history = [
                            *messages,
                            {"role": "assistant", "content": message["content"]},
                        ]
                except (httpx.HTTPError, ValueError, KeyError, RuntimeError) as exc:
                    definitely_unsent = isinstance(
                        exc, (httpx.ConnectError, httpx.InvalidURL)
                    )
                    if case["state"] == "dispatching":
                        case["state"] = "unknown"
                    case["error_type"] = type(exc).__name__
                    raise RuntimeError(
                        f"Cost test stopped at {case['case']}; do not replay"
                    ) from None
                finally:
                    if plans:
                        plans.finish(
                            case["request_sha256"],
                            "completed"
                            if case["state"] == "completed"
                            else "not_sent"
                            if definitely_unsent
                            else "unknown",
                        )
                    case["seconds"] = round(time.monotonic() - started, 3)
                    with path.open("w", encoding="utf-8") as handle:
                        json.dump(record, handle, ensure_ascii=False, indent=2)
                        handle.flush()
                        os.fsync(handle.fileno())
                print(
                    json.dumps(
                        {
                            k: case.get(k)
                            for k in (
                                "case",
                                "state",
                                "seconds",
                                "response_model",
                                "usage",
                                "finish_reason",
                            )
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
    finally:
        if relay_receipt and "usage_before" in record:
            try:
                with httpx.Client(
                    timeout=20,
                    trust_env=False,
                    follow_redirects=False,
                    transport=httpx.HTTPTransport(retries=0),
                ) as receipt_client:
                    record["usage_after"] = usage_receipt(
                        receipt_client, base, key, model_name
                    )
                record.update(
                    reconcile_receipts(
                        record["usage_before"], record["usage_after"], record["cases"]
                    )
                )
            except (httpx.HTTPError, ValueError, KeyError, RuntimeError) as exc:
                record["receipt_error_type"] = type(exc).__name__
        with sqlite3.connect(f"file:{store}?mode=ro", uri=True) as db:
            record["public_budget_unchanged"] = (
                budget_before
                == db.execute("SELECT * FROM ai_budget ORDER BY id").fetchall()
            )
            record["public_call_count_unchanged"] = (
                calls_before == db.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
            )
        record["production_config_unchanged"] = config_path.read_bytes() == config_bytes
        record["completed"] = len(record["cases"]) == 4 and all(
            c["state"] == "completed" for c in record["cases"]
        )
        record["finished_utc"] = datetime.now(timezone.utc).isoformat()
        with path.open("w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
    if not all(
        record[field]
        for field in (
            "public_budget_unchanged",
            "public_call_count_unchanged",
            "production_config_unchanged",
        )
    ):
        raise RuntimeError("Protected model or service state changed")
    print(str(path), flush=True)


if __name__ == "__main__":
    main()
