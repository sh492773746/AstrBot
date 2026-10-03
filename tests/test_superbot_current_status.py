"""Admission diagnostics distinguish a healthy close from unreliable data."""

# ruff: noqa: F811

import pytest
from test_superbot import env  # noqa: F401

from data.plugins.astrbot_plugin_superbot.store import Rejected


@pytest.mark.parametrize(
    "age,received_age,expected",
    [
        (190, 0, "第 101 期已封盘"),
        (209, 0, "开奖数据正常"),
        (210, 0, "下一期开奖尚未更新"),
        (300, 0, "已超出预计周期 90 秒"),
        (190, 61, "开奖数据已过期"),
        (10, 61, "上限60秒"),
        (-1, 0, "开奖时间异常"),
    ],
)
def test_specific_admission_failure(env, age, received_age, expected):
    store, game, clock = env
    store.db.execute(
        "UPDATE draws SET at=?,received=?",
        (clock[0] - age, clock[0] - received_age),
    )
    with pytest.raises(Rejected, match=expected):
        game.current()


def test_conflict_missing_and_collector_error(env):
    store, game, _ = env
    with store.tx() as db:
        store.put(db, "keno_error", "internal URL must never be exposed")
    with pytest.raises(Rejected, match="开奖采集异常") as error:
        game.current()
    assert "internal URL" not in str(error.value)
    store.db.execute("UPDATE draws SET conflict=1")
    with pytest.raises(Rejected, match="存在冲突"):
        game.current()
    store.db.execute("DELETE FROM draws")
    with pytest.raises(Rejected, match="暂无可靠开奖数据"):
        game.current()


def test_acceptance_boundary_unchanged(env):
    store, game, clock = env
    store.db.execute(
        "UPDATE draws SET at=?,received=?", (clock[0] - 189, clock[0] - 60)
    )
    assert game.current() == (101, clock[0] + 1)
