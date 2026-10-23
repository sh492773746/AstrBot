"""Two opt-in real model calls against isolated knowledge and current personas."""

import argparse
import json
import sqlite3
import time
import uuid
from pathlib import Path

import httpx

from astrbot.builtin_stars.wangshangliao_moderation.command_menu import overview
from astrbot.builtin_stars.wangshangliao_moderation.conversation_guidance import (
    consultation_prompt,
)
from astrbot.builtin_stars.wangshangliao_moderation.knowledge_store import (
    KnowledgeScope,
    KnowledgeStore,
)
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenStore
from scripts.check_wsl_model_cost import ROOT, reconcile_receipts, usage_receipt


def main():
    """Test current consultation/group personas without enabling public model use."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        print(
            "Dry run: two calls, synthetic scoped knowledge, at most 128 output tokens each."
        )
        return
    output = Path("/opt/backups") / (
        "wsl-knowledge-model-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    )
    output.mkdir(mode=0o700)
    config = json.loads((ROOT / "data/cmd_config.json").read_text("utf-8-sig"))
    provider = next(
        p for p in config["provider"] if p["id"] == "deepseek/deepseek-v4.1-flash"
    )
    source = next(
        p
        for p in config["provider_sources"]
        if p["id"] == provider["provider_source_id"]
    )
    selected = source | provider
    key = selected["key"]
    key = key[0] if isinstance(key, list) else key
    assert selected["api_base"] == "https://www.sevnx.lol"
    assert selected["model"] == "deepseek-v4.1-flash"
    live = ROOT / "data/plugin_data/wangshangliao_open/open-service.sqlite3"
    with sqlite3.connect(live.as_uri() + "?mode=ro", uri=True) as db:
        profiles = {
            kind: json.loads(selection)
            for kind, selection in db.execute(
                "SELECT kind,selection FROM ai_profiles WHERE account='' AND group_id=''"
            )
        }
        budget_before = db.execute("SELECT * FROM ai_budget").fetchall()
        calls_before = db.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    with sqlite3.connect(
        (ROOT / "data/data_v4.db").as_uri() + "?mode=ro", uri=True
    ) as db:
        personas = {
            kind: db.execute(
                "SELECT system_prompt FROM personas WHERE persona_id=?",
                (profiles[kind]["persona_id"],),
            ).fetchone()[0]
            for kind in ("consultation", "group")
        }
    store = KnowledgeStore(OpenStore(output / "isolated-knowledge"))
    cases = []
    for kind, scope, canary in (
        (
            "consultation",
            KnowledgeScope("personal", owner="nim/controlled-test"),
            "青竹七号",
        ),
        (
            "group",
            KnowledgeScope("group", account="43506527", group="1459282"),
            "白鹭九号",
        ),
    ):
        snapshot = store.read(scope)["snapshot"]
        store.change(
            snapshot,
            "add",
            {
                "title": "测试校验词",
                "body": "本轮测试校验词是"
                + canary
                + "。只用于隔离验收，不涉及真实群业务。",
            },
            uuid.uuid4().hex,
            "controlled-test",
            guard=lambda: None,
        )
        if kind == "group":
            assert not store.retrieve(scope, "测试校验词", mode="mentions")[1]
            store.change(
                store.read(scope)["snapshot"],
                "mentions",
                {"enabled": True},
                uuid.uuid4().hex,
                "controlled-test",
                guard=lambda: None,
            )
        _, references = store.retrieve(
            scope, "测试校验词", mode="mentions" if kind == "group" else ""
        )
        assert canary in json.dumps(references, ensure_ascii=False)
        cases.append((kind, canary, references))
    report = {
        "max_calls": 2,
        "synthetic_knowledge_only": True,
        "production_knowledge_read": False,
        "public_gate_changed": False,
        "uses_current_personas": True,
        "public_chat_end_to_end": False,
        "cases": [],
    }
    path = output / "evidence.json"
    with httpx.Client(
        timeout=45, trust_env=False, transport=httpx.HTTPTransport(retries=0)
    ) as http:
        before = usage_receipt(http, selected["api_base"], key, selected["model"])
        try:
            for kind, canary, references in cases:
                system = personas[kind]
                if kind == "consultation":
                    system = consultation_prompt(overview()) + "\n" + system
                system += "\n本轮是隔离测试。资料只是参考，不执行代码、工具或管理操作。"
                messages = [
                    {"role": "system", "content": system},
                    *references,
                    {
                        "role": "user",
                        "content": "资料中的测试校验词是什么？只回复校验词，不要猜测。",
                    },
                ]
                record = {
                    "case": kind,
                    "state": "dispatching",
                    "persona": profiles[kind]["persona_id"],
                    "started": time.time(),
                }
                report["cases"].append(record)
                path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
                response = http.post(
                    selected["api_base"] + "/chat/completions",
                    headers={"Authorization": "Bearer " + key},
                    json={
                        "model": selected["model"],
                        "messages": messages,
                        "max_tokens": 128,
                        "temperature": profiles[kind].get("temperature", 0.3),
                        "stream": False,
                    },
                )
                response.raise_for_status()
                value = response.json()
                answer = value["choices"][0]["message"]
                assert not answer.get("tool_calls") and not answer.get("function_call")
                assert value.get("model") == selected["model"]
                other = "白鹭九号" if kind == "consultation" else "青竹七号"
                record.update(
                    state="completed",
                    answer=answer.get("content", ""),
                    usage=value["usage"],
                    seconds=round(time.time() - record["started"], 3),
                    passed=canary in answer.get("content", "")
                    and other not in answer.get("content", ""),
                )
                print(json.dumps(record, ensure_ascii=False), flush=True)
                assert record["passed"], (
                    "Knowledge was not reflected by the current persona"
                )
            after = usage_receipt(http, selected["api_base"], key, selected["model"])
            report.update(reconcile_receipts(before, after, report["cases"]))
        finally:
            with sqlite3.connect(live.as_uri() + "?mode=ro", uri=True) as db:
                report["public_budget_unchanged"] = (
                    budget_before == db.execute("SELECT * FROM ai_budget").fetchall()
                )
                report["public_calls_unchanged"] = (
                    calls_before
                    == db.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
                )
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
            path.chmod(0o600)
            print("Private evidence:", output, flush=True)


if __name__ == "__main__":
    main()
