"""Offline wheel settlement, permissions and retry-safety coverage."""

# ruff: noqa: F811
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_superbot_ad_killer import env  # noqa: F401

from data.plugins.astrbot_plugin_superbot.group_points import migrate
from data.plugins.astrbot_plugin_superbot.store import Rejected, encode
from data.plugins.astrbot_plugin_superbot.wheel import (
    ANIMATION_ASSET,
    ANIMATION_CACHE,
    DEFAULT,
    PRIZES,
    Wheel,
)


@pytest.fixture
def wheel(env):
    runtime = env.runtime
    migrate(runtime.store, "-1001")
    runtime.community = SimpleNamespace(member=AsyncMock())
    runtime.bot.edit_message_caption = AsyncMock()
    runtime.bot.send_animation = AsyncMock(
        return_value=SimpleNamespace(
            message_id=90, animation=SimpleNamespace(file_id="animation-id")
        )
    )
    runtime.wheel = Wheel(runtime)
    store = runtime.store
    with store.tx() as db:
        store.put(db, "modules", {"game": True, "wheel": True})
        store.credit(db, "seed", "2", 10000, "test", chat="-1001")
        db.execute(
            "INSERT INTO wheel_groups VALUES(?,?,1)",
            ("-1001", encode({**DEFAULT, "enabled": True})),
        )
        db.execute(
            "INSERT INTO wheel_panels VALUES('panel','-1001','2','source',90,'active',?,0)",
            (env.now[0] + 600,),
        )
    return SimpleNamespace(
        engine=runtime.wheel, store=store, runtime=runtime, now=env.now
    )


def spin(wheel, round_id=0, stake=100, **kwargs):
    return wheel.engine.settle(
        kwargs.get("identity", "panel"),
        round_id,
        kwargs.get("uid", "2"),
        kwargs.get("chat", "-1001"),
        kwargs.get("message", 90),
        stake,
        kwargs.get("version", 1),
        kwargs.get("draws", 1),
    )


@pytest.mark.parametrize("draws", [5, 10])
def test_batch_atomic_and_replay(wheel, monkeypatch, draws):
    monkeypatch.setattr(
        "data.plugins.astrbot_plugin_superbot.wheel.secrets.randbelow", lambda _: 0
    )
    order, fresh = spin(wheel, draws=draws)
    assert fresh and order["draws"] == draws
    assert order["stake"] == 100 * draws and order["payout"] == 80 * draws
    assert wheel.store.balance("2", "-1001") == 10000 - 20 * draws
    assert spin(wheel, draws=1) == (order, False)
    wheel.now[0] += 3
    spin(wheel, round_id=1, draws=10)
    wheel.now[0] += 3
    before = wheel.store.balance("2", "-1001")
    with pytest.raises(Rejected, match="次数"):
        spin(wheel, round_id=2, draws=10)
    assert wheel.store.balance("2", "-1001") == before


def test_batch_failure_rolls_back(wheel, monkeypatch):
    def fail(_):
        raise RuntimeError("draw failure")

    monkeypatch.setattr(
        "data.plugins.astrbot_plugin_superbot.wheel.secrets.randbelow", fail
    )
    with pytest.raises(RuntimeError):
        spin(wheel, draws=5)
    assert wheel.store.balance("2", "-1001") == 10000
    assert not wheel.store.db.execute("SELECT 1 FROM wheel_orders").fetchone()


def test_batch_requires_full_upfront_balance(wheel, monkeypatch):
    with wheel.store.tx() as db:
        wheel.store.credit(db, "reduce", "2", -9500, "test", chat="-1001")
    with pytest.raises(Rejected):
        spin(wheel, stake=100, draws=10)
    assert wheel.store.balance("2", "-1001") == 500
    assert not wheel.store.db.execute("SELECT 1 FROM wheel_orders").fetchone()


@pytest.mark.asyncio
async def test_batch_button_and_caption(wheel, monkeypatch):
    monkeypatch.setattr(
        "data.plugins.astrbot_plugin_superbot.wheel.secrets.randbelow", lambda _: 9999
    )
    await wheel.engine.action(callback("100x10"))
    args = wheel.runtime.bot.edit_message_caption.await_args.kwargs
    assert "10连抽" in args["caption"] and "每抽100积分" in args["caption"]
    assert "<blockquote expandable>" in args["caption"]
    assert len(args["caption"]) < 1024


def callback(action="100", round_id=0):
    user = SimpleNamespace(id=2, username="tester", full_name="Tester")
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=-1001, type="supergroup"),
        effective_user=user,
        callback_query=SimpleNamespace(
            id="query1",
            data=f"wh:panel:{round_id}:{action}",
            message=SimpleNamespace(message_id=90),
        ),
    )


def test_probability_and_stakes_exact():
    assert sum(weight for _, weight in PRIZES) == 10000
    assert sum(multiplier * weight for multiplier, weight in PRIZES) == 95000
    assert all(
        stake * multiplier % 10 == 0
        for stake in DEFAULT["stakes"]
        for multiplier, _ in PRIZES
    )


@pytest.mark.parametrize(
    "ticket,multiplier",
    [
        (0, 8),
        (7949, 8),
        (7950, 12),
        (9399, 12),
        (9400, 18),
        (9799, 18),
        (9800, 25),
        (9919, 25),
        (9920, 40),
        (9989, 40),
        (9990, 100),
        (9999, 100),
    ],
)
def test_bucket_boundaries(wheel, monkeypatch, ticket, multiplier):
    monkeypatch.setattr(
        "data.plugins.astrbot_plugin_superbot.wheel.secrets.randbelow", lambda _: ticket
    )
    order, fresh = spin(wheel)
    assert fresh and order["multiplier"] == multiplier
    assert wheel.store.balance("2", "-1001") == 9900 + multiplier * 10
    assert (
        wheel.store.db.execute(
            "SELECT count(*) FROM group_ledger WHERE reason LIKE 'wheel_%'"
        ).fetchone()[0]
        == 2
    )


def test_replay_other_stake_never_redraws(wheel, monkeypatch):
    monkeypatch.setattr(
        "data.plugins.astrbot_plugin_superbot.wheel.secrets.randbelow", lambda _: 0
    )
    original, _ = spin(wheel)
    balance = wheel.store.balance("2", "-1001")
    assert spin(wheel, stake=1000) == (original, False)
    assert wheel.store.balance("2", "-1001") == balance
    wheel.now[0] += 700
    assert spin(wheel) == (original, False)
    with pytest.raises(Rejected):
        spin(wheel, round_id=1)


@pytest.mark.parametrize(
    "override",
    [
        {"uid": "3"},
        {"chat": "-1002"},
        {"message": 91},
        {"version": 2},
        {"stake": 33},
        {"round_id": 9},
    ],
)
def test_invalid_request_never_debits(wheel, override):
    with pytest.raises(Rejected):
        spin(wheel, **override)
    assert wheel.store.balance("2", "-1001") == 10000
    assert (
        wheel.store.db.execute("SELECT count(*) FROM wheel_orders").fetchone()[0] == 0
    )


def test_insufficient_and_payout_failure_roll_back(wheel, monkeypatch):
    with wheel.store.tx() as db:
        wheel.store.credit(db, "reduce", "2", -9990, "test", chat="-1001")
    with pytest.raises(Rejected):
        spin(wheel)
    assert wheel.store.balance("2", "-1001") == 10
    with wheel.store.tx() as db:
        wheel.store.credit(db, "refill", "2", 100, "test", chat="-1001")
    original = wheel.store.credit

    def fail(db, op, *args, **kwargs):
        if op.endswith(":payout"):
            raise Rejected("Injected payout failure")
        return original(db, op, *args, **kwargs)

    monkeypatch.setattr(wheel.store, "credit", fail)
    with pytest.raises(Rejected):
        spin(wheel)
    assert wheel.store.balance("2", "-1001") == 110
    assert (
        wheel.store.db.execute("SELECT count(*) FROM wheel_orders").fetchone()[0] == 0
    )


def test_daily_limit_cooldown_and_beijing_midnight(wheel, monkeypatch):
    monkeypatch.setattr(
        "data.plugins.astrbot_plugin_superbot.wheel.secrets.randbelow", lambda _: 0
    )
    wheel.now[0] = datetime(2026, 9, 26, 15, 58, tzinfo=timezone.utc).timestamp()
    wheel.store.db.execute("UPDATE wheel_panels SET expires=?", (wheel.now[0] + 600,))
    spin(wheel, 0)
    with pytest.raises(Rejected, match="间隔"):
        spin(wheel, 1)
    for i in range(1, 20):
        wheel.now[0] += 3
        spin(wheel, i)
    wheel.now[0] += 3
    with pytest.raises(Rejected, match="次数"):
        spin(wheel, 20)
    wheel.now[0] = datetime(2026, 9, 26, 16, 0, tzinfo=timezone.utc).timestamp()
    order, fresh = spin(wheel, 20)
    assert fresh and order["day"] == "2026-09-27"


def test_switches_and_configuration_change(wheel):
    for module in ["wheel", "game"]:
        with wheel.store.tx() as db:
            wheel.store.put(db, "modules", {"game": True, "wheel": True, module: False})
        with pytest.raises(Rejected):
            spin(wheel)
    with wheel.store.tx() as db:
        wheel.store.put(db, "modules", {"game": True, "wheel": True})
    wheel.store.db.execute("UPDATE wheel_groups SET version=2")
    with pytest.raises(Rejected, match="调整"):
        spin(wheel)
    wheel.store.db.execute("UPDATE mod_groups SET enabled=0")
    with pytest.raises(Rejected):
        spin(wheel, version=2)


@pytest.mark.asyncio
async def test_two_callbacks_once_and_refresh(wheel):
    results = await asyncio.gather(
        wheel.engine.action(callback()), wheel.engine.action(callback())
    )
    assert any("未重复扣分" in result for result in results)
    assert (
        wheel.store.db.execute("SELECT count(*) FROM wheel_orders").fetchone()[0] == 1
    )
    await wheel.engine.action(callback("refresh"))
    assert (
        wheel.store.db.execute("SELECT count(*) FROM wheel_orders").fetchone()[0] == 1
    )


@pytest.mark.asyncio
async def test_display_timeout_keeps_committed_order(wheel):
    wheel.runtime.bot.edit_message_caption.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        await wheel.engine.action(callback())
    balance = wheel.store.balance("2", "-1001")
    wheel.runtime.bot.edit_message_caption.side_effect = None
    assert "未重复扣分" in await wheel.engine.action(callback())
    assert wheel.store.balance("2", "-1001") == balance


@pytest.mark.asyncio
async def test_member_left_or_switch_changes_during_lookup(wheel):
    wheel.runtime.community.member.side_effect = Rejected("left")
    with pytest.raises(Rejected):
        await wheel.engine.action(callback())
    assert wheel.store.balance("2", "-1001") == 10000

    async def disable(*args, **kwargs):
        with wheel.store.tx() as db:
            wheel.store.put(db, "modules", {"game": True, "wheel": False})

    wheel.runtime.community.member.side_effect = disable
    with pytest.raises(Rejected):
        await wheel.engine.action(callback())
    assert wheel.store.balance("2", "-1001") == 10000


def test_restart_reads_order_without_redraw(wheel):
    original, _ = spin(wheel)
    wheel.engine = Wheel(wheel.runtime)
    assert spin(wheel) == (original, False)
    caption, markup = wheel.engine.render("panel", callback().effective_user, True)
    assert "79.5%" in caption and "<blockquote expandable>" in caption
    assert markup.inline_keyboard[0][0].callback_data.startswith("wh:panel:1:")


def test_other_group_wallet_unchanged(wheel):
    wheel.store.db.execute(
        "INSERT INTO mod_groups(chat,title,enabled) VALUES('-1002','Other',1)"
    )
    wheel.store.db.execute(
        "INSERT INTO wheel_groups VALUES(?,?,1)",
        ("-1002", encode({**DEFAULT, "enabled": True})),
    )
    with wheel.store.tx() as db:
        wheel.store.credit(db, "other", "2", 777, "test", chat="-1002")
    spin(wheel)
    assert wheel.store.balance("2", "-1002") == 777


@pytest.mark.asyncio
async def test_unknown_animation_send_not_replayed(wheel):
    update = callback()
    update.callback_query.id = "open-query"
    wheel.runtime.bot.send_animation.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        await wheel.engine.open(update)
    await wheel.engine.open(update)
    assert wheel.runtime.bot.send_animation.await_count == 1
    assert (
        wheel.store.db.execute(
            "SELECT status FROM wheel_panels WHERE source='callback:open-query'"
        ).fetchone()[0]
        == "review"
    )


@pytest.mark.asyncio
async def test_animation_file_cache_and_panel_binding(wheel):
    update = callback()
    update.callback_query.id = "open-success"
    with wheel.store.tx() as db:
        wheel.store.put(db, "wheel_photo", "legacy-photo-id")
    await wheel.engine.open(update)
    args = wheel.runtime.bot.send_animation.await_args.kwargs
    assert isinstance(args["animation"], bytes)
    assert args["animation"].startswith(b"GIF89a")
    assert args["filename"] == ANIMATION_ASSET
    assert args["parse_mode"] == "HTML"
    assert args["reply_markup"].inline_keyboard
    assert "指针仅作展示" in args["caption"]
    assert wheel.store.get(ANIMATION_CACHE) == "animation-id"
    await wheel.engine.open(update)
    assert wheel.runtime.bot.send_animation.await_count == 1
    update.callback_query.id = "open-success-2"
    await wheel.engine.open(update)
    assert (
        wheel.runtime.bot.send_animation.await_args.kwargs["animation"]
        == "animation-id"
    )


def test_animation_only_moves_pointer_and_preserves_probability():
    from pathlib import Path

    from PIL import Image, ImageChops

    asset = (
        Path(__file__).parents[1]
        / "data/plugins/astrbot_plugin_superbot/assets"
        / ANIMATION_ASSET
    )
    with Image.open(asset) as animation:
        assert animation.size == (720, 816)
        assert animation.n_frames == 48
        assert animation.info["loop"] == 0
        first = animation.convert("RGB")
        duration = 0
        changed = False
        for index in range(animation.n_frames):
            animation.seek(index)
            duration += animation.info["duration"]
            bounds = ImageChops.difference(first, animation.convert("RGB")).getbbox()
            if bounds:
                changed = True
                # All label, rim and footer pixels stay exactly static.
                assert bounds[0] > 175 and bounds[2] < 545
                assert bounds[1] > 160 and bounds[3] < 535
        assert changed and duration == 3840
    assert asset.stat().st_size < 1024 * 1024


@pytest.mark.asyncio
async def test_config_change_while_network_verification_rejects(wheel):
    async def change(*args, **kwargs):
        wheel.store.db.execute("UPDATE wheel_groups SET version=version+1")

    wheel.runtime.community.member.side_effect = change
    with pytest.raises(Rejected, match="调整"):
        await wheel.engine.action(callback())
    assert wheel.store.balance("2", "-1001") == 10000


def test_multiple_panels_share_daily_limit_and_cooldown(wheel):
    wheel.store.db.execute(
        "INSERT INTO wheel_panels VALUES('other','-1001','2','other',91,'active',?,0)",
        (wheel.now[0] + 600,),
    )
    spin(wheel)
    with pytest.raises(Rejected, match="间隔"):
        spin(wheel, identity="other", message=91)
    wheel.now[0] += 3
    spin(wheel, identity="other", message=91)
    assert (
        wheel.store.db.execute("SELECT count(*) FROM wheel_orders").fetchone()[0] == 2
    )


@pytest.mark.asyncio
async def test_admin_preview_save_stale_and_permissions(wheel):
    from data.plugins.astrbot_plugin_superbot.wheel_admin import action

    with wheel.store.tx() as db:
        wheel.store.put(
            db, "modules", {"game": True, "wheel": True, "moderation": True}
        )
    ui = SimpleNamespace(store=wheel.store, runtime=wheel.runtime, render=AsyncMock())
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=1, type="private"),
    )
    payload = {
        "action": "wheel_preview",
        "chat": "-1001",
        "field": "stakes",
        "value": "5 50 1000",
        "version": 1,
    }
    await action(ui, update, payload)
    assert wheel.engine.config("-1001")[0]["stakes"] == DEFAULT["stakes"]
    await action(ui, update, {**payload, "action": "wheel_save"})
    assert wheel.engine.config("-1001")[0]["stakes"] == [5, 50, 1000]
    with pytest.raises(Rejected, match="变化"):
        await action(ui, update, {**payload, "action": "wheel_save"})
    update.effective_user.id = 2
    with pytest.raises(Rejected):
        await action(ui, update, {"action": "wheel_groups"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("stakes", "0 50"),
        ("stakes", "7"),
        ("stakes", "50 50"),
        ("stakes", "5 10 15 20 25"),
        ("stakes", "1005"),
        ("limit", "0"),
        ("limit", "1001"),
        ("cooldown", "2"),
        ("cooldown", "3601"),
    ],
)
async def test_admin_rejects_invalid_settings(wheel, field, value):
    from data.plugins.astrbot_plugin_superbot.wheel_admin import action

    with wheel.store.tx() as db:
        wheel.store.put(
            db, "modules", {"game": True, "wheel": True, "moderation": True}
        )
    ui = SimpleNamespace(store=wheel.store, runtime=wheel.runtime, render=AsyncMock())
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=1, type="private"),
    )
    with pytest.raises(Rejected):
        await action(
            ui,
            update,
            {
                "action": "wheel_save",
                "chat": "-1001",
                "field": field,
                "value": value,
                "version": 1,
            },
        )
    assert wheel.engine.config("-1001")[1] == 1
