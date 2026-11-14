"""Keep the read-only UI reference aligned with actual command classification."""

import json
from pathlib import Path

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.activities import PUBLIC_COMMANDS
from astrbot.builtin_stars.wangshangliao_moderation.command_capabilities import (
    CAPABILITIES,
    COMMON,
)
from astrbot.builtin_stars.wangshangliao_moderation.open_service import (
    recognize_integrated,
)
from astrbot.builtin_stars.wangshangliao_moderation.syntax import (
    ALIASES,
    REMOVED_GROUP_COMMANDS,
    RETIRED_CHAT_ACTIONS,
    is_removed_group_command,
    recognize_command,
)

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = json.loads(
    (ROOT / "dashboard/plugin-pages/wangshangliao/reference-tables.json").read_text()
)


def test_every_group_capability_is_documented_once_without_removed_entries():
    canonical = {
        ALIASES.get(name, name)
        for name, spec in CAPABILITIES.items()
        if "group" in spec["locations"]
    }
    documented = [
        name
        for row in REFERENCE["permissions"]
        if row["access"] != ["unavailable", "unavailable"]
        for name in row["commands"]
        if name != "0"
    ]
    assert set(documented) == canonical
    assert len(documented) == len(set(documented))
    assert not set(documented) & (
        REMOVED_GROUP_COMMANDS | (RETIRED_CHAT_ACTIONS - {"禁言"})
    )


def test_public_group_commands_and_manager_denials_match_dispatch():
    public = {
        name
        for row in REFERENCE["permissions"]
        if row["access"][0] == "allowed"
        for name in row["commands"]
        if name != "0"
    }
    assert public == {ALIASES.get(name, name) for name in PUBLIC_COMMANDS | COMMON} | {
        "排名"
    }
    for row in REFERENCE["permissions"]:
        for command in row["commands"]:
            if row["id"] == "restricted-routing":
                assert row["access"] == ["unavailable", "unavailable"]
                assert command not in CAPABILITIES
                assert recognize_integrated(command) == ""
                continue
            if command == "0":
                assert row["id"] == "cancel"
                assert recognize_integrated(command) == command
                continue
            capability = CAPABILITIES[command]
            assert capability["locations"] == ("group",)
            if row["id"] == "delegated-mute":
                assert capability["role"] == "mute_delegate"
                assert row["access"] == ["denied", "denied"]
            else:
                assert row["access"] == [
                    "allowed" if capability["role"] == "public" else "denied",
                    "allowed",
                ], command
    rows = {row["id"]: row for row in REFERENCE["permissions"]}
    for feature, grants in {
        "unmute": ["unmute"],
        "kick": ["kick"],
        "announce": ["announce"],
        "schedules": ["mute_all", "unmute_all"],
    }.items():
        assert rows[feature]["access"] == ["denied", "allowed"]
        assert rows[feature]["grants"] == grants
    assert rows["cleanup"]["grants"] == ["cleanup"]
    assert rows["cards"]["grants"] == ["rename"]


def test_reference_is_group_and_webui_only_in_current_mode():
    serialized = json.dumps(REFERENCE, ensure_ascii=False).lower()
    for retired in ("private", "私信", "私聊", "好友", "/chat", "/exit", "客服"):
        assert retired not in serialized
    assert {row["id"] for row in REFERENCE["roles"]} == {
        "member",
        "manager",
        "dashboard",
        "finance",
        "mute_delegate",
    }
    assert REFERENCE["mode"] == "integrated"
    assert REFERENCE["updated"] == "2026-11-08"
    assert all(REFERENCE["scope"])


def test_retired_tools_and_financial_writes_have_no_chat_authority():
    rows = {row["id"]: row for row in REFERENCE["permissions"]}
    assert all("tool" not in row["access"] for row in rows.values())
    for feature in (
        "ai-tools",
        "cleanup",
        "cards",
        "settlement",
        "restricted-routing",
        "manual-mutes",
    ):
        assert rows[feature]["access"] == ["unavailable"] * 2
    assert rows["unmute"]["access"] == ["denied", "allowed"]
    assert recognize_integrated("管理 开发门禁") == ""
    for text in ("帮助", "群管帮助", "help", "我的权限", "sid", "黑名单禁言时长 1分钟"):
        assert recognize_integrated(text) == ""
        assert recognize_command(text) == ""
        assert is_removed_group_command(text)


def test_ranking_reference_is_public_top_twenty_without_member_ids_or_pagination():
    row = next(row for row in REFERENCE["permissions"] if row["id"] == "ranking")
    assert row["commands"] == ["排名"]
    assert row["access"] == ["allowed", "allowed"]
    assert row["grants"] == []
    assert CAPABILITIES["排名"]["role"] == "public"
    for requirement in (
        "前20名",
        "昵称",
        "有效发言数",
        "不显示UID",
        "不分页",
        "自动撤回",
    ):
        assert requirement in row["rule"][0]


@pytest.mark.parametrize("row", REFERENCE["architecture"], ids=lambda row: row["id"])
def test_architecture_paths_are_real_and_bilingual(row):
    for file in row["files"]:
        assert (ROOT / file).is_file(), file
    for field in ("name", "responsibility", "boundary"):
        assert len(row[field]) == 2
        assert all(row[field])


def test_architecture_covers_group_boundaries_without_retired_private_modules():
    documented = {name for row in REFERENCE["architecture"] for name in row["files"]}
    for source, names in {
        "astrbot/core/platform/sources/wangshangliao": {
            "adapter",
            "nim",
            "wire",
            "directory",
            "identity",
            "group_scope",
            "moderation",
            "raid",
        },
        "astrbot/builtin_stars/wangshangliao_moderation": {
            "group_management",
            "commands",
            "command_capabilities",
            "delegated_mutes",
            "group_ai",
            "ai_plans",
            "knowledge",
            "knowledge_store",
            "content_rules",
            "flood",
            "media_moderation",
            "action_receipts",
            "recall_audit",
            "member_blacklist",
            "member_whitelist",
            "speech_activity",
            "raid_defense",
            "activities",
            "activity_store",
            "rule_replies",
            "schedules",
            "cards",
            "cashback_store",
            "open_rpc",
            "component_governance",
            "lifecycle",
        },
    }.items():
        assert {f"{source}/{name}.py" for name in names} <= documented
    assert not {Path(name).stem for name in documented} & {
        "owner_support",
        "service_settings",
        "friends",
        "private_menu",
    }


@pytest.mark.parametrize("row", REFERENCE["permissions"], ids=lambda row: row["id"])
def test_matrix_examples_and_grants_are_valid(row):
    assert len(row["access"]) == 2
    assert all(
        value
        in {
            "allowed",
            "denied",
            "unavailable",
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
        canonical = (
            command if command in COMMON else recognize_command(command)
        ).split(maxsplit=1)
        assert canonical and canonical[0] in row["commands"], example
    for field in ("name", "rule"):
        assert len(row[field]) == 2
        assert all(row[field])


@pytest.mark.parametrize("row", REFERENCE["roles"], ids=lambda row: row["id"])
def test_roles_are_bilingual_and_scoped(row):
    for field in ("name", "scope", "allowed", "denied"):
        assert len(row[field]) == 2
        assert all(row[field])


def test_bilingual_architecture_guides_point_to_current_reference():
    for locale in ("zh", "en"):
        guide = (
            ROOT
            / "docs"
            / locale
            / "platform"
            / "wangshangliao-architecture-permissions.md"
        ).read_text()
        assert "dashboard/plugin-pages/wangshangliao/reference-tables.json" in guide
