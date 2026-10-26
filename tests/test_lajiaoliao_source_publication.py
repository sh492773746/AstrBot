"""Public source archives must remain complete without live-account evidence."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_public_runtime_package_has_no_private_acceptance_dependencies():
    source = ROOT / "services/lajiaoliao-bridge/build_plugin.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    files = next(
        ast.literal_eval(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "runtime_files"
            for target in node.targets
        )
    )
    assert not any("ACCEPTANCE" in name for name in files)
    for name in set(files) - {"bin", "build-info.json"}:
        assert (ROOT / "data/plugins/astrbot_plugin_lajiaoliao" / name).exists(), name


def test_publication_contains_required_cross_module_source():
    for relative in (
        "astrbot/core/platform/instance_control.py",
        "astrbot/core/platform/message_policy.py",
        "astrbot/core/provider/event_guard.py",
        "astrbot/core/platform/sources/lajiaoliao/adapter.py",
        "dashboard/src/components/platform/LajiaoLiaoLogin.vue",
        "dashboard/src/components/platform/lajiaoliao-captcha.html",
        "dashboard/src/components/shared/GroupAllowlistControl.vue",
        "integrations/personas/xiaoman/SOUL.md",
        "integrations/personas/xiaoman/dialogs.json",
        "services/lajiaoliao-bridge/go.mod",
        "services/lajiaoliao-bridge/go.sum",
    ):
        assert (ROOT / relative).is_file(), relative
