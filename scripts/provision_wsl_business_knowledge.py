"""Provision a reviewed business KB without changing the source or persona.

Run from the repository root. Preview is read-only; --apply imports and binds.
"""

from __future__ import annotations

import argparse
import copy
import json
import time
from pathlib import Path

import httpx
import jwt

ROOT = Path(__file__).resolve().parents[1]
CONTENT = ROOT / "docs/knowledge/wangshangliao/content"
SOURCE_ID = "eceef1f1-0ada-4144-ac49-ac07c1681e37"
SOURCE_NAME = "大海传媒超级机器人-8b5578bc81a0-使用知识"
PROFILE_ID = "cba0c0a2-f0f6-4edb-b7e1-1d656fc9a7f1"
PROFILE_NAME = "旺商聊群管配置文件"
TARGET_NAME = "旺商聊·大海传媒业务客服知识"
SOURCE_DOCUMENTS = {
    "客服补充-直播会议与机器人制作-自然表达修订.md",
    "客服资料-20260923-彩金修订.md",
}
RETRIEVAL_CHECKS = (
    ("大海传媒导航是什么", "大海传媒.cc"),
    ("怎么联系大海和简单", "@dahai855"),
    ("直播网页入口在哪里", "userId=68475611160680765&guildId=444"),
    ("大海传媒几点开播", "每晚8点"),
    ("机器人制作找谁", "@example_owner"),
    ("茶水和代言费是什么币种", "平台彩金"),
    ("100个直属充值门槛和茶水奖励", "| 100 | 5万 | 388 | 388 |"),
    ("机器人能查充值流水确认达标吗", "人工"),
    ("会议号和会议密码是什么", "没有独立会议"),
    ("大海兼职群有哪些岗位和薪资", "未提供"),
)


def load_documents(directory: Path = CONTENT) -> list[dict]:
    documents = []
    for path in sorted(directory.glob("*.md")):
        text = path.read_text(encoding="utf-8").strip()
        header, *sections = text.split("\n## ")
        if (
            not header.startswith("# ")
            or not sections
            or any(not section.strip() for section in sections)
        ):
            raise ValueError(f"Invalid reviewed document: {path.name}")
        documents.append(
            {
                "file_name": path.name,
                "file_type": "md",
                "chunks": [
                    (header + "\n\n## " + section).strip() for section in sections
                ],
            }
        )
    if not documents:
        raise ValueError("No reviewed content")
    return documents


def checked_binding(current: dict, original: dict) -> dict:
    if current != original:
        raise RuntimeError("Profile changed during import; binding not written")
    updated = copy.deepcopy(current)
    updated["kb_names"] = [TARGET_NAME]
    return updated


def verify_documents(exported: list[dict], expected: list[dict]) -> None:
    actual = {}
    for item in exported:
        name = item["document"]["doc_name"]
        if name in actual:
            raise RuntimeError("Duplicate imported document")
        actual[name] = sorted(chunk["content"] for chunk in item["chunks"])
    desired = {item["file_name"]: sorted(item["chunks"]) for item in expected}
    if actual != desired:
        raise RuntimeError("Imported content differs from reviewed documents")


def pending_documents(existing: list[dict], expected: list[dict]) -> list[dict]:
    names = {item["document"]["doc_name"] for item in existing}
    desired_names = {item["file_name"] for item in expected}
    if not names <= desired_names:
        raise RuntimeError("Existing target contains unreviewed documents")
    verify_documents(
        existing, [item for item in expected if item["file_name"] in names]
    )
    return [item for item in expected if item["file_name"] not in names]


class API:
    def __init__(self, client: httpx.Client):
        self.client = client

    def call(self, method: str, path: str, **kwargs):
        response = self.client.request(method, path, **kwargs)
        response.raise_for_status()
        body = response.json()
        if body.get("status") != "ok":
            raise RuntimeError(body.get("message", "API request failed"))
        return body["data"]

    def items(self, path: str, **params):
        items = []
        for page in range(1, 1001):
            result = self.call(
                "GET", path, params={**params, "page": page, "page_size": 100}
            )
            items.extend(result["items"])
            if len(items) >= result["total"]:
                return items
            if not result["items"]:
                raise RuntimeError("Incomplete API pagination")
        raise RuntimeError("API pagination limit exceeded")

    def export(self, kb_id: str):
        return [
            {
                "document": document,
                "chunks": self.items(
                    f"/knowledge-bases/{kb_id}/chunks", doc_id=document["doc_id"]
                ),
            }
            for document in self.items(f"/knowledge-bases/{kb_id}/documents")
        ]

    def profiles(self):
        infos = self.call("GET", "/config-profiles")["info_list"]
        info = next((item for item in infos if item["id"] == PROFILE_ID), None)
        if not info or info["name"] != PROFILE_NAME:
            raise RuntimeError("Target profile identity changed")
        return {
            item["id"]: self.call("GET", "/config-profiles/" + item["id"])["config"]
            for item in infos
        }


def backup_json(directory: Path, name: str, value):
    path = directory / name
    # Restrict permissions before writing potentially sensitive configurations.
    with path.open("x", encoding="utf-8") as handle:
        path.chmod(0o600)
        json.dump(value, handle, ensure_ascii=False, indent=2)


def provision(api: API, *, apply: bool):
    documents = load_documents()
    kbs = api.items("/knowledge-bases")
    source = next((item for item in kbs if item["kb_id"] == SOURCE_ID), None)
    if not source or source["kb_name"] != SOURCE_NAME:
        raise RuntimeError("Source knowledge identity changed")
    source_export = api.export(SOURCE_ID)
    if not SOURCE_DOCUMENTS <= {item["document"]["doc_name"] for item in source_export}:
        raise RuntimeError("Source business documents missing")
    profiles = api.profiles()
    original = profiles[PROFILE_ID]
    if original.get("kb_names") not in ([SOURCE_NAME], [TARGET_NAME]):
        raise RuntimeError("Unexpected existing knowledge binding; review required")
    print(
        json.dumps(
            {
                "mode": "apply" if apply else "preview",
                "source": SOURCE_NAME,
                "target": TARGET_NAME,
                "profile": PROFILE_NAME,
                "documents": len(documents),
                "chunks": sum(len(item["chunks"]) for item in documents),
                "current_binding": original.get("kb_names"),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    if not apply:
        return
    matches = [item for item in kbs if item["kb_name"] == TARGET_NAME]
    if len(matches) > 1:
        raise RuntimeError("Ambiguous target knowledge name")
    kb = matches[0] if matches else None
    if kb and any(
        TARGET_NAME in profile.get("kb_names", [])
        for key, profile in profiles.items()
        if key != PROFILE_ID
    ):
        raise RuntimeError("Target is used by another profile; review required")
    backup = Path("/opt/rebo-backups") / f"wsl-business-kb-{time.time_ns()}"
    backup.mkdir(mode=0o700, parents=True)
    backup_json(backup, "profiles.json", profiles)
    backup_json(backup, "source-export.json", source_export)
    backup_json(backup, "kb-metadata.json", kbs)
    existing = api.export(kb["kb_id"]) if kb else []
    backup_json(backup, "target-export.json", existing)
    pending = pending_documents(existing, documents)
    if not kb:
        settings = {
            key: source[key]
            for key in (
                "embedding_provider_id",
                "rerank_provider_id",
                "chunk_size",
                "chunk_overlap",
                "top_k_dense",
                "top_k_sparse",
                "top_m_final",
            )
            if key in source
        }
        kb = api.call(
            "POST",
            "/knowledge-bases",
            json={
                **settings,
                "name": TARGET_NAME,
                "description": (
                    "大海传媒业务客服：导航、公开联系人、直播、机器人制作及"
                    "代理奖励人工核验；来源2026-09-23，非实时公告，不含功能玩法。"
                ),
            },
        )
    kb_id = kb["kb_id"]
    if pending:
        task = api.call(
            "POST",
            f"/knowledge-bases/{kb_id}/documents/import",
            json={"documents": pending, "tasks_limit": 1},
        )
        print("Import task:", task["task_id"], flush=True)
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            state = api.call("GET", "/knowledge-bases/tasks/" + task["task_id"])
            if state["status"] == "failed":
                raise RuntimeError("Import failed; original binding preserved")
            if state["status"] == "completed":
                result = state["result"]
                if result.get("failed_count") or result.get("success_count") != len(
                    pending
                ):
                    raise RuntimeError("Incomplete import; original binding preserved")
                break
            time.sleep(2)
        else:
            raise RuntimeError("Import timed out; original binding preserved")
    verify_documents(api.export(kb_id), documents)
    checks = []
    for query, expected in RETRIEVAL_CHECKS:
        result = api.call(
            "POST",
            f"/knowledge-bases/{kb_id}/retrieve",
            json={"query": query, "kb_names": [TARGET_NAME], "top_k": 5},
        )
        passed = bool(result.get("total")) and expected in json.dumps(
            result.get("results"), ensure_ascii=False
        )
        checks.append({"query": query, "passed": passed})
        print(json.dumps(checks[-1], ensure_ascii=False), flush=True)
        if not passed:
            raise RuntimeError("Retrieval failed; original binding preserved")
    if api.export(SOURCE_ID) != source_export:
        raise RuntimeError("Source changed during import; binding not written")
    latest = api.profiles()
    if latest != profiles:
        raise RuntimeError("Configuration changed during import; binding not written")
    updated = checked_binding(latest[PROFILE_ID], original)
    if updated != original:
        api.call("PUT", "/config-profiles/" + PROFILE_ID, json=updated)
    actual = api.profiles()
    expected_profiles = {**profiles, PROFILE_ID: updated}
    if actual != expected_profiles:
        raise RuntimeError(
            "Post-binding configuration verification failed; inspect backup"
        )
    if api.export(SOURCE_ID) != source_export:
        raise RuntimeError("Post-binding source verification failed; inspect backup")
    report = {
        "status": "imported_bound_verified",
        "kb_id": kb_id,
        "kb_name": TARGET_NAME,
        "profile": PROFILE_NAME,
        "documents": len(documents),
        "chunks": sum(len(item["chunks"]) for item in documents),
        "retrieval_checks": checks,
        "source_unchanged": True,
        "other_profiles_unchanged": True,
        "persona_tools_skills_changed": False,
        "messages_sent": 0,
        "backup": str(backup),
    }
    backup_json(backup, "acceptance.json", report)
    print(json.dumps(report, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    dashboard = json.loads(
        (ROOT / "data/cmd_config.json").read_text(encoding="utf-8-sig")
    )["dashboard"]
    token = jwt.encode(
        {"username": dashboard["username"], "exp": int(time.time()) + 1800},
        dashboard["jwt_secret"],
        algorithm="HS256",
    )
    with httpx.Client(
        base_url="http://127.0.0.1:6185/api/v1",
        headers={"Authorization": "Bearer " + token},
        trust_env=False,
        timeout=90,
    ) as client:
        provision(API(client), apply=args.apply)


if __name__ == "__main__":
    main()
