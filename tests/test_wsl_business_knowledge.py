import copy

import pytest

from scripts.provision_wsl_business_knowledge import (
    TARGET_NAME,
    checked_binding,
    load_documents,
    pending_documents,
    verify_documents,
)


def exported(documents):
    return [
        {
            "document": {"doc_name": item["file_name"]},
            "chunks": [{"content": chunk} for chunk in reversed(item["chunks"])],
        }
        for item in documents
    ]


def test_reviewed_business_chunks_keep_provenance_and_original_links():
    documents = load_documents()
    assert len(documents) == 4
    chunks = [chunk for item in documents for chunk in item["chunks"]]
    assert len(chunks) == 16
    assert all(chunk == chunk.strip() for chunk in chunks)
    assert all("2026-09-23" in chunk and "2026-10-02" in chunk for chunk in chunks)
    text = "\n".join(chunks)
    for value in (
        "@dahai855", "@w41890", "@Aclee888", "@example_owner", "@yuan",
        "大海传媒.cc", "大海团队.cc", "tv28.cc", "rb666.vip", "433336",
        "userId=68475611160680765&guildId=444",
        "pt=CA1DACFE-3620-D314-75B6-D252D4F4E99B",
        "| 100 | 5万 | 388 | 388 |", "| 1000 | 50万 | 3888 | 388 |",
        "平台彩金", "不是旺商聊拉群奖励", "没有独立会议", "未提供",
    ):
        assert value in text
    for excluded in ("/start", "/link", "/群管", "扫雷", "签到", "下注", "轮盘"):
        assert excluded not in text


def test_only_kb_binding_changes_and_input_is_not_mutated():
    original = {"kb_names": ["source"], "provider_settings": {"persona": "same"}}
    snapshot = copy.deepcopy(original)
    updated = checked_binding(original, snapshot)
    assert original == snapshot
    assert updated == {**snapshot, "kb_names": [TARGET_NAME]}
    updated["provider_settings"]["persona"] = "changed"
    assert original == snapshot


def test_concurrent_profile_edit_rejected():
    with pytest.raises(RuntimeError, match="Profile changed"):
        checked_binding({"kb_names": ["new"]}, {"kb_names": ["source"]})


def test_complete_import_is_idempotent_and_ignores_chunk_order():
    documents = load_documents()
    verify_documents(exported(documents), documents)
    assert pending_documents(exported(documents), documents) == []
    assert pending_documents(exported(documents[:1]), documents) == documents[1:]


def test_unreviewed_document_rejected():
    documents = load_documents()
    existing = exported(documents)
    existing[0]["document"]["doc_name"] = "unreviewed-gameplay.md"
    with pytest.raises(RuntimeError, match="unreviewed"):
        pending_documents(existing, documents)


def test_changed_chunk_is_not_silently_overwritten():
    documents = load_documents()
    existing = exported(documents[:1])
    existing[0]["chunks"][0]["content"] = "different"
    with pytest.raises(RuntimeError, match="differs"):
        pending_documents(existing, documents)


def test_incomplete_and_duplicate_imports_rejected():
    documents = load_documents()
    with pytest.raises(RuntimeError, match="differs"):
        verify_documents(exported(documents[:1]), documents)
    existing = exported(documents)
    with pytest.raises(RuntimeError, match="Duplicate"):
        verify_documents(existing + existing[:1], documents)


def test_empty_or_unsectioned_document_rejected(tmp_path):
    with pytest.raises(ValueError, match="No reviewed"):
        load_documents(tmp_path)
    (tmp_path / "invalid.md").write_text("# Missing sections", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid reviewed"):
        load_documents(tmp_path)
