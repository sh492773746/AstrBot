"""Keep the read-only UI reference aligned with actual command classification."""

import json
from pathlib import Path

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.activities import PUBLIC_COMMANDS
from astrbot.builtin_stars.wangshangliao_moderation.syntax import (
    ALIASES,
    NO_ARGUMENT,
    WITH_ARGUMENT,
    recognize_command,
)

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = json.loads(
    (ROOT / "dashboard/plugin-pages/wangshangliao/reference-tables.json").read_text()
)


def test_every_canonical_command_is_documented_once():
    canonical = {ALIASES.get(name, name) for name in NO_ARGUMENT | WITH_ARGUMENT}
    documented = [name for row in REFERENCE["permissions"] for name in row["commands"]]
    assert set(documented) == canonical
    assert len(documented) == len(set(documented))


def test_public_group_commands_and_private_denials_match_dispatch():
    public = {
        name
        for row in REFERENCE["permissions"]
        if row["access"][0] == "allowed"
        for name in row["commands"]
    }
    assert public == PUBLIC_COMMANDS | {"排名"}
    for row in REFERENCE["permissions"]:
        if row["commands"]:
            assert row["access"][2] == "denied"
    rows = {row["id"]: row for row in REFERENCE["permissions"]}
    assert rows["lottery-join"]["access"][3] == "group-only"
    assert rows["kick"]["access"][1] == "kick-mode"
    assert rows["ai-tools"]["access"] == [
        "unavailable",
        "unavailable",
        "denied",
        "tool",
    ]
    assert rows["cleanup"]["grants"] == ["cleanup"]
    assert rows["cards"]["grants"] == ["rename"]


@pytest.mark.parametrize("row", REFERENCE["architecture"], ids=lambda row: row["id"])
def test_architecture_paths_are_real_and_bilingual(row):
    for file in row["files"]:
        assert (ROOT / file).is_file(), file
    for field in ("name", "responsibility", "boundary"):
        assert len(row[field]) == 2
        assert all(row[field])


@pytest.mark.parametrize("row", REFERENCE["permissions"], ids=lambda row: row["id"])
def test_matrix_examples_and_grants_are_valid(row):
    assert len(row["access"]) == 4
    assert all(
        value
        in {
            "allowed",
            "denied",
            "private-only",
            "group-only",
            "unavailable",
            "tool",
            "kick-mode",
        }
        for value in row["access"]
    )
    assert set(row["grants"]) <= {
        "mute",
        "unmute",
        "kick",
        "announce",
        "mute_all",
        "unmute_all",
        "rename",
        "cleanup",
    }
    for example in row["examples"]:
        if row["commands"]:
            command = recognize_command(example).split(maxsplit=1)
            assert command and command[0] in row["commands"], example
    for field in ("name", "rule"):
        assert len(row[field]) == 2
        assert all(row[field])
