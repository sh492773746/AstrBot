"""Group-scoped CMS configuration and durable publication records."""

import hashlib
import json
import re
import secrets
import shutil
import sqlite3
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from astrbot.core.platform.sources.wangshangliao.database import AsyncDatabase
from astrbot.dashboard.responses import ApiError

TMZYZ_FEATURED_SOURCE = "tmzyz_featured_short_video"
TMZYZ_CMS_URL = "https://tmzyz.com/api.php/provide/vod/"
TMZYZ_LIST_URL = TMZYZ_CMS_URL + "?ac=list"


class CmsItem(BaseModel):
    """One structurally valid MacCMS-compatible video record."""

    model_config = ConfigDict(extra="forbid", strict=True)
    vod_id: int = Field(gt=0, le=9223372036854775807)
    title: str = Field(min_length=1, max_length=256)
    category_id: int = Field(default=0, ge=0, le=2147483647)
    category_name: str = Field(default="", max_length=64)
    media_url: str = ""
    resource_id: str = ""


class CmsMediaRange(BaseModel):
    """One bounded byte range from a provider-generated media playlist."""

    model_config = ConfigDict(extra="forbid", strict=True)
    url: str = Field(min_length=1, max_length=4096)
    offset: int = Field(ge=0)
    length: int = Field(gt=0)


class CmsMediaPlan(BaseModel):
    """Ephemeral download plan; signed media URLs must never be persisted."""

    model_config = ConfigDict(extra="forbid", strict=True)
    manifest_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    ranges: list[CmsMediaRange] = Field(min_length=2, max_length=4096)
    total_bytes: int = Field(gt=0)


def _cms_text(value) -> str:
    return value.strip() if type(value) is str else ""


def public_url(value: str) -> bool:
    """Validate an HTTPS URL; DNS addresses are checked again before connection.

    Args:
        value: Configured CMS or server-returned media URL.

    Returns:
        Whether its URL shape excludes credentials, local names and unsafe ports.
    """
    import ipaddress

    if (
        type(value) is not str
        or not 0 < len(value) <= 4096
        or "\\" in value
        or any(ord(char) <= 32 for char in value)
    ):
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (
            parsed.scheme != "https"
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.fragment
            or host.lower() == "localhost"
            or host.endswith(".local")
        ):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return "." in host and not host.endswith(".")
    except ValueError:
        return False


def cms_url(base: str, *, operation="list", category=0, vod_id=None, page=1) -> str:
    """Build a credential-free MacCMS request with structured query parameters.

    Args:
        base: Normalized configured CMS endpoint.
        operation: List or detail.
        category: Optional positive category ID, zero for all.
        vod_id: Positive detail item ID.
        page: Positive list page, omitted from the first-page request.

    Returns:
        Request URL scoped to the configured endpoint.
    """
    parsed = urlsplit(base)
    query = {"ac": operation}
    if category:
        query["t"] = str(category)
    if vod_id is not None:
        query["ids"] = str(vod_id)
    if operation == "list" and page != 1:
        query["pg"] = str(page)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def cms_detail(item: object) -> CmsItem | None:
    """Select the first supported media entry without content keyword filtering.

    Args:
        item: One MacCMS-compatible detail record.

    Returns:
        Valid video metadata, or None for an unsupported record/player.
    """
    if (
        not isinstance(item, dict)
        or type(item.get("vod_id")) is not int
        or not 0 < item["vod_id"] < 1 << 63
    ):
        return None
    title = _cms_text(item.get("vod_name"))
    if not 0 < len(title) <= 256:
        return None
    category_id = item.get("type_id")
    category_id = category_id if type(category_id) is int and category_id >= 0 else 0
    category_name = _cms_text(item.get("type_name"))[:64]
    play = _cms_text(item.get("vod_play_url"))
    for entry in play.split("$$$")[:8]:
        for part in entry.split("#")[:100]:
            label, separator, value = part.partition("$")
            address = value if separator else label
            if public_url(address) and urlsplit(address).path.lower().endswith(".mp4"):
                return CmsItem(
                    vod_id=item["vod_id"],
                    title=title,
                    category_id=category_id,
                    category_name=category_name,
                    media_url=address,
                )
            if _cms_text(item.get("vod_play_from")) == "bkplayer" and re.fullmatch(
                r"[0-9a-f]{32}", address
            ):
                return CmsItem(
                    vod_id=item["vod_id"],
                    title=title,
                    category_id=category_id,
                    category_name=category_name,
                    resource_id=address,
                )
    return None


def cms_ids(payload: object, category=0) -> list[int]:
    """Return bounded CMS IDs, with only an optional operator-selected category.

    Args:
        payload: MacCMS JSON list.
        category: Zero for all, otherwise require this category ID.

    Returns:
        Unique positive IDs in provider order; titles and labels are unrestricted.
    """
    if (
        not isinstance(payload, dict)
        or type(payload.get("code")) is not int
        or payload["code"] != 1
    ):
        return []
    records = payload.get("list")
    if not isinstance(records, list) or len(records) > 100:
        return []
    result = []
    for item in records:
        if (
            isinstance(item, dict)
            and type(item.get("vod_id")) is int
            and 0 < item["vod_id"] < 1 << 63
            and (not category or str(item.get("type_id")) == str(category))
        ):
            result.append(item["vod_id"])
    return list(dict.fromkeys(result))


def cms_categories(payload: object) -> list[dict]:
    """Return a bounded CMS category catalog without accepting arbitrary text.

    Args:
        payload: MacCMS list JSON, optionally including a ``class`` catalog.

    Returns:
        Unique ``{"id", "name"}`` entries in provider order; at most 200.
    """
    if not isinstance(payload, dict):
        return []
    result, seen = [], set()
    records = payload.get("class")
    if not isinstance(records, list):
        records = payload.get("list")
    if not isinstance(records, list) or len(records) > 500:
        return []
    for item in records:
        if not isinstance(item, dict):
            continue
        category_id = item.get("type_id")
        name = _cms_text(item.get("type_name"))
        if (
            type(category_id) is not int
            or not 0 < category_id <= 2147483647
            or not 0 < len(name) <= 64
            or category_id in seen
        ):
            continue
        seen.add(category_id)
        result.append({"id": category_id, "name": name})
        if len(result) >= 200:
            break
    return result


def tmzyz_manifest_id(player_page: object) -> str | None:
    """Extract one fixed-host manifest identifier from the reviewed player page."""
    if type(player_page) is not str or not 0 < len(player_page.encode()) <= 65536:
        return None
    matches = set(re.findall(r"api\.php\?mpd=([0-9a-f]{32})", player_page))
    return next(iter(matches)) if len(matches) == 1 else None


TMZYZ_MEDIA_SUFFIXES = (
    ".cmecloud.cn",
    ".ctyunxs.cn",
    ".mini189.cn",
)


def _tmzyz_media_url(value: str) -> bool:
    """Accept only reviewed TMZYZ edge hosts returned by its playlists."""
    if not 0 < len(value) <= 4096 or any(ord(char) < 32 for char in value):
        return False
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        return (
            parsed.scheme == "https"
            and bool(host)
            and host.endswith(TMZYZ_MEDIA_SUFFIXES)
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 443)
            and bool(parsed.path)
            and not parsed.fragment
        )
    except ValueError:
        return False


def tmzyz_media_plan(
    playlist: object, manifest_id: str, *, max_bytes: int
) -> CmsMediaPlan | None:
    """Parse the provider's bounded fMP4 byte-range playlist without fetching it."""
    if (
        type(playlist) is not str
        or type(manifest_id) is not str
        or not re.fullmatch(r"[0-9a-f]{32}", manifest_id)
        or type(max_bytes) is not int
        or not 1 <= max_bytes <= 1024 * 1024 * 1024
        or not 0 < len(playlist.encode()) <= 1024 * 1024
    ):
        return None
    lines = [line.strip() for line in playlist.splitlines() if line.strip()]
    if not lines or lines[0] != "#EXTM3U" or lines[-1] != "#EXT-X-ENDLIST":
        return None
    map_rows = [line for line in lines if line.startswith("#EXT-X-MAP:")]
    if len(map_rows) != 1:
        return None
    mapped = re.fullmatch(
        r'#EXT-X-MAP:URI="([^"]+)",BYTERANGE="([1-9][0-9]{0,9})@([0-9]{1,10})"',
        map_rows[0],
    )
    if not mapped or not _tmzyz_media_url(mapped.group(1)):
        return None
    ranges = [
        CmsMediaRange(
            url=mapped.group(1),
            offset=int(mapped.group(3)),
            length=int(mapped.group(2)),
        )
    ]
    for index, line in enumerate(lines):
        if not line.startswith("#EXT-X-BYTERANGE:"):
            continue
        if (
            index == 0
            or index + 1 >= len(lines)
            or not lines[index - 1].startswith("#EXTINF:")
        ):
            return None
        match = re.fullmatch(r"#EXT-X-BYTERANGE:([1-9][0-9]{0,9})@([0-9]{1,10})", line)
        media_url = lines[index + 1]
        if not match or not _tmzyz_media_url(media_url):
            return None
        ranges.append(
            CmsMediaRange(
                url=media_url, offset=int(match.group(2)), length=int(match.group(1))
            )
        )
    if not 2 <= len(ranges) <= 4096:
        return None
    total = sum(item.length for item in ranges)
    if total > max_bytes:
        return None
    return CmsMediaPlan(manifest_id=manifest_id, ranges=ranges, total_bytes=total)


class RelayConfig(BaseModel):
    """Explicit CMS destination schedule, disabled until confirmed by an operator."""

    model_config = ConfigDict(extra="forbid", strict=True)
    cms_url: str = ""
    category_id: int = Field(default=0, ge=0, le=2147483647)
    enabled: bool = False
    interval_minutes: int = Field(default=20, ge=1, le=1440)
    max_video_mb: int = Field(default=32, ge=1, le=112)
    min_free_gb: int = Field(default=2, ge=1, le=20)
    follow_group_id: str = Field(default="", pattern=r"^(?:[1-9][0-9]{0,9})?$")

    @field_validator("cms_url")
    @classmethod
    def normalize_url(cls, value):
        """Normalize a public CMS endpoint without accepting credentials.

        Args:
            value: Empty or HTTPS CMS endpoint.

        Returns:
            Canonical URL with only an optional category query.

        Raises:
            ValueError: If the URL is unsafe or contains unsupported parameters.
        """
        value = value.strip()
        if not value:
            return value
        if not public_url(value):
            raise ValueError("invalid_cms_url")
        parsed = urlsplit(value)
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        if any(k not in {"ac", "pg", "t"} for k, _ in pairs):
            raise ValueError("unsupported_cms_query")
        return urlunsplit(("https", parsed.netloc.lower(), parsed.path or "/", "", ""))

    @model_validator(mode="after")
    def validate_scope(self):
        """Require a CMS endpoint before enabling scheduled publication.

        Returns:
            Validated configuration.

        Raises:
            ValueError: If scheduling has no source.
        """
        if self.enabled and not self.cms_url:
            raise ValueError("cms_source_required")
        return self


def saved_config(raw: str) -> RelayConfig:
    """Read legacy drafts without retaining retired Telegram configuration.

    Args:
        raw: Persisted JSON, never a new dashboard request.

    Returns:
        Migrated configuration; legacy drafts never opt into automatic sending.
    """
    value = json.loads(raw)
    if "cms_source" in value or "sources" in value:
        value = {
            "cms_url": TMZYZ_LIST_URL
            if value.get("cms_source") == TMZYZ_FEATURED_SOURCE
            else "",
            "category_id": 71
            if value.get("cms_source") == TMZYZ_FEATURED_SOURCE
            else 0,
            "max_video_mb": min(32, value.get("max_video_mb", 32)),
            "min_free_gb": value.get("min_free_gb", 2),
        }
    return RelayConfig.model_validate(value)


async def follow_source(db, account, group, config):
    """Validate a same-account, non-recursive publication source.

    Args:
        db: Existing asynchronous control database transaction.
        account: Current native account.
        group: Destination group.
        config: Frozen destination configuration.

    Returns:
        Current source configuration row, or None for independent selection.

    Raises:
        ApiError: If the source is disabled, recursive or incompatible.
    """
    if not config.follow_group_id:
        return None
    source = await db.fetchone(
        "SELECT * FROM relay_configs WHERE account=? AND group_id=?",
        (account, config.follow_group_id),
    )
    source_config = saved_config(source["config"]) if source else None
    dependents = await db.fetchone(
        "SELECT 1 FROM relay_configs WHERE account=? AND "
        "json_extract(config,'$.follow_group_id')=? LIMIT 1",
        (account, group),
    )
    if (
        group == config.follow_group_id
        or source_config is None
        or not source_config.enabled
        or source_config.follow_group_id
        or dependents
        or any(
            getattr(source_config, key) != getattr(config, key)
            for key in ("cms_url", "category_id", "interval_minutes")
        )
    ):
        raise ApiError(
            "同步来源须为同机器人已启用的独立群，CMS、分类和间隔须相同；不支持循环跟随。",
            status_code=409,
            data={"code": "video_follow_source_changed"},
        )
    return source


def database(path: Path):
    """Open a private, separate control database with additive schema creation.

    Args:
        path: Instance-owned control database path.

    Returns:
        Connection owned by the existing asynchronous database worker.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(path, timeout=0.25)
    try:
        path.chmod(0o600)
        db.row_factory = sqlite3.Row
        db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS relay_configs(
                account TEXT NOT NULL,group_id TEXT NOT NULL,
                version INTEGER NOT NULL,config TEXT NOT NULL,
                updated REAL NOT NULL,actor TEXT NOT NULL,
                PRIMARY KEY(account,group_id));
            CREATE TABLE IF NOT EXISTS relay_previews(
                digest TEXT PRIMARY KEY,account TEXT NOT NULL,group_id TEXT NOT NULL,
                actor TEXT NOT NULL,revision TEXT NOT NULL,version INTEGER NOT NULL,
                config TEXT NOT NULL,expires REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS relay_preview_expiry ON relay_previews(expires);
            CREATE TABLE IF NOT EXISTS relay_config_audit(
                id INTEGER PRIMARY KEY,account TEXT NOT NULL,group_id TEXT NOT NULL,
                actor TEXT NOT NULL,version INTEGER NOT NULL,config TEXT NOT NULL,
                created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS relay_video_jobs(
                id TEXT PRIMARY KEY,account TEXT NOT NULL,group_id TEXT NOT NULL,
                actor TEXT NOT NULL,revision TEXT NOT NULL,version INTEGER NOT NULL,
                token_digest TEXT NOT NULL,expires REAL NOT NULL,
                state TEXT NOT NULL,metadata TEXT NOT NULL DEFAULT '{}',
                asset TEXT NOT NULL DEFAULT '',error TEXT NOT NULL DEFAULT '',
                created REAL NOT NULL,updated REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS relay_video_scope
                ON relay_video_jobs(account,group_id,created);
            CREATE TABLE IF NOT EXISTS relay_publications(
                account TEXT NOT NULL,group_id TEXT NOT NULL,source TEXT NOT NULL,
                source_id INTEGER NOT NULL,digest TEXT NOT NULL,job TEXT NOT NULL,
                state TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,
                PRIMARY KEY(account,group_id,source,source_id));
            CREATE INDEX IF NOT EXISTS relay_publication_digest
                ON relay_publications(account,group_id,digest);
            CREATE TABLE IF NOT EXISTS relay_candidate_failures(
                account TEXT NOT NULL,group_id TEXT NOT NULL,source TEXT NOT NULL,
                version INTEGER NOT NULL,source_id INTEGER NOT NULL,
                error TEXT NOT NULL,retry_at REAL NOT NULL,updated REAL NOT NULL,
                PRIMARY KEY(account,group_id,source,version,source_id));
            CREATE INDEX IF NOT EXISTS relay_candidate_retry
                ON relay_candidate_failures(retry_at);
            CREATE TABLE IF NOT EXISTS relay_scan_cursors(
                account TEXT NOT NULL,group_id TEXT NOT NULL,source TEXT NOT NULL,
                version INTEGER NOT NULL,category_id INTEGER NOT NULL,
                page INTEGER NOT NULL CHECK(page BETWEEN 1 AND 1000000),
                offset INTEGER NOT NULL CHECK(offset BETWEEN 0 AND 100),
                updated REAL NOT NULL,PRIMARY KEY(account,group_id));
            CREATE TABLE IF NOT EXISTS relay_metadata(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS relay_schedules(
                account TEXT NOT NULL,group_id TEXT NOT NULL,version INTEGER NOT NULL,
                next_at REAL NOT NULL,state TEXT NOT NULL,error TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(account,group_id));
            CREATE TABLE IF NOT EXISTS relay_follow_starts(
                account TEXT NOT NULL,group_id TEXT NOT NULL,
                source_group_id TEXT NOT NULL,since REAL NOT NULL,
                PRIMARY KEY(account,group_id));
            CREATE INDEX IF NOT EXISTS relay_publication_rounds
                ON relay_publications(account,group_id,state,created);
        """)
        columns = {
            row["name"] for row in db.execute("PRAGMA table_info(relay_video_jobs)")
        }
        if "cleanup_state" not in columns:
            db.execute(
                "ALTER TABLE relay_video_jobs ADD COLUMN cleanup_state TEXT NOT NULL DEFAULT ''"
            )
        if not db.execute(
            "SELECT 1 FROM relay_metadata WHERE key='publication_migration'"
        ).fetchone():
            for row in db.execute(
                "SELECT * FROM relay_video_jobs WHERE state IN "
                "('accepted','unknown','uploading','sending')"
            ).fetchall():
                metadata = json.loads(row["metadata"])
                if type(metadata.get("vod_id")) is int and re.fullmatch(
                    r"[0-9a-f]{64}", metadata.get("digest", "")
                ):
                    db.execute(
                        "INSERT OR IGNORE INTO relay_publications VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            row["account"],
                            row["group_id"],
                            metadata.get("source", TMZYZ_CMS_URL),
                            metadata["vod_id"],
                            metadata["digest"],
                            row["id"],
                            "accepted" if row["state"] == "accepted" else "unknown",
                            row["created"],
                            row["updated"],
                        ),
                    )
            db.execute("INSERT INTO relay_metadata VALUES('publication_migration','1')")
            db.commit()
        return db
    except BaseException:
        db.close()
        raise


async def control(path, account, group, actor, revision, body, guard):
    """Read, preview or confirm CMS configuration without sending immediately.

    Args:
        path: Private per-instance control database path.
        account: Bound Wangshangliao account.
        group: Bound enabled destination group.
        actor: Authenticated dashboard principal.
        revision: Current bot configuration fingerprint.
        body: Validated operation envelope.
        guard: Existing live lifecycle and scope fence.

    Returns:
        Saved configuration or single-use configuration preview.

    Raises:
        ApiError: If the version, actor, scope or preview has changed.
    """
    operation = body.get("operation", "read")
    draft = (
        RelayConfig.model_validate(body["config"]) if operation == "preview" else None
    )
    now = time.time()
    async with AsyncDatabase(
        lambda: database(path), write=operation != "read", guard=guard
    ) as db:
        row = await db.fetchone(
            "SELECT * FROM relay_configs WHERE account=? AND group_id=?",
            (account, group),
        )
        version = row["version"] if row else 0
        if operation == "read":
            configuration = (
                saved_config(row["config"]).model_dump()
                if row
                else RelayConfig().model_dump()
            )
            latest = await db.fetchone(
                "SELECT id,state,metadata,error,cleanup_state,created,updated FROM relay_video_jobs "
                "WHERE account=? AND group_id=? ORDER BY created DESC LIMIT 1",
                (account, group),
            )
            schedule = await db.fetchone(
                "SELECT next_at,state,error FROM relay_schedules WHERE account=? AND group_id=?",
                (account, group),
            )
            return {
                "version": version,
                "revision": revision,
                "config": configuration,
                "updated_at": row["updated"] if row else None,
                "destination": {
                    "platform": "wangshangliao",
                    "account": account,
                    "group_id": group,
                },
                "runtime": {
                    "state": schedule["state"] if schedule else "stopped",
                    "next_at": schedule["next_at"] if schedule else None,
                    "error": schedule["error"] if schedule else "",
                    "video_sender": "single_video_ready"
                    if shutil.which("ffmpeg") and shutil.which("ffprobe")
                    else "tools_missing",
                    "can_preview_video": bool(
                        configuration["cms_url"]
                        and not configuration["follow_group_id"]
                        and shutil.which("ffmpeg")
                        and shutil.which("ffprobe")
                    ),
                    "tasks": [dict(latest, metadata=json.loads(latest["metadata"]))]
                    if latest
                    else [],
                },
            }
        if body["revision"] != revision or body["version"] != version:
            raise ApiError("配置版本已变化，请刷新后重新预览。", status_code=409)
        if operation == "preview":
            if draft.enabled:
                await follow_source(db, account, group, draft)
            await db.execute("DELETE FROM relay_previews WHERE expires<=?", (now,))
            count = await db.fetchone("SELECT COUNT(*) FROM relay_previews")
            if count[0] >= 128:
                raise ApiError("待确认配置过多，请稍后再试。", status_code=429)
            token = secrets.token_urlsafe(32)
            await db.execute(
                "INSERT INTO relay_previews VALUES(?,?,?,?,?,?,?,?)",
                (
                    hashlib.sha256(token.encode()).hexdigest(),
                    account,
                    group,
                    actor,
                    revision,
                    version,
                    draft.model_dump_json(),
                    now + 300,
                ),
            )
            return {
                "token": token,
                "config": draft.model_dump(),
                "version": version,
                "expires_at": now + 300,
            }
        preview = await db.fetchone(
            "SELECT * FROM relay_previews WHERE digest=? AND account=? AND group_id=? "
            "AND actor=? AND revision=? AND version=? AND expires>?",
            (
                hashlib.sha256(body["token"].encode()).hexdigest(),
                account,
                group,
                actor,
                revision,
                version,
                now,
            ),
        )
        if preview is None:
            raise ApiError(
                "预览已过期、已使用或不属于当前账号和群，请重新预览。", status_code=409
            )
        confirmed = saved_config(preview["config"])
        if confirmed.enabled:
            await follow_source(db, account, group, confirmed)
        previous = saved_config(row["config"]) if row else RelayConfig()
        if confirmed.follow_group_id and confirmed.enabled:
            if (
                not previous.enabled
                or previous.follow_group_id != confirmed.follow_group_id
            ):
                await db.execute(
                    "INSERT INTO relay_follow_starts VALUES(?,?,?,?) "
                    "ON CONFLICT(account,group_id) DO UPDATE SET "
                    "source_group_id=excluded.source_group_id,since=excluded.since",
                    (account, group, confirmed.follow_group_id, now),
                )
        else:
            await db.execute(
                "DELETE FROM relay_follow_starts WHERE account=? AND group_id=?",
                (account, group),
            )
        await db.execute(
            "INSERT INTO relay_configs VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(account,group_id) DO UPDATE SET version=excluded.version,"
            "config=excluded.config,updated=excluded.updated,actor=excluded.actor",
            (account, group, version + 1, confirmed.model_dump_json(), now, actor),
        )
        await db.execute(
            "INSERT INTO relay_config_audit(account,group_id,actor,version,config,created) VALUES(?,?,?,?,?,?)",
            (account, group, actor, version + 1, confirmed.model_dump_json(), now),
        )
        await db.execute(
            "INSERT INTO relay_schedules(account,group_id,version,next_at,state,error) "
            "VALUES(?,?,?,?,?,'') ON CONFLICT(account,group_id) DO UPDATE SET "
            "version=excluded.version,next_at=excluded.next_at,state=excluded.state,error=''",
            (
                account,
                group,
                version + 1,
                now
                if confirmed.follow_group_id
                else now + confirmed.interval_minutes * 60,
                "scheduled" if confirmed.enabled else "stopped",
            ),
        )
        await db.execute(
            "DELETE FROM relay_previews WHERE account=? AND group_id=?",
            (account, group),
        )
        return {"saved": True, "version": version + 1, "started": confirmed.enabled}
