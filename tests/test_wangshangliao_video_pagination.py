"""Bounded CMS pagination, durable progress and unchanged publication isolation."""

import json
import time
from unittest.mock import AsyncMock, Mock
from unittest.mock import call as mock_call
from urllib.parse import parse_qs, urlsplit

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import video_relay
from astrbot.builtin_stars.wangshangliao_moderation import (
    video_relay_delivery as delivery,
)
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from astrbot.dashboard.responses import ApiError
from tests.test_wangshangliao_native_guide_upload import Response, Session
from tests.test_wangshangliao_native_video import prepared as prepared
from tests.test_wangshangliao_video_delivery import call
from tests.test_wangshangliao_video_delivery import flow as flow


def record(value):
    return {
        "vod_id": value,
        "vod_name": "neutral fixture",
        "type_id": 7,
        "vod_play_url": f"media$https://cdn.example.com/{value}.mp4",
    }


def listing(ids, page=1, pagecount=1):
    return Response(
        json.dumps(
            {
                "code": 1,
                "page": str(page),
                "pagecount": pagecount,
                "list": [record(value) for value in ids],
            }
        ).encode()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("skip_first_page", [False, True])
async def test_exhausted_first_page_advances_in_same_round(
    monkeypatch, prepared, skip_first_page
):
    responses = [listing([1, 2], pagecount=3)]
    if not skip_first_page:
        for value in (1, 2):
            responses += [listing([value]), Response(prepared.data)]
    responses += [
        listing([3, 4], page=2, pagecount=3),
        listing([3]),
        Response(prepared.data),
    ]
    session = Session(responses)
    monkeypatch.setattr(delivery.aiohttp, "ClientSession", Mock(return_value=session))
    advance = AsyncMock()
    validate = AsyncMock(
        side_effect=(
            [None]
            if skip_first_page
            else [ProtocolError("video_format_unsupported")] * 2 + [None]
        )
    )
    item, data = await delivery.fetch_cms_video(
        video_relay.RelayConfig(cms_url="https://cms.example.com/api", category_id=7),
        {1, 2} if skip_first_page else set(),
        Mock(),
        advance_cursor=advance,
        validate_candidate=validate,
    )
    assert item.vod_id == 3 and data == prepared.data
    assert mock_call(2, 0) in advance.await_args_list
    assert advance.await_args == mock_call(2, 1)
    pages = [
        parse_qs(urlsplit(url).query).get("pg", ["1"])[0]
        for _, url, _ in session.calls
        if parse_qs(urlsplit(url).query).get("ac") == ["list"]
    ]
    assert pages == ["1", "2"]


@pytest.mark.asyncio
async def test_skipped_pages_are_bounded_and_save_next_page(monkeypatch):
    session = Session(
        [listing([page], page=page, pagecount=100) for page in range(1, 6)]
    )
    monkeypatch.setattr(delivery.aiohttp, "ClientSession", Mock(return_value=session))
    advance = AsyncMock()
    with pytest.raises(ProtocolError, match="^video_cms_unavailable$"):
        await delivery.fetch_cms_video(
            video_relay.RelayConfig(cms_url="https://cms.example.com/api"),
            set(range(1, 101)),
            Mock(),
            advance_cursor=advance,
        )
    assert len(session.calls) == 5
    assert advance.await_args == mock_call(6, 0)


@pytest.mark.asyncio
async def test_candidate_budget_resumes_inside_large_page(monkeypatch, prepared):
    responses = [listing(range(1, 41), pagecount=2)]
    for value in range(1, 21):
        item = record(value)
        item["vod_play_url"] = ""
        responses.append(Response(json.dumps({"code": 1, "list": [item]}).encode()))
    session = Session(responses)
    monkeypatch.setattr(delivery.aiohttp, "ClientSession", Mock(return_value=session))
    advance = AsyncMock()
    config = video_relay.RelayConfig(cms_url="https://cms.example.com/api")
    with pytest.raises(ProtocolError, match="^video_cms_unavailable$"):
        await delivery.fetch_cms_video(config, set(), Mock(), advance_cursor=advance)
    assert len(session.calls) == 21
    assert advance.await_args == mock_call(1, 20)
    session.responses = [
        listing(range(1, 41), pagecount=2),
        listing([21]),
        Response(prepared.data),
    ]
    item, _ = await delivery.fetch_cms_video(
        config, set(), Mock(), page=1, offset=20, advance_cursor=advance
    )
    assert item.vod_id == 21
    assert advance.await_args == mock_call(1, 21)


@pytest.mark.asyncio
async def test_scan_wraps_and_does_not_revisit_pages_in_same_round(monkeypatch):
    session = Session([listing([9], page=2, pagecount=2), listing([1], pagecount=2)])
    monkeypatch.setattr(delivery.aiohttp, "ClientSession", Mock(return_value=session))
    advance = AsyncMock()
    with pytest.raises(ProtocolError, match="^video_cms_unavailable$"):
        await delivery.fetch_cms_video(
            video_relay.RelayConfig(cms_url="https://cms.example.com/api"),
            {1, 9},
            Mock(),
            page=2,
            advance_cursor=advance,
        )
    assert len(session.calls) == 2
    assert advance.await_args_list == [mock_call(1, 0), mock_call(2, 0)]


@pytest.mark.asyncio
async def test_shrinking_catalog_resets_out_of_range_cursor(monkeypatch, prepared):
    session = Session(
        [
            listing([], page=42, pagecount=2),
            listing([1], pagecount=2),
            listing([1]),
            Response(prepared.data),
        ]
    )
    monkeypatch.setattr(delivery.aiohttp, "ClientSession", Mock(return_value=session))
    advance = AsyncMock()
    item, _ = await delivery.fetch_cms_video(
        video_relay.RelayConfig(cms_url="https://cms.example.com/api"),
        set(),
        Mock(),
        page=42,
        offset=19,
        advance_cursor=advance,
    )
    assert item.vod_id == 1
    assert advance.await_args_list == [mock_call(1, 0), mock_call(2, 0)]


@pytest.mark.asyncio
async def test_ignored_page_parameter_is_rejected_without_detail_or_media(monkeypatch):
    session = Session([listing([1], page=1, pagecount=42)])
    monkeypatch.setattr(delivery.aiohttp, "ClientSession", Mock(return_value=session))
    advance = AsyncMock()
    with pytest.raises(ProtocolError, match="^video_pagination_invalid$"):
        await delivery.fetch_cms_video(
            video_relay.RelayConfig(cms_url="https://cms.example.com/api"),
            set(),
            Mock(),
            page=2,
            advance_cursor=advance,
        )
    assert len(session.calls) == 1
    advance.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("pagecount", [True, -1, 1000001, "100000000000", {}, "nan"])
async def test_malformed_pagination_fails_closed(monkeypatch, pagecount):
    session = Session([listing([1], pagecount=pagecount)])
    monkeypatch.setattr(delivery.aiohttp, "ClientSession", Mock(return_value=session))
    with pytest.raises(ProtocolError, match="^video_pagination_invalid$"):
        await delivery.fetch_cms_video(
            video_relay.RelayConfig(cms_url="https://cms.example.com/api"),
            set(),
            Mock(),
        )
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_saved_cursor_survives_cancel_and_new_preview(flow):
    async def advance_scan(*args, **kwargs):
        assert (kwargs["page"], kwargs["offset"]) == (1, 0)
        await kwargs["advance_cursor"](4, 8)
        return flow.fetch.return_value

    flow.fetch.side_effect = advance_scan
    preview = await call(flow)
    await call(flow, "cancel_video", token=preview["token"])
    with video_relay.database(flow.path) as db:
        cursor = dict(db.execute("SELECT * FROM relay_scan_cursors").fetchone())
    assert cursor["page"] == 4 and cursor["offset"] == 8
    assert set(cursor) == {
        "account",
        "group_id",
        "source",
        "version",
        "category_id",
        "page",
        "offset",
        "updated",
    }
    flow.fetch.side_effect = None
    await call(flow)
    assert flow.fetch.call_args.kwargs["page"] == 4
    assert flow.fetch.call_args.kwargs["offset"] == 8
    flow.upload.assert_not_awaited()
    flow.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"account": "other"},
        {"group_id": "other"},
        {"source": "https://other.example.com/api"},
        {"version": 2},
        {"category_id": 7},
    ],
)
async def test_cursor_does_not_cross_account_group_source_version_or_category(
    flow, change
):
    cursor = {
        "account": "43506527",
        "group_id": "1459282",
        "source": video_relay.TMZYZ_CMS_URL,
        "version": 1,
        "category_id": 0,
        "page": 4,
        "offset": 8,
        "updated": time.time(),
    }
    cursor.update(change)
    with video_relay.database(flow.path) as db:
        db.execute(
            "INSERT INTO relay_scan_cursors VALUES(?,?,?,?,?,?,?,?)",
            tuple(cursor.values()),
        )
    await call(flow)
    assert flow.fetch.call_args.kwargs["page"] == 1
    assert flow.fetch.call_args.kwargs["offset"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override", [{"vod_id": 99}, {"category_id": 7}, {"max_video_mb": 64}]
)
async def test_one_off_preview_does_not_consume_automatic_cursor(flow, override):
    with video_relay.database(flow.path) as db:
        db.execute(
            "INSERT INTO relay_scan_cursors VALUES(?,?,?,?,?,?,?,?)",
            ("43506527", "1459282", video_relay.TMZYZ_CMS_URL, 1, 0, 4, 8, time.time()),
        )
    await call(flow, **override)
    assert flow.fetch.call_args.kwargs["advance_cursor"] is None
    assert flow.fetch.call_args.kwargs["page"] == 1
    with video_relay.database(flow.path) as db:
        assert tuple(
            db.execute("SELECT page,offset FROM relay_scan_cursors").fetchone()
        ) == (4, 8)


@pytest.mark.asyncio
async def test_configuration_revocation_blocks_late_progress_write(flow):
    async def revoked(*args, **kwargs):
        with video_relay.database(flow.path) as db:
            db.execute("UPDATE relay_configs SET version=2")
        await kwargs["advance_cursor"](4, 8)

    flow.fetch.side_effect = revoked
    with pytest.raises(ApiError) as error:
        await call(flow)
    assert error.value.data == {"code": "video_configuration_changed"}
    with video_relay.database(flow.path) as db:
        assert db.execute("SELECT COUNT(*) FROM relay_scan_cursors").fetchone()[0] == 0
    flow.upload.assert_not_awaited()
    flow.send.assert_not_awaited()
