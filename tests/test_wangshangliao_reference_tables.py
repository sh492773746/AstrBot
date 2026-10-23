"""Keep the read-only UI reference aligned with actual command classification."""

import json
from pathlib import Path

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.activities import PUBLIC_COMMANDS
from astrbot.builtin_stars.wangshangliao_moderation.open_service import (
    OPEN_COMMANDS,
    recognize_integrated,
)
from astrbot.builtin_stars.wangshangliao_moderation.syntax import (
    ALIASES,
    BUSINESS_COMMANDS,
    NO_ARGUMENT,
    WITH_ARGUMENT,
    recognize_command,
)

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = json.loads(
    (ROOT / "dashboard/plugin-pages/wangshangliao/reference-tables.json").read_text()
)


def test_every_canonical_command_is_documented_once():
    canonical = {
        ALIASES.get(name, name)
        for name in NO_ARGUMENT | WITH_ARGUMENT | BUSINESS_COMMANDS
    }
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
    assert public == PUBLIC_COMMANDS | {"排名", "帮助", "私聊菜单"}
    for row in REFERENCE["permissions"]:
        if row["commands"]:
            assert row["access"][2] == (
                "allowed"
                if row["id"]
                in {
                    "private-menu",
                    "own-cashback",
                    "help",
                    "own-plan",
                    "personal-knowledge",
                    "support-select",
                }
                else "unavailable"
                if row["id"] in {"test-gate", "mute", "mute-all", "server-ai-writes"}
                else "denied"
            )
    rows = {row["id"]: row for row in REFERENCE["permissions"]}
    assert rows["lottery-join"]["access"][3] == "group-only"
    assert rows["kick"]["access"][1] == "allowed"
    assert rows["ai-tools"]["access"] == [
        "unavailable",
        "unavailable",
        "denied",
        "denied",
    ]
    assert rows["cleanup"]["grants"] == ["cleanup"]
    assert rows["cards"]["grants"] == ["rename"]


def test_public_service_commands_and_current_mode_are_complete():
    documented = [
        name
        for row in REFERENCE["permissions"]
        for name in row.get("service_commands", [])
    ]
    assert set(documented) == {"开通", "认领", "/chat", "/exit"}
    assert len(documented) == len(set(documented))
    canonical = {name for row in REFERENCE["permissions"] for name in row["commands"]}
    assert OPEN_COMMANDS <= canonical | set(documented)
    assert REFERENCE["mode"] == "integrated"
    assert REFERENCE["updated"] == "2026-10-23"
    assert all(REFERENCE["scope"])


def test_retired_tools_and_financial_writes_have_no_chat_authority():
    rows = {row["id"]: row for row in REFERENCE["permissions"]}
    assert all("tool" not in row["access"] for row in rows.values())
    for feature in ("ai-tools", "cards", "settlement"):
        assert rows[feature]["access"][2:] == ["denied", "denied"]
    for feature in (
        "friend-approval",
        "friend-delete",
        "test-gate",
        "mute",
        "mute-all",
    ):
        assert rows[feature]["access"] == ["unavailable"] * 4
    assert rows["unmute"]["access"] == ["denied", "allowed", "denied", "allowed"]
    assert recognize_integrated("管理 开发门禁") == ""
    assert recognize_integrated("sid") == "我的权限"


@pytest.mark.parametrize("row", REFERENCE["architecture"], ids=lambda row: row["id"])
def test_architecture_paths_are_real_and_bilingual(row):
    for file in row["files"]:
        assert (ROOT / file).is_file(), file
    for field in ("name", "responsibility", "boundary"):
        assert len(row[field]) == 2
        assert all(row[field])


@pytest.mark.parametrize(
    "source",
    (
        "astrbot/builtin_stars/wangshangliao_moderation",
        "astrbot/core/platform/sources/wangshangliao",
    ),
)
def test_architecture_covers_every_python_module(source):
    """Require a responsibility owner for every plugin and adapter module."""
    directory = ROOT / source
    actual = {str(path.relative_to(ROOT)) for path in directory.glob("*.py")}
    documented = {
        name
        for row in REFERENCE["architecture"]
        for name in row["files"]
        if name.startswith(str(directory.relative_to(ROOT)) + "/")
    }
    assert actual == documented


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
            "verified",
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
        command = recognize_integrated(example)
        assert command, example
        canonical = recognize_command(command).split(maxsplit=1)
        name = command.split(maxsplit=1)[0]
        if name not in row.get("service_commands", []) and canonical:
            name = canonical[0]
        assert name in row["commands"] + row.get("service_commands", []), example
    for field in ("name", "rule"):
        assert len(row[field]) == 2
        assert all(row[field])


@pytest.mark.parametrize("row", REFERENCE["roles"], ids=lambda row: row["id"])
def test_roles_and_current_bilingual_documentation_match(row):
    for field in ("name", "scope", "allowed", "denied"):
        assert len(row[field]) == 2
        assert all(row[field])
    for index, locale in enumerate(("zh", "en")):
        guide = (
            ROOT
            / "docs"
            / locale
            / "platform"
            / "wangshangliao-architecture-permissions.md"
        ).read_text()
        assert row["name"][index] in guide


def test_current_architecture_and_new_command_docs_are_not_partial():
    for index, locale in enumerate(("zh", "en")):
        guide = (
            ROOT
            / "docs"
            / locale
            / "platform"
            / "wangshangliao-architecture-permissions.md"
        ).read_text()
        for row in REFERENCE["architecture"]:
            assert row["name"][index] in guide, row["id"]
        for row in REFERENCE["permissions"]:
            for command in row.get("service_commands", []):
                assert command in guide, command
