"""CMS configuration validation, durable confirmation and management scope."""

import asyncio
import copy
import json
import os
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from pydantic import ValidationError

from astrbot.builtin_stars.wangshangliao_moderation import video_relay
from astrbot.dashboard.responses import ApiError
from tests.test_wangshangliao_dashboard import PREFIX
from tests.test_wangshangliao_dashboard import headers as headers
from tests.test_wangshangliao_dashboard import page_app as page_app


@pytest.mark.parametrize(
    "source",
    [
        "https://news.example.com/api.php/provide/vod/?ac=list",
        "https://news.example.com/api.php/provide/vod/?pg=1",
    ],
)
def test_source_normalization(source):
    assert (
        video_relay.RelayConfig(cms_url=source).cms_url
        == "https://news.example.com/api.php/provide/vod/"
    )


@pytest.mark.parametrize(
    "change",
    [
        {"sources": [{"source": "https://t.me/+private-invitation"}]},
        {"sources": [{"source": "https://127.0.0.1/secrets"}]},
        {"sources": [{"source": "https://t.me/name/123"}]},
        {"sources": [{"source": "123456"}]},
        {"sources": [{"source": "-9999999999999999999"}]},
        {"sources": [{"source": "@Example"}, {"source": "https://t.me/example"}]},
        {"sources": [{"source": "@example", "token": "no-secrets"}]},
        {"sources": [{"source": "@example", "enabled": "yes"}]},
        {"sources": [{"source": f"@example{i}"} for i in range(31)]},
        {"interval_minutes": True},
        {"interval_minutes": "20"},
        {"interval_minutes": 0},
        {"category_id": -1},
        {"max_video_mb": 113},
        {"cms_url": "https://127.0.0.1/api"},
        {"cms_url": "https://169.254.169.254/api"},
        {"cms_url": "https://localhost/api"},
        {"cms_url": "https://news.example.com/api?token=secret"},
        {"cms_url": "https://user:pass@news.example.com/api"},
        {"cms_source": "unreviewed_cms"},
        {"enabled": True},
        {"target_host": "https://not-a-proxy"},
    ],
)
def test_draft_validation_is_bounded_and_rejects_secrets(change):
    with pytest.raises(ValidationError):
        video_relay.RelayConfig.model_validate(change)


def test_cms_list_has_no_title_or_content_keyword_restrictions():
    payload = {
        "code": 1,
        "list": [
            {
                "vod_id": 161582,
                "vod_name": "每日精选推荐",
                "type_id": 71,
                "type_name": "短视频",
            },
            {
                "vod_id": 161569,
                "vod_name": "短视频精选推荐",
                "type_id": 71,
                "type_name": "短视频",
            },
            {
                "vod_id": 161409,
                "vod_name": "抖音风第105期",
                "type_id": 71,
                "type_name": "短视频",
            },
            {
                "vod_id": 9,
                "vod_name": "短视频精选推荐",
                "type_id": 1,
                "type_name": "电影",
            },
        ],
    }
    assert video_relay.cms_ids(payload) == [161582, 161569, 161409, 9]
    assert video_relay.cms_ids(payload, 71) == [161582, 161569, 161409]
    assert video_relay.cms_ids({"code": 0, "list": payload["list"]}) == []


def test_cms_detail_checks_player_not_titles_labels_or_cover():
    base = {
        "vod_id": 161569,
        "vod_name": "短视频精选推荐",
        "type_id": 71,
        "type_name": "短视频",
        "vod_remarks": "",
        "vod_tag": "精选,短视频",
        "vod_class": "精选,短视频",
        "vod_content": "",
        "vod_pic": "https://img.tmzyz.com/vod/a4542d6c8208784e95023bb13a4bc507.jpg",
        "vod_play_from": "bkplayer",
        "vod_play_url": "HD$a4542d6c8208784e95023bb13a4bc507",
    }
    accepted = video_relay.cms_detail(base)
    assert accepted is not None
    assert accepted.resource_id == "a4542d6c8208784e95023bb13a4bc507"

    for change in (
        {"vod_name": "抖音风第105期"},
        {"vod_tag": "精选,裸舞合集"},
        {"vod_content": "成人无码视频"},
        {"vod_pic": "https://example.com/image.jpg"},
    ):
        assert video_relay.cms_detail({**base, **change}) is not None
    for change in (
        {"vod_play_from": "unreviewed"},
        {"vod_play_url": "HD$https://127.0.0.1/private"},
        {"vod_play_url": "HD$javascript:alert(1)"},
    ):
        assert video_relay.cms_detail({**base, **change}) is None
    news = video_relay.cms_detail(
        {
            "vod_id": 1,
            "vod_name": "新闻：警方通报涉黄网站整治",
            "vod_tag": "新闻",
            "vod_play_url": "高清$https://cdn.news.example.com/news.mp4",
        }
    )
    assert news.media_url == "https://cdn.news.example.com/news.mp4"


def test_cms_categories_are_bounded_and_preserve_provider_order():
    payload = {
        "code": 1,
        "class": [
            {"type_id": 71, "type_name": "短视频"},
            {"type_id": 36, "type_name": "国产精选"},
            {"type_id": 71, "type_name": "重复分类"},
            {"type_id": 0, "type_name": "全部"},
            {"type_id": "99", "type_name": "字符串ID"},
            {"type_id": 42, "type_name": ""},
            {"type_id": 41, "type_name": "热点黑料"},
        ],
    }
    assert video_relay.cms_categories(payload) == [
        {"id": 71, "name": "短视频"},
        {"id": 36, "name": "国产精选"},
        {"id": 41, "name": "热点黑料"},
    ]
    assert video_relay.cms_categories({"code": 1, "list": payload["class"]}) != []
    assert video_relay.cms_categories({"code": 0}) == []


def test_tmzyz_player_and_byte_range_playlist_build_ephemeral_plan():
    manifest = "e07775c0f4c6fbdc1df829b40c9bd680"
    assert (
        video_relay.tmzyz_manifest_id(
            f"<script>const config = {{url: 'api.php?mpd={manifest}'}}</script>"
        )
        == manifest
    )
    assert (
        video_relay.tmzyz_manifest_id(f"api.php?mpd={manifest} api.php?mpd={'f' * 32}")
        is None
    )
    media = "https://edge.eos-dongguan-6.cmecloud.cn/video-id?signature=fixture"
    playlist = "\n".join(
        [
            "#EXTM3U",
            "#EXT-X-VERSION:7",
            f'#EXT-X-MAP:URI="{media}",BYTERANGE="1351@0"',
            "#EXTINF:9.700000,",
            "#EXT-X-BYTERANGE:1546498@1351",
            media,
            "#EXT-X-ENDLIST",
        ]
    )
    plan = video_relay.tmzyz_media_plan(playlist, manifest, max_bytes=2 * 1024 * 1024)
    assert plan is not None
    assert plan.total_bytes == 1547849
    assert [(part.offset, part.length) for part in plan.ranges] == [
        (0, 1351),
        (1351, 1546498),
    ]

    assert (
        video_relay.tmzyz_media_plan(
            playlist.replace("cmecloud.cn", "example.com"),
            manifest,
            max_bytes=2 * 1024 * 1024,
        )
        is None
    )
    assert (
        video_relay.tmzyz_media_plan(playlist, manifest, max_bytes=1024 * 1024) is None
    )


@pytest.mark.asyncio
async def test_old_relay_config_reads_with_cms_disabled(tmp_path):
    path = tmp_path / "relay.sqlite3"
    old = {"sources": [{"source": "@legacy"}], "max_video_mb": 100}
    with video_relay.database(path) as db:
        db.execute(
            "INSERT INTO relay_configs VALUES(?,?,?,?,?,?)",
            ("1", "5", 1, json.dumps(old), 0, "admin"),
        )
        db.commit()
    restored = await video_relay.control(path, "1", "5", "admin", "r", {}, lambda: None)
    assert restored["config"]["cms_url"] == ""
    assert restored["config"]["enabled"] is False
    assert "sources" not in restored["config"]
    assert restored["runtime"]["state"] == "stopped"


@pytest.mark.asyncio
async def test_dashboard_draft_preview_commit_and_restart_without_business_mutation(
    page_app, headers
):
    app, page, service, configs, root = page_app
    before = copy.deepcopy(configs)
    transport = httpx.ASGITransport(app=app)
    scope = {"bot_id": "fixture", "group_id": "5"}
    async with httpx.AsyncClient(
        transport=transport, base_url="http://fixture", headers=headers
    ) as c:
        read = await c.get(f"{PREFIX}/video-relay", params=scope)
        assert read.status_code == 200
        state = read.json()["data"]
        assert state["runtime"]["state"] == "stopped"
        assert state["runtime"]["tasks"] == []
        assert state["version"] == 0
        draft = {**state["config"], "cms_url": "https://news.example.com/api?ac=list"}
        preview = await c.post(
            f"{PREFIX}/video-relay",
            json={
                **scope,
                "operation": "preview",
                "version": 0,
                "revision": state["revision"],
                "config": draft,
            },
        )
        assert preview.status_code == 200
        token = preview.json()["data"]["token"]
        assert (
            preview.json()["data"]["config"]["cms_url"]
            == "https://news.example.com/api"
        )
        assert (await c.get(f"{PREFIX}/video-relay", params=scope)).json()["data"][
            "version"
        ] == 0
        commit = {
            **scope,
            "operation": "commit",
            "version": 0,
            "revision": state["revision"],
            "token": token,
        }
        result = await c.post(f"{PREFIX}/video-relay", json=commit)
        assert result.json()["data"] == {"saved": True, "version": 1, "started": False}
        assert (await c.post(f"{PREFIX}/video-relay", json=commit)).status_code == 409
        persisted = (await c.get(f"{PREFIX}/video-relay", params=scope)).json()["data"]
        assert persisted["config"]["cms_url"] == "https://news.example.com/api"
        assert (
            await c.get(f"{PREFIX}/video-relay", params={**scope, "group_id": "6"})
        ).json()["data"]["version"] == 0
    assert configs == before
    service.update_bot.assert_not_awaited()
    path = root / "video-relay-control.sqlite3"
    assert path.is_file()
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM relay_config_audit").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM relay_previews").fetchone()[0] == 0
    restored = await video_relay.control(
        path, "1", "5", "dashboard:new-session", state["revision"], {}, lambda: None
    )
    assert restored["version"] == 1
    assert restored["runtime"]["state"] == "stopped"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["actor", "account", "group", "revision", "expired", "changed_config"]
)
async def test_confirmation_is_scope_actor_version_and_expiry_bound(
    tmp_path, monkeypatch, case
):
    path = tmp_path / "relay.sqlite3"
    base = {
        "operation": "preview",
        "config": video_relay.RelayConfig().model_dump(),
        "version": 0,
        "revision": "original",
    }
    preview = await video_relay.control(
        path, "1", "5", "admin", "original", base, lambda: None
    )
    kwargs = {
        "path": path,
        "account": "1",
        "group": "5",
        "actor": "admin",
        "revision": "original",
        "body": {
            "operation": "commit",
            "version": 0,
            "revision": "original",
            "token": preview["token"],
        },
        "guard": lambda: None,
    }
    if case in {"actor", "account", "group"}:
        kwargs[case] = "different"
    elif case == "revision":
        kwargs["revision"] = kwargs["body"]["revision"] = "changed"
    elif case == "expired":
        monkeypatch.setattr(video_relay.time, "time", lambda: preview["expires_at"] + 1)
    else:
        with sqlite3.connect(path) as db:
            db.execute(
                "INSERT INTO relay_configs VALUES(?,?,?,?,?,?)",
                ("1", "5", 1, json.dumps(base["config"]), 0, "admin"),
            )
    with pytest.raises(ApiError) as rejected:
        await video_relay.control(**kwargs)
    assert rejected.value.status_code == 409
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM relay_config_audit").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_parallel_confirmation_writes_once(tmp_path):
    path = tmp_path / "relay.sqlite3"
    draft = {"operation": "preview", "config": {}, "revision": "r1", "version": 0}
    preview = await video_relay.control(
        path, "1", "5", "admin", "r1", draft, lambda: None
    )
    body = {
        "operation": "commit",
        "revision": "r1",
        "version": 0,
        "token": preview["token"],
    }
    results = await asyncio.gather(
        *[
            video_relay.control(path, "1", "5", "admin", "r1", body, lambda: None)
            for _ in range(2)
        ],
        return_exceptions=True,
    )
    assert sum(isinstance(r, dict) and r.get("saved") is True for r in results) == 1
    assert sum(isinstance(r, ApiError) for r in results) == 1


@pytest.mark.asyncio
async def test_commit_guard_rolls_back_draft_and_token_consumption(tmp_path):
    path = tmp_path / "relay.sqlite3"
    preview = await video_relay.control(
        path,
        "1",
        "5",
        "admin",
        "r",
        {"operation": "preview", "config": {}, "version": 0, "revision": "r"},
        lambda: None,
    )
    checks = 0

    def revoked_at_commit():
        nonlocal checks
        checks += 1
        if checks > 1:
            raise ApiError("revoked", status_code=409)

    with pytest.raises(ApiError):
        await video_relay.control(
            path,
            "1",
            "5",
            "admin",
            "r",
            {
                "operation": "commit",
                "version": 0,
                "revision": "r",
                "token": preview["token"],
            },
            revoked_at_commit,
        )
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM relay_configs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM relay_previews").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_dashboard_authentication_scope_and_lifecycle(page_app, headers):
    app, page, _, configs, _ = page_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as c:
        url = f"{PREFIX}/video-relay?bot_id=fixture&group_id=5"
        assert (await c.get(url)).status_code == 401
        assert (
            await c.get(url, headers={"Authorization": "ApiKey fixture-key"})
        ).status_code == 403
        assert (
            await c.get(
                f"{PREFIX}/video-relay?bot_id=other&group_id=5", headers=headers
            )
        ).status_code == 409
        assert (
            await c.get(
                f"{PREFIX}/video-relay?bot_id=fixture&group_id=999", headers=headers
            )
        ).status_code == 409
        page.context.platform_manager.platform_insts[0].account = "changed"
        assert (await c.get(url, headers=headers)).status_code == 409
        page.active = False
        assert (await c.get(url, headers=headers)).status_code == 403


@pytest.mark.asyncio
async def test_test_video_inspection_requires_jwt_and_is_get_only(
    page_app, headers, monkeypatch
):
    from astrbot.builtin_stars.wangshangliao_moderation import video_relay_fixture

    app, _page, _service, _configs, root = page_app
    inspect = AsyncMock(return_value={"videos": [], "scanned": 0, "undecodable": 0})
    monkeypatch.setattr(video_relay_fixture, "inspect_test_videos", inspect)
    params = {
        "bot_id": "fixture",
        "group_id": "5",
        "operation": "inspect_test_video",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        url = f"{PREFIX}/video-relay"
        assert (await client.get(url, params=params)).status_code == 401
        assert (
            await client.get(
                url, params=params, headers={"Authorization": "ApiKey fixture-key"}
            )
        ).status_code == 403
        inspect.assert_not_awaited()
        assert (
            await client.get(url, params=params, headers=headers)
        ).status_code == 200
        inspect.assert_awaited_once()
        assert (
            await client.post(
                url, json={**params, "revision": "r", "version": 0}, headers=headers
            )
        ).status_code == 400
        assert (
            await client.get(
                url, params={**params, "message_id": "arbitrary"}, headers=headers
            )
        ).status_code == 400
        inspect.assert_awaited_once()
    assert not (root / "video-relay-control.sqlite3").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {},
        {"bot_id": "fixture", "group_id": "5", "operation": "start"},
        {
            "bot_id": "fixture",
            "group_id": "5",
            "operation": "commit",
            "version": True,
            "revision": "r",
            "token": "x" * 32,
        },
    ],
)
async def test_dashboard_rejects_unknown_operations_and_malformed_envelopes(
    page_app, headers, body
):
    app, *_ = page_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fixture",
        headers=headers,
    ) as c:
        response = await c.post(
            f"{PREFIX}/video-relay",
            content=json.dumps(body),
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_large_or_invalid_draft_never_persists(page_app, headers):
    app, *_ = page_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fixture",
        headers=headers,
    ) as c:
        response = await c.post(f"{PREFIX}/video-relay", content=b" " * 16385)
        assert response.status_code == 413
        state = (await c.get(f"{PREFIX}/video-relay?bot_id=fixture&group_id=5")).json()[
            "data"
        ]
        response = await c.post(
            f"{PREFIX}/video-relay",
            json={
                "bot_id": "fixture",
                "group_id": "5",
                "operation": "preview",
                "version": 0,
                "revision": state["revision"],
                "config": {"target_token": "sensitive-fixture"},
            },
        )
        assert response.status_code == 400
        assert "sensitive-fixture" not in response.text


@pytest.mark.asyncio
async def test_dashboard_lists_cms_categories_for_the_saved_source(
    page_app, headers, monkeypatch
):
    from astrbot.builtin_stars.wangshangliao_moderation import video_relay_delivery

    app, _, _, _, _ = page_app
    fetch = AsyncMock(
        return_value={
            "cms_url": "https://tmzyz.com/api.php/provide/vod/",
            "categories": [{"id": 71, "name": "短视频"}],
        }
    )
    monkeypatch.setattr(video_relay_delivery, "fetch_cms_categories", fetch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fixture",
        headers=headers,
    ) as client:
        response = await client.get(
            f"{PREFIX}/video-relay",
            params={
                "bot_id": "fixture",
                "group_id": "5",
                "operation": "categories",
            },
        )
    assert response.status_code == 200
    assert response.json()["data"]["categories"] == [{"id": 71, "name": "短视频"}]
    fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_dashboard_rejects_category_outside_cms_scope(page_app, headers):
    app, _, _, _, _ = page_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fixture",
        headers=headers,
    ) as client:
        response = await client.post(
            f"{PREFIX}/video-relay",
            json={
                "bot_id": "fixture",
                "group_id": "5",
                "operation": "preview_video",
                "version": 0,
                "revision": "fixture",
                "category_id": -1,
            },
        )
    assert response.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "hint"),
    [
        ("business_native_permission", "群管权限"),
        ("business_owner_revoked", "现任管理员"),
        ("proactive_not_authorized", "主动发送"),
        ("business_rules_inactive", "已暂停"),
        ("unsaved", "确认保存规则"),
    ],
)
async def test_video_preview_explains_rules_and_permission_failures(
    page_app, headers, monkeypatch, failure, hint
):
    from astrbot.builtin_stars.wangshangliao_moderation import video_relay_delivery
    from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError

    app, page, _, _, _ = page_app
    rules = SimpleNamespace(
        read=Mock(return_value={"version": 0 if failure == "unsaved" else 1}),
        send_guard=Mock(side_effect=OpenError(failure)),
    )
    page.plugin = SimpleNamespace(
        open_service=SimpleNamespace(active=True, rule_replies=rules)
    )
    fetch = AsyncMock()
    monkeypatch.setattr(video_relay_delivery, "fetch_cms_video", fetch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fixture",
        headers=headers,
    ) as client:
        response = await client.post(
            f"{PREFIX}/video-relay",
            json={
                "bot_id": "fixture",
                "group_id": "5",
                "operation": "preview_video",
                "version": 0,
                "revision": "fixture",
            },
        )
    assert response.status_code == 409
    assert hint in response.json()["message"]
    fetch.assert_not_awaited()
