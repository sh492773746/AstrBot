"""Import reviewed WSL knowledge through the authenticated local dashboard API."""

import copy
import json
import time
from pathlib import Path

import jwt
import requests


def main():
    """Back up the profile, test retrieval, and bind reviewed knowledge."""
    root = Path("data/wsl_knowledge/2026-09-21")
    plan = json.loads((root / "binding-plan.json").read_text())
    config = json.loads(Path("data/cmd_config.json").read_text(encoding="utf-8-sig"))
    token = jwt.encode(
        {"username": config["dashboard"]["username"], "exp": int(time.time()) + 1800},
        config["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )
    client = requests.Session()
    client.headers["Authorization"] = "Bearer " + token

    def api(method, path, **kwargs):
        response = client.request(
            method, "http://127.0.0.1:6185/api/v1" + path, timeout=120, **kwargs
        )
        response.raise_for_status()
        body = response.json()
        if body.get("status") != "ok":
            raise RuntimeError(str(body.get("message")))
        return body["data"]

    profile_path = "/config-profiles/" + plan["profile_id"]
    original = api("GET", profile_path)["config"]
    backup = Path("/opt/rebo-backups") / f"wsl-knowledge-{time.time_ns()}"
    backup.mkdir(mode=0o700)
    backup_file = backup / "profile.json"
    backup_file.write_text(json.dumps(original, ensure_ascii=False, indent=2))
    backup_file.chmod(0o600)
    kbs = api("GET", "/knowledge-bases", params={"page_size": 100})["items"]
    kb = next((k for k in kbs if k["kb_name"] == plan["knowledge_name"]), None)
    if kb is None:
        kb = api(
            "POST",
            "/knowledge-bases",
            json={
                "name": plan["knowledge_name"],
                "description": "经审核的旺商聊共享知识：历史直播访问、活动核实、用语和管理入口；非实时公告。",
                "embedding_provider_id": plan["embedding_provider_id"],
                "chunk_size": 700,
                "chunk_overlap": 80,
            },
        )
    kb_id = kb["kb_id"]
    documents = api("GET", f"/knowledge-bases/{kb_id}/documents")["items"]
    existing = {d["doc_name"] for d in documents}
    pending = []
    for file in sorted((root / "knowledge").glob("*.md")):
        if file.name in existing:
            continue
        title, *sections = file.read_text().split("\n## ")
        pending.append(
            {
                "file_name": file.name,
                "file_type": "md",
                "chunks": [title + "\n\n## " + section for section in sections],
            }
        )
    if pending:
        task = api(
            "POST",
            f"/knowledge-bases/{kb_id}/documents/import",
            json={"documents": pending, "tasks_limit": 1},
        )
        print("Import started", task["task_id"], flush=True)
        for _ in range(120):
            state = api("GET", "/knowledge-bases/tasks/" + task["task_id"])
            if state["status"] == "failed":
                raise RuntimeError("Knowledge import failed")
            if state["status"] == "completed":
                if state["result"].get("failed_count"):
                    raise RuntimeError("Some documents failed; profile unchanged")
                print(
                    "Imported documents:", state["result"]["success_count"], flush=True
                )
                break
            time.sleep(2)
        else:
            raise RuntimeError("Import timed out; profile unchanged")
    checks = []
    for query, expected in [
        ("直播几点开始", "8 点"),
        ("找不到直播间换手机看不到怎么办", "浏览器"),
        ("送猪脚饭是真的吗", "猪脚饭"),
        ("怎么使用群管帮助", "/群管帮助"),
    ]:
        result = api(
            "POST",
            f"/knowledge-bases/{kb_id}/retrieve",
            json={"query": query, "kb_names": [plan["knowledge_name"]], "top_k": 3},
        )
        passed = bool(result["total"]) and expected in json.dumps(
            result["results"], ensure_ascii=False
        )
        checks.append({"query": query, "passed": passed, "hits": result["total"]})
        print(checks[-1], flush=True)
        if not passed:
            raise RuntimeError("Retrieval acceptance failed; profile unchanged")
    # Only bind the knowledge requested here; preserve persona and other settings.
    current = api("GET", profile_path)["config"]
    updated = copy.deepcopy(current)
    updated["kb_names"] = [plan["knowledge_name"]]
    api("PUT", profile_path, json=updated)
    actual = api("GET", profile_path)["config"]
    assert actual["kb_names"] == [plan["knowledge_name"]]
    untouched = copy.deepcopy(actual)
    untouched["kb_names"] = current.get("kb_names")
    assert untouched == current, "Unexpected configuration changes"
    report = {
        "status": "knowledge_imported_bound_tested",
        "kb_id": kb_id,
        "kb_name": plan["knowledge_name"],
        "profile_id": plan["profile_id"],
        "backup": str(backup),
        "tests": checks,
        "persona_changed": False,
        "live_group_messages_sent": 0,
    }
    (root / "import-result.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
