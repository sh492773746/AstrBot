"""AI card operations are retired; native dashboard card coverage is separate."""

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.main import Main


@pytest.mark.parametrize("entry", ["private_management", "card_tool"])
def test_retired_card_entry_points_do_not_exist(entry):
    assert not hasattr(Main, entry)
    assert not hasattr(object.__new__(Main), entry)
