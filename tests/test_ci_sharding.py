"""Complete-suite sharding must never hide or duplicate collected cases."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.ci_pytest import ModuleShard
from scripts.verify_ci_shards import verify


def test_large_parameter_ids_are_bounded_stable_and_keep_distinct_inputs(request):
    hook = request.config.hook.pytest_make_parametrize_id
    labels = []
    for value in (b"x" * 1_048_577, "x" * 100_000, b"y" * 1_048_577):
        label = hook(config=request.config, val=value, argname="body")
        assert len(label) < 80
        assert label == hook(config=request.config, val=value, argname="body")
        labels.append(label)
    assert len(set(labels)) == 3
    assert hook(config=request.config, val="short", argname="body") is None


def test_module_shards_are_disjoint_complete_and_order_independent(tmp_path):
    items = [
        SimpleNamespace(path=Path(f"tests/{module}.py"), nodeid=f"{module}::{index}")
        for module, size in (("a", 7), ("b", 5), ("c", 4), ("d", 3), ("e", 2))
        for index in range(size)
    ]
    assignments = {}
    for shard in range(4):
        evidence = tmp_path / f"collection-{shard}.json"
        plugin = ModuleShard(shard, 4, evidence)
        selected = items.copy()
        config = SimpleNamespace(hook=SimpleNamespace(pytest_deselected=Mock()))
        plugin.pytest_collection_modifyitems(config, selected)
        for item in selected:
            assert assignments.setdefault(str(item.path), shard) == shard
        reversed_items = list(reversed(items))
        plugin.pytest_collection_modifyitems(config, reversed_items)
        assert {item.nodeid for item in reversed_items} == {
            item.nodeid for item in selected
        }
    assert verify(tmp_path) == len(items)
    (tmp_path / "collection-1.json").unlink()
    with pytest.raises(ValueError, match="Missing or duplicate"):
        verify(tmp_path)


@pytest.mark.parametrize("index,count", [(-1, 4), (4, 4), (0, 0)])
def test_invalid_shard_parameters_are_rejected(tmp_path, index, count):
    with pytest.raises(ValueError):
        ModuleShard(index, count, tmp_path / "collection.json")


@pytest.mark.parametrize("overlap", [True, False])
def test_manifest_verification_rejects_overlaps_and_missing_cases(tmp_path, overlap):
    for index, selected in enumerate((["a"], ["a"] if overlap else ["b"])):
        (tmp_path / f"collection-{index}.json").write_text(
            json.dumps(
                {
                    "index": index,
                    "count": 2,
                    "total_collected": 2 if overlap else 3,
                    "selected": selected,
                }
            )
        )
    with pytest.raises(ValueError, match="Incomplete or overlapping"):
        verify(tmp_path)
