import asyncio
import base64
import copy
import io
import os
import struct
import time
import zlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web
from PIL import Image

from data.plugins.astrbot_plugin_lajiaoliao.business import (
    DEFAULTS,
    BusinessStore,
)
from data.plugins.astrbot_plugin_lajiaoliao.media import (
    Images,
    PublicResolver,
    validate_bytes,
    validate_url,
)
from data.plugins.astrbot_plugin_lajiaoliao.protocol import LajiaoLiaoProtocolError
from data.plugins.astrbot_plugin_lajiaoliao.storage import SessionError


def rules(**updates):
    value = copy.deepcopy(DEFAULTS)
    value.update(updates)
    return value


@pytest.mark.parametrize(
    "url",
    [
        "http://lj-im-chat-01.lajiaoliao.com/a",
        "https://127.0.0.1/a",
        "https://169.254.169.254/a",
        "https://[::1]/a",
        "https://lj-im-chat-01.lajiaoliao.com.evil.test/a",
        "https://user@lj-im-chat-01.lajiaoliao.com/a",
        "https://lj-im-chat-01.lajiaoliao.com:8443/a",
        "https://[invalid/a",
    ],
)
def test_untrusted_image_addresses(url):
    with pytest.raises(LajiaoLiaoProtocolError):
        validate_url(url, inbound=True)


def test_image_formats_private_files_and_cleanup(tmp_path):
    async def run():
        raw = io.BytesIO()
        Image.new("RGB", (4, 3)).save(raw, format="PNG")
        assert validate_bytes(raw.getvalue()) == "image/png"
        images = Images(tmp_path)
        path, mime = await images.materialize(
            "base64://" + base64.b64encode(raw.getvalue()).decode()
        )
        assert mime == "image/png" and path.stat().st_mode & 0o777 == 0o600
        assert images.directory.stat().st_mode & 0o777 == 0o700
        with pytest.raises(LajiaoLiaoProtocolError):
            await images.materialize("/etc/passwd")
        with pytest.raises(LajiaoLiaoProtocolError, match="base64_invalid"):
            await images.materialize("data:image/png;base64")
        replaced = images.directory / "symlink"
        replaced.symlink_to("/etc/passwd")
        with pytest.raises(LajiaoLiaoProtocolError, match="local_path_rejected"):
            images.read_local(replaced)
        raw = io.BytesIO()
        Image.new("RGB", (4, 3)).save(raw, format="GIF")
        assert validate_bytes(raw.getvalue()) == "image/gif"
        animated = io.BytesIO()
        Image.new("RGB", (4, 3), "red").save(
            animated,
            format="GIF",
            save_all=True,
            append_images=[Image.new("RGB", (4, 3), "blue")],
            duration=100,
            loop=0,
        )
        assert validate_bytes(animated.getvalue()) == "image/gif"
        path, mime = await images.materialize(
            "base64://" + base64.b64encode(animated.getvalue()).decode()
        )
        assert mime == "image/gif" and path.suffix == ".gif"
        with Image.open(path) as decoded:
            assert decoded.format == "GIF" and decoded.n_frames == 2
        path.unlink()

    asyncio.run(run())


def test_image_byte_pixel_animation_limits_and_stale_cleanup(tmp_path):
    with pytest.raises(LajiaoLiaoProtocolError, match="size_exceeded"):
        validate_bytes(b"x" * ((20 << 20) + 1))
    header = struct.pack(">IIBBBBB", 10000, 10000, 8, 2, 0, 0, 0)
    chunk = b"IHDR" + header
    raw = (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", len(header))
        + chunk
        + struct.pack(">I", zlib.crc32(chunk))
    )
    with pytest.raises(LajiaoLiaoProtocolError, match="dimensions_rejected"):
        validate_bytes(raw)
    animation = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(
        animation,
        format="PNG",
        save_all=True,
        append_images=[Image.new("RGB", (2, 2), "blue")],
        duration=10,
        loop=0,
    )
    with pytest.raises(LajiaoLiaoProtocolError, match="format_or_dimensions"):
        validate_bytes(animation.getvalue())
    many_frames = io.BytesIO()
    Image.new("RGB", (1, 1), "red").save(
        many_frames,
        format="GIF",
        save_all=True,
        append_images=[
            Image.new("RGB", (1, 1), (index, 0, 0)) for index in range(120)
        ],
        duration=10,
        loop=0,
    )
    with pytest.raises(LajiaoLiaoProtocolError, match="format_or_dimensions"):
        validate_bytes(many_frames.getvalue())
    images = Images(tmp_path)
    expired = images.directory / "image-expired"
    current = images.directory / "image-current"
    expired.touch()
    current.touch()
    past = time.time() - 86401
    os.utime(expired, (past, past))
    images.cleanup()
    assert not expired.exists() and current.exists()


def test_media_materialization_global_concurrency_is_two(tmp_path, monkeypatch):
    import threading

    from data.plugins.astrbot_plugin_lajiaoliao import media

    active = peak = 0
    guard = threading.Lock()
    validate = media.validate_bytes

    def measured(raw):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.03)
            return validate(raw)
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(media, "validate_bytes", measured)
    monkeypatch.setattr(media, "SLOTS", asyncio.Semaphore(2))

    async def run():
        raw = io.BytesIO()
        Image.new("RGB", (2, 2)).save(raw, format="PNG")
        value = "base64://" + base64.b64encode(raw.getvalue()).decode()
        accounts = [Images(tmp_path / str(index)) for index in range(6)]
        results = await asyncio.gather(
            *(account.materialize(value) for account in accounts)
        )
        assert peak == 2
        assert len({path for path, _ in results}) == 6
        for path, _ in results:
            path.unlink()

    asyncio.run(run())


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "100.64.0.1", "::1"]
)
def test_media_resolver_rejects_rebound_and_reserved_addresses(address):
    async def run():
        resolver = PublicResolver.__new__(PublicResolver)
        resolver.delegate = SimpleNamespace(
            resolve=AsyncMock(return_value=[{"host": "8.8.8.8"}, {"host": address}]),
            close=AsyncMock(),
        )
        with pytest.raises(LajiaoLiaoProtocolError) as error:
            await resolver.resolve("im-api.lajiaoliao.com", 443)
        assert error.value.delivery_status == "rejected"
        await resolver.close()

    asyncio.run(run())


def test_future_tasks_default_off_conflicts_and_scoped_versions(tmp_path, monkeypatch):
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    store = BusinessStore(tmp_path)
    assert store.scheduled("g") == []
    value = rules(lottery_enabled=True)
    value["lottery"].update(prize="Prize", contact="Contact")
    value["lottery_plan"] = {"enabled": True, "starts_at": now + 60}
    store.save_settings("g", 0, value, "dashboard:owner")
    jobs = store.scheduled("g")
    assert (
        len(jobs) == 1
        and jobs[0]["state"] == "pending"
        and jobs[0]["owner"] == "dashboard:owner"
    )
    value["rankings_enabled"] = True
    store.save_settings("g", 1, value, "dashboard:owner")
    assert store.scheduled("g")[0]["id"] == jobs[0]["id"]
    with pytest.raises(SessionError):
        store.save_settings("g", 0, value, "dashboard:owner")
    assert store.reserve_job(jobs[0]["id"], now) is None
    now += 61
    assert store.reserve_job(jobs[0]["id"], now)
    restarted = BusinessStore(tmp_path)
    assert restarted.scheduled("g")[0]["state"] == "unknown"
    assert restarted.reserve_job(jobs[0]["id"], now) is None


def test_missed_boundary_pauses_and_does_not_compensate(tmp_path, monkeypatch):
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    store = BusinessStore(tmp_path)
    value = rules(lottery_enabled=True)
    value["lottery"].update(prize="Prize", contact="Contact")
    value["lottery_plan"] = {"enabled": True, "starts_at": now + 60}
    store.save_settings("g", 0, value, "dashboard:owner")
    identifier = store.scheduled("g")[0]["id"]
    assert store.reserve_job(identifier, now + 100) is None
    assert store.scheduled("g")[0]["state"] == "paused"


def test_name_original_graphemes_dedup_and_unknown_quarantine(tmp_path):
    store = BusinessStore(tmp_path)
    config = copy.deepcopy(DEFAULTS["maintenance"])
    config.update(prefix="[", suffix="]", name_characters=2)
    name = "A\u0301bc"
    target = store.card_plan("g", "u", name, config)
    assert target == "[A\u0301b]"
    store.card_applied("g", "u", target)
    assert store.card_plan("g", "u", target, config) is None
    config["suffix"] = "!"
    assert store.card_plan("g", "u", target, config) == "[A\u0301b!"
    identifier = store.reserve_effect("g", "card", "one", "u", {})
    assert identifier and store.reserve_effect("g", "card", "two", "u", {}) is None
    store.finish_effect(identifier, {"status": "succeeded"})
    assert store.reserve_effect("g", "card", "one", "u", {}) is None
    assert store.reserve_effect("g", "card", "two", "u", {})


def test_invitation_gate_both_enrollment_and_draw(tmp_path, monkeypatch):
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    store = BusinessStore(tmp_path)
    value = rules(lottery_enabled=True, invitations_enabled=True)
    value["lottery"].update(prize="Prize", contact="Contact", min_invitations=1)
    store.save_settings("g", 0, value, "dashboard:owner", [])
    identifier, _ = store.prepare_activity("g", "owner", "umo", 1)
    store.activity_receipt(identifier, "opening", {"status": "sent"})
    with pytest.raises(SessionError, match="threshold"):
        store.enroll("g", "u", "Member", now)
    with store.connect() as db:
        db.execute(
            "INSERT INTO invitations VALUES(?,?,?,?,?,?)",
            ("g", "new", "u", "event", now, 0),
        )
    assert store.enroll("g", "u", "Member", now) == "enrolled"
    with store.connect() as db:
        db.execute("DELETE FROM invitations")
    winners, count, _ = store.draw(identifier, {"u"})
    assert winners == [] and count == 0


def test_manager_departure_does_not_reset_unrelated_jobs(tmp_path):
    store = BusinessStore(tmp_path)
    store.save_settings(
        "g", 0, rules(manager_user_ids=["present", "left"]), "dashboard:owner"
    )
    store.revoke_departed_managers("g", ["present"])
    assert store.settings("g")["settings"]["manager_user_ids"] == ["present"]
    assert store.settings("g")["version"] == 2


def test_reconciliation_rotates_bounded_members_and_survives_restart(tmp_path):
    store = BusinessStore(tmp_path)
    members = [f"{number:04}" for number in range(50)]
    first = store.scan_batch("g", "invitations", members, 20)
    assert first == members[:20]
    restarted = BusinessStore(tmp_path)
    second = restarted.scan_batch("g", "invitations", members, 20)
    assert second == members[20:40]
    third = restarted.scan_batch("g", "invitations", members, 20)
    assert third == members[40:] + members[:10]
    assert restarted.scan_batch("other", "invitations", members, 20) == first


def test_progressive_grace_counts_successes_only(tmp_path, monkeypatch):
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    value = rules(review_enabled=True, native_actions=["mute", "revoke_others"])
    value["automation"].update(enabled=True, punishment_mode="recall_progressive")
    store = BusinessStore(tmp_path)
    store.save_settings("g", 0, value, "dashboard:owner")
    for index, seconds in enumerate([0, 0, 0, 300, 900, 3600]):
        plan = store.reserve_automatic("g", str(index), "u", now, "keyword", 1)
        assert plan["muted_seconds"] == seconds
        store.finish_automatic("g", plan["operation_id"], "succeeded")
        now += 90


def test_chat_settings_draft_cannot_enable_new_automation(tmp_path):
    store = BusinessStore(tmp_path)
    store.save_settings("g", 0, rules(), "dashboard:owner")
    value = rules()
    value["maintenance"]["reply_recall_enabled"] = True
    value["native_actions"] = ["reply_recall"]
    draft = store.preview_settings("g", "delegate", "umo", value, 1)
    with pytest.raises(SessionError, match="dashboard_(automation|action_grants)"):
        store.confirm_settings("g", "delegate", "umo", draft)


@pytest.mark.parametrize("fail_second", [False, True, "policy"])
def test_image_batch_preflight_order_partial_receipts_and_cleanup(
    tmp_path, fail_second, monkeypatch
):
    async def run():
        import astrbot.api.platform

        monkeypatch.setattr(
            astrbot.api.platform,
            "register_platform_adapter",
            lambda *args, **kwargs: lambda cls: cls,
        )
        from data.plugins.astrbot_plugin_lajiaoliao.adapter import LajiaoLiaoAdapter

        calls = []

        async def upload(request):
            reader = await request.multipart()
            part = await reader.next()
            validate_bytes(await part.read())
            calls.append("upload")
            return web.json_response({"image_ref": "private-reference"})

        async def send(request):
            value = await request.json()
            calls.append(value["parts"][0]["type"])
            if fail_second == "policy" and calls.count("text") == 1:
                adapter.config["message_policy"]["group_ids"] = []
            if fail_second is True and calls.count("text") == 2:
                return web.json_response({"status": "unknown"}, status=502)
            return web.json_response({"status": "sent", "message_id": str(len(calls))})

        app = web.Application()
        app.router.add_post("/v1/media/images", upload)
        app.router.add_post("/v1/messages", send)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        address = site._server.sockets[0].getsockname()[1]
        raw = io.BytesIO()
        Image.new("RGB", (4, 3)).save(raw, format="PNG")
        source = "base64://" + base64.b64encode(raw.getvalue()).decode()
        adapter = object.__new__(LajiaoLiaoAdapter)
        adapter.manager = SimpleNamespace(commerce=None)
        adapter.config = {
            "id": "one",
            "message_policy": {
                "group_mode": "allowlist",
                "group_ids": ["g"],
                "private_enabled": False,
            },
        }
        adapter.images = Images(tmp_path)
        adapter._send_lock = asyncio.Lock()
        adapter.base_url = f"http://127.0.0.1:{address}"
        try:
            async with aiohttp.ClientSession() as adapter.http:
                for parts in [
                    [{"type": "text", "text": "no"}, {"type": "file", "url": source}],
                    [
                        {"type": "quote", "message_id": "old"},
                        {"type": "image", "url": source},
                    ],
                    [
                        {"type": "image", "url": source},
                        {"type": "image", "url": "/etc/passwd"},
                    ],
                ]:
                    with pytest.raises(LajiaoLiaoProtocolError):
                        await adapter.send_parts("g", "", parts, "group")
                    assert calls == []
                parts = [
                    {"type": "text", "text": "before"},
                    {"type": "image", "url": source},
                    {"type": "text", "text": "after"},
                ]
                if fail_second == "policy":
                    with pytest.raises(LajiaoLiaoProtocolError) as error:
                        await adapter.send_parts("g", "", parts, "group")
                    assert error.value.partial
                    assert error.value.delivery_status == "rejected"
                    assert [item["status"] for item in error.value.receipts] == [
                        "sent",
                        "rejected",
                    ]
                    assert error.value.receipts[-1]["part_index"] == 1
                elif fail_second:
                    with pytest.raises(LajiaoLiaoProtocolError) as error:
                        await adapter.send_parts("g", "", parts, "group")
                    assert (
                        error.value.partial and error.value.delivery_status == "unknown"
                    )
                    assert len(error.value.receipts) == 3
                else:
                    receipt = await adapter.send_parts("g", "", parts, "group")
                    assert len(receipt["receipts"]) == 3 and receipt["status"] == "sent"
                assert calls == (
                    ["upload", "text"]
                    if fail_second == "policy"
                    else ["upload", "text", "image", "text"]
                )
                assert list(adapter.images.directory.iterdir()) == []
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_scheduled_job_rechecks_principal_and_feature_revision(tmp_path, monkeypatch):
    from test_lajiaoliao_businesses import prepared_plugin

    async def run():
        plugin, config, instance = prepared_plugin(tmp_path, monkeypatch)
        ref = config["session_ref"]
        now = time.time()
        value = rules(reminders_enabled=True)
        value["reminder_plan"] = {
            "enabled": True,
            "clock": "09:00",
            "text": "Notification",
        }
        await plugin.business.db(ref, "save_settings", "g", 0, value, "dashboard:owner")
        store = plugin.business.stores[ref]
        with store.connect() as db:
            db.execute("UPDATE scheduled_jobs SET due=?", (now,))
        plugin.business.task_authorized = AsyncMock(side_effect=[True, False])
        await plugin.business.poll_scheduled(ref)
        instance.send_parts.assert_not_awaited()
        assert store.scheduled("g")[0]["state"] == "failed"
        await plugin.business.close()

    asyncio.run(run())


def test_reserved_job_expires_during_authorization_without_sending(
    tmp_path, monkeypatch
):
    from test_lajiaoliao_businesses import prepared_plugin

    async def run():
        plugin, config, instance = prepared_plugin(tmp_path, monkeypatch)
        ref = config["session_ref"]
        now = time.time()
        value = rules(reminders_enabled=True)
        value["reminder_plan"] = {
            "enabled": True,
            "clock": "09:00",
            "text": "Notification",
        }
        await plugin.business.db(ref, "save_settings", "g", 0, value, "dashboard:owner")
        store = plugin.business.stores[ref]
        with store.connect() as db:
            db.execute("UPDATE scheduled_jobs SET due=?", (now,))
        calls = 0

        async def authorize(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                monkeypatch.setattr(time, "time", lambda: now + 31)
            return True

        plugin.business.task_authorized = authorize
        await plugin.business.poll_scheduled(ref)
        instance.send_parts.assert_not_awaited()
        assert store.scheduled("g")[0]["state"] == "paused"
        await plugin.business.poll_scheduled(ref)
        instance.send_parts.assert_not_awaited()
        await plugin.business.close()

    asyncio.run(run())


def test_lottery_boundary_expiry_closes_unsent_opening(tmp_path, monkeypatch):
    from test_lajiaoliao_businesses import prepared_plugin

    async def run():
        plugin, config, instance = prepared_plugin(tmp_path, monkeypatch)
        ref = config["session_ref"]
        now = time.time()
        value = rules(lottery_enabled=True)
        value["lottery"].update(prize="Prize", contact="Contact")
        value["lottery_plan"] = {"enabled": True, "starts_at": now + 60}
        await plugin.business.db(ref, "save_settings", "g", 0, value, "dashboard:owner")
        store = plugin.business.stores[ref]
        with store.connect() as db:
            db.execute("UPDATE scheduled_jobs SET due=?", (now,))
        calls = 0

        async def authorize(*args):
            nonlocal calls
            calls += 1
            if calls == 3:
                monkeypatch.setattr(time, "time", lambda: now + 31)
            return True

        plugin.business.task_authorized = authorize
        plugin.business.roster = AsyncMock(return_value={})
        await plugin.business.poll_scheduled(ref)
        instance.send_parts.assert_not_awaited()
        assert store.scheduled("g")[0]["state"] == "paused"
        assert store.activities("g")[0]["state"] == "failed"
        await plugin.business.poll_scheduled(ref)
        instance.send_parts.assert_not_awaited()
        await plugin.business.close()

    asyncio.run(run())


def test_reply_recall_requires_an_explicit_group_action_grant(tmp_path):
    from data.plugins.astrbot_plugin_lajiaoliao.business import validate_settings

    value = copy.deepcopy(DEFAULTS)
    value["maintenance"]["reply_recall_enabled"] = True
    with pytest.raises(ValueError, match="maintenance_action_grant_required"):
        validate_settings(value)
    value["native_actions"] = ["reply_recall"]
    assert validate_settings(value)["maintenance"]["reply_recall_enabled"]
    store = BusinessStore(tmp_path)
    store.save_settings("g", 0, value, "dashboard:owner")
    store.schedule_reply("g", "message", "group", "g", "dashboard:owner")
    assert len(store.scheduled("g")) == 1


def test_legacy_api_payload_does_not_silently_enable_invitation_rewards(
    tmp_path, monkeypatch
):
    from test_lajiaoliao_businesses import prepared_plugin
    from test_lajiaoliao_plugin_api import call

    from astrbot.core.platform.instance_control import instance_revision

    async def run():
        plugin, config, instance = prepared_plugin(tmp_path, monkeypatch)
        plugin.business.roster = AsyncMock(
            return_value={"ordinary": {"userID": "ordinary"}}
        )
        value = copy.deepcopy(DEFAULTS)
        value["invitations_enabled"] = True
        value.pop("invitation_rewards_enabled")
        status, data = await call(
            plugin,
            "business_page",
            "alice",
            {
                "session_ref": config["session_ref"],
                "group_id": "g",
                "action": "save",
                "confirm": True,
                "settings": value,
                "version": 0,
                "instance_revision": instance_revision(config),
            },
        )
        assert status == 200 and data["settings"]["invitations_enabled"]
        assert not data["settings"]["invitation_rewards_enabled"]
        instance.send_parts.assert_not_awaited()
        await plugin.business.close()

    asyncio.run(run())
