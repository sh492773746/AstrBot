"""Offline avatar quota, identity, transport and image acceptance."""

import asyncio
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image, ImageChops
from telegram.error import Forbidden, TimedOut

from data.plugins.astrbot_plugin_superbot.avatar import (
    ASSETS,
    KEY,
    PLATFORM,
    Avatar,
    compose,
    nickname,
)
from data.plugins.astrbot_plugin_superbot.store import Rejected, Store


def update(uid=2, chat=2, name="大海传媒·青鱼", reply=None):
    message = SimpleNamespace(
        message_id=10, sender_chat=None, reply_to_message=reply, text="小周"
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=uid, full_name=name),
        effective_chat=SimpleNamespace(
            id=chat, type="private" if chat > 0 else "supergroup"
        ),
        effective_message=message,
        message=message,
        callback_query=None,
    )


@pytest.fixture
def avatar(tmp_path):
    from data.plugins.astrbot_plugin_superbot.ui import UI

    store = Store(tmp_path / "test.sqlite3", "1")
    store.db.execute("CREATE TABLE mod_groups(chat TEXT PRIMARY KEY,enabled INTEGER)")
    store.db.execute("INSERT INTO mod_groups(chat,enabled) VALUES('-1001',1)")
    runtime = SimpleNamespace(
        config={"platform_id": PLATFORM},
        store=store,
        bot=SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=55)),
            send_photo=AsyncMock(return_value=SimpleNamespace(message_id=56)),
            send_document=AsyncMock(return_value=SimpleNamespace(message_id=57)),
        ),
    )
    module = Avatar(runtime)
    runtime.ui = UI(runtime)
    with store.tx() as db:
        store.put(db, KEY, True)
        store.put(db, "modules", {"moderation": True})
    yield module
    store.close()


def confirmation(avatar, event, name="青鱼"):
    payload = {"action": "avatar_confirm", "name": name, "template": avatar.version}
    token = avatar.store.callback(
        event.effective_user.id, event.effective_chat.id, payload
    )[3:]
    return payload, token


def test_fixed_base_unicode_font_and_bounds():
    base = Image.open(ASSETS / "base.png").convert("RGB")
    for name in ("周", "青鱼", "大海传媒青鱼测试", "Ab123"):
        image = Image.open(BytesIO(compose(name))).convert("RGB")
        difference = ImageChops.difference(base, image).getbbox()
        assert image.size == (1280, 1280)
        assert difference
        assert difference[0] >= 270 and difference[1] >= 795
        assert difference[2] <= 1010 and difference[3] <= 1025
    for value in (
        "",
        "a\nb",
        "t.me/a",
        "../",
        "a b",
        "123456789",
        "<b>",
        "😀",
        "\u200b",
    ):
        with pytest.raises(Rejected):
            nickname(value)


@pytest.mark.asyncio
async def test_lifetime_quota_shared_between_group_private_and_duplicate(avatar):
    first = update()
    payload, token = confirmation(avatar, first)
    await avatar.action(first, payload, token)
    assert avatar.remaining(2) == 1
    with pytest.raises(Rejected):
        await avatar.action(first, payload, token)
    second = update(chat=-1001, name="大海传媒·改名")
    payload, token = confirmation(avatar, second, "周")
    await avatar.action(second, payload, token)
    assert avatar.remaining(2) == 0
    payload, token = confirmation(avatar, first)
    with pytest.raises(Rejected):
        await avatar.action(first, payload, token)
    assert avatar.runtime.bot.send_document.await_count == 2
    assert not avatar.store.db.execute("SELECT 1 FROM ledger").fetchone()


@pytest.mark.asyncio
async def test_concurrent_one_inflight(avatar):
    event = update()
    p1, t1 = confirmation(avatar, event)
    p2, t2 = confirmation(avatar, event)
    result = await asyncio.gather(
        avatar.action(event, p1, t1),
        avatar.action(event, p2, t2),
        return_exceptions=True,
    )
    assert sum(isinstance(item, Rejected) for item in result) == 1
    assert avatar.remaining(2) == 1


@pytest.mark.asyncio
async def test_brand_group_scope_template_expiry_and_callback_owner(avatar):
    event = update()
    payload, token = confirmation(avatar, event)
    for other in (
        update(uid=3),
        update(chat=3),
        update(name="青鱼"),
        update(chat=-1009),
    ):
        with pytest.raises(Rejected):
            await avatar.action(other, payload, token)
    with pytest.raises(Rejected):
        await avatar.action(event, {**payload, "template": "old"}, token)
    avatar.store.db.execute("UPDATE callbacks SET expires=0 WHERE token=?", (token,))
    with pytest.raises(Rejected):
        await avatar.action(event, payload, token)
    avatar.runtime.config["platform_id"] = "Other"
    with pytest.raises(Rejected):
        await avatar.action(event, {"action": "avatar_home"})
    assert avatar.remaining(2) == 2


@pytest.mark.asyncio
async def test_send_failure_cache_unknown_and_explicit_resend(avatar):
    event = update()
    avatar.runtime.bot.send_document.side_effect = TimedOut()
    payload, token = confirmation(avatar, event)
    with pytest.raises(TimedOut):
        await avatar.action(event, payload, token)
    row = avatar.store.db.execute("SELECT * FROM avatar_jobs").fetchone()
    assert row["status"] == "ready" and row["delivery"] == "unknown"
    assert avatar.remaining(2) == 1
    await avatar.deliver(event, row["id"])
    assert avatar.runtime.bot.send_document.await_count == 1
    avatar.runtime.bot.send_document.side_effect = None
    await avatar.deliver(event, row["id"], explicit=True)
    assert avatar.runtime.bot.send_document.await_count == 2
    assert avatar.remaining(2) == 1
    with pytest.raises(Rejected):
        await avatar.deliver(update(uid=3), row["id"], explicit=True)


@pytest.mark.asyncio
async def test_render_failure_releases_quota_and_forbidden_keeps_artifact(
    avatar, monkeypatch
):
    event = update()
    with monkeypatch.context() as patch:
        patch.setattr(avatar, "render", AsyncMock(side_effect=OSError("disk")))
        payload, token = confirmation(avatar, event)
        with pytest.raises(Rejected):
            await avatar.action(event, payload, token)
    assert avatar.remaining(2) == 2
    avatar.runtime.bot.send_document.side_effect = Forbidden("denied")
    payload, token = confirmation(avatar, event)
    with pytest.raises(Rejected):
        await avatar.action(event, payload, token)
    assert avatar.remaining(2) == 1
    row = avatar.store.db.execute(
        "SELECT * FROM avatar_jobs WHERE status='ready'"
    ).fetchone()
    assert row["delivery"] == "failed_delivery"
    assert (avatar.root / (row["id"] + ".png")).exists()


@pytest.mark.asyncio
async def test_restart_recovers_atomic_artifact_and_does_not_resend(avatar):
    event = update()
    payload, token = confirmation(avatar, event)
    await avatar.action(event, payload, token)
    avatar.store.db.execute(
        "UPDATE avatar_jobs SET status='rendering',delivery='sending'"
    )
    restarted = Avatar(avatar.runtime)
    row = avatar.store.db.execute("SELECT * FROM avatar_jobs").fetchone()
    assert row["status"] == "ready" and row["delivery"] == "unknown"
    assert restarted.remaining(2) == 1
    assert avatar.runtime.bot.send_document.await_count == 1
    Avatar(avatar.runtime)
    assert restarted.remaining(2) == 1


@pytest.mark.asyncio
async def test_custom_input_exact_reply_navigation_and_non_owner(avatar):
    event = update(chat=-1001)
    await avatar.action(event, {"action": "avatar_custom"})
    assert not await avatar.message(
        update(uid=3, chat=-1001, reply=SimpleNamespace(message_id=55)), ""
    )
    assert not await avatar.message(event, "")
    event.message.reply_to_message = SimpleNamespace(message_id=55)
    assert await avatar.message(event, "")
    assert avatar.runtime.bot.send_photo.await_count == 1
    assert avatar.remaining(2) == 2
    await avatar.action(event, {"action": "avatar_custom"})
    event.message.reply_to_message = None
    assert not await avatar.message(event, "/points")
    assert not avatar.store.db.execute("SELECT 1 FROM avatar_inputs").fetchone()


@pytest.mark.asyncio
async def test_admin_only_disable_and_old_confirmation(avatar):
    with pytest.raises(Rejected):
        await avatar.action(update(), {"action": "avatar_admin"})
    owner = update(uid=1, chat=1)
    await avatar.action(owner, {"action": "avatar_toggle_preview"})
    token = (
        avatar.runtime.bot.send_message.await_args.kwargs["reply_markup"]
        .inline_keyboard[0][0]
        .callback_data[3:]
    )
    payload = avatar.store.resolve(token, "1", "1")
    await avatar.action(owner, payload, token)
    assert not avatar.enabled()
    with pytest.raises(Rejected):
        await avatar.action(owner, payload, token)
    with pytest.raises(Rejected):
        await avatar.action(update(), {"action": "avatar_pick"})


@pytest.mark.asyncio
async def test_actual_write_failure_and_database_commit_recovery(avatar, monkeypatch):
    from pathlib import Path

    event = update()
    original = Path.open

    def fail_temporary(path, *args, **kwargs):
        if path.suffix == ".tmp":
            raise OSError("No space left")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", fail_temporary)
        payload, token = confirmation(avatar, event)
        with pytest.raises(Rejected):
            await avatar.action(event, payload, token)
    assert avatar.remaining(2) == 2
    avatar.store.db.execute(
        "CREATE TRIGGER fail_ready BEFORE UPDATE ON avatar_jobs WHEN NEW.status='ready' "
        "BEGIN SELECT RAISE(ABORT,'write failed'); END"
    )
    payload, token = confirmation(avatar, event)
    with pytest.raises(Rejected):
        await avatar.action(event, payload, token)
    assert avatar.remaining(2) == 1
    avatar.store.db.execute("DROP TRIGGER fail_ready")
    restarted = Avatar(avatar.runtime)
    assert restarted.remaining(2) == 1
    assert avatar.store.db.execute(
        "SELECT 1 FROM avatar_jobs WHERE status='ready'"
    ).fetchone()


@pytest.mark.asyncio
async def test_preview_is_not_a_free_full_resolution_artifact(avatar):
    await avatar.action(update(), {"action": "avatar_preview", "name": "青鱼"})
    sent = avatar.runtime.bot.send_photo.await_args.kwargs["photo"]
    preview = Image.open(BytesIO(sent))
    assert preview.size == (640, 640)
    plain = Image.open(BytesIO(compose("青鱼"))).convert("RGB").resize((640, 640))
    assert ImageChops.difference(plain, preview).getbbox()
    assert avatar.remaining(2) == 2
