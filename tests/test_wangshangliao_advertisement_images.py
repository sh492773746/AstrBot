"""Offline image storage, native delivery and rotation regressions."""

import asyncio
import base64
import hashlib
import io
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from pydantic import ValidationError

from astrbot.builtin_stars.wangshangliao_moderation import advertisement_media
from astrbot.builtin_stars.wangshangliao_moderation.native_guide_upload import (
    VerifiedGuideAsset,
)
from astrbot.builtin_stars.wangshangliao_moderation.open_rpc import OpenCall, OpenRPC
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from astrbot.builtin_stars.wangshangliao_moderation.rule_replies import (
    Advertisement,
    RuleReplies,
)
from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.storage import Ledger
from tests.test_wangshangliao_business_rules import rules as rules
from tests.test_wangshangliao_business_rules import save_rules
from tests.test_wangshangliao_first_use import decode_image
from tests.test_wangshangliao_group_ai import tenant
from tests.test_wangshangliao_open_rpc_guards import rpc_scope as rpc_scope
from tests.test_wangshangliao_public_hardening import chat_event
from tests.test_wangshangliao_test_witness_revocation import witness as witness


def bitmap(color="red", format="PNG"):
    stream = io.BytesIO()
    Image.new("RGB", (320, 180), color).save(stream, format)
    return base64.b64encode(stream.getvalue()).decode()


def upload(rules, color="red", account="99", group="5"):
    return rules.media.upload(account, group, bitmap(color), guard=lambda: None)["id"]


def configuration(images, text=""):
    return {
        "ads": [
            {
                "id": "a",
                "name": "Image notice",
                "text": text,
                "images": images,
                "times": ["09:00"],
                "enabled": True,
            }
        ],
    }


def test_images_are_normalized_private_and_immutable(rules):
    reference = upload(rules)
    with rules.store.connect() as db:
        image = dict(db.execute("SELECT * FROM business_rule_images").fetchone())
    assert image["id"] == reference
    assert image["digest"] == hashlib.sha256(image["data"]).hexdigest()
    with Image.open(io.BytesIO(image["data"])) as content:
        assert content.format == "JPEG" and content.size == (320, 180)
        assert not content.getexif()
    assert image["thumbnail"].startswith("data:image/jpeg;base64,")
    assert "data" not in rules.media.previews("99", "5", {"ads": []})
    assert not rules.media.previews("88", "5", configuration([reference]))


def test_scoped_upload_deduplicates_transactionally_and_only_reuses_safe_rows(rules):
    encoded = bitmap()
    original = rules.media.upload("99", "5", encoded, guard=lambda: None)
    repeated = rules.media.upload("99", "5", encoded, guard=lambda: None)
    assert repeated == original
    assert (
        rules.media.upload("88", "5", encoded, guard=lambda: None)["id"]
        != original["id"]
    )
    assert (
        rules.media.upload("99", "6", encoded, guard=lambda: None)["id"]
        != original["id"]
    )
    with rules.store.connect(write=True) as db:
        db.execute(
            "UPDATE business_rule_images SET state='unknown',asset='12345' WHERE id=?",
            (original["id"],),
        )
    fresh = rules.media.upload("99", "5", encoded, guard=lambda: None)
    assert fresh["id"] != original["id"]
    with rules.store.connect() as db:
        row = db.execute(
            "SELECT state,asset FROM business_rule_images WHERE id=?",
            (original["id"],),
        ).fetchone()
        assert tuple(row) == ("unknown", "12345")
        assert (
            db.execute(
                "SELECT count(*) FROM business_rule_images WHERE account='99' "
                "AND group_id='5'"
            ).fetchone()[0]
            == 2
        )


def test_concurrent_duplicate_uploads_share_one_reference(rules):
    encoded = bitmap()
    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(
            workers.map(
                lambda _: rules.media.upload("99", "5", encoded, guard=lambda: None),
                range(8),
            )
        )
    assert len({result["id"] for result in results}) == 1
    with rules.store.connect() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM business_rule_images WHERE account='99' "
                "AND group_id='5'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    "state,asset,age,reuse",
    [
        ("", "", 0, True),
        ("", "", 90000, False),
        ("ready", "1234567", 90000, True),
        ("ready", "", 0, False),
        ("ready", "https://invalid.test/image", 0, False),
        ("", "1234567", 0, False),
        ("unknown", "", 0, False),
        ("uploading", "", 0, False),
    ],
)
def test_dedup_only_reuses_fresh_local_or_valid_ready_images(
    rules, state, asset, age, reuse
):
    reference = upload(rules)
    with rules.store.connect(write=True) as db:
        db.execute(
            "UPDATE business_rule_images SET state=?,asset=?,created=? WHERE id=?",
            (state, asset, time.time() - age, reference),
        )
    # Retain the old reference independently of whether it is reusable.
    save_rules(rules, configuration([reference]))
    assert (upload(rules) == reference) is reuse


def test_legacy_duplicates_remain_distinct_and_lookup_index_is_not_unique(rules):
    original = upload(rules)
    legacy = "a" * 32
    with rules.store.connect(write=True) as db:
        db.execute(
            "INSERT INTO business_rule_images "
            "SELECT ?,account,group_id,digest,width,height,data,thumbnail,created,asset,state "
            "FROM business_rule_images WHERE id=?",
            (legacy, original),
        )
        before = {
            row["id"]: dict(row)
            for row in db.execute("SELECT * FROM business_rule_images")
        }
        indexes = {
            row["name"]: row["unique"]
            for row in db.execute("PRAGMA index_list(business_rule_images)")
        }
    assert indexes["business_rule_image_digest"] == 0
    assert upload(rules) in {original, legacy}
    with rules.store.connect() as db:
        after = {
            row["id"]: dict(row)
            for row in db.execute("SELECT * FROM business_rule_images")
        }
    assert after == before


def test_duplicate_upload_is_admitted_at_quota_but_new_image_is_not(rules):
    original = rules.media.upload("99", "5", bitmap(), guard=lambda: None)
    with rules.store.connect(write=True) as db:
        row = db.execute(
            "SELECT account,group_id,width,height,data,thumbnail,created "
            "FROM business_rule_images WHERE id=?",
            (original["id"],),
        ).fetchone()
        for index in range(119):
            db.execute(
                "INSERT INTO business_rule_images "
                "(id,account,group_id,digest,width,height,data,thumbnail,created) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    f"{index:032x}",
                    row["account"],
                    row["group_id"],
                    f"{index + 1:064x}",
                    row["width"],
                    row["height"],
                    row["data"],
                    row["thumbnail"],
                    row["created"],
                ),
            )
    assert (
        rules.media.upload("99", "5", bitmap(), guard=lambda: None)["id"]
        == original["id"]
    )
    with pytest.raises(OpenError, match="image_quota"):
        rules.media.upload("99", "5", bitmap("blue"), guard=lambda: None)


def test_previews_query_only_referenced_scoped_image_ids(rules, monkeypatch):
    reference = upload(rules)
    upload(rules, "blue")
    statements = []
    connect = rules.store.connect

    class TracedConnection:
        def __init__(self, context):
            self.context = context

        def __enter__(self):
            db = self.context.__enter__()
            db.set_trace_callback(statements.append)
            return db

        def __exit__(self, *args):
            return self.context.__exit__(*args)

    monkeypatch.setattr(
        rules.store,
        "connect",
        lambda **kwargs: TracedConnection(connect(**kwargs)),
    )
    assert set(rules.media.previews("99", "5", configuration([reference]))) == {
        reference
    }
    query = next(
        statement
        for statement in statements
        if "SELECT id,thumbnail FROM business_rule_images" in statement
    )
    assert "id IN (" in query


@pytest.mark.parametrize(
    "encoded",
    [
        "",
        "not-a-bitmap",
        "a" * (7 * 1024 * 1024),
        base64.b64encode(b"<svg onload='bad'/>").decode(),
        "https://example.invalid/image.jpg",
        True,
        None,
    ],
)
def test_invalid_images_never_store_or_send(rules, encoded):
    with pytest.raises(OpenError, match="advertisement_image_invalid"):
        rules.media.upload("99", "5", encoded, guard=lambda: None)
    with rules.store.connect() as db:
        assert (
            db.execute("SELECT count(*) FROM business_rule_images").fetchone()[0] == 0
        )


def test_animated_and_excess_pixel_images_are_rejected(rules):
    stream = io.BytesIO()
    first = Image.new("RGB", (20, 20), "red")
    first.save(
        stream,
        "WEBP",
        save_all=True,
        append_images=[Image.new("RGB", (20, 20), "blue")],
    )
    with pytest.raises(OpenError, match="invalid"):
        rules.media.upload(
            "99", "5", base64.b64encode(stream.getvalue()).decode(), guard=lambda: None
        )
    stream = io.BytesIO()
    Image.new("RGB", (4096, 4096)).save(stream, "PNG")
    with pytest.raises(OpenError, match="invalid"):
        rules.media.upload(
            "99", "5", base64.b64encode(stream.getvalue()).decode(), guard=lambda: None
        )


@pytest.mark.parametrize("case", ["missing", "other_account", "other_group", "revoked"])
def test_image_references_and_upload_writes_are_scope_guarded(rules, case):
    reference = upload(
        rules,
        account="88" if case == "other_account" else "99",
        group="6" if case == "other_group" else "5",
    )
    if case == "revoked":
        calls = [0]

        def guard():
            calls[0] += 1
            if calls[0] >= 3:
                raise OpenError("revoked")

        with pytest.raises(OpenError, match="revoked"):
            rules.media.upload("99", "5", bitmap(), guard=guard)
        with rules.store.connect() as db:
            assert (
                db.execute("SELECT count(*) FROM business_rule_images").fetchone()[0]
                == 1
            )
    else:
        with pytest.raises(OpenError, match="image_scope"):
            save_rules(
                rules, configuration(["f" * 32 if case == "missing" else reference])
            )


@pytest.mark.parametrize(
    "images", [["x"], ["f" * 32] * 2, [str(n) * 32 for n in range(4)]]
)
def test_advertisement_max_three_unique_image_references(images):
    with pytest.raises(ValidationError):
        Advertisement(id="a", name="a", times=["09:00"], images=images)
    with pytest.raises(ValidationError):
        Advertisement(id="a", name="a", times=["09:00"])


@pytest.mark.asyncio
async def test_four_occurrences_rotate_one_of_three_and_survive_restart(
    rules, monkeypatch
):
    images = [upload(rules, color) for color in ("red", "green", "blue")]
    save_rules(rules, configuration(images))
    rules.started = time.time() - 10
    monkeypatch.setattr(RuleReplies, "send_guard", lambda *a, **kw: None)
    sender = AsyncMock(return_value="accepted")
    adapter = SimpleNamespace(send_reply_text=AsyncMock())
    base = time.time()
    for index in range(4):
        monkeypatch.setattr(time, "time", lambda: base + index * 60)
        if index == 2:
            rules = RuleReplies(rules.service)
            rules.started = time.time() - 10
        rules.media.send = sender
        planned = time.time() - 0.1
        with rules.store.connect(write=True) as db:
            db.execute("UPDATE business_ad_cursors SET next_at=?", (planned,))
        cursor = {"account": "99", "group_id": "5", "rule_id": "a", "next_at": planned}
        await rules.send_ad(adapter, cursor)
        await rules.send_ad(adapter, cursor)
    assert [call.args[2] for call in sender.await_args_list] == [*images, images[0]]
    adapter.send_reply_text.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["unknown", "not_sent", "rejected", "exception"])
async def test_image_failure_stops_text_without_advancing_or_replaying(
    rules, monkeypatch, result
):
    images = [upload(rules)]
    save_rules(rules, configuration(images, text="Notice"))
    rules.started = time.time() - 10
    monkeypatch.setattr(rules, "send_guard", lambda *a, **kw: None)
    rules.media.send = AsyncMock(return_value=result)
    if result == "exception":
        rules.media.send.side_effect = OpenError("image_upload_unknown")
    adapter = SimpleNamespace(send_reply_text=AsyncMock(return_value="accepted"))
    planned = time.time() - 0.1
    with rules.store.connect(write=True) as db:
        db.execute("UPDATE business_ad_cursors SET next_at=?", (planned,))
    cursor = {"account": "99", "group_id": "5", "rule_id": "a", "next_at": planned}
    await rules.send_ad(adapter, cursor)
    await rules.send_ad(adapter, cursor)
    adapter.send_reply_text.assert_not_awaited()
    rules.media.send.assert_awaited_once()
    with rules.store.connect() as db:
        assert (
            db.execute("SELECT position FROM business_ad_rotations").fetchone()[0] == 0
        )
        assert db.execute("SELECT blocked FROM business_ad_cursors").fetchone()[0] == (
            "delivery_unknown" if result in {"unknown", "exception"} else ""
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "image_result,text_result",
    [
        ("accepted", "accepted"),
        ("verified", "verified"),
        ("accepted", "not_sent"),
        ("accepted", "rejected"),
        ("accepted", "unknown"),
        ("accepted", "exception"),
    ],
)
async def test_image_precedes_text_and_partial_delivery_pauses_the_schedule(
    rules, monkeypatch, image_result, text_result
):
    images = [upload(rules), upload(rules, "blue")]
    save_rules(rules, configuration(images, text="Notice"))
    rules.started = time.time() - 10
    monkeypatch.setattr(rules, "send_guard", lambda *a, **kw: None)
    sequence = []

    async def send_image(*args):
        sequence.append("image")
        return image_result

    async def send_text(*args, **kwargs):
        assert sequence == ["image"]
        sequence.append("text")
        if text_result == "exception":
            raise OpenError("text_delivery_unknown")
        return text_result

    rules.media.send = AsyncMock(side_effect=send_image)
    adapter = SimpleNamespace(send_reply_text=AsyncMock(side_effect=send_text))
    planned = time.time() - 0.1
    with rules.store.connect(write=True) as db:
        db.execute("UPDATE business_ad_cursors SET next_at=?", (planned,))
    cursor = {"account": "99", "group_id": "5", "rule_id": "a", "next_at": planned}
    await rules.send_ad(adapter, cursor)
    await rules.send_ad(adapter, cursor)
    assert sequence == ["image", "text"]
    image_call = rules.media.send.await_args
    text_call = adapter.send_reply_text.await_args
    assert image_call.args[2] == images[0]
    assert image_call.args[3] == text_call.args[1] + "/image"
    assert text_call.kwargs["auto_recall"] is False
    expected = (
        text_result
        if text_result in {"accepted", "verified"}
        else "partial"
        if text_result in {"not_sent", "rejected"}
        else "unknown"
    )
    with rules.store.connect() as db:
        saved = db.execute("SELECT state,error FROM business_rule_sends").fetchone()
        assert saved["state"] == expected
        if expected == "partial":
            assert saved["error"] == "advertisement_text_not_sent"
        assert db.execute("SELECT position FROM business_ad_rotations").fetchone()[
            0
        ] == int(expected in {"accepted", "verified"})
        assert db.execute("SELECT blocked FROM business_ad_cursors").fetchone()[0] == (
            ""
            if expected in {"accepted", "verified"}
            else "partial_delivery"
            if expected == "partial"
            else "delivery_unknown"
        )


def test_replacing_images_resets_rotation_but_text_edit_preserves_it(rules):
    first, second = upload(rules), upload(rules, "blue")
    save_rules(rules, configuration([first, second]))
    with rules.store.connect(write=True) as db:
        db.execute("UPDATE business_ad_rotations SET position=1")
    save_rules(rules, configuration([first, second], text="Updated"))
    with rules.store.connect() as db:
        assert (
            db.execute("SELECT position FROM business_ad_rotations").fetchone()[0] == 1
        )
    save_rules(rules, configuration([second, first]))
    with rules.store.connect() as db:
        assert (
            db.execute("SELECT position FROM business_ad_rotations").fetchone()[0] == 0
        )


def test_old_unreferenced_images_expire_without_losing_saved_or_unknown_assets(rules):
    saved = upload(rules, "red")
    stale = upload(rules, "blue")
    uncertain = upload(rules, "green")
    draft = upload(rules, "yellow")
    other = upload(rules, account="88")
    save_rules(rules, configuration([saved]))
    with rules.store.connect(write=True) as db:
        db.execute(
            "INSERT INTO business_rule_drafts(account,group_id,config,expires,used) "
            "VALUES(?,?,?,?,0)",
            (
                "99",
                "5",
                json.dumps(configuration([draft])),
                time.time() + 600,
            ),
        )
        db.execute(
            "UPDATE business_rule_images SET created=? WHERE account='99' AND group_id='5'",
            (time.time() - 90000,),
        )
        db.execute(
            "UPDATE business_rule_images SET state='unknown' WHERE id=?", (uncertain,)
        )
    rules.media.upload("99", "5", bitmap("black"), guard=lambda: None)
    with rules.store.connect() as db:
        remaining = {
            row[0] for row in db.execute("SELECT id FROM business_rule_images")
        }
    assert {saved, uncertain, draft, other} <= remaining and stale not in remaining


@pytest.mark.asyncio
async def test_in_memory_inflight_send_protects_old_image_from_collection(
    rules, monkeypatch
):
    reference = upload(rules)
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait_for_send(*args, **kwargs):
        entered.set()
        await release.wait()
        return "accepted"

    monkeypatch.setattr(rules.media, "_send", wait_for_send)
    adapter = SimpleNamespace(account="99", config={"id": "fixture"})
    sending = asyncio.create_task(
        rules.media.send(adapter, "5", reference, "inflight", lambda: None)
    )
    await entered.wait()
    with rules.store.connect(write=True) as db:
        db.execute(
            "UPDATE business_rule_images SET created=? WHERE id=?",
            (time.time() - 90000, reference),
        )
    rules.media.upload("99", "5", bitmap("blue"), guard=lambda: None)
    with rules.store.connect() as db:
        assert db.execute(
            "SELECT 1 FROM business_rule_images WHERE id=?", (reference,)
        ).fetchone()
    release.set()
    assert await sending == "accepted"


def test_durable_image_intent_survives_restart_and_old_image_collection(rules):
    reference, stale, uploading = [
        upload(rules, color) for color in ("red", "blue", "green")
    ]
    with rules.store.connect(write=True) as db:
        db.execute("UPDATE business_rule_images SET created=?", (time.time() - 90000,))
        db.execute(
            "UPDATE business_rule_images SET state='uploading' WHERE id=?", (uploading,)
        )
        db.execute(
            "INSERT INTO business_rule_image_sends VALUES(?,?,?,?)",
            ("unknown-operation/image", "99", "5", reference),
        )
    restarted = RuleReplies(rules.service)
    restarted.media.upload("99", "5", bitmap("yellow"), guard=lambda: None)
    with rules.store.connect() as db:
        remaining = {
            row["id"]: row["state"]
            for row in db.execute("SELECT id,state FROM business_rule_images")
        }
    assert reference in remaining
    assert remaining[uploading] == "unknown"
    assert stale not in remaining


def test_departure_removes_only_this_groups_images_and_rotation(rules):
    own, other = upload(rules), upload(rules, group="6")
    save_rules(rules, configuration([own]))
    tenant = rules.store.tenant("99", "5")
    rules.store.depart("99", "5", tenant["epoch"], "verified-native-departure")
    with rules.store.connect() as db:
        assert [
            row[0] for row in db.execute("SELECT id FROM business_rule_images")
        ] == [other]
        assert not db.execute("SELECT * FROM business_ad_rotations").fetchall()


@pytest.mark.asyncio
async def test_private_rule_query_explains_missing_group_without_calling_ai(rpc_scope):
    service, adapter, _ = rpc_scope
    event = chat_event(adapter)
    event.get_group_id = lambda: ""
    with pytest.raises(OpenError, match="business_group_required"):
        await service.rule_replies.command(event, "定时广告")


@pytest.mark.parametrize(
    "change",
    [
        "none",
        "window_closed",
        "wrong_group",
        "wrong_sender",
        "wrong_client",
        "removed_reference",
        "changed_bytes",
        "unknown_image",
        "revoked",
    ],
)
@pytest.mark.parametrize("kind", ["advertisement", "keyword"])
def test_scheduled_image_witness_requires_current_exact_plan_and_native_intent(
    rules,
    monkeypatch,
    change,
    kind,
):
    from astrbot.builtin_stars.wangshangliao_moderation.feature_image import (
        ImageMessage,
    )

    account, group = "43506527", "1459282"
    tenant(rules.store, account, group)
    reference = upload(rules, account=account, group=group)
    config = configuration([reference])
    config["ads"][0]["id"] = "WSL_TEST_ACTIVITY_images"
    if kind == "keyword":
        config = {
            "keywords": [
                {
                    "id": "WSL_TEST_ACTIVITY_images",
                    "name": "Neutral image",
                    "enabled": True,
                    "terms": ["WSL_TEST_NEUTRAL"],
                    "text": "Neutral reply",
                    "images": [reference],
                }
            ]
        }
    save_rules(rules, config, account=account, group=group)
    receiver = SimpleNamespace(account=account, config={"id": "fixture"})
    window = SimpleNamespace(
        receiver=receiver,
        sender=SimpleNamespace(account="23691273"),
        groups=[group],
        scopes=["group_commands" if kind == "advertisement" else "group_rules"],
        active=lambda: True,
        _witness_id="fixed-window",
        _witness_since=int(time.time() * 1000) - 1000,
    )
    operation = f"advertisement/{account}/{group}/WSL_TEST_ACTIVITY_images/123"
    client = str(uuid.uuid5(uuid.NAMESPACE_URL, f"fixture/{operation}/image"))
    with rules.store.connect(write=True) as db:
        db.execute(
            "UPDATE business_rule_images SET asset='12345',state='ready' WHERE id=?",
            (reference,),
        )
        image = dict(
            db.execute(
                "SELECT * FROM business_rule_images WHERE id=?", (reference,)
            ).fetchone()
        )
        db.execute(
            "INSERT INTO business_rule_sends VALUES(?,?,?,?,?,?,'dispatched',?,'')",
            (
                operation,
                account,
                group,
                kind,
                "WSL_TEST_ACTIVITY_images",
                time.time(),
                time.time(),
            ),
        )
    message = ImageMessage(
        sender={"id": int(account)},
        target={"id": int(group)},
        session=2,
        format=1,
        client_id=client,
        image={
            "asset": {"uri": "12345"},
            "width": image["width"],
            "height": image["height"],
            "size": len(image["data"]),
        },
    )
    monkeypatch.setattr(rules, "send_guard", lambda *a, **kw: None)
    if change == "window_closed":
        window.active = lambda: False
    elif change == "wrong_group":
        window.groups = ["1516387"]
    elif change == "wrong_sender":
        window.sender.account = "31846829"
    elif change == "wrong_client":
        message.client_id = "unrelated"
    elif change == "removed_reference":
        save_rules(rules, {"ads": []}, account=account, group=group)
    elif change in {"changed_bytes", "unknown_image"}:
        with rules.store.connect(write=True) as db:
            db.execute(
                "UPDATE business_rule_images SET "
                + ("data=x'01'" if change == "changed_bytes" else "state='unknown'")
                + " WHERE id=?",
                (reference,),
            )
    elif change == "revoked":
        monkeypatch.setattr(
            rules,
            "send_guard",
            lambda *a, **kw: (_ for _ in ()).throw(OpenError("revoked")),
        )
    result = rules.media.test_asset(receiver, window, message, {11: client})
    assert bool(result) == (change == "none")
    if result:
        assert result["asset"] == "12345" and result["digest"] == image["digest"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["none", "removed_plan", "closed", "revoked"])
async def test_queued_scheduled_image_witness_rechecks_authority_before_commit(
    rules,
    witness,
    monkeypatch,
    change,
):
    from astrbot.builtin_stars.wangshangliao_moderation.feature_image import (
        ImageMessage,
    )

    account, group = "43506527", "1459282"
    window, _, outer = witness.reply_window(witness)
    witness.service.rule_replies = rules
    tenant(rules.store, account, group)
    reference = upload(rules, account=account, group=group)
    config = configuration([reference])
    config["ads"][0]["id"] = "WSL_TEST_ACTIVITY_queued"
    save_rules(rules, config, account=account, group=group)
    operation = f"advertisement/{account}/{group}/WSL_TEST_ACTIVITY_queued/123"
    client = str(uuid.uuid5(uuid.NAMESPACE_URL, f"receiver/{operation}/image"))
    with rules.store.connect(write=True) as db:
        db.execute(
            "UPDATE business_rule_images SET asset='12345',state='ready' WHERE id=?",
            (reference,),
        )
        image = dict(
            db.execute(
                "SELECT * FROM business_rule_images WHERE id=?",
                (reference,),
            ).fetchone()
        )
        db.execute(
            "INSERT INTO business_rule_sends VALUES(?,?,?,'advertisement',?,?,'dispatched',?,'')",
            (
                operation,
                account,
                group,
                config["ads"][0]["id"],
                time.time(),
                time.time(),
            ),
        )
    message = ImageMessage(
        sender={"id": int(account)},
        target={"id": int(group)},
        session=2,
        format=1,
        created_at=window._witness_since,
        client_id=client,
        image={
            "asset": {"uri": "12345"},
            "width": image["width"],
            "height": image["height"],
            "size": len(image["data"]),
        },
    )
    outer[11] = client
    monkeypatch.setattr(rules, "send_guard", lambda *a, **kw: None)
    ledger = witness.sender.ledger
    await ledger.ingress_lock.acquire()
    tasks = ()
    try:
        assert witness.probe.observe_reply(
            witness.service,
            witness.sender,
            group,
            outer,
            message,
        )
        tasks = tuple(witness.service.candidate_tasks)
        await asyncio.sleep(0)
        if change == "removed_plan":
            save_rules(rules, {"ads": []}, account=account, group=group)
        elif change == "closed":
            window.expires = 0
        elif change == "revoked":

            def revoked(*a, **kw):
                raise OpenError("revoked")

            monkeypatch.setattr(rules, "send_guard", revoked)
    finally:
        ledger.ingress_lock.release()
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), 5)
    async with ledger.db.execute("SELECT state,payload FROM inbox") as cursor:
        rows = await cursor.fetchall()
    assert len(rows) == (1 if change == "none" else 0)
    if rows:
        assert rows[0][0] == "ignored_bot"
        assert json.loads(rows[0][1])["image"]["digest"] == image["digest"]
    witness.sender.nim.request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data,status", [(b"{", 400), (b" " * (7 * 1024 * 1024 + 1), 413)]
)
async def test_open_dashboard_upload_body_is_bounded_before_dispatch(
    monkeypatch, data, status
):
    from astrbot.builtin_stars.wangshangliao_moderation import dashboard

    page = object.__new__(dashboard.DashboardPage)
    page.authorize = AsyncMock()

    async def stream():
        yield data

    monkeypatch.setattr(
        dashboard,
        "request",
        SimpleNamespace(
            _request=SimpleNamespace(method="POST", stream=stream),
        ),
    )
    with pytest.raises(dashboard.ApiError) as caught:
        await page.open_service()
    assert caught.value.status_code == status
    page.authorize.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("cached_asset", [False, True])
@pytest.mark.parametrize("proactive", [False, True])
async def test_native_image_is_uploaded_once_and_each_slot_has_durable_intent(
    rules, tmp_path, monkeypatch, cached_asset, proactive
):
    event_thread = threading.get_ident()
    connect = rules.media._connect
    compress = advertisement_media.lz4.block.compress

    def worker_connect():
        assert threading.get_ident() != event_thread
        return connect()

    def worker_compress(*args, **kwargs):
        assert threading.get_ident() != event_thread
        return compress(*args, **kwargs)

    monkeypatch.setattr(rules.media, "_connect", worker_connect)
    monkeypatch.setattr(advertisement_media.lz4.block, "compress", worker_compress)
    reference = upload(rules)
    with rules.store.connect() as db:
        stored = dict(db.execute("SELECT * FROM business_rule_images").fetchone())
    asset = "1234567890123456789"
    uploader = AsyncMock(
        return_value=VerifiedGuideAsset(
            "99", stored["digest"], asset, len(stored["data"])
        )
    )
    monkeypatch.setattr(advertisement_media, "upload_verified_image", uploader)
    if cached_asset:
        with rules.store.connect(write=True) as db:
            db.execute(
                "UPDATE business_rule_images SET asset=?,state='ready' WHERE id=?",
                (asset, reference),
            )

    async def request(*args):
        with rules.store.connect() as db:
            assert (
                db.execute(
                    "SELECT image_id FROM business_rule_image_sends WHERE operation=?",
                    (f"slot/{request.index}",),
                ).fetchone()[0]
                == reference
            )
        request.index += 1
        return 200, wire.properties([(12, b"server"), (7, b"123")])

    request.index = 0
    adapter = SimpleNamespace(
        account="99",
        groups={"5": "team5"},
        config={
            "id": "fixture-images",
            "_business_active": True,
            "proactive_send": {
                "enabled": proactive,
                "targets": ["5"] if proactive else [],
            },
        },
        business=SimpleNamespace(
            deployment=SimpleNamespace(key=lambda name: b"x" * 32)
        ),
        nim=SimpleNamespace(request=AsyncMock(side_effect=request)),
        ledger=Ledger(tmp_path / "messages.sqlite3"),
        send_lock=asyncio.Lock(),
    )
    await adapter.ledger.open()
    for index in range(2):
        adapter._onboarding_sent_at = 0
        result = await rules.media.send(
            adapter, "5", reference, f"slot/{index}", lambda: None, proactive=proactive
        )
        assert result == "accepted"
    assert uploader.await_count == (0 if cached_asset else 1)
    assert adapter.nim.request.await_count == 2
    with rules.store.connect() as db:
        assert not db.execute("SELECT * FROM business_rule_image_sends").fetchall()
    adapter._onboarding_sent_at = 0
    assert (
        await rules.media.send(
            adapter, "5", reference, "slot/0", lambda: None, proactive=proactive
        )
        == "accepted"
    )
    assert adapter.nim.request.await_count == 2
    if not proactive:
        with pytest.raises(wire.ProtocolError, match="scope_changed"):
            await rules.media.send(adapter, "5", reference, "scheduled", lambda: None)
        assert adapter.nim.request.await_count == 2
    await adapter.ledger.close()
    for call in adapter.nim.request.await_args_list:
        properties, _ = wire.read_properties(call.args[2])
        attachment = properties[10].decode("ascii")
        # Official 3.7.3 crypto.a strips this exact wrapper before Base64 decoding;
        # json.loads in the adapter's decoder would hide incompatible whitespace.
        assert attachment.startswith('{"b":"') and attachment.endswith('"}')
        encoded = attachment[6:-2]
        envelope = wire.Envelope.FromString(wire.unb64(encoded))
        assert envelope.body and len(envelope.tag) == 16
        message = decode_image(properties[10], b"x" * 32)
        assert message.image.asset.uri == asset
        assert (message.image.width, message.image.height) == (320, 180)


@pytest.mark.asyncio
async def test_ambiguous_native_image_send_keeps_its_durable_pin(
    rules, tmp_path, monkeypatch
):
    reference = upload(rules)
    with rules.store.connect(write=True) as db:
        db.execute(
            "UPDATE business_rule_images SET asset='1234567',state='ready' WHERE id=?",
            (reference,),
        )
    adapter = SimpleNamespace(
        account="99",
        groups={"5": "team5"},
        config={
            "id": "fixture-unknown-image",
            "_business_active": True,
            "proactive_send": {"enabled": True, "targets": ["5"]},
        },
        business=SimpleNamespace(
            deployment=SimpleNamespace(key=lambda name: b"x" * 32)
        ),
        nim=SimpleNamespace(request=AsyncMock(side_effect=wire.ProtocolError("lost"))),
        ledger=Ledger(tmp_path / "unknown-image.sqlite3"),
        send_lock=asyncio.Lock(),
    )
    await adapter.ledger.open()
    try:
        with pytest.raises(wire.ProtocolError, match="lost"):
            await rules.media.send(adapter, "5", reference, "uncertain", lambda: None)
        with rules.store.connect() as db:
            assert tuple(
                db.execute(
                    "SELECT group_id,image_id FROM business_rule_image_sends "
                    "WHERE account=? AND operation=?",
                    ("99", "uncertain"),
                ).fetchone()
            ) == ("5", reference)
        adapter._onboarding_sent_at = 0
        assert (
            await rules.media.send(adapter, "5", reference, "uncertain", lambda: None)
            == "unknown"
        )
        adapter.nim.request.assert_awaited_once()
    finally:
        await adapter.ledger.close()


@pytest.mark.asyncio
async def test_unknown_upload_persists_across_restart_and_cannot_repeat(
    rules, monkeypatch
):
    reference = upload(rules)
    error = wire.ProtocolError("upload_receipt_lost")
    error.submitted = True
    uploader = AsyncMock(side_effect=error)
    monkeypatch.setattr(advertisement_media, "upload_verified_image", uploader)
    adapter = SimpleNamespace(
        account="99",
        config={"id": "fixture", "_business_active": True},
        business=object(),
    )
    with pytest.raises(wire.ProtocolError):
        await rules.media.send(adapter, "5", reference, "slot", lambda: None)
    restarted = RuleReplies(rules.service)
    with pytest.raises(OpenError, match="upload_unknown"):
        await restarted.media.send(adapter, "5", reference, "other-slot", lambda: None)
    uploader.assert_awaited_once()


@pytest.mark.asyncio
async def test_upload_rpc_is_authorized_and_returns_only_scoped_preview(rpc_scope):
    service, _, _ = rpc_scope
    rpc = OpenRPC(service)
    version = service.store.tenant("99", "5")["version"]
    receipt = await rpc.dispatch(
        OpenCall(
            action="business_rules",
            actor="dashboard/test",
            payload={
                "account": "99",
                "group_id": "5",
                "tenant_version": version,
                "operation": "image_upload",
                "data": bitmap(),
            },
        )
    )
    assert set(receipt) == {"id", "thumbnail"}
    for group in ("6", "unknown"):
        with pytest.raises((OpenError, ValueError)):
            await rpc.dispatch(
                OpenCall(
                    action="business_rules",
                    actor="dashboard/test",
                    payload={
                        "account": "99",
                        "group_id": group,
                        "tenant_version": 1,
                        "operation": "image_upload",
                        "data": bitmap(),
                    },
                )
            )
