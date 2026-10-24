"""Use the production action port in Wangshangliao transport test doubles."""

from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter


def bind_action_port(adapter):
    """Attach the real versioned dispatcher to an isolated transport double.

    Args:
        adapter: Test double with native action method mocks.

    Returns:
        The original test double with its production action entry point.
    """
    adapter.config.setdefault("id", "test-action-adapter")
    adapter.execute_action = WangshangliaoAdapter.execute_action.__get__(adapter)
    return adapter
