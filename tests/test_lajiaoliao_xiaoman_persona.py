"""Persona assets preserve truthful data boundaries and instance authority."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def tool(monkeypatch):
    directory = Path(__file__).resolve().parents[1] / "services/lajiaoliao-bridge"
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location(
        "xiaoman_configuration", directory / "configure_xiaoman_persona.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_native_persona_pairs_and_minimal_tools(tool):
    payload = tool.persona_payload()
    assert payload["persona_id"] == "小满｜高拟真生活型"
    assert len(payload["begin_dialogs"]) == 20
    assert payload["tools"] == ["lajiaoliao_private_management"]
    assert payload["skills"] == []
    prompt = payload["system_prompt"]
    for boundary in (
        "不要输出以上分析过程",
        "不得假装记得",
        "不得凭空编造开奖结果",
        "不因为连续未中就鼓励加倍投注",
        "不代表她拥有真实的现实赌博经历",
        "人格设定不授予群管权限",
        "其他群、其他机器人或私聊",
        "预设对话仅是表达风格示例",
    ):
        assert boundary in prompt


@pytest.mark.parametrize("dialogs", [["odd"], ["question", ""], "invalid"])
def test_invalid_preset_rejected(tool, tmp_path, dialogs):
    (tmp_path / "SOUL.md").write_text("Synthetic prompt. " * 10)
    (tmp_path / "dialogs.json").write_text(json.dumps(dialogs))
    with pytest.raises(ValueError, match="invalid_xiaoman_preset"):
        tool.persona_payload(tmp_path)


def test_profile_changes_only_persona_and_preserves_model_policy(tool):
    original = {
        "agent_runner": {
            "runner_type": "local",
            "config": {
                "persona": {"persona_id": "old", "safety_mode": True},
                "model": {"provider_id": "native", "fallback_provider_ids": []},
            },
        },
        "provider_settings": {"enable": True, "display_reasoning_text": False},
        "admins_id": ["owner"],
        "plugin_set": ["astrbot_plugin_lajiaoliao"],
        "platform_settings": {"reply_with_quote": True},
    }
    expected = copy.deepcopy(original)
    expected["agent_runner"]["config"]["persona"]["persona_id"] = tool.PERSONA_ID
    assert tool.persona_profile(original) == expected
    assert original["agent_runner"]["config"]["persona"]["persona_id"] == "old"
    assert tool.persona_profile(expected) == expected
    original["agent_runner"]["runner_type"] = "dify"
    with pytest.raises(ValueError, match="local_agent_runner_required"):
        tool.persona_profile(original)
