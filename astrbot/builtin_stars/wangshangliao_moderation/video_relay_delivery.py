"""Single-video CMS preview and explicit native delivery, never a bulk worker."""

import asyncio
import hashlib
import ipaddress
import json
import re
import secrets
import shutil
import time
import uuid
from pathlib import Path

import aiohttp

from astrbot.core import logger
from astrbot.core.platform.sources.wangshangliao.database import AsyncDatabase
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from astrbot.dashboard.responses import ApiError

from .native_guide_upload import _read_bounded, is_native_asset_id
from .native_video import (
    VIDEO_LIMIT,
    PreparedVideo,
    VideoAsset,
    prepare_video,
    send_video,
    upload_video,
)
from .open_store import OpenError
from .tmzyz_gateway import load as load_gateway
from .tmzyz_gateway import resolve as resolve_gateway
from .video_relay import (
    TMZYZ_CMS_URL,
    cms_categories,
    cms_detail,
    cms_ids,
    cms_url,
    database,
    follow_source,
    saved_config,
    tmzyz_manifest_id,
    tmzyz_media_plan,
)
from .video_relay_fixture import TEST_SOURCE, neutral_video, require_test_scope


class PublicResolver(aiohttp.resolver.ThreadedResolver):
    """Reject private DNS answers before the connector opens any media socket."""

    async def resolve(self, host, port=0, family=0):
        records = await super().resolve(host, port, family)
        if not records or any(
            not ipaddress.ip_address(row["host"]).is_global for row in records
        ):
            raise OSError("video_source_address_rejected")
        return records


async def _fetch(http, url, limit, guard):
    """Read a fixed-source document with no credentials, redirects or URL logging.

    Args:
        http: Cookie-free, public-address-only HTTP client.
        url: Server-owned fixed provider URL.
        limit: Bounded response size.
        guard: Current account and group authority fence.

    Returns:
        Bounded raw bytes, not persisted signed source URLs.

    Raises:
        ProtocolError: If status, size or scope is invalid.
    """
    guard()
    async with http.get(url, allow_redirects=False, ssl=True) as response:
        if response.status != 200:
            raise ProtocolError("video_source_rejected")
        try:
            raw = await _read_bounded(response, limit)
        except ProtocolError:
            raise ProtocolError("video_source_limit") from None
    guard()
    return raw


def _gateway(path):
    """Load optional private TMZYZ credentials from an instance directory.

    Args:
        path: Instance-owned video control database or its parent directory.

    Returns:
        Validated gateway credentials, or None when no private file exists.

    Raises:
        ProtocolError: If the file exists but is not private or is invalid.
    """
    directory = path if path.is_dir() else path.parent
    return load_gateway(directory / "tmzyz_gateway.json")


async def _tmzyz_playlist(http, resource_id, gateway, guard):
    """Resolve one bkplayer ID through the private gateway or public player.

    Args:
        http: Cookie-free, public-address-only HTTP client.
        resource_id: 32-character lowercase TMZYZ resource identifier.
        gateway: Optional private gateway credentials.
        guard: Current account and group fence.

    Returns:
        Playlist text and its manifest identifier.

    Raises:
        ProtocolError: If the selected resolver cannot return a valid playlist.
    """
    gateway_error = None
    if gateway is not None and gateway.prefer:
        try:
            return await resolve_gateway(http, resource_id, gateway, guard)
        except ProtocolError as cause:
            # Quota, carrier or provider failures fall back to the public player.
            gateway_error = cause
    try:
        player = (
            await _fetch(
                http, "https://player.tmzyz.com?url=" + resource_id, 65536, guard
            )
        ).decode("utf-8")
        manifest = tmzyz_manifest_id(player)
        if manifest is None:
            raise ProtocolError("video_manifest_invalid")
        playlist = (
            await _fetch(
                http,
                "https://player.tmzyz.com/api.php?mpd=" + manifest,
                1024 * 1024,
                guard,
            )
        ).decode("utf-8")
    except ProtocolError:
        if gateway_error is not None:
            raise gateway_error from None
        raise
    return playlist, manifest


async def fetch_cms_video(
    config,
    skipped,
    guard,
    database_path=None,
    category=None,
    max_video_mb=None,
    vod_id=None,
    validate_candidate=None,
    reject_candidate=None,
    page=1,
    offset=0,
    advance_cursor=None,
):
    """Resolve one CMS item and download supported MP4 or provider fMP4 media.

    Args:
        config: Saved CMS endpoint, optional category and resource limits.
        skipped: Source IDs already accepted or uncertain in this exact scope.
        guard: Current account and group fence.
        database_path: Instance-owned control database; its sibling private
            ``tmzyz_gateway.json`` enables the private parse gateway.
        category: Optional operator-selected category for this one preview;
            None keeps the saved category.
        max_video_mb: Optional one-shot size limit for this preview; None keeps
            the saved limit.
        vod_id: Optional exact CMS item ID for a targeted preview; bypasses the
            list/category filter but keeps every media and dedup check.
        validate_candidate: Optional asynchronous local media validation. A
            candidate-level failure continues selection within the same round.
        reject_candidate: Optional asynchronous callback receiving only a source
            ID and sanitized failure code for bounded retry cooldowns.
        page: Saved list page for this exact automatic scan scope.
        offset: Saved candidate offset within that page.
        advance_cursor: Optional durable progress callback, without media metadata.

    Returns:
        Source metadata and bounded local MP4 bytes, without content filtering.

    Raises:
        ProtocolError: If no candidate, metadata, range or response is acceptable.
    """
    limit_mb = config.max_video_mb if max_video_mb is None else max_video_mb
    max_bytes = limit_mb * 1024 * 1024
    effective_category = config.category_id if category is None else category
    if (
        type(page) is not int
        or not 1 <= page <= 1000000
        or type(offset) is not int
        or not 0 <= offset <= 100
    ):
        raise ProtocolError("video_pagination_invalid")
    gateway = _gateway(database_path) if database_path is not None else None
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(resolver=PublicResolver(), limit=2),
        cookie_jar=aiohttp.DummyCookieJar(),
        trust_env=False,
        timeout=aiohttp.ClientTimeout(total=300, connect=10, sock_read=60),
    ) as http:

        async def candidates():
            """Yield at most 20 candidates across at most five bounded list reads."""
            if vod_id is not None:
                yield vod_id
                return
            current_page, current_offset = page, offset
            visited_pages, seen_ids, attempted = set(), set(), 0
            for _ in range(5):
                guard()
                if current_page in visited_pages:
                    return
                visited_pages.add(current_page)
                listing = json.loads(
                    await _fetch(
                        http,
                        cms_url(
                            config.cms_url,
                            category=effective_category,
                            page=current_page,
                        ),
                        262144,
                        guard,
                    )
                )
                if (
                    not isinstance(listing, dict)
                    or type(listing.get("code")) is not int
                    or listing["code"] != 1
                    or not isinstance(listing.get("list"), list)
                    or len(listing["list"]) > 100
                ):
                    raise ProtocolError("video_pagination_invalid")
                pagination = {}
                for key, default in (
                    ("pagecount", 1),
                    ("page", 1 if current_page == 1 else None),
                ):
                    value = listing.get(key, default)
                    if type(value) is str and re.fullmatch(r"[0-9]{1,7}", value):
                        value = int(value)
                    if type(value) is not int or not 0 <= value <= 1000000:
                        raise ProtocolError("video_pagination_invalid")
                    pagination[key] = value
                page_count = max(1, pagination["pagecount"])
                if current_page > page_count:
                    current_page, current_offset = 1, 0
                    if advance_cursor is not None:
                        await advance_cursor(current_page, current_offset)
                    continue
                if pagination["page"] != current_page:
                    raise ProtocolError("video_pagination_invalid")
                ids = cms_ids(listing, effective_category)
                next_page = current_page + 1 if current_page < page_count else 1
                for index, candidate in enumerate(ids):
                    if index < current_offset or candidate in seen_ids:
                        continue
                    seen_ids.add(candidate)
                    if candidate in skipped:
                        continue
                    if advance_cursor is not None:
                        await advance_cursor(
                            current_page if index + 1 < len(ids) else next_page,
                            index + 1 if index + 1 < len(ids) else 0,
                        )
                    guard()
                    attempted += 1
                    yield candidate
                    if attempted >= 20:
                        return
                current_page, current_offset = next_page, 0
                if advance_cursor is not None:
                    await advance_cursor(current_page, current_offset)

        last_error = None
        async for candidate_id in candidates():
            detail = json.loads(
                await _fetch(
                    http,
                    cms_url(config.cms_url, operation="detail", vod_id=candidate_id),
                    262144,
                    guard,
                )
            )
            records = detail.get("list") if isinstance(detail, dict) else None
            if not (
                isinstance(detail, dict)
                and detail.get("code") == 1
                and isinstance(records, list)
                and len(records) == 1
                and isinstance(records[0], dict)
                and records[0].get("vod_id") == candidate_id
            ):
                continue
            item = cms_detail(records[0])
            if item is None:
                continue
            try:
                if item.media_url:
                    media = await _fetch(http, item.media_url, max_bytes, guard)
                    if validate_candidate is not None:
                        await validate_candidate(media)
                    return item, media
                if config.cms_url != TMZYZ_CMS_URL:
                    raise ProtocolError("video_player_unsupported")
                playlist, manifest = await _tmzyz_playlist(
                    http, item.resource_id, gateway, guard
                )
                plan = tmzyz_media_plan(playlist, manifest, max_bytes=max_bytes)
                if plan is None:
                    # A private-gateway playlist can name a carrier edge that cannot
                    # be served as bounded byte ranges. Retry the public player once.
                    if gateway is not None and gateway.prefer:
                        playlist, manifest = await _tmzyz_playlist(
                            http, item.resource_id, None, guard
                        )
                        plan = tmzyz_media_plan(playlist, manifest, max_bytes=max_bytes)
                if plan is None:
                    unbounded = tmzyz_media_plan(
                        playlist, manifest, max_bytes=1024 * 1024 * 1024
                    )
                    if unbounded is not None and unbounded.total_bytes > max_bytes:
                        raise ProtocolError("video_size_limit")
                    raise ProtocolError("video_manifest_invalid")
                # The byte-range plan can under-count the remuxed MP4. Stop early
                # when the source is already larger than the configured limit.
                if plan.total_bytes > max_bytes:
                    raise ProtocolError("video_size_limit")
                data = bytearray()
                source_total = 0
                for index, part in enumerate(plan.ranges):
                    guard()
                    end = part.offset + part.length - 1
                    async with http.get(
                        part.url,
                        headers={"Range": f"bytes={part.offset}-{end}"},
                        allow_redirects=False,
                        ssl=True,
                    ) as response:
                        match = re.fullmatch(
                            r"bytes ([0-9]+)-([0-9]+)/([1-9][0-9]*)",
                            response.headers.get("Content-Range", ""),
                        )
                        if (
                            response.status != 206
                            or response.headers.get("Content-Encoding", "identity")
                            != "identity"
                            or match is None
                            or (int(match[1]), int(match[2])) != (part.offset, end)
                            or int(match[3]) <= end
                        ):
                            raise ProtocolError("video_range_invalid")
                        chunk = await _read_bounded(response, part.length)
                        if len(chunk) != part.length:
                            raise ProtocolError("video_range_invalid")
                        if index == 0:
                            source_total = int(match[3])
                            if source_total < plan.total_bytes:
                                # A playlist that omits the tail produces a MP4
                                # whose final moof/mdat cannot be parsed. Reject
                                # before downloading the rest.
                                raise ProtocolError(
                                    f"video_media_invalid:tail:{source_total}:{plan.total_bytes}"
                                )
                        data.extend(chunk)
                        guard()
                if len(data) != plan.total_bytes:
                    raise ProtocolError("video_range_invalid")
                last = plan.ranges[-1]
                if source_total and last.offset + last.length != source_total:
                    # The provider's playlist can omit the trailing object bytes.
                    # Fetch only that tail from the reviewed signed URL and verify
                    # its exact Content-Range before accepting the MP4.
                    if last.offset + last.length > source_total:
                        raise ProtocolError("video_media_invalid")
                    start = last.offset + last.length
                    remaining = source_total - start
                    if remaining <= 0 or len(data) + remaining > max_bytes:
                        raise ProtocolError("video_size_limit")
                    guard()
                    async with http.get(
                        last.url,
                        headers={"Range": f"bytes={start}-{source_total - 1}"},
                        allow_redirects=False,
                        ssl=True,
                    ) as response:
                        match = re.fullmatch(
                            r"bytes ([0-9]+)-([0-9]+)/([1-9][0-9]*)",
                            response.headers.get("Content-Range", ""),
                        )
                        if (
                            response.status != 206
                            or response.headers.get("Content-Encoding", "identity")
                            != "identity"
                            or match is None
                            or (int(match[1]), int(match[2]), int(match[3]))
                            != (start, source_total - 1, source_total)
                        ):
                            raise ProtocolError("video_range_invalid")
                        chunk = await _read_bounded(response, remaining)
                        if len(chunk) != remaining:
                            raise ProtocolError("video_range_invalid")
                        data.extend(chunk)
                        guard()
                media = bytes(data)
                if source_total and len(media) != source_total:
                    raise ProtocolError(
                        f"video_media_invalid:length:{len(media)}:{source_total}"
                    )
                if media[4:8] == b"guzq":
                    # First-party player /js/sw.js reverses a fixed per-byte +1 offset.
                    media = await asyncio.to_thread(
                        media.translate,
                        bytes((value - 1) & 0xFF for value in range(256)),
                    )
                guard()
                if validate_candidate is not None:
                    await validate_candidate(media)
                return item, media
            except ProtocolError as cause:
                # A category can contain unsupported, unavailable or oversized
                # entries before a usable one. Continue within the same bounded
                # candidate window only for candidate-level failures; a malformed
                # range or transport response must fail closed.
                if not str(cause).startswith("video_media_invalid:") and str(
                    cause
                ) not in {
                    "tmzyz_gateway_insufficient",
                    "tmzyz_gateway_carrier",
                    "tmzyz_gateway_rejected",
                    "video_manifest_invalid",
                    "video_player_unsupported",
                    "video_size_limit",
                    "video_source_limit",
                    "video_media_invalid",
                    "video_format_unsupported",
                    "video_duration_limit",
                    "tmzyz_gateway_response_limit",
                }:
                    raise
                reason = str(cause)
                if reason.startswith("video_media_invalid:"):
                    reason = "video_media_invalid"
                if reject_candidate is not None:
                    await reject_candidate(item.vod_id, reason)
                logger.info(
                    "WSL video relay candidate skipped: category=%s vod=%s reason=%s",
                    effective_category,
                    item.vod_id,
                    reason,
                )
                last_error = ProtocolError(reason)
                guard()
                continue
        if last_error is not None:
            # Preserve actionable reasons; unsupported candidates still produce a
            # stable, user-facing failure instead of leaking arbitrary exception text.
            if str(last_error) in {
                "video_cms_unavailable",
                "video_already_published",
                "video_size_limit",
                "tmzyz_gateway_insufficient",
                "tmzyz_gateway_response_limit",
                "video_source_limit",
                "video_media_invalid",
                "video_format_unsupported",
                "video_duration_limit",
                "video_manifest_invalid",
                "video_player_unsupported",
            }:
                raise last_error
            raise ProtocolError("video_cms_unavailable") from None
        raise ProtocolError("video_cms_unavailable")


async def fetch_cms_categories(path, account, group, guard):
    """Read the saved CMS category catalog for the current bot and group.

    Args:
        path: Private per-instance control database.
        account: Bound Wangshangliao account.
        group: Current enabled destination group.
        guard: Current account, group and lifecycle fence.

    Returns:
        Saved CMS URL plus bounded ``{"id", "name"}`` categories.

    Raises:
        ApiError: If no CMS URL is saved or the source is unreachable.
    """
    async with AsyncDatabase(lambda: database(path), guard=guard) as db:
        row = await db.fetchone(
            "SELECT config FROM relay_configs WHERE account=? AND group_id=?",
            (account, group),
        )
    config = saved_config(row["config"]) if row else None
    if config is None or not config.cms_url:
        raise ApiError("请先保存CMS接口地址。", status_code=409)
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(resolver=PublicResolver(), limit=2),
        cookie_jar=aiohttp.DummyCookieJar(),
        trust_env=False,
        timeout=aiohttp.ClientTimeout(total=30, connect=10),
    ) as http:
        payload = json.loads(
            await _fetch(
                http,
                cms_url(config.cms_url),
                262144,
                guard,
            )
        )
    return {"cms_url": config.cms_url, "categories": cms_categories(payload)}


async def refresh_native_evidence(service, adapter, group, guard):
    """Refresh a stale native-role roster before a saved-send guard reads it.

    The send guard deliberately reads evidence synchronously. Video previews and
    scheduled jobs can start long after the last message-driven roster refresh,
    so refresh before its 60-second deadline, leaving time for send-lock waiting.

    Args:
        service: Active integrated open service.
        adapter: Current authenticated native adapter.
        group: Exact enabled destination group.
        guard: Current account, group and lifecycle fence.

    Raises:
        OpenError: If current membership or the bot's role cannot be proven.
    """
    capabilities = getattr(service, "capabilities", None)
    roster = getattr(service, "roster", None)
    if not isinstance(capabilities, dict) or not callable(roster):
        return
    key = (str(adapter.account), group)
    checked = (capabilities.get(key) or {}).get("checked_at", 0)
    if checked >= time.time() - 45:
        return
    await roster(adapter, group)
    guard()


async def cleanup_cache(path: Path, job_id: str):
    """Remove one operation's files and persist cleanup failures for recovery.

    Args:
        path: Private relay database.
        job_id: Server-generated operation UUID, never a caller-supplied path.
    """
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        return
    directory = path.parent / "video-relay-cache" / job_id
    state = "deleted"
    try:
        await asyncio.to_thread(shutil.rmtree, directory)
    except FileNotFoundError:
        pass
    except OSError:
        state = "pending"
    async with AsyncDatabase(lambda: database(path), write=True) as db:
        await db.execute(
            "UPDATE relay_video_jobs SET cleanup_state=? WHERE id=?", (state, job_id)
        )


async def expire_previews(path: Path):
    """Expire unsubmitted previews and remove only their operation-owned files.

    Args:
        path: Private relay control database location.
    """
    async with AsyncDatabase(lambda: database(path), write=True) as db:
        stale = await db.fetchall(
            "SELECT id,state FROM relay_video_jobs WHERE "
            "(state='prepared' AND expires<=?) OR "
            "(state IN ('preparing','uploading','sending') AND updated<?) LIMIT 64",
            (time.time(), time.time() - 150),
        )
        for row in stale:
            await db.execute(
                "UPDATE relay_video_jobs SET state=?,token_digest='',updated=? WHERE id=?",
                (
                    "unknown"
                    if row["state"] in {"uploading", "sending"}
                    else "expired",
                    time.time(),
                    row["id"],
                ),
            )
            if row["state"] in {"uploading", "sending"}:
                await db.execute(
                    "UPDATE relay_publications SET state='unknown',updated=? WHERE job=? "
                    "AND state='pending'",
                    (time.time(), row["id"]),
                )
        pending = await db.fetchall(
            "SELECT id FROM relay_video_jobs WHERE cleanup_state IN ('','pending') "
            "AND state NOT IN ('preparing','prepared','uploading','sending') LIMIT 64"
        )
    for job_id in dict.fromkeys(row["id"] for row in [*stale, *pending]):
        await cleanup_cache(path, job_id)


async def delivery_control(
    path,
    adapter,
    group,
    actor,
    revision,
    body,
    guard,
    posting_guard,
    *,
    refresh_authority=None,
):
    """Prepare, cancel or explicitly send exactly one scoped CMS video.

    Args:
        path: Private per-instance control database.
        adapter: Existing authenticated native transport.
        group: Current enabled destination.
        actor: Authenticated dashboard principal.
        revision: Frozen bot configuration fingerprint.
        body: Strict operation envelope.
        guard: Cheap synchronous scope and lifecycle fence.
        posting_guard: Async saved tenant, rule and current native-authority check.
        refresh_authority: Optional roster refresh, called only outside send locks.

    Returns:
        Single-use preview or durable delivery outcome.

    Raises:
        ApiError: If draft, authority, media, quota or confirmation is invalid.
    """
    await expire_previews(path)
    operation, account = body["operation"], str(adapter.account)
    preparing = operation in {"preview_video", "preview_test_video"}
    testing = operation == "preview_test_video"
    if preparing:
        category = body.get("category_id")
        if category is not None and (
            type(category) is not int or not 0 <= category <= 2147483647
        ):
            raise ApiError("分类ID无效。", status_code=400)
        limit_mb = body.get("max_video_mb")
        if limit_mb is not None and (
            type(limit_mb) is not int or not 1 <= limit_mb <= 112
        ):
            raise ApiError("单视频上限只能是1—112 MiB。", status_code=400)
        target = body.get("vod_id")
        if target is not None and (type(target) is not int or not 0 < target < 1 << 63):
            raise ApiError("视频编号无效。", status_code=400)
    if operation not in {
        "preview_video",
        "preview_test_video",
        "send_video",
        "cancel_video",
        "cancel_active",
    }:
        raise ApiError("视频操作无效。", status_code=400)
    if testing:
        try:
            require_test_scope(account, group)
        except ProtocolError:
            raise ApiError(
                "中性验收视频仅限指定测试账号及测试群。", status_code=403
            ) from None
    guard()
    if operation not in {"cancel_video", "cancel_active"}:
        await posting_guard()
    token = body.get("token", "")
    digest = hashlib.sha256(token.encode()).hexdigest()
    now = time.time()
    job_id, config, metadata = "", None, {}
    follow = None
    scan_page, scan_offset, use_scan_cursor = 1, 0, False
    async with AsyncDatabase(lambda: database(path), write=True, guard=guard) as db:
        if operation == "cancel_active":
            job = await db.fetchone(
                "SELECT id,state FROM relay_video_jobs WHERE account=? AND group_id=? "
                "AND actor=? AND state IN ('preparing','prepared') "
                "ORDER BY created DESC LIMIT 1",
                (account, group, actor),
            )
            if job is None:
                submitted = await db.fetchone(
                    "SELECT id FROM relay_video_jobs WHERE account=? AND group_id=? "
                    "AND actor=? AND state IN ('uploading','sending') LIMIT 1",
                    (account, group, actor),
                )
                if submitted is not None:
                    raise ApiError(
                        "视频已提交上传或发送，不能取消；请等待结果并核对记录。",
                        status_code=409,
                    )
                raise ApiError("当前没有可取消的视频预览。", status_code=409)
            job_id = job["id"]
            was_prepared = job["state"] == "prepared"
            await db.execute(
                "UPDATE relay_video_jobs SET state='cancelled',token_digest='',"
                "expires=0,updated=? WHERE id=? AND state IN ('preparing','prepared')",
                (time.time(), job_id),
            )
            await db.execute(
                "DELETE FROM relay_publications WHERE job=? AND state='pending'",
                (job_id,),
            )
            return_result = {
                "id": job_id,
                "state": "cancelled",
                "cleanup": was_prepared,
            }
        else:
            return_result = None
        row = await db.fetchone(
            "SELECT version,config FROM relay_configs WHERE account=? AND group_id=?",
            (account, group),
        )
        if operation not in {"cancel_video", "cancel_active"} and (
            not row or row["version"] != body["version"] or body["revision"] != revision
        ):
            raise ApiError("配置已变化，请保存配置后重新预览视频。", status_code=409)
        config = saved_config(row["config"]) if row else None
        if operation == "preview_video" and not config.cms_url:
            raise ApiError("请先填写并保存CMS接口地址。", status_code=409)
        if preparing:
            if config.follow_group_id:
                if (
                    testing
                    or body.get("category_id", config.category_id) != config.category_id
                ):
                    raise ApiError(
                        "同步群不能独立选片，请在来源群操作。", status_code=409
                    )
                await follow_source(db, account, group, config)
                follow = await db.fetchone(
                    "SELECT p.*,j.asset,j.metadata FROM relay_follow_starts f "
                    "JOIN relay_publications p ON p.account=f.account "
                    "AND p.group_id=f.source_group_id AND p.created>=f.since "
                    "JOIN relay_video_jobs j ON j.id=p.job AND j.account=p.account "
                    "AND j.group_id=p.group_id AND j.state='accepted' "
                    "WHERE f.account=? AND f.group_id=? AND f.source_group_id=? "
                    "AND p.source=? AND p.state='accepted' AND NOT EXISTS("
                    "SELECT 1 FROM relay_publications t WHERE t.account=? "
                    "AND t.group_id=? AND ((t.source=p.source AND t.source_id=p.source_id) "
                    "OR t.digest=p.digest)) ORDER BY p.created,p.job LIMIT 1",
                    (
                        account,
                        group,
                        config.follow_group_id,
                        config.cms_url,
                        account,
                        group,
                    ),
                )
                if follow is None:
                    raise ApiError(
                        "等待来源群新一轮受理记录，不独立选片或补发历史视频。",
                        status_code=409,
                        data={"code": "video_follow_waiting"},
                    )
                if body.get("vod_id", follow["source_id"]) != follow["source_id"]:
                    raise ApiError("同步视频编号与来源轮次不符。", status_code=409)
            count = await db.fetchone("SELECT COUNT(*) FROM relay_video_jobs")
            if count[0] >= 4096:
                raise ApiError(
                    "视频记录已达容量上限，请先由管理员归档审计。", status_code=429
                )
            active = await db.fetchone(
                "SELECT 1 FROM relay_video_jobs WHERE "
                "state IN ('preparing','prepared','uploading','sending') LIMIT 1"
            )
            if active:
                raise ApiError(
                    "已有视频正在处理或等待确认，请先完成、取消或等待预览过期。",
                    status_code=409,
                )
            skipped = {
                value["source_id"]
                for value in await db.fetchall(
                    "SELECT source_id FROM relay_publications WHERE account=? AND group_id=? "
                    "AND source=?",
                    (account, group, config.cms_url),
                )
            }
            if not config.follow_group_id:
                skipped.update(
                    value["source_id"]
                    for value in await db.fetchall(
                        "SELECT p.source_id FROM relay_follow_starts f "
                        "JOIN relay_configs c ON c.account=f.account AND c.group_id=f.group_id "
                        "JOIN relay_publications p ON p.account=f.account AND p.group_id=f.group_id "
                        "WHERE f.account=? AND f.source_group_id=? AND p.source=? "
                        "AND json_extract(c.config,'$.enabled')=1 "
                        "AND json_extract(c.config,'$.follow_group_id')=?",
                        (account, group, config.cms_url, group),
                    )
                )
            await db.execute(
                "DELETE FROM relay_candidate_failures WHERE retry_at<=?",
                (now,),
            )
            use_candidate_cooldown = (
                not testing
                and body.get("vod_id") is None
                and body.get("max_video_mb", config.max_video_mb) == config.max_video_mb
            )
            if use_candidate_cooldown:
                skipped.update(
                    value["source_id"]
                    for value in await db.fetchall(
                        "SELECT source_id FROM relay_candidate_failures WHERE "
                        "account=? AND group_id=? AND source=? AND version=? "
                        "AND retry_at>?",
                        (account, group, config.cms_url, row["version"], now),
                    )
                )
            use_scan_cursor = (
                use_candidate_cooldown
                and body.get("category_id", config.category_id) == config.category_id
            )
            if use_scan_cursor:
                cursor = await db.fetchone(
                    "SELECT page,offset FROM relay_scan_cursors WHERE account=? "
                    "AND group_id=? AND source=? AND version=? AND category_id=?",
                    (
                        account,
                        group,
                        config.cms_url,
                        row["version"],
                        config.category_id,
                    ),
                )
                if cursor is not None:
                    scan_page, scan_offset = cursor["page"], cursor["offset"]
            job_id, token = uuid.uuid4().hex, secrets.token_urlsafe(32)
            await db.execute(
                "INSERT INTO relay_video_jobs "
                "(id,account,group_id,actor,revision,version,token_digest,expires,"
                "state,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    account,
                    group,
                    actor,
                    revision,
                    row["version"],
                    hashlib.sha256(token.encode()).hexdigest(),
                    now + 300,
                    "preparing",
                    now,
                    now,
                ),
            )
        elif operation != "cancel_active":
            job = await db.fetchone(
                "SELECT * FROM relay_video_jobs WHERE token_digest=? AND account=? "
                "AND group_id=? AND actor=? AND revision=? AND version=? "
                "AND state='prepared' AND expires>?",
                (digest, account, group, actor, body["revision"], body["version"], now),
            )
            if job is None:
                raise ApiError(
                    "视频预览已过期、已使用或不属于当前范围，请刷新核对。",
                    status_code=409,
                )
            job_id, metadata = job["id"], json.loads(job["metadata"])
            asset_id = job["asset"]
            if metadata.get("source") == TEST_SOURCE:
                try:
                    require_test_scope(account, group)
                except ProtocolError:
                    raise ApiError("验收视频范围已失效。", status_code=403) from None
            elif operation != "cancel_video" and not config.cms_url:
                raise ApiError("请先填写并保存CMS接口地址。", status_code=409)
            await db.execute(
                "UPDATE relay_video_jobs SET state=?,token_digest='',updated=? WHERE id=?",
                (
                    "cancelled" if operation == "cancel_video" else "uploading",
                    now,
                    job_id,
                ),
            )
    if return_result is not None:
        # A still-running preparation owns its directory and will clean it in its
        # own finally block; only a settled preview can be removed here.
        if return_result.pop("cleanup", False):
            await cleanup_cache(path, job_id)
        return return_result
    directory = path.parent / "video-relay-cache" / job_id
    keep, submitted, uploaded, send_started = False, False, False, False
    state, error = "failed", ""
    asset_id = "" if preparing else asset_id

    async def before_send(*, refresh=False):
        guard()
        if refresh and refresh_authority is not None:
            try:
                async with asyncio.timeout(15):
                    await refresh_authority()
            except TimeoutError:
                raise ProtocolError("video_permission_timeout") from None
            guard()
        await posting_guard()
        async with AsyncDatabase(lambda: database(path), guard=guard) as db:
            current = await db.fetchone(
                "SELECT version FROM relay_configs WHERE account=? AND group_id=?",
                (account, group),
            )
            if current is None or current["version"] != body["version"]:
                raise ProtocolError("video_configuration_changed")
            if config.follow_group_id:
                try:
                    await follow_source(db, account, group, config)
                except ApiError:
                    raise ProtocolError("video_follow_source_changed") from None
                if config.follow_group_id not in adapter.config.get(
                    "enabled_groups", []
                ):
                    raise ProtocolError("video_follow_source_changed")
                if metadata:
                    source = await db.fetchone(
                        "SELECT p.digest,j.asset FROM relay_publications p "
                        "JOIN relay_video_jobs j ON j.id=p.job AND j.account=p.account "
                        "AND j.group_id=p.group_id AND j.state='accepted' "
                        "JOIN relay_follow_starts f ON f.account=p.account "
                        "AND f.source_group_id=p.group_id AND p.created>=f.since "
                        "WHERE p.account=? AND p.group_id=? AND f.group_id=? "
                        "AND p.job=? AND p.source=? AND p.source_id=? AND p.state='accepted'",
                        (
                            account,
                            config.follow_group_id,
                            group,
                            metadata.get("follow_job_id"),
                            metadata["source"],
                            metadata["vod_id"],
                        ),
                    )
                    if (
                        source is None
                        or source["digest"] != metadata["digest"]
                        or source["asset"] != asset_id
                    ):
                        raise ProtocolError("video_follow_source_changed")
            job = await db.fetchone(
                "SELECT state FROM relay_video_jobs WHERE id=?", (job_id,)
            )
            allowed = {"preparing"} if preparing else {"uploading", "sending"}
            if job is None or job["state"] not in allowed:
                raise ProtocolError("video_operation_expired")

    try:
        if operation == "cancel_video":
            return {"id": job_id, "state": "cancelled"}
        if preparing and follow is not None:
            source_metadata = json.loads(follow["metadata"])
            metadata = {
                key: source_metadata[key]
                for key in (
                    "vod_id",
                    "source",
                    "title",
                    "category_id",
                    "category_name",
                    "digest",
                    "size",
                    "width",
                    "height",
                    "duration",
                    "thumbnail_digest",
                )
            }
            if (
                metadata["digest"] != follow["digest"]
                or not re.fullmatch(r"[0-9a-f]{64}", metadata["digest"])
                or not is_native_asset_id(follow["asset"])
                or metadata["vod_id"] != follow["source_id"]
                or metadata["source"] != config.cms_url
                or any(
                    type(metadata[key]) is not int
                    for key in ("size", "width", "height", "duration")
                )
                or not 2 <= metadata["width"] <= 2048
                or not 2 <= metadata["height"] <= 2048
                or not 1 <= metadata["duration"] <= 1200
                or not 0
                < metadata["size"]
                <= min(config.max_video_mb * 1024**2, VIDEO_LIMIT)
            ):
                raise ProtocolError("video_follow_asset_invalid")
            metadata["follow_job_id"] = follow["job"]
            metadata["follow_group_id"] = config.follow_group_id
            asset_id = follow["asset"]
            await before_send(refresh=True)
            async with AsyncDatabase(
                lambda: database(path), write=True, guard=guard
            ) as db:
                changed = await db.execute(
                    "UPDATE relay_video_jobs SET state='prepared',metadata=?,asset=?,"
                    "expires=?,updated=? WHERE id=? AND state='preparing'",
                    (
                        json.dumps(metadata),
                        asset_id,
                        time.time() + 300,
                        time.time(),
                        job_id,
                    ),
                )
                if changed != 1:
                    raise ProtocolError("video_preview_expired")
            keep = True
            return {
                "id": job_id,
                "token": token,
                "state": "prepared",
                "expires_at": time.time() + 300,
                "metadata": metadata,
            }
        if preparing:
            root = directory.parent
            await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True, mode=0o700)
            free = (await asyncio.to_thread(shutil.disk_usage, root)).free
            effective_limit_mb = body.get("max_video_mb", config.max_video_mb)
            max_bytes = min(effective_limit_mb * 1024 * 1024, VIDEO_LIMIT)
            if free < config.min_free_gb * 1024**3 + max_bytes * 3:
                raise ProtocolError("video_disk_space")
            await asyncio.to_thread(directory.mkdir, mode=0o700)
            async with asyncio.timeout(420):
                requested_category = body.get("category_id", config.category_id)
                video = None

                async def advance_cursor(page, offset):
                    """Save bounded scan progress under current configuration authority.

                    Args:
                        page: Next list page.
                        offset: Next candidate offset in that page.
                    """
                    await before_send(refresh=True)
                    async with AsyncDatabase(
                        lambda: database(path), write=True, guard=guard
                    ) as db:
                        current = await db.fetchone(
                            "SELECT version FROM relay_configs WHERE account=? AND group_id=?",
                            (account, group),
                        )
                        if current is None or current["version"] != body["version"]:
                            raise ProtocolError("video_configuration_changed")
                        await db.execute(
                            "INSERT INTO relay_scan_cursors VALUES(?,?,?,?,?,?,?,?) "
                            "ON CONFLICT(account,group_id) DO UPDATE SET "
                            "source=excluded.source,version=excluded.version,"
                            "category_id=excluded.category_id,page=excluded.page,"
                            "offset=excluded.offset,updated=excluded.updated",
                            (
                                account,
                                group,
                                config.cms_url,
                                body["version"],
                                config.category_id,
                                page,
                                offset,
                                time.time(),
                            ),
                        )

                async def validate_candidate(data):
                    """Validate local bytes within the current preview authority.

                    Args:
                        data: Bounded downloaded media bytes.
                    """
                    nonlocal video
                    await before_send(refresh=True)
                    video = await prepare_video(data, directory, max_bytes=max_bytes)
                    await before_send(refresh=True)

                async def reject_candidate(source_id, reason):
                    """Cool down one failed source without retaining media.

                    Args:
                        source_id: Numeric CMS record identifier.
                        reason: Sanitized candidate-level error code.
                    """
                    await before_send(refresh=True)
                    if not use_candidate_cooldown:
                        return
                    retry_at = time.time() + (
                        3600
                        if reason
                        in {
                            "video_duration_limit",
                            "video_format_unsupported",
                            "video_size_limit",
                        }
                        else 900
                    )
                    async with AsyncDatabase(
                        lambda: database(path), write=True, guard=guard
                    ) as db:
                        await db.execute(
                            "INSERT INTO relay_candidate_failures "
                            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT "
                            "(account,group_id,source,version,source_id) DO UPDATE SET "
                            "error=excluded.error,retry_at=excluded.retry_at,"
                            "updated=excluded.updated",
                            (
                                account,
                                group,
                                config.cms_url,
                                body["version"],
                                source_id,
                                reason,
                                retry_at,
                                time.time(),
                            ),
                        )
                        await db.execute(
                            "DELETE FROM relay_candidate_failures WHERE rowid IN "
                            "(SELECT rowid FROM relay_candidate_failures "
                            "ORDER BY updated DESC,rowid DESC LIMIT -1 OFFSET 1024)"
                        )

                item, data = (
                    await neutral_video(directory)
                    if testing
                    else await fetch_cms_video(
                        config,
                        skipped,
                        guard,
                        path,
                        category=requested_category,
                        max_video_mb=body.get("max_video_mb"),
                        vod_id=body.get("vod_id"),
                        validate_candidate=validate_candidate,
                        reject_candidate=reject_candidate,
                        page=scan_page,
                        offset=scan_offset,
                        advance_cursor=advance_cursor if use_scan_cursor else None,
                    )
                )
                if video is None:
                    video = await prepare_video(data, directory, max_bytes=max_bytes)
            metadata = {
                "vod_id": item.vod_id,
                "source": TEST_SOURCE if testing else config.cms_url,
                "title": item.title,
                "category_id": getattr(item, "category_id", 0),
                "category_name": getattr(item, "category_name", ""),
                "digest": video.digest,
                "size": len(video.data),
                "width": video.width,
                "height": video.height,
                "duration": video.duration,
                "thumbnail_digest": hashlib.sha256(video.thumbnail).hexdigest(),
            }
            await before_send(refresh=True)
            async with AsyncDatabase(
                lambda: database(path), write=True, guard=guard
            ) as db:
                changed = await db.execute(
                    "UPDATE relay_video_jobs SET state='prepared',metadata=?,"
                    "expires=?,updated=? WHERE id=? AND state='preparing'",
                    (json.dumps(metadata), time.time() + 300, time.time(), job_id),
                )
                if changed != 1:
                    raise ProtocolError("video_preview_expired")
            keep = True
            return {
                "id": job_id,
                "token": token,
                "state": "prepared",
                "expires_at": time.time() + 300,
                "metadata": metadata,
            }
        if config.follow_group_id:
            video = VideoAsset(
                account,
                asset_id,
                metadata["digest"],
                metadata["size"],
                metadata["width"],
                metadata["height"],
                metadata["duration"],
            )
        elif (
            directory.is_symlink()
            or any(
                (directory / name).is_symlink() for name in ("video.mp4", "cover.jpg")
            )
            or (await asyncio.to_thread((directory / "video.mp4").stat)).st_size
            != metadata["size"]
            or not 0 < metadata["size"] <= VIDEO_LIMIT
            or not 0
            < (await asyncio.to_thread((directory / "cover.jpg").stat)).st_size
            <= 1024 * 1024
        ):
            raise ProtocolError("video_preview_changed")
        if not config.follow_group_id:
            data = await asyncio.to_thread((directory / "video.mp4").read_bytes)
            thumbnail = await asyncio.to_thread((directory / "cover.jpg").read_bytes)
            if (
                await asyncio.to_thread(lambda: hashlib.sha256(data).hexdigest())
                != metadata["digest"]
                or hashlib.sha256(thumbnail).hexdigest() != metadata["thumbnail_digest"]
            ):
                raise ProtocolError("video_preview_changed")
            video = PreparedVideo(
                data,
                thumbnail,
                metadata["width"],
                metadata["height"],
                metadata["duration"],
                metadata["digest"],
            )
        await before_send(refresh=True)
        async with AsyncDatabase(lambda: database(path), write=True, guard=guard) as db:
            duplicate = await db.fetchone(
                "SELECT 1 FROM relay_publications WHERE account=? AND group_id=? "
                "AND ((source=? AND source_id=?) OR digest=?)",
                (account, group, metadata["source"], metadata["vod_id"], video.digest),
            )
            if duplicate is None and not config.follow_group_id:
                duplicate = await db.fetchone(
                    "SELECT 1 FROM relay_follow_starts f "
                    "JOIN relay_configs c ON c.account=f.account AND c.group_id=f.group_id "
                    "JOIN relay_publications p ON p.account=f.account AND p.group_id=f.group_id "
                    "WHERE f.account=? AND f.source_group_id=? AND p.digest=? "
                    "AND json_extract(c.config,'$.enabled')=1 "
                    "AND json_extract(c.config,'$.follow_group_id')=? LIMIT 1",
                    (account, group, video.digest, group),
                )
            if duplicate:
                await db.execute(
                    "INSERT OR IGNORE INTO relay_publications VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        account,
                        group,
                        metadata["source"],
                        metadata["vod_id"],
                        video.digest,
                        job_id,
                        "duplicate",
                        time.time(),
                        time.time(),
                    ),
                )
            else:
                await db.execute(
                    "INSERT INTO relay_publications VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        account,
                        group,
                        metadata["source"],
                        metadata["vod_id"],
                        video.digest,
                        job_id,
                        "pending",
                        time.time(),
                        time.time(),
                    ),
                )
        if duplicate:
            raise ProtocolError("video_already_published")
        try:
            asset = (
                video
                if config.follow_group_id
                else await upload_video(adapter.business, video, authorize=guard)
            )
            submitted, uploaded, asset_id = True, True, asset.asset_id
        except BaseException as cause:
            submitted = getattr(cause, "submitted", False)
            asset_id = getattr(cause, "asset_id", "")
            raise
        await before_send(refresh=True)
        async with AsyncDatabase(lambda: database(path), write=True, guard=guard) as db:
            changed = await db.execute(
                "UPDATE relay_video_jobs SET state='sending',asset=?,updated=? "
                "WHERE id=? AND state='uploading'",
                (asset_id, time.time(), job_id),
            )
            if changed != 1:
                raise ProtocolError("video_operation_expired")
        send_started = True
        state = await send_video(
            adapter,
            group,
            asset,
            "video-relay/" + job_id,
            authorize=guard,
            before_send=before_send,
        )
        return {"id": job_id, "state": state}
    except BaseException as cause:
        state = (
            "not_sent"
            if uploaded and not send_started
            else "unknown"
            if submitted
            else "failed"
        )
        # Never store an HTTP exception, media URL, signed query or token.
        if isinstance(cause, TimeoutError):
            error = "video_prepare_timeout"
        elif isinstance(cause, ProtocolError):
            error = str(cause)
        elif isinstance(cause, OpenError):
            error = (
                str(cause)
                if str(cause)
                in {
                    "business_native_permission",
                    "business_rules_inactive",
                    "business_owner_revoked",
                    "proactive_not_authorized",
                }
                else "video_authority_rejected"
            )
        elif isinstance(cause, (OSError, KeyError, IndexError, ValueError)):
            error = "video_media_invalid"
        else:
            error = "video_operation_failed"
        if not re.fullmatch(r"[a-z_]{1,64}", error):
            error = "video_operation_failed"
        if isinstance(cause, asyncio.CancelledError):
            raise
        if not isinstance(cause, Exception):
            raise
        if submitted:
            return {"id": job_id, "state": state, "error": error}
        raise ApiError(
            {
                "video_disk_space": "磁盘剩余空间不足，请清理缓存或调整空间预留。",
                "video_follow_source_changed": "同步来源或轮次已变化，本次未发送；请核对两群配置和来源回执。",
                "video_follow_asset_invalid": "来源视频素材证据无效或超过本群限制，本次未发送。",
                "video_cms_unavailable": "当前CMS列表没有未发送且格式受支持的视频。",
                "video_already_published": "该来源编号或视频摘要已有发送记录，已阻止重复发送。",
                "video_player_unsupported": "此CMS播放器暂不支持，请使用直接MP4媒体地址。",
                "video_tools_missing": "服务器未安装FFmpeg和ffprobe，请管理员安装后重试。",
                "video_format_unsupported": "该视频不是受支持的H.264/AAC短视频，未上传或发送。",
                "video_duration_limit": "该视频超过20分钟时长上限，未上传或发送；自动任务会继续选择其他候选。",
                "video_manifest_invalid": "当前候选视频的播放清单无法解析，未上传或发送；请查看失败记录或稍后重试。",
                "video_pagination_invalid": "CMS分页数据异常或未按请求返回对应页，本次未发送；请核对来源接口。",
                "video_size_limit": "该分类中的视频超过单条体积上限；请调高上限（最多112MiB）或换一个短视频分类。",
                "video_media_invalid": "该分类中的视频文件不完整或格式不受支持；请换一个分类或视频。",
                "video_source_limit": "播放清单超过安全大小限制，未下载或发送；请换一个分类或视频。",
                "video_prepare_timeout": "视频下载或整理超时，已停止；请换一个分类或稍后重试。",
                "video_permission_timeout": "刷新群管理权限超时，本次未发送；请稍后重试。",
                "business_native_permission": "机器人当前群管权限无法核实，本次未发送；请刷新群管理状态后重试。",
                "business_rules_inactive": "本群规则已变化、已暂停或未启用，本次未发送；请刷新核对。",
                "business_owner_revoked": "原规则管理员权限已变更，本次未发送；请由现任管理员重新保存规则。",
                "proactive_not_authorized": "本群尚未授权主动发送，本次未发送；请启用后重新预览视频。",
                "video_authority_rejected": "本群当前发送权限校验未通过，本次未发送；请刷新群管理状态后核对。",
                "video_upload_rejected_auth": "视频上传被拒绝：机器人上传凭证已失效，请刷新账号登录后重试。",
                "video_upload_rejected_forbidden": "视频上传被拒绝：当前账号没有素材上传权限。",
                "video_upload_rejected_size": "视频上传被拒绝：素材体积超过平台限制。",
                "video_upload_rejected_rate": "视频上传被平台限流，请稍后再试；本次未发送。",
                "video_upload_rejected_upstream": "视频上传服务暂时故障，本次未发送，请稍后再试。",
                "video_upload_rejected": "视频上传被平台拒绝，本次未发送。",
                "video_upload_unknown": "视频上传结果未知，可能已提交素材但未取得回执；请核对后再决定，不会自动重发。",
                "video_upload_duplicate": "平台已有同一素材，已阻止重复上传。",
                "tmzyz_gateway_config_insecure": "天马解析网关配置文件权限不安全：须为仅当前服务账号可读的600普通文件。",
                "tmzyz_gateway_config_invalid": "天马解析网关配置无效：请核对播放ID、TOKEN、运营商和客户端IP。",
                "tmzyz_gateway_insufficient": "天马解析网关点数不足，且公共播放器也无法解析该视频。",
                "tmzyz_gateway_carrier": "天马解析网关不支持当前运营商，请改为移动、电信或联通。",
                "tmzyz_gateway_rejected": "天马解析网关拒绝了该播放ID，请核对专属播放器配置。",
                "tmzyz_gateway_response_limit": "天马解析网关响应超过限制，未下载或发送。",
                "tmzyz_gateway_response_invalid": "天马解析网关返回内容无效，未下载或发送。",
            }.get(error, "视频处理失败，未发送；请刷新查看记录后再试。"),
            status_code=409,
            data={"code": error},
        ) from None
    finally:
        if not keep:
            try:
                if operation != "cancel_video":
                    async with AsyncDatabase(lambda: database(path), write=True) as db:
                        await db.execute(
                            "UPDATE relay_video_jobs SET state=?,asset=?,error=?,"
                            "token_digest='',updated=? WHERE id=?",
                            (state, asset_id, error, time.time(), job_id),
                        )
                        if submitted:
                            await db.execute(
                                "UPDATE relay_publications SET state=?,updated=? WHERE job=?",
                                (state, time.time(), job_id),
                            )
                        else:
                            await db.execute(
                                "DELETE FROM relay_publications WHERE job=? AND state='pending'",
                                (job_id,),
                            )
            finally:
                await cleanup_cache(path, job_id)
