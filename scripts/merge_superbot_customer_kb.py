"""Merge the dedicated customer documents via authenticated local APIs."""

import json
import time
from pathlib import Path

import httpx
import jwt


def main():
    """Back up source documents, verify the merge, and remove the duplicate KB."""
    dashboard = json.loads(
        Path("data/cmd_config.json").read_text(encoding="utf-8-sig")
    )["dashboard"]
    token = jwt.encode(
        {"username": dashboard["username"], "exp": int(time.time()) + 1200},
        dashboard["jwt_secret"],
        algorithm="HS256",
    )
    source = "f19bbe42-8b24-4fb0-9685-60ec410f87fb"
    target = "eceef1f1-0ada-4144-ac49-ac07c1681e37"
    profile_path = "/config-profiles/a6444ee4-7c24-4e59-9ac8-86413228346f"
    backup = Path("/opt/rebo-backups") / f"superbot-kb-merge-{time.time_ns()}"
    backup.mkdir(mode=0o700)
    with httpx.Client(
        base_url="http://127.0.0.1:6185/api/v1",
        headers={"Authorization": "Bearer " + token},
        trust_env=False,
        timeout=90,
    ) as client:

        def api(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            body = response.json()
            if body.get("status") != "ok":
                raise RuntimeError(body.get("message"))
            return body["data"]

        kbs = api("GET", "/knowledge-bases", params={"page_size": 100})["items"]
        target_name = next(k["kb_name"] for k in kbs if k["kb_id"] == target)
        source_name = next(k["kb_name"] for k in kbs if k["kb_id"] == source)
        original = api("GET", profile_path)["config"]
        profiles = api("GET", "/config-profiles")["info_list"]
        for profile in profiles:
            if profile["id"] == profile_path.rsplit("/", 1)[1]:
                continue
            other = api("GET", "/config-profiles/" + profile["id"])["config"]
            if source_name in other.get("kb_names", []):
                raise RuntimeError("Source is referenced by another profile")
        documents = api(
            "GET", f"/knowledge-bases/{source}/documents", params={"page_size": 100}
        )
        assert len(documents["items"]) == documents["total"]
        exported = []
        for document in documents["items"]:
            chunks = []
            page = 1
            while True:
                result = api(
                    "GET",
                    f"/knowledge-bases/{source}/chunks",
                    params={
                        "doc_id": document["doc_id"],
                        "page": page,
                        "page_size": 100,
                    },
                )
                chunks.extend(result["items"])
                if len(chunks) >= result["total"]:
                    break
                page += 1
            exported.append({"document": document, "chunks": chunks})
        for filename, data in (
            ("profile.json", original),
            ("source-export.json", exported),
            ("kb-metadata.json", kbs),
        ):
            file = backup / filename
            file.write_text(json.dumps(data, ensure_ascii=False, indent=2))
            file.chmod(0o600)
        print("Backup:", backup, flush=True)
        pending = []
        existing = api("GET", f"/knowledge-bases/{target}/documents")["items"]
        names = {d["doc_name"] for d in existing}
        for item in exported:
            name = item["document"]["doc_name"]
            if name in names:
                raise RuntimeError("Duplicate document name requires review")
            pending.append(
                {
                    "file_name": name,
                    "file_type": "md",
                    "chunks": [chunk["content"] for chunk in item["chunks"]],
                }
            )
        task = api(
            "POST",
            f"/knowledge-bases/{target}/documents/import",
            json={"documents": pending, "tasks_limit": 1},
        )
        for _ in range(60):
            state = api("GET", "/knowledge-bases/tasks/" + task["task_id"])
            if state["status"] == "failed":
                raise RuntimeError("Import failed; original bindings preserved")
            if state["status"] == "completed":
                assert not state["result"]["failed_count"]
                break
            time.sleep(1)
        else:
            raise RuntimeError("Import pending; original bindings preserved")
        for query, expected in (
            ("奖励币种", "平台彩金"),
            ("大海联系方式", "@dahai855"),
            ("广告取消退款", "退款"),
        ):
            result = api(
                "POST",
                f"/knowledge-bases/{target}/retrieve",
                json={"query": query, "kb_names": [target_name], "top_k": 5},
            )
            assert expected in json.dumps(result, ensure_ascii=False), query
            print("Retrieval verified:", query, flush=True)
        current = api("GET", profile_path)["config"]
        assert current["kb_names"] == original["kb_names"]
        current["kb_names"] = [target_name]
        api("PUT", profile_path, json=current)
        assert api("GET", profile_path)["config"]["kb_names"] == [target_name]
        api("DELETE", f"/knowledge-bases/{source}")
        print("Merged; source removed; single binding:", target_name, flush=True)


if __name__ == "__main__":
    main()
