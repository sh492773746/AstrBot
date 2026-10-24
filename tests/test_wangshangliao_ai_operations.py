"""Retired natural-language operations must never recover execution authority."""

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.ai_config import AdminConfigDrafts
from astrbot.builtin_stars.wangshangliao_moderation.main import Main


def test_retired_configurator_has_no_write_dependencies_or_runtime_registration():
    root = Path(__file__).resolve().parents[1]
    plugin = root / "astrbot/builtin_stars/wangshangliao_moderation"
    retired = ast.parse((plugin / "ai_config.py").read_text())
    imports = [
        name.name
        for node in ast.walk(retired)
        if isinstance(node, ast.Import)
        for name in node.names
    ]
    assert imports == ["json"]
    assert not any(isinstance(node, ast.ImportFrom) for node in ast.walk(retired))
    active = ast.parse((plugin / "main.py").read_text())
    assert not any(
        isinstance(node, ast.ImportFrom) and node.module == "ai_config"
        for node in ast.walk(active)
    )
    assert not any(
        isinstance(node, ast.Attribute) and node.attr == "ai_config"
        for node in ast.walk(active)
    )


FORMER_ACTIONS = [
    "lottery_draw",
    "lottery_cancel",
    "invitation_tracking_enable",
    "invitation_tracking_pause",
    "invitation_reward_enable",
    "invitation_reward_pause",
    "schedule_pause",
    "schedule_resume",
    "schedule_delete",
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    FORMER_ACTIONS
    + [
        "execute_python",
        "grant_admin",
        "pay",
        "cash_rate",
        "shell",
        "lottery_cancel; exec",
    ],
)
@pytest.mark.parametrize("scope", ["ordinary", "admin", "revoked", "other_group"])
async def test_legacy_proposals_and_confirmations_always_reject(action, scope):
    services = SimpleNamespace(command=AsyncMock())
    service = AdminConfigDrafts(SimpleNamespace(), services, services)
    proposal = json.dumps(
        {"target": "operation", "group": "5", "changes": {"action": action}}
    )
    event = SimpleNamespace(scope=scope)
    expected = {"status": "rejected", "reason": "ai_management_retired"}
    assert json.loads(await service.preview(event, proposal)) == expected
    assert json.loads(await service.confirm(event, "old-confirmation")) == expected
    assert not service.drafts
    services.command.assert_not_awaited()
    assert set(vars(service)) == {"drafts"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action", FORMER_ACTIONS + ["config_preview", "config_confirm"]
)
@pytest.mark.parametrize("admin", [False, True])
async def test_retired_chat_entry_never_dispatches_fixed_management(action, admin):
    plugin = object.__new__(Main)
    plugin.commands = SimpleNamespace(execute=AsyncMock())
    event = SimpleNamespace(
        get_platform_name=lambda: "wangshangliao",
        is_private_chat=lambda: True,
        is_admin=lambda: admin,
    )
    assert json.loads(await plugin.private_management(event, action, "{}")) == {
        "status": "rejected",
        "reason": "ai_management_retired",
    }
    plugin.commands.execute.assert_not_awaited()
