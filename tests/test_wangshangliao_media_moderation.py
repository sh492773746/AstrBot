"""QR/OCR inspection is bounded, scoped, revocable and recall-only."""

import asyncio
import io
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image, ImageDraw, ImageFont

from astrbot.builtin_stars.wangshangliao_moderation import (
    content_rules,
    media_moderation,
    media_worker,
    member_exemptions,
    policy,
)
from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.group_scope import group_config, scoped
from astrbot.core.platform.sources.wangshangliao.runtime_policy import PolicySnapshot
from tests.test_wangshangliao_content_fastlane import runtime_for
from tests.test_wangshangliao_flood import fresh_bot, ingest
from tests.test_wangshangliao_image_intake import image_message
from tests.test_wangshangliao_open_service import activate
from tests.test_wangshangliao_open_service import store as store

bot = fresh_bot
WORKER = Path(media_moderation.__file__).with_name("media_worker.py")
METADATA = {"asset": "123456789", "width": 400, "height": 160, "size": 200}


@pytest.mark.parametrize(
    "field", ["recall_forward", "image_qr_recall", "image_digits_recall"]
)
@pytest.mark.parametrize("enabled", [True, False])
def test_media_flags_are_typed_scoped_and_cannot_be_dropped(store, field, enabled):
    from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError

    row = activate(store)
    rules = {**row["rules"], field: enabled}
    store.change("99", "5", row["version"], "1", rules=rules)
    saved = store.tenant("99", "5")
    assert saved["rules"][field] is enabled
    assert field not in store.tenant("99", "6")["rules"]
    with pytest.raises(OpenError, match="tenant_rules_refresh"):
        store.change("99", "5", saved["version"], "1", rules=row["rules"])
    with pytest.raises(OpenError, match="tenant_invalid"):
        store.change("99", "5", saved["version"], "1", rules={**rules, field: "true"})


def configure(bot):
    """Restrict only the fixture group and permit recall, never mute."""
    bot.diagnostics.gauges = {}
    bot.config.update(
        _open_groups={
            "5": {
                "advertising": False,
                "flood": False,
                "keywords": [],
                "recall_forward": True,
                "image_qr_recall": True,
                "image_digits_recall": True,
            },
            "6": {"advertising": False, "flood": False, "keywords": []},
        },
        _open_native_roles={"5": "GROUP_ROLE_ADMIN", "6": "GROUP_ROLE_ADMIN"},
        _open_revisions={"5": "media-5", "6": "media-6"},
    )
    bot.config["moderation"]["permissions"]["5"] = ["recall"]
    bot.business = SimpleNamespace(
        deployment=SimpleNamespace(origin="https://47.86.91.23:5015")
    )


@pytest.mark.asyncio
async def test_forward_recall_has_no_warning_mute_or_nested_target(bot):
    configure(bot)
    runtime = runtime_for(bot)
    try:
        payload = await ingest(bot, "100", "", kind="forward")
        assert payload["_ingress_content"]["category"] == "restricted_forward"
        await runtime.step()
        await asyncio.gather(*runtime.workers.values())
        assert bot.calls == [("recall", "100", "22")]
        bot.execute_moderation.assert_not_awaited()
        await policy.handle(bot, "5", "100", payload)
        assert len(bot.calls) == 1
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["white", "admin", "stale", "disabled", "other"])
async def test_forward_protections_and_group_isolation(
    bot, monkeypatch, tmp_path, change
):
    configure(bot)
    if change == "white":
        monkeypatch.setattr(member_exemptions, "instance_dir", lambda _: tmp_path)
        member_exemptions.set_exemption(
            "fixture",
            "1",
            "5",
            "2",
            "22",
            enabled=True,
            actor="server/test",
            reason="Fixture",
            guard=lambda: None,
        )
    if change == "admin":
        bot.get_moderation_members.return_value["groupMemberInfo"][1]["groupRole"] = (
            "GROUP_ROLE_ADMIN"
        )
    if change == "disabled":
        bot.config["_open_groups"]["5"]["recall_forward"] = False
    payload = await ingest(
        bot,
        "100",
        "",
        kind="forward",
        stamp=int((time.time() - 1000) * 1000) if change == "stale" else None,
    )
    await policy.handle(bot, "6" if change == "other" else "5", "100", payload)
    assert not bot.calls


def test_media_switches_default_off_and_static_stickers_stay_private_safe():
    config = {
        "_open_groups": {"5": {"advertising": False, "flood": False, "keywords": []}},
        "moderation": {},
    }
    assert group_config(config, "5")["moderation"]["image_qr_recall"] is False
    assert not content_rules.content_category({"kind": "forward"}, {})
    message = wire.ApplicationMessage.FromString(
        image_message(is_sticker=True).SerializeToString()
    )
    assert wire.image_metadata(message) is None
    assert wire.image_metadata(message, allow_sticker=True)["asset"] == "123456789"


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["image_qr", "image_digits"])
async def test_json_or_model_cannot_forge_ocr_evidence(bot, category):
    configure(bot)
    payload = await ingest(bot, "100", "", kind="image")
    payload["image"] = METADATA
    assert not await content_rules.enforce(
        scoped(bot, "5"),
        "5",
        "100",
        payload,
        assessment={
            "decision": "violation",
            "category": category,
            "message_id": "100",
            "_media_proof": {"qr": True, "digits": True},
        },
    )
    assert not bot.calls


@pytest.mark.asyncio
async def test_image_admission_never_waits_and_deduplicates_both_lanes(
    bot, monkeypatch
):
    configure(bot)
    runtime = media_moderation.MediaModeration()
    waiting = asyncio.Event()

    async def inspect(*args):
        await waiting.wait()

    monkeypatch.setattr(runtime, "inspect", inspect)
    try:
        payload = await ingest(bot, "100", "", kind="image")
        payload["image"] = METADATA
        for number in range(
            media_moderation.MAX_GROUP_PENDING_IMAGES
            + media_moderation.MAX_MEDIA_WORKERS
        ):
            assert await policy.handle(
                bot, "5", str(100 + number), payload, media_runtime=runtime
            )
        assert runtime.enqueue(scoped(bot, "5"), "5", "100", payload)
        assert len(runtime.tasks) == media_moderation.MAX_MEDIA_WORKERS
        assert runtime.queued == media_moderation.MAX_GROUP_PENDING_IMAGES
        assert not runtime.enqueue(scoped(bot, "5"), "5", "200", payload)
        assert runtime.counts["busy"] == 1
        assert runtime.counts["unchecked"] == 1
        assert runtime.counts["clear"] == 0
        assert not bot.calls
    finally:
        await runtime.close()
    assert not runtime.tasks


@pytest.mark.asyncio
async def test_image_fastlane_works_without_chat_dispatch(bot):
    configure(bot)
    media = SimpleNamespace(enqueue=Mock(return_value=True))
    runtime = runtime_for(bot, media_runtime=media)
    payload = {
        "sender": "2",
        "text": "",
        "kind": "image",
        "image": METADATA,
        "recall_route": {"time": str(int(time.time() * 1000)), "peer": "22"},
    }
    try:

        def observe(envelope, received):
            snapshot = PolicySnapshot("1", "1:1", json.dumps(bot.config), 0)
            bot.settings.observe(snapshot, "1", "5", "100", envelope, received)

        await bot.ledger.ingest("1", "5", "100", payload, observe=observe)
        await runtime.step()
        await asyncio.gather(*runtime.workers.values())
        media.enqueue.assert_called_once()
        assert media.enqueue.call_args.args[3]["_ingress_content"]["category"] == (
            "image_inspection"
        )
        assert not bot.calls
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "none",
        "policy",
        "account",
        "revision",
        "pause",
        "whitelist",
        "admin",
        "timeout",
        "bad_result",
        "closed",
        "permission",
        "download",
        "recompressed",
        "placeholder",
        "unsupported_format",
        "unsupported_animation",
        "ocr_failed",
    ],
)
async def test_late_image_results_are_fenced_and_only_recall(
    bot, monkeypatch, tmp_path, change
):
    configure(bot)
    runtime = media_moderation.MediaModeration()
    payload = await ingest(bot, "100", "", kind="image")
    payload["image"] = METADATA
    if change == "placeholder":
        payload["image"] = {**METADATA, "size": 100}
    process = SimpleNamespace(
        returncode=0,
        communicate=AsyncMock(return_value=(b'{"qr":true,"digits":false}', b"")),
        wait=AsyncMock(),
    )
    if change == "timeout":
        process.communicate.side_effect = TimeoutError
    elif change == "bad_result":
        process.communicate.return_value = (b'{"qr":"true"}', b"")
    elif change in {"unsupported_format", "unsupported_animation", "ocr_failed"}:
        process.returncode = 1
        reason = {
            "unsupported_format": "media_image_unsupported_format",
            "unsupported_animation": "media_image_unsupported_animation",
            "ocr_failed": "media_ocr_failed",
        }[change]
        process.communicate.return_value = (json.dumps({"error": reason}).encode(), b"")
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    response = SimpleNamespace(status=503 if change == "download" else 200)

    class Request:
        async def __aenter__(self):
            return response

        async def __aexit__(self, *_):
            return False

    class Session(Request):
        def get(self, url, **kwargs):
            assert url.endswith("/assets/123456789")
            assert kwargs["allow_redirects"] is False
            return Request()

        async def __aenter__(self):
            return self

    monkeypatch.setattr(
        media_moderation.aiohttp, "ClientSession", lambda **_: Session()
    )

    async def read(*_):
        if change == "policy":
            bot.config["_open_groups"]["5"]["image_qr_recall"] = False
        elif change == "account":
            bot.account = "99"
        elif change == "revision":
            bot.config["_open_revisions"]["5"] = "new"
        elif change == "pause":
            bot.config["_business_active"] = False
        elif change == "closed":
            runtime.active = False
        elif change == "permission":
            bot.config["moderation"]["permissions"]["5"] = []
        elif change == "admin":
            bot.get_moderation_members.return_value["groupMemberInfo"][1][
                "groupRole"
            ] = "GROUP_ROLE_ADMIN"
        elif change == "whitelist":
            monkeypatch.setattr(member_exemptions, "instance_dir", lambda _: tmp_path)
            member_exemptions.set_exemption(
                "fixture",
                "1",
                "5",
                "2",
                "22",
                enabled=True,
                actor="server/test",
                reason="Fixture",
                guard=lambda: None,
            )
        return b"x" * (201 if change == "recompressed" else 200)

    monkeypatch.setattr(media_moderation, "_read_bounded", read)
    try:
        assert await policy.handle(bot, "5", "100", payload, media_runtime=runtime)
        await asyncio.gather(*runtime.tasks)
        assert bot.calls == (
            [("recall", "100", "22")]
            if change in {"none", "recompressed", "placeholder"}
            else []
        )
        bot.execute_moderation.assert_not_awaited()
        if change in {"unsupported_format", "unsupported_animation"}:
            assert runtime.counts["skipped"] == 1
            assert runtime.counts["failed"] == 0
            assert runtime.counts[change] == 1
            assert bot.diagnostics.gauges["media_" + change] == 1
        elif change == "ocr_failed":
            assert runtime.counts["failed"] == 1
            assert bot.diagnostics.emit.call_args.kwargs["error"] == "media_ocr_failed"
        elif change == "timeout":
            assert (
                bot.diagnostics.emit.call_args.kwargs["error"]
                == "media_recognizer_timeout"
            )
        observed = {call.args[0] for call in bot.diagnostics.observe.call_args_list}
        assert {"media_queue", "media_download", "media_total"} <= observed
    finally:
        await runtime.close()


@pytest.mark.parametrize("text,expected", [("123456", True), ("HELLO WORLD", False)])
@pytest.mark.parametrize("format", ["PNG", "GIF", "BMP"])
@pytest.mark.parametrize("dimensions", [("600", "180"), ("0", "0"), ("600", "0")])
def test_real_local_digit_worker(text, expected, format, dimensions):
    pytest.importorskip("cv2")
    if not shutil.which("tesseract"):
        pytest.skip("Optional local Tesseract runtime is not installed")
    bitmap = Image.new("RGB", (600, 180), "white")
    font = ImageFont.truetype("DejaVuSans.ttf", 56)
    ImageDraw.Draw(bitmap).text((30, 40), text, font=font, fill="black")
    buffer = io.BytesIO()
    bitmap.save(buffer, format)
    result = subprocess.run(
        [sys.executable, str(WORKER), *dimensions, "0", "1"],
        input=buffer.getvalue(),
        capture_output=True,
        timeout=12,
        check=True,
    )
    assert json.loads(result.stdout) == {"qr": False, "digits": expected}
    assert text.encode() not in result.stdout


def test_real_local_qr_worker_does_not_disclose_destination():
    cv2 = pytest.importorskip("cv2")
    code = cv2.QRCodeEncoder_create().encode("neutral-test-qr")
    code = cv2.copyMakeBorder(code, 4, 4, 4, 4, cv2.BORDER_CONSTANT, value=255)
    code = cv2.resize(code, (400, 400), interpolation=cv2.INTER_NEAREST)
    _, data = cv2.imencode(".png", code)
    result = subprocess.run(
        [sys.executable, str(WORKER), "400", "400", "1", "0"],
        input=data.tobytes(),
        capture_output=True,
        timeout=12,
        check=True,
    )
    assert json.loads(result.stdout) == {"qr": True, "digits": False}
    assert b"neutral-test" not in result.stdout


@pytest.mark.parametrize(
    "first,second,expected,calls",
    [
        ("high", None, True, 1),
        ("low", "high", True, 2),
        ("timeout", "high", True, 2),
        ("clear", "clear", False, 2),
        ("low", "low", False, 2),
        ("timeout", "clear", "unknown", 2),
    ],
)
def test_digit_fallback_preserves_confidence_budget_and_unknown(
    monkeypatch, capsys, first, second, expected, calls
):
    pytest.importorskip("cv2")
    bitmap = Image.new("RGB", (400, 160), "#aaaaaa")
    ImageDraw.Draw(bitmap).rectangle((40, 40, 240, 120), fill="#ffcc00")
    buffer = io.BytesIO()
    bitmap.save(buffer, "PNG")
    monkeypatch.setattr(
        sys, "stdin", SimpleNamespace(buffer=io.BytesIO(buffer.getvalue()))
    )
    monkeypatch.setattr(sys, "argv", [str(WORKER), "400", "160", "0", "1"])
    requests = []
    results = iter((first, second))

    def recognize(command, **kwargs):
        requests.append(kwargs)
        assert command == [
            "tesseract",
            "stdin",
            "stdout",
            "-l",
            "eng",
            "--psm",
            "11",
            "tsv",
        ]
        assert 0 < kwargs["timeout"] <= 3
        result = next(results)
        if result == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        confidence, text = {
            "high": (90, "123456"),
            "low": (74, "123456"),
            "clear": (95, "HELLO"),
        }[result]
        return SimpleNamespace(stdout=f"conf\ttext\n{confidence}\t{text}\n".encode())

    monkeypatch.setattr(media_worker.subprocess, "run", recognize)
    if expected == "unknown":
        with pytest.raises(RuntimeError, match="media_ocr_timeout"):
            media_worker.main()
        assert capsys.readouterr().out == ""
    else:
        media_worker.main()
        assert json.loads(capsys.readouterr().out) == {"qr": False, "digits": expected}
    assert len(requests) == calls
    if calls == 2:
        with Image.open(io.BytesIO(requests[1]["input"])) as binary:
            assert binary.size == bitmap.size
            assert set(binary.getdata()) <= {0, 255}


@pytest.mark.parametrize("invalid", ["dimensions", "format", "corrupt"])
def test_actual_worker_still_rejects_invalid_image_bytes(invalid):
    pytest.importorskip("cv2")
    bitmap = Image.new("RGB", (400, 160), "white")
    buffer = io.BytesIO()
    bitmap.save(buffer, "TIFF" if invalid == "format" else "PNG")
    result = subprocess.run(
        [
            sys.executable,
            str(WORKER),
            "401" if invalid == "dimensions" else "400",
            "160",
            "0",
            "1",
        ],
        input=b"not-an-image" if invalid == "corrupt" else buffer.getvalue(),
        capture_output=True,
        timeout=12,
    )
    assert result.returncode != 0
    error = json.loads(result.stdout)["error"]
    assert error == (
        "media_image_unsupported_format"
        if invalid == "format"
        else "media_image_invalid"
        if invalid == "dimensions"
        else "media_recognizer_failed"
    )
    assert not result.stderr


def test_animated_gif_is_unchecked_not_clear():
    pytest.importorskip("cv2")
    first = Image.new("RGB", (400, 160), "white")
    second = Image.new("RGB", (400, 160), "black")
    buffer = io.BytesIO()
    first.save(
        buffer, "GIF", save_all=True, append_images=[second], duration=100, loop=0
    )
    result = subprocess.run(
        [sys.executable, str(WORKER), "400", "160", "0", "1"],
        input=buffer.getvalue(),
        capture_output=True,
        timeout=12,
    )
    assert result.returncode != 0
    assert json.loads(result.stdout) == {"error": "media_image_unsupported_animation"}


@pytest.mark.asyncio
async def test_missing_metadata_has_its_own_coverage_counter(bot):
    configure(bot)
    runtime = media_moderation.MediaModeration()
    try:
        payload = await ingest(bot, "100", "", kind="image")
        assert not runtime.enqueue(scoped(bot, "5"), "5", "100", payload)
        assert runtime.counts["metadata_invalid"] == 1
        assert runtime.counts["unchecked"] == 1
        assert bot.diagnostics.gauges["media_metadata_invalid"] == 1
        assert runtime.counts["clear"] == 0
        assert not runtime.tasks
    finally:
        await runtime.close()
