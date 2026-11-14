"""Private TMZYZ gateway configuration, signing and reviewed edge hosts."""

import hashlib
import json

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import tmzyz_gateway, video_relay
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from tests.test_wangshangliao_native_guide_upload import Response, Session


def _playlist(host="edge.eos-dongguan-6.cmecloud.cn"):
    media = f"https://{host}/video-id?signature=fixture"
    return "\n".join(
        [
            "#EXTM3U",
            f'#EXT-X-MAP:URI="{media}",BYTERANGE="12@0"',
            "#EXTINF:2.0,",
            "#EXT-X-BYTERANGE:100@12",
            media,
            "#EXT-X-ENDLIST",
        ]
    )


def _write(tmp_path, **changes):
    value = {
        "player_code": "fixture-player",
        "token": "fixture-only-not-a-real-credential",
        "carrier": "移动",
    }
    value.update(changes)
    path = tmp_path / "tmzyz_gateway.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_gateway_config_is_private_and_bounded(tmp_path):
    path = _write(tmp_path)
    config = tmzyz_gateway.load(path)
    assert config.player_code == "fixture-player"
    assert config.carrier == "移动"
    assert config.prefer is True

    path.chmod(0o644)
    with pytest.raises(ProtocolError, match="tmzyz_gateway_config_insecure"):
        tmzyz_gateway.load(path)

    path.chmod(0o600)
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(ProtocolError, match="tmzyz_gateway_config_invalid"):
        tmzyz_gateway.load(path)


@pytest.mark.asyncio
async def test_gateway_posts_reviewed_signature_and_returns_playlist(monkeypatch):
    resource = "8e49c0570914606000e39d3d684c3ab2"
    manifest = "a" * 32
    playlist = _playlist()
    session = Session(
        [
            Response(
                json.dumps(
                    {"code": 0, "data": {"mpd": manifest}, "message": "Request Success"}
                ).encode()
            ),
            Response(
                json.dumps(
                    {"code": 0, "data": {"mpd": playlist}, "message": "Request Success"}
                ).encode()
            ),
        ]
    )
    config = tmzyz_gateway.GatewayConfig(
        "fixture-player", "fixture-only-not-a-real-credential"
    )
    result, returned = await tmzyz_gateway.resolve(
        session, resource, config, lambda: None
    )
    assert result == playlist and returned == manifest
    assert session.calls[0][0] == "POST"
    assert session.calls[0][1].endswith("/api/getPlayerUrl")
    body = session.calls[0][2]["json"]
    assert body["name"] == resource
    assert body["player_code"] == "fixture-player"
    assert body["carrier"] == "移动"
    assert (
        body["sign"]
        == hashlib.md5(
            f"fixture-playerfixture-only-not-a-real-credential{resource}".encode()
        ).hexdigest()
    )
    assert session.calls[1][0] == "GET"
    assert session.calls[1][1].endswith("/api/mpd/" + manifest)
    assert all(call[2].get("allow_redirects") is False for call in session.calls)


@pytest.mark.asyncio
async def test_gateway_quota_and_carrier_errors_are_explicit():
    resource = "8e49c0570914606000e39d3d684c3ab2"
    for message, code in (("点数不足,请及时充值", "tmzyz_gateway_insufficient"),):
        session = Session(
            [
                Response(
                    json.dumps({"code": -1, "data": "", "message": message}).encode()
                )
            ]
        )
        config = tmzyz_gateway.GatewayConfig(
            "fixture-player", "fixture-only-not-a-real-credential"
        )
        with pytest.raises(ProtocolError, match=code):
            await tmzyz_gateway.resolve(session, resource, config, lambda: None)


@pytest.mark.parametrize(
    "host",
    [
        "edge.eos-dongguan-6.cmecloud.cn",
        "media-qhxn-fj-person.qh6oss.ctyunxs.cn",
        "gdtelecom-gdgznj-fj-person.shusheng2.mini189.cn",
    ],
)
def test_reviewed_gateway_edge_hosts_are_accepted(host):
    playlist = _playlist(host)
    plan = video_relay.tmzyz_media_plan(playlist, "a" * 32, max_bytes=1024)
    assert plan is not None
    assert len(plan.ranges) == 2


def test_unreviewed_gateway_edge_host_is_rejected():
    playlist = _playlist("edge.example.com")
    assert video_relay.tmzyz_media_plan(playlist, "a" * 32, max_bytes=1024) is None
