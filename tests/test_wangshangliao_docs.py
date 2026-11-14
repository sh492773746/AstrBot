"""Keep the main documentation command inventory aligned with the parser."""

import ast
from pathlib import Path

from astrbot.builtin_stars.wangshangliao_moderation.command_capabilities import (
    COMMAND_SPECS,
)
from astrbot.builtin_stars.wangshangliao_moderation.syntax import (
    ALIASES,
    NO_ARGUMENT,
    RETIRED_CHAT_ACTIONS,
    WITH_ARGUMENT,
)

ROOT = Path(__file__).resolve().parents[1]


def test_plugin_readme_covers_active_canonical_commands_not_retired_dm_inventory():
    text = (
        ROOT / "astrbot/builtin_stars/wangshangliao_moderation/README.md"
    ).read_text()
    inventory = text.split("## 完整命令索引", 1)[1].split("### 数值范围", 1)[0]
    canonical = {
        ALIASES.get(name, name)
        for name in (NO_ARGUMENT | WITH_ARGUMENT) - RETIRED_CHAT_ACTIONS
    }
    for command in canonical:
        assert f"`{command}`" in inventory or f"`{command} " in inventory, command
    from astrbot.builtin_stars.wangshangliao_moderation import command_capabilities

    assert not hasattr(command_capabilities, "RETIRED_DM_COMMANDS")
    for command in ("菜单", "完整菜单", "客服设置", "我的套餐", "/chat", "/exit"):
        assert command not in COMMAND_SPECS
        assert f"`{command}`" not in inventory
        assert f"`{command} " not in inventory


def test_connection_guidance_does_not_confuse_missing_state_with_stopped_or_zero():
    page = (ROOT / "dashboard/src/views/PlatformPage.vue").read_text()
    fallback = page.split("function getPlatformStat(platformId)", 1)[1].split(
        "function getPlatformStatusLabel", 1
    )[0]
    assert "connection_state: 'unknown'" in fallback
    assert "connection_state: 'stopped'" not in fallback
    for locale, fragments in {
        "zh": ["历史错误记录", "群列表读取失败", "业务暂停／已启用群0", "待核实"],
        "en": [
            "historical records",
            "Group list or member permissions unavailable",
            "Business paused or zero enabled groups",
            "never raw provider responses",
        ],
    }.items():
        text = (ROOT / "docs" / locale / "platform" / "wangshangliao.md").read_text()
        for fragment in fragments:
            assert fragment in text, fragment


def test_current_docs_separate_historical_evidence():
    text = (ROOT / "docs/zh/platform/wangshangliao-testing.md").read_text()
    assert "当次未执行" in text
    assert "发送测试失败" in text
    history = (ROOT / "docs/zh/platform/wangshangliao-testing-history.md").read_text()
    assert "历史归档" in history
    assert "版本日期：2026-09-19" in history


def test_entry_guides_link_current_public_architecture_and_permissions():
    for locale in ("zh", "en"):
        for name in ("wangshangliao", "wangshangliao-start", "wangshangliao-refactor"):
            text = (ROOT / "docs" / locale / "platform" / f"{name}.md").read_text()
            assert "wangshangliao-architecture-permissions.md" in text
        acceptance = (
            ROOT / "docs" / locale / "platform" / "wangshangliao-acceptance-20261014.md"
        ).read_text()
        assert "wsl-activation-reply-20261014T124509" in acceptance
        assert (
            "friend" in acceptance.lower() if locale == "en" else "新好友" in acceptance
        )


def test_current_public_reference_keeps_budget_and_lifecycle_limits():
    text = (
        ROOT / "docs/zh/platform/wangshangliao-architecture-permissions.md"
    ).read_text()
    for fragment in (
        "不能绕过真实邀请来源",
        "不自动删除返现账本",
        "不触发删除",
        "20次",
        "100000次",
        "30秒",
        "4096",
        "256",
        "45秒",
        "90天",
        "仍未通过",
    ):
        assert fragment in text, fragment


def test_maintenance_guide_uses_current_fixed_command_and_queue_boundaries():
    text = (ROOT / "docs/wangshangliao-maintenance.md").read_text()
    for fragment in (
        "wangshangliao-architecture-permissions.md",
        "4096",
        "256",
        "4条待处理固定命令",
        "`admins_id`不会直接授予公共群管理权",
        "自主接话",
        "历史排障，不适用于公共合并模式",
    ):
        assert fragment in text, fragment
    assert "未单独开启群聊天AI时" in text
    assert "结构化@、自主接话及冷场均走本账号／本群的群聊配置" in text
    assert "不走私聊客服咨询" in text
    assert "不加`【群聊AI】`或`【客服咨询】`前缀" in text


def test_current_entry_points_use_latest_evidence_and_no_retired_ai_setup():
    sources = [
        ROOT / "docs/wangshangliao.md",
        ROOT / "astrbot/builtin_stars/wangshangliao_moderation/README.md",
    ]
    for locale in ("zh", "en"):
        sources.extend(
            ROOT / "docs" / locale / "platform" / f"wangshangliao-{name}.md"
            for name in ("start", "architecture-permissions")
        )
    for source in sources:
        text = source.read_text()
        assert "wangshangliao-acceptance-20261015.md" in text, source
        for retired in (
            "wsl_private_management",
            "merge_wsl_open.py",
            "check_wsl_automatic_cards_live.py",
        ):
            assert retired not in text, (source, retired)


def test_current_ai_sources_document_profile_selection_without_write_tools():
    examples = (
        ROOT / "dashboard/plugin-pages/wangshangliao/ConversationExamples.vue"
    ).read_text()
    assert "wsl_private_management" not in examples
    assert "确认修改名片" not in examples
    assert "群聊活动示例" in examples
    assert "示例数据 · 不会发送" in examples
    assert "Invitation Settlement" in examples and "我的邀请" in examples
    assert "菜单9" not in examples
    assert "当前预算门禁仍关闭" not in examples
    for locale in ("zh", "en"):
        guide = (
            ROOT
            / "docs"
            / locale
            / "platform"
            / "wangshangliao-architecture-permissions.md"
        ).read_text()
        assert (
            "不继承工具" if locale == "zh" else "no real history, tools or skills"
        ) in guide
    page = (ROOT / "dashboard/plugin-pages/wangshangliao/OpenService.vue").read_text()
    assert "客服AI咨询" not in page
    assert "群聊 / 热群AI默认配置" in page
    assert "普通聊天自主接话" in page
    editor = (
        ROOT / "dashboard/plugin-pages/wangshangliao/AIProfileEditor.vue"
    ).read_text()
    assert (
        "AstrBot配置" in editor and "persona_id" in editor and "history_turns" in editor
    )


def test_bilingual_retirement_maps_keep_webui_finance_and_no_global_admin_grant():
    for locale, fragments in {
        "zh": (
            "2026-11-02 01:48（北京时间）",
            "受控完整重启加载",
            "普通群管理员",
            "不会因此获得AstrBot全局后台访问权",
            "历史会话",
            "好友自动审批",
            "个人知识",
            "专属客服",
            "私信AI",
            "管理员私信指引",
            "私信财务",
            "图片收取",
            "邀请记账／结账",
            "完整实群验收未完成",
        ),
        "en": (
            "Loaded by a controlled full restart",
            "01:48 Asia/Shanghai, November 2, 2026",
            "do not acquire global AstrBot Dashboard access",
            "historical conversations",
            "automatic friend approval",
            "personal knowledge",
            "dedicated support",
            "private AI",
            "private administrator guidance",
            "private finance",
            "image intake",
            "Invitation Settlement",
            "pending full live acceptance",
        ),
    }.items():
        text = (
            ROOT / "docs" / locale / "platform" / "wangshangliao-start.md"
        ).read_text()
        for fragment in fragments:
            assert fragment in text, (locale, fragment)
        for name in ("group-only-ai-zh-mobile.png", "group-only-matrix-en-desktop.png"):
            assert name in text
            image = ROOT / "docs/public/images/wangshangliao" / name
            assert image.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_script_inventory_keeps_destructive_tools_out_of_daily_acceptance():
    text = (ROOT / "scripts/README.wangshangliao.md").read_text()
    for fragment in (
        "不代表只读",
        "correct_wsl_cashback_amounts.py",
        "不属于验收必跑项",
        "check_wsl_model_cost.py",
        "不打开公共AI门禁",
        "拒绝兼容",
    ):
        assert fragment in text
    for removed in (
        "merge_wsl_open.py",
        "check_wsl_automatic_cards_live.py",
        "check_wsl_open_deployment.py",
        "finish_wsl_rule_management.py",
        "check_wsl_activation_reply.py",
        "check_wsl_activities_live.py",
        "check_wsl_group_authorization.py",
        "check_wsl_live_capabilities.py",
        "check_wsl_invitation_recalls.py",
        "check_wsl_saved_suffix.py",
        "check_wsl_shared_platforms.py",
        "check_wsl_welcome_repair.py",
        "check_wsl_finance_chat_live.py",
        "check_wsl_private_invitations_live.py",
        "check_wsl_guidance_dialogue_live.py",
        "check_wsl_three_guides_live.py",
    ):
        assert not (ROOT / "scripts" / removed).exists()
        assert f"]({removed})" not in text


def test_script_imports_resolve_without_executing_live_runners():
    """Detect broken cleanup dependencies without importing side-effectful tools."""
    plugin = "astrbot.builtin_stars.wangshangliao_moderation"
    removed = {
        "owner_support",
        "private_batches",
        "private_interaction",
        "private_invitation",
        "friend_auto",
        "guidance_trial",
        "consultation_access",
        "service_settings",
        "finance_chat",
    }
    sources = list((ROOT / "scripts").glob("*.py"))
    sources.extend((ROOT / "tests").rglob("*.py"))
    for source in sources:
        tree = ast.parse(source.read_text(encoding="utf-8-sig"), filename=str(source))
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.ImportFrom):
                if node.module == plugin or (node.module or "").startswith(
                    plugin + "."
                ):
                    assert node.module.split(".")[-1] not in removed, (
                        source,
                        node.lineno,
                    )
                    assert not (
                        {"FeatureGuides"}
                        | (removed if node.module == plugin else set())
                    ) & {alias.name for alias in node.names}, (source, node.lineno)
                if node.module == "scripts":
                    modules = [alias.name for alias in node.names]
                elif node.module and node.module.startswith("scripts."):
                    modules = [node.module.split(".")[1]]
            elif isinstance(node, ast.Import):
                assert not any(
                    alias.name.startswith(plugin + ".")
                    and alias.name.split(".")[-1] in removed
                    for alias in node.names
                ), (source, node.lineno)
                modules = [
                    alias.name.split(".")[1]
                    for alias in node.names
                    if alias.name.startswith("scripts.")
                ]
            for module in modules:
                target = ROOT / "scripts" / module
                assert target.with_suffix(".py").is_file() or target.is_dir(), (
                    source,
                    node.lineno,
                    module,
                )


def test_local_reset_and_activation_examples_disable_initial_cli_file_sinks():
    sources = (
        ROOT / "docs/zh/platform/wangshangliao-testing.md",
        ROOT / "docs/en/platform/wangshangliao-membership-testing.md",
    )
    prefix = "env ASTRBOT_DISABLE_FILE_LOGGING=1 .venv/bin/python -m "
    for source in sources:
        lines = source.read_text().splitlines()
        commands = [
            line
            for line in lines
            if ".venv/bin/python -m scripts.reset_wsl_test_group" in line
            or ".venv/bin/python -m scripts.check_wsl_test_activation_live" in line
        ]
        assert len(commands) == 5, source
        assert all(line.startswith(prefix) for line in commands), source


def test_root_cli_logging_guides_preserve_astrbot_logger_and_restart_history():
    sources = (
        ROOT / "scripts/README.wangshangliao.md",
        ROOT / "docs/zh/platform/wangshangliao-architecture-permissions.md",
        ROOT / "docs/en/platform/wangshangliao-architecture-permissions.md",
    )
    for source in sources:
        text = source.read_text()
        assert "ASTRBOT_DISABLE_FILE_LOGGING=1" in text, source
        assert "NRestarts" in text, source
        assert "logger" in text, source
