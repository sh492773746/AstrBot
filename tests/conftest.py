"""
AstrBot 测试配置

提供共享的 pytest fixtures 和测试工具。
"""

import hashlib
import json
import os
import sys
from asyncio import Queue
from copy import copy
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

# 使用 tests/fixtures/helpers.py 中的共享工具函数，避免重复定义

# 将项目根目录添加到 sys.path
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# The managed LajiaoLiao plugin requires POSIX flock and directory fsync.
# Shared message-policy and coexistence tests still run on Windows.
collect_ignore_glob = ["test_lajiaoliao_*.py"] if sys.platform == "win32" else []

# 设置测试环境变量
os.environ.setdefault("TESTING", "true")
os.environ.setdefault("ASTRBOT_TEST_MODE", "true")
os.environ.setdefault("ASTRBOT_DISABLE_FILE_LOGGING", "1")


def pytest_make_parametrize_id(val, argname):
    """Bound diagnostic IDs without changing test inputs or selection.

    Args:
        val: Parameter value retained unchanged by pytest.
        argname: Parameter name used to distinguish the bounded label.

    Returns:
        Stable short identity for long strings or bytes, otherwise pytest's default.
    """
    if isinstance(val, (str, bytes)) and len(val) > 128:
        data = (
            val.encode("utf-8", errors="backslashreplace")
            if isinstance(val, str)
            else val
        )
        digest = hashlib.sha256(data).hexdigest()[:16]
        return f"{argname}-{type(val).__name__}-{len(val)}-{digest}"
    return None


@pytest.fixture(autouse=True)
def wsl_offline_metrics(request, monkeypatch):
    """Keep Wangshangliao offline tests out of global metrics storage and uploads.

    Args:
        request: Current test identity used to limit this fixture to Wangshangliao.
        monkeypatch: Test-scoped environment restoration.
    """
    if request.node.path.name.startswith(("test_wangshangliao_", "test_wsl_")):
        monkeypatch.setenv("ASTRBOT_DISABLE_METRICS", "1")
        from types import SimpleNamespace

        from astrbot.builtin_stars.wangshangliao_moderation import model_stats
        from astrbot.core import conversation_mgr

        monkeypatch.setattr(
            model_stats, "db_helper", SimpleNamespace(insert_provider_stat=AsyncMock())
        )
        monkeypatch.setattr(
            conversation_mgr,
            "sp",
            SimpleNamespace(
                session_get=AsyncMock(return_value=None),
                session_put=AsyncMock(),
                session_remove=AsyncMock(),
            ),
        )


@pytest.fixture
def wsl_active_policy(monkeypatch):
    """Register an explicit plugin policy for transport-focused unit tests."""
    from types import SimpleNamespace

    from astrbot.builtin_stars.wangshangliao_moderation.syntax import recognize_command
    from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
    from astrbot.core.platform.sources.wangshangliao.runtime_policy import (
        BUSINESS_FIELDS,
        PolicySnapshot,
        runtime,
    )

    monkeypatch.setattr(runtime, "service", None)
    monkeypatch.setattr(runtime, "snapshots", {})
    monkeypatch.setattr(runtime, "observation_groups", {})
    monkeypatch.setattr(runtime, "classifier", recognize_command)
    monkeypatch.setattr(runtime, "observer", None)
    monkeypatch.setattr(runtime, "help_responder", None)
    monkeypatch.setattr(runtime, "generation", 1)
    original = WangshangliaoAdapter.__init__

    def publish(config):
        values = {key: config[key] for key in BUSINESS_FIELDS if key in config}
        values.setdefault("reply_private", True)
        values.setdefault("reply_groups", {})
        runtime.service = SimpleNamespace(active=True)
        runtime.publish(
            config["id"],
            PolicySnapshot(
                str(config.get("account_id", "")),
                "fixture-policy",
                json.dumps(values),
                1,
            ),
        )

    def create(adapter, config, settings, queue):
        publish(config)
        original(adapter, config, settings, queue)

    monkeypatch.setattr(WangshangliaoAdapter, "__init__", create)
    return publish


@pytest.fixture
def wsl_command_classifier(monkeypatch):
    """Supply only the registered syntax callback for lightweight event fixtures."""
    from types import SimpleNamespace

    from astrbot.builtin_stars.wangshangliao_moderation.syntax import recognize_command
    from astrbot.core.platform.sources.wangshangliao.runtime_policy import runtime

    monkeypatch.setattr(runtime, "service", SimpleNamespace(active=True))
    monkeypatch.setattr(runtime, "classifier", recognize_command)


@pytest.fixture(scope="module")
def preserve_plugin_imports():
    """Restore imports and registrations after full-lifecycle integration tests."""
    from astrbot.core.platform import register
    from astrbot.core.star.star import star_map, star_registry
    from astrbot.core.star.star_handler import star_handlers_registry

    prefixes = ("data.plugins", "astrbot.builtin_stars")
    original = {
        name: module
        for name, module in sys.modules.items()
        if name.startswith(prefixes)
    }
    module_refs = {
        name: {
            key: value
            for key, value in vars(module).items()
            if isinstance(value, ModuleType) and value.__name__.startswith(prefixes)
        }
        for name, module in original.items()
    }
    stars = dict(star_map)
    metadata = list(star_registry)
    handlers = [copy(handler) for handler in star_handlers_registry]
    platforms = dict(register.platform_cls_map)
    platform_metadata = list(register.platform_registry)
    yield
    for name in list(sys.modules):
        if name.startswith(prefixes) and name not in original:
            sys.modules.pop(name, None)
    sys.modules.update(original)
    for name, module in original.items():
        # Removed modules can still survive as attributes on their old package.
        for key, value in list(vars(module).items()):
            if isinstance(value, ModuleType) and value.__name__.startswith(prefixes):
                if key not in module_refs[name]:
                    delattr(module, key)
        for key, value in module_refs[name].items():
            setattr(module, key, value)
        parent, _, child = name.rpartition(".")
        if parent in sys.modules:
            setattr(sys.modules[parent], child, module)
    star_map.clear()
    star_map.update(stars)
    star_registry[:] = metadata
    star_handlers_registry.clear()
    for handler in handlers:
        star_handlers_registry.append(handler)
    register.platform_cls_map.clear()
    register.platform_cls_map.update(platforms)
    register.platform_registry[:] = platform_metadata


# ============================================================
# 测试收集和排序
# ============================================================


def pytest_collection_modifyitems(session, config, items):  # noqa: ARG001
    """重新排序测试：单元测试优先，集成测试在后。"""
    unit_tests = []
    integration_tests = []
    deselected = []
    profile = config.getoption("--test-profile") or os.environ.get(
        "ASTRBOT_TEST_PROFILE", "all"
    )

    for item in items:
        item_path = Path(str(item.path))
        is_integration = "integration" in item_path.parts

        if is_integration:
            if item.get_closest_marker("integration") is None:
                item.add_marker(pytest.mark.integration)
            item.add_marker(pytest.mark.tier_d)
            integration_tests.append(item)
        else:
            if item.get_closest_marker("unit") is None:
                item.add_marker(pytest.mark.unit)
            if any(
                item.get_closest_marker(marker) is not None
                for marker in ("platform", "provider", "slow")
            ):
                item.add_marker(pytest.mark.tier_c)
            unit_tests.append(item)

    # 单元测试 -> 集成测试
    ordered_items = unit_tests + integration_tests
    if profile == "blocking":
        selected_items = []
        for item in ordered_items:
            if item.get_closest_marker("tier_c") or item.get_closest_marker("tier_d"):
                deselected.append(item)
            else:
                selected_items.append(item)
        if deselected:
            config.hook.pytest_deselected(items=deselected)
        items[:] = selected_items
        return

    items[:] = ordered_items


def pytest_addoption(parser):
    """增加测试执行档位选择。"""
    parser.addoption(
        "--test-profile",
        action="store",
        default=None,
        choices=["all", "blocking"],
        help="Select test profile. 'blocking' excludes auto-classified tier_c/tier_d tests.",
    )


def pytest_configure(config):
    """注册自定义标记。"""
    config.addinivalue_line("markers", "unit: 单元测试")
    config.addinivalue_line("markers", "integration: 集成测试")
    config.addinivalue_line("markers", "slow: 慢速测试")
    config.addinivalue_line("markers", "platform: 平台适配器测试")
    config.addinivalue_line("markers", "provider: LLM Provider 测试")
    config.addinivalue_line("markers", "db: 数据库相关测试")
    config.addinivalue_line("markers", "tier_c: C-tier tests (optional / non-blocking)")
    config.addinivalue_line("markers", "tier_d: D-tier tests (extended / integration)")


# ============================================================
# 临时目录和文件 Fixtures
# ============================================================


@pytest.fixture
def temp_dir(tmp_path: Path) -> Path:
    """创建临时目录用于测试。"""
    return tmp_path


@pytest.fixture
def require_symlink(tmp_path: Path) -> Path:
    """Skip the test when symlink creation is denied by the OS.

    Windows only allows symbolic links with admin or developer-mode
    privileges, so tests that build symlink fixtures cannot run there.
    """
    probe = tmp_path / ".symlink_probe"
    try:
        probe.symlink_to(tmp_path)
    except OSError as exc:
        # 1314 (ERROR_PRIVILEGE_NOT_HELD): Windows requires admin or
        # developer mode. Anything else is a real error and must fail.
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("symlink creation is not permitted on this platform")
        raise
    probe.unlink()
    return tmp_path


@pytest.fixture
def event_queue() -> Queue:
    """Create a shared asyncio queue fixture for tests."""
    return Queue()


@pytest.fixture
def platform_settings() -> dict:
    """Create a shared empty platform settings fixture for adapter tests."""
    return {}


@pytest.fixture
def temp_data_dir(temp_dir: Path) -> Path:
    """创建模拟的 data 目录结构。"""
    data_dir = temp_dir / "data"
    data_dir.mkdir()

    # 创建必要的子目录
    (data_dir / "config").mkdir()
    (data_dir / "plugins").mkdir()
    (data_dir / "temp").mkdir()
    (data_dir / "attachments").mkdir()

    return data_dir


@pytest.fixture
def temp_config_file(temp_data_dir: Path) -> Path:
    """创建临时配置文件。"""
    config_path = temp_data_dir / "config" / "cmd_config.json"
    default_config = {
        "provider": [],
        "platform": [],
        "provider_settings": {},
        "default_personality": None,
        "timezone": "Asia/Shanghai",
    }
    config_path.write_text(json.dumps(default_config, indent=2), encoding="utf-8")
    return config_path


@pytest.fixture
def temp_db_file(temp_data_dir: Path) -> Path:
    """创建临时数据库文件路径。"""
    return temp_data_dir / "test.db"


# ============================================================
# Mock Fixtures
# ============================================================


@pytest.fixture
def mock_provider():
    """创建模拟的 Provider。"""
    provider = MagicMock()
    provider.provider_config = {
        "id": "test-provider",
        "type": "openai_chat_completion",
        "model": "gpt-4o-mini",
    }
    provider.get_model = MagicMock(return_value="gpt-4o-mini")
    provider.text_chat = AsyncMock()
    provider.text_chat_stream = AsyncMock()
    provider.terminate = AsyncMock()
    return provider


@pytest.fixture
def mock_platform():
    """创建模拟的 Platform。"""
    platform = MagicMock()
    platform.platform_name = "test_platform"
    platform.platform_meta = MagicMock()
    platform.platform_meta.support_proactive_message = False
    platform.send_message = AsyncMock()
    platform.terminate = AsyncMock()
    return platform


@pytest.fixture
def mock_conversation():
    """创建模拟的 Conversation。"""
    from astrbot.core.db.po import ConversationV2

    return ConversationV2(
        conversation_id="test-conv-id",
        platform_id="test_platform",
        user_id="test_user",
        content=[],
        persona_id=None,
    )


@pytest.fixture
def mock_event():
    """创建模拟的 AstrMessageEvent。"""
    event = MagicMock()
    event.unified_msg_origin = "test_umo"
    event.session_id = "test_session"
    event.message_str = "Hello, world!"
    event.message_obj = MagicMock()
    event.message_obj.message = []
    event.message_obj.sender = MagicMock()
    event.message_obj.sender.user_id = "test_user"
    event.message_obj.sender.nickname = "Test User"
    event.message_obj.group_id = None
    event.message_obj.group = None
    event.get_platform_name = MagicMock(return_value="test_platform")
    event.get_platform_id = MagicMock(return_value="test_platform")
    event.get_group_id = MagicMock(return_value=None)
    event.get_extra = MagicMock(return_value=None)
    event.set_extra = MagicMock()
    event.trace = MagicMock()
    event.platform_meta = MagicMock()
    event.platform_meta.support_proactive_message = False
    return event


# ============================================================
# 配置 Fixtures
# ============================================================


@pytest.fixture
def astrbot_config(temp_config_file: Path):
    """创建 AstrBotConfig 实例。"""
    from astrbot.core.config.astrbot_config import AstrBotConfig

    config = AstrBotConfig()
    config._config_path = str(temp_config_file)  # noqa: SLF001
    return config


@pytest.fixture
def main_agent_build_config():
    """创建 MainAgentBuildConfig 实例。"""
    from astrbot.core.astr_main_agent import MainAgentBuildConfig

    return MainAgentBuildConfig(
        tool_call_timeout=60,
        tool_schema_mode="full",
        provider_wake_prefix="",
        streaming_response=True,
        sanitize_context_by_modalities=False,
        kb_agentic_mode=False,
        file_extract_enabled=False,
        context_limit_reached_strategy="truncate_by_turns",
        llm_safety_mode=True,
        computer_use_runtime="local",
        add_cron_tools=True,
    )


# ============================================================
# 数据库 Fixtures
# ============================================================


@pytest_asyncio.fixture
async def temp_db(temp_db_file: Path):
    """创建临时数据库实例。"""
    from astrbot.core.db.sqlite import SQLiteDatabase

    db = SQLiteDatabase(str(temp_db_file))
    try:
        yield db
    finally:
        await db.engine.dispose()
        if temp_db_file.exists():
            temp_db_file.unlink()


# ============================================================
# Context Fixtures
# ============================================================


@pytest_asyncio.fixture
async def mock_context(
    astrbot_config,
    temp_db,
    mock_provider,
    mock_platform,
):
    """创建模拟的插件上下文。"""
    from asyncio import Queue

    from astrbot.core.star.context import Context

    event_queue = Queue()

    provider_manager = MagicMock()
    provider_manager.get_using_provider = MagicMock(return_value=mock_provider)
    provider_manager.get_using_provider_async = AsyncMock(
        side_effect=lambda *args, **kwargs: provider_manager.get_using_provider(
            *args,
            **kwargs,
        )
    )
    provider_manager.get_provider_by_id = MagicMock(return_value=mock_provider)

    platform_manager = MagicMock()
    conversation_manager = MagicMock()
    message_history_manager = MagicMock()
    persona_manager = MagicMock()
    persona_manager.personas_v3 = []
    astrbot_config_mgr = MagicMock()
    knowledge_base_manager = MagicMock()
    cron_manager = MagicMock()
    subagent_orchestrator = None

    context = Context(
        event_queue,
        astrbot_config,
        temp_db,
        provider_manager,
        platform_manager,
        conversation_manager,
        message_history_manager,
        persona_manager,
        astrbot_config_mgr,
        knowledge_base_manager,
        cron_manager,
        subagent_orchestrator,
    )

    return context


# ============================================================
# Provider Request Fixtures
# ============================================================


@pytest.fixture
def provider_request():
    """创建 ProviderRequest 实例。"""
    from astrbot.core.provider.entities import ProviderRequest

    return ProviderRequest(
        prompt="Hello",
        session_id="test_session",
        image_urls=[],
        contexts=[],
        system_prompt="You are a helpful assistant.",
    )


# ============================================================
# 跳过条件
# ============================================================


def pytest_runtest_setup(item):
    """在测试运行前检查跳过条件。"""
    # 跳过需要 API Key 但未设置的 Provider 测试
    if item.get_closest_marker("provider"):
        if not os.environ.get("TEST_PROVIDER_API_KEY"):
            pytest.skip("TEST_PROVIDER_API_KEY not set")

    # 跳过需要特定平台的测试
    if item.get_closest_marker("platform"):
        required_platform = None
        marker = item.get_closest_marker("platform")
        if marker and marker.args:
            required_platform = marker.args[0]

        if required_platform and not os.environ.get(
            f"TEST_{required_platform.upper()}_ENABLED"
        ):
            pytest.skip(f"TEST_{required_platform.upper()}_ENABLED not set")
