"""PC28 reads never accept arbitrary destinations or unmetered model continuations."""

import json
import time
from datetime import datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from astrbot.builtin_stars.wangshangliao_moderation import pc28
from astrbot.builtin_stars.wangshangliao_moderation.group_ai import DEFAULT
from astrbot.builtin_stars.wangshangliao_moderation.open_store import (
    OpenError,
    OpenStore,
)
from tests.test_wangshangliao_ai_plans import MODEL, PAYLOAD
from tests.test_wangshangliao_ai_plans import plans as plans
from tests.test_wangshangliao_group_ai import model_scope
from tests.test_wangshangliao_open_rpc_guards import rpc_scope as rpc_scope


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": "exec", "window": 20},
        {"query": "latest", "window": 200},
        {"query": "forecast", "window": "20"},
        {"query": "latest", "window": 20, "url": "https://evil.test"},
        {"query": "latest", "window": 20, "account": "99"},
        {"query": "latest", "window": 20, "group": "5"},
        {"query": "latest", "window": 20, "function": "system"},
        {"query": "forecast"},
    ],
)
def test_restricted_tool_arguments(arguments):
    with pytest.raises(ValidationError):
        pc28.PC28Arguments.model_validate(arguments)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("加拿大28有什么路子", True),
        ("PC28最新开奖", True),
        ("pc 28走势", True),
        ("路子怎么样", False),
        ("北京28最新", False),
        ("28", False),
        ("hello pc280", False),
        ("下午28个人", False),
    ],
)
def test_explicit_object_gate(text, expected):
    assert pc28.explicitly_pc28(text) is expected


def test_context_followup_does_not_cross_unrelated_objects():
    history = [{"role": "user", "content": "加拿大28最新开奖"}]
    assert pc28.explicitly_pc28("下期预测呢", history)
    assert not pc28.explicitly_pc28("北京28下期预测呢", history)
    assert not pc28.explicitly_pc28("随便聊聊", history)
    assert not pc28.explicitly_pc28(
        "走势呢", [*history, {"role": "user", "content": "北京28"}]
    )


def api_http(monkeypatch, case):
    now = time.time()
    local = datetime.fromtimestamp(now - 30, ZoneInfo("Asia/Shanghai"))
    draw = {
        "nbr": 100,
        "time": local.strftime("%Y-%m-%d %H:%M:%S"),
        "n1": 1,
        "n2": 2,
        "n3": 3,
        "sum": 6,
    }
    if case == "stale":
        draw["time"] = datetime.fromtimestamp(
            now - 500, ZoneInfo("Asia/Shanghai")
        ).strftime("%Y-%m-%d %H:%M:%S")
    bodies = {
        "meta": {"resting": case == "resting"},
        "latest": {"draw": draw, "next_nbr": 101},
        "forecast": {
            "target": 100 if case == "wrong_period" else 101,
            "last_nbr": 100,
            "countdown": {
                "next_draw_at": (now + (-10 if case == "expired" else 120)) * 1000
            },
            "algo": {
                "dx": {"pick": "小" if case != "empty" else None, "rate20": 1},
                "ds": {"pick": None},
                "sz": {"pick": None},
                "sha": {"pick": None},
            },
        },
        "history": {"items": [{**draw, "big": False, "odd": False}]},
        "omissions": {"items": {"大": 3}, "sums": {"6": 1}},
        "dragons": {
            "items": [
                {"dim": "dx", "kind": "小", "value": "小", "len": 2, "ongoing": True}
            ]
        },
    }
    requests = []

    class Response:
        status = 429 if case == "429" else 200
        headers = {"Retry-After": "120"}

        def __init__(self, name):
            generated = datetime.fromtimestamp(
                now - (200 if case == "stale_response" else 0),
                ZoneInfo("Asia/Shanghai"),
            ).isoformat()
            self.raw = json.dumps(
                {
                    "ok": True,
                    "data": bodies[name],
                    "meta": {"source": "pc28.world", "generated_at": generated},
                }
            ).encode()
            self.content = self

        async def __aenter__(self):
            if case == "timeout":
                raise TimeoutError()
            return self

        async def __aexit__(self, *args):
            return False

        async def iter_chunked(self, size):
            yield self.raw

    class Session:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def get(self, url, **kwargs):
            name = next(
                name
                for name, path in pc28.PATHS.items()
                if url == "https://pc28.world" + path
            )
            requests.append((url, kwargs))
            return Response(name)

    monkeypatch.setattr(pc28.aiohttp, "ClientSession", Session)
    return requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("ok", ""),
        ("empty", "empty_forecast"),
        ("expired", "expired_forecast"),
        ("wrong_period", "expired_forecast"),
        ("stale", "stale_draw"),
        ("resting", "resting"),
        ("429", "rate_limited"),
        ("timeout", "timeout"),
        ("stale_response", "api_stale"),
    ],
)
async def test_forecasts_are_checked_and_never_fabricated(
    tmp_path, monkeypatch, case, reason
):
    requests = api_http(monkeypatch, case)
    tool = pc28.PC28Analysis(OpenStore(tmp_path))
    result = await tool.query({"query": "forecast", "window": 20}, guard=lambda: None)
    assert result["reason"] == reason
    assert "rate20" not in json.dumps(result)
    assert len(requests) <= 3
    assert all(kwargs["allow_redirects"] is False for _, kwargs in requests)
    if case == "ok":
        assert result["data"]["picks"] == {"dx": "小"}
    else:
        assert not result["data"].get("picks")
        answer = tool.fallback(result)
        assert pc28.NOTICE in answer
        assert "pc28.world" not in answer
        assert "https://" not in answer
    assert result["source"] == pc28.SOURCE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query", ["latest", "history", "omissions", "dragons", "forecast"]
)
async def test_fixed_get_paths_and_shared_cache(tmp_path, monkeypatch, query):
    requests = api_http(monkeypatch, "ok")
    tool = pc28.PC28Analysis(OpenStore(tmp_path))
    first = await tool.query({"query": query, "window": 20}, guard=lambda: None)
    count = len(requests)
    second = await tool.query({"query": query, "window": 20}, guard=lambda: None)
    assert first["status"] == second["status"] == "ok"
    assert len(requests) == count
    assert all(url.startswith("https://pc28.world/api/v1/") for url, _ in requests)
    if query == "history":
        assert requests[-1][1]["params"] == {"limit": "20"}


@pytest.mark.asyncio
async def test_rate_limit_survives_tool_reinitialization(tmp_path, monkeypatch):
    requests = api_http(monkeypatch, "ok")
    store = OpenStore(tmp_path)
    tool = pc28.PC28Analysis(store)
    with store.connect(write=True) as db:
        db.executemany(
            "INSERT INTO pc28_requests VALUES(?)", [(time.time() - 10,)] * 60
        )
    tool = pc28.PC28Analysis(store)
    result = await tool.query({"query": "latest", "window": 20}, guard=lambda: None)
    assert result["reason"] == "rate_limited"
    assert not requests


def prepare_parent(plans):
    row = plans.store.tenant("99", "5")
    with plans.store.connect(write=True) as db:
        db.execute(
            "INSERT INTO group_ai VALUES('99','5',1,?,'',0,'')",
            (json.dumps({**DEFAULT, "enabled": True}),),
        )
    state = plans.snapshot("nim/1001", "1")
    plans.bind(
        "99",
        "5",
        "1",
        "nim/1001",
        0,
        row["version"],
        state["version"],
        "1",
        guard=lambda: None,
    )
    quote = plans.quote(MODEL, PAYLOAD)
    plans.reserve(
        "nim/1001", "1", "parent", quote, "99", "5", payer_version=1, group_version=1
    )
    plans.dispatched("parent", quote, PAYLOAD)
    plans.settle("parent", quote, {"prompt_tokens": 100, "completion_tokens": 30})
    return quote


def test_one_continuation_is_separately_counted_and_billed(plans):
    quote = prepare_parent(plans)
    with pytest.raises(OpenError, match="cooldown"):
        plans.reserve(
            "nim/1001",
            "1",
            "unrelated",
            quote,
            "99",
            "5",
            payer_version=1,
            group_version=1,
        )
    plans.reserve(
        "nim/1001",
        "1",
        "parent/tool-answer",
        quote,
        "99",
        "5",
        payer_version=1,
        group_version=1,
        continuation="parent",
    )
    plans.dispatched("parent/tool-answer", quote, PAYLOAD)
    plans.settle(
        "parent/tool-answer", quote, {"prompt_tokens": 200, "completion_tokens": 40}
    )
    assert plans.snapshot("nim/1001", "1")["used"] == 2
    with plans.store.connect() as db:
        rows = list(db.execute("SELECT * FROM ai_charges"))
    assert len(rows) == 2 and all(row["charged_micro"] > 0 for row in rows)
    with pytest.raises(OpenError, match="continuation_invalid"):
        plans.reserve(
            "nim/1001",
            "1",
            "parent/tool-answer/tool-answer",
            quote,
            "99",
            "5",
            payer_version=1,
            group_version=1,
            continuation="parent/tool-answer",
        )


@pytest.mark.parametrize(
    "case", ["quota", "revoked", "wrong_account", "expired", "wrong_name"]
)
def test_continuation_never_bypasses_budget_quota_or_authority(plans, case):
    quote = prepare_parent(plans)
    account, request = "99", "parent/tool-answer"
    if case == "quota":
        settings = plans.store.settings()
        plans.store.configure(settings["version"], False, 20, 1, "test")
    elif case == "revoked":
        with plans.store.connect(write=True) as db:
            db.execute("UPDATE ai_payers SET confirmed=0")
    elif case == "expired":
        with plans.store.connect(write=True) as db:
            db.execute("UPDATE ai_charges SET created=?", (time.time() - 46,))
    elif case == "wrong_account":
        account = "88"
    elif case == "wrong_name":
        request = "random"
    with pytest.raises(OpenError):
        plans.reserve(
            "nim/1001",
            "1",
            request,
            quote,
            account,
            "5",
            payer_version=1,
            group_version=1,
            continuation="parent",
        )
    assert plans.snapshot("nim/1001", "1")["used"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "ok",
        "quota",
        "empty",
        "forged",
        "multiple",
        "wrong_name",
        "invalid_id",
        "dict_arguments",
        "ordinary",
        "idle",
        "revoked",
        "circuit",
        "ignored",
        "truncated",
        "reasoning_echo",
        "history_summary",
        "dragon_summary",
        "source_links",
        "autonomous_source_links",
        "long_answer",
    ],
)
async def test_group_tool_turn_has_at_most_two_independently_billed_calls(
    rpc_scope, monkeypatch, caplog, case
):
    from astrbot.builtin_stars.wangshangliao_moderation import group_ai

    value, adapter, _ = rpc_scope
    row = await model_scope(value, adapter)
    context = ()
    if case == "autonomous_source_links":
        from tests.test_wangshangliao_autonomous_chat import batch

        row["config"]["autonomous"] = True
        adapter.quote_message = AsyncMock(return_value={})
        context = ({**batch()[0], "text": "加拿大28下期路子"},)
    requests = []
    result = {
        "query": "forecast",
        "status": "ok",
        "source": pc28.SOURCE,
        "reason": "empty_forecast" if case == "empty" else "",
        "latest": {
            "nbr": 100,
            "time": "2026-10-20 12:00:00",
            "n1": 1,
            "n2": 2,
            "n3": 3,
            "sum": 6,
        },
        "data": {"target": 101, "picks": {} if case == "empty" else {"dx": "小"}},
    }
    if case == "history_summary":
        result.update(
            query="history",
            data={"items": [{"nbr": 100, "sum": 6}, {"nbr": 99, "sum": 15}]},
        )
    if case == "dragon_summary":
        result.update(
            query="dragons",
            data={
                "items": [
                    {
                        "dim": "大小",
                        "type": "周期",
                        "count": 6,
                        "status": "进行中",
                        "chunks": [["小", "小", "大"], ["小", "小", "大"]],
                        "expect": "小",
                    }
                ]
            },
        )

    async def query(arguments, *, guard):
        pc28.PC28Arguments.model_validate(arguments)
        guard()
        if case == "revoked":
            with value.store.connect(write=True) as db:
                db.execute("UPDATE ai_payers SET confirmed=0")
        return result

    value.pc28.query = AsyncMock(side_effect=query)
    if case == "circuit":
        original_run = value.governance.run

        async def governed(name, adapter, operation, **kwargs):
            if name == "pc28":
                raise OpenError("component_circuit_open")
            return await original_run(name, adapter, operation, **kwargs)

        monkeypatch.setattr(value.governance, "run", governed)
    if case == "quota":
        s = value.store.settings()
        value.store.configure(s["version"], False, 20, 1, "test")
        value.settings = value.store.settings()

    class Response:
        status = 200

        def __init__(self, number):
            self.content = self
            self.number = number

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def iter_chunked(self, size):
            arguments = {"query": result["query"], "window": 20}
            if case == "forged":
                arguments["url"] = "https://evil.test"
            message = (
                {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "tool1",
                            "type": "function",
                            "function": {
                                "name": "pc28_analysis",
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                }
                if self.number == 1
                else {"content": "第三方资料仅供参考，不保证下期结果。"}
            )
            if self.number == 1:
                call = message["tool_calls"][0]
                if case == "multiple":
                    message["tool_calls"].append({**call, "id": "second-request"})
                elif case == "wrong_name":
                    call["function"]["name"] = "unregistered"
                elif case == "invalid_id":
                    call["id"] = None
                elif case == "dict_arguments":
                    call["function"]["arguments"] = arguments
            if case == "ignored":
                message = {"content": "下期必中999999，已经预测成功。"}
            if self.number == 2 and case in {"truncated", "reasoning_echo"}:
                message = {"content": "未完成的分析草稿999999"}
                if case == "reasoning_echo":
                    message["reasoning_content"] = message["content"]
            if self.number == 2 and case in {
                "source_links",
                "autonomous_source_links",
            }:
                message = {
                    "content": "目标期号101，仅供参考。"
                    "\n来源：https://pc28.world/api/v1/forecast，开奖随机。"
                    "\n[查询详情](https://pc28.world/apidoc)"
                    "\n<HTTPS://PC28.WORLD> pc28.world www.pc28.world"
                    "\nhttps://example.test/source\n" + pc28.SOURCE
                }
                if context:
                    message["content"] = json.dumps(
                        {
                            "reply": True,
                            "target": 1,
                            "quote": False,
                            "text": message["content"],
                        },
                        ensure_ascii=False,
                    )
            if self.number == 2 and case == "long_answer":
                message = {"content": "历史参考。" * 400 + pc28.SOURCE}
            yield json.dumps(
                {
                    "choices": [
                        {
                            "message": message,
                            "finish_reason": "length"
                            if self.number == 2 and case == "truncated"
                            else "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 50,
                        "total_tokens": 150,
                    },
                }
            ).encode()

    class HTTP:
        def __init__(self, **kwargs):
            assert 0 < kwargs["timeout"].total <= 45

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, **kwargs):
            requests.append(kwargs["json"])
            return Response(len(requests))

    monkeypatch.setattr(group_ai.aiohttp, "ClientSession", HTTP)
    trigger = f"idle/{time.time()}" if case == "idle" else "mention/test/2/1002"
    question = "普通闲聊" if case == "ordinary" else "加拿大28下期路子"
    await value.group_ai.run(
        adapter,
        dict(row),
        "auto/links" if context else trigger,
        "" if context else question,
        context=context,
    )
    expected_calls = (
        2
        if case
        in {
            "ok",
            "truncated",
            "reasoning_echo",
            "source_links",
            "autonomous_source_links",
            "long_answer",
        }
        else 1
    )
    assert len(requests) == expected_calls
    assert value.store.usage()["used"] == expected_calls
    assert ("tools" in requests[0]) is (case not in {"ordinary", "idle"})
    if case not in {"ordinary", "idle"}:
        assert requests[0]["tool_choice"] == {
            "type": "function",
            "function": {"name": "pc28_analysis"},
        }
        assert any(
            m["role"] == "system" and m["content"] == pc28.TOOL_PROMPT
            for m in requests[0]["messages"]
        )
        assert "必须先调用该工具" in json.dumps(
            requests[0]["messages"], ensure_ascii=False
        )
    if expected_calls == 2:
        assert not {"tools", "tool_choice", "parallel_tool_calls"} & requests[1].keys()
        assert any(m["role"] == "tool" for m in requests[1]["messages"])
        assert any(
            pc28.SOURCE in m["content"]
            for m in requests[1]["messages"]
            if m["role"] == "tool"
        )
    invalid_calls = {"forged", "multiple", "wrong_name", "invalid_id", "dict_arguments"}
    if case in invalid_calls | {"ordinary", "idle", "revoked"}:
        adapter.send_text.assert_not_awaited()
    else:
        answer = adapter.send_text.call_args.args[2]
        assert answer.endswith(pc28.NOTICE)
        assert "pc28.world" not in answer.lower()
        assert "http://" not in answer.lower()
        assert "https://" not in answer.lower()
        assert len(answer) <= 1600
    if case in {"source_links", "autonomous_source_links"}:
        answer = adapter.send_text.call_args.args[2]
        assert "目标期号101" in answer
        assert "开奖随机" in answer
        assert "example.test" not in answer
        assert "()" not in answer
    if case == "circuit":
        value.pc28.query.assert_not_awaited()
        assert "熔断" in adapter.send_text.call_args.args[2]
    if case == "ignored":
        value.pc28.query.assert_not_awaited()
        assert "999999" not in adapter.send_text.call_args.args[2]
        assert "未完成数据查询" in adapter.send_text.call_args.args[2]
    if case in invalid_calls:
        value.pc28.query.assert_not_awaited()
        assert (
            "validation=arguments" in caplog.text
            if case == "forged"
            else ("validation=envelope" in caplog.text)
        )
        assert "https://evil.test" not in caplog.text
    if case in {"truncated", "reasoning_echo"}:
        answer = adapter.send_text.call_args.args[2]
        assert "999999" not in answer
        assert "目标期号：101" in answer
        with value.store.connect() as db:
            charges = list(db.execute("SELECT state FROM ai_charges"))
        assert len(charges) == 2
        assert all(row["state"] == "settled" for row in charges)
    if case == "history_summary":
        assert "大1、小1；单1、双1" in adapter.send_text.call_args.args[2]
    if case == "dragon_summary":
        answer = adapter.send_text.call_args.args[2]
        assert "大小/周期 6期" in answer
        assert "小连6" not in answer


def test_history_fallback_counts_verified_draws_without_partial_json():
    result = {
        "query": "history",
        "status": "ok",
        "data": {
            "items": [
                {"nbr": 101, "sum": 15},
                {"nbr": 100, "sum": 10},
                {"nbr": 99, "sum": 13},
            ]
        },
    }
    text = pc28.PC28Analysis.fallback(result)
    assert "已核验3期（99—101）" in text
    assert "大1、小2；单2、双1" in text
    assert '"items"' not in text
    assert "不保证下期结果" in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "加拿大28能保证赢吗？",
        "刚才的下期预测输了是不是该借钱翻本？",
    ],
)
async def test_risk_questions_do_not_require_a_live_data_lookup(
    rpc_scope, monkeypatch, question
):
    from tests.test_wangshangliao_group_ai import fake_http

    value, adapter, _ = rpc_scope
    row = await model_scope(value, adapter)
    requests = fake_http(monkeypatch, content="不能保证赢，不要借钱翻本。")
    value.pc28.query = AsyncMock()
    await value.group_ai.run(adapter, dict(row), "mention/risk/2/1002", question)
    assert len(requests) == 1
    assert "tools" not in requests[0][1]["json"]
    value.pc28.query.assert_not_awaited()
    assert "不要借钱翻本" in adapter.send_text.call_args.args[2]
