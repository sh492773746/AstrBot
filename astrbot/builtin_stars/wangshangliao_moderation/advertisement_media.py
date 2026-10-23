"""Scope-bound scheduled images without public files or arbitrary media URLs."""

import asyncio
import base64
import hashlib
import io
import json
import secrets
import sqlite3
import struct
import threading
import time
import uuid
import warnings
from collections import Counter

import aiohttp
import lz4.block
import nacl.bindings
from PIL import Image, ImageOps

from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.database import (
    AsyncDatabase,
    account_guard,
)

from .native_guide_upload import (
    REVIEWED_MEDIA_ENDPOINTS,
    _read_bounded,
    is_native_asset_id,
    upload_verified_image,
)
from .open_store import OpenError

UPLOAD_LIMIT = 5 * 1024 * 1024


class AdvertisementMedia:
    """Keep immutable images, authorization scope and native upload receipts."""

    _inflight = Counter()
    _inflight_lock = threading.Lock()

    def __init__(self, rules):
        self.rules, self.store = rules, rules.store
        with self.store.connect(write=True) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS business_rule_images(
                    id TEXT PRIMARY KEY,account TEXT NOT NULL,group_id TEXT NOT NULL,
                    digest TEXT NOT NULL,width INTEGER NOT NULL,height INTEGER NOT NULL,
                    data BLOB NOT NULL,thumbnail TEXT NOT NULL,created REAL NOT NULL,
                    asset TEXT NOT NULL DEFAULT '',state TEXT NOT NULL DEFAULT '');
                CREATE INDEX IF NOT EXISTS business_rule_image_scope
                    ON business_rule_images(account,group_id);
                CREATE INDEX IF NOT EXISTS business_rule_image_digest
                    ON business_rule_images(account,group_id,digest);
                CREATE TABLE IF NOT EXISTS business_rule_image_sends(
                    operation TEXT NOT NULL,account TEXT NOT NULL,
                    group_id TEXT NOT NULL,image_id TEXT NOT NULL,
                    PRIMARY KEY(account,operation));
                CREATE INDEX IF NOT EXISTS business_rule_image_send_scope
                    ON business_rule_image_sends(account,group_id,image_id);
                CREATE TABLE IF NOT EXISTS business_ad_rotations(
                    account TEXT,group_id TEXT,rule_id TEXT,images TEXT NOT NULL,
                    position INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(account,group_id,rule_id));
            """)
            db.execute(
                "UPDATE business_rule_images SET state='unknown' WHERE state='uploading'"
            )

    def upload(self, account, group, encoded, *, guard):
        """Normalize one bounded bitmap before storing it under the exact group.

        Args:
            account: Authorized receiving account.
            group: Authorized business group.
            encoded: Strict base64 image bytes, not a data URL or filename.
            guard: Current native and tenant authorization check.

        Returns:
            Opaque image reference and a small JPEG preview.

        Raises:
            OpenError: If image data, quota or authorization is invalid.
        """
        guard()
        if type(encoded) is not str or not 0 < len(encoded) <= 4 * (
            (UPLOAD_LIMIT + 2) // 3
        ):
            raise OpenError("advertisement_image_invalid")
        try:
            raw = base64.b64decode(encoded, validate=True)
            if not 0 < len(raw) <= UPLOAD_LIMIT:
                raise ValueError
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(raw)) as original:
                    if (
                        original.format not in {"JPEG", "PNG", "WEBP"}
                        or original.width * original.height > 16_000_000
                        or getattr(original, "is_animated", False)
                    ):
                        raise ValueError
                    original.load()
                    bitmap = ImageOps.exif_transpose(original).convert("RGB")
            bitmap.thumbnail((2048, 2048))
            output = io.BytesIO()
            bitmap.save(output, "JPEG", quality=85, optimize=True)
            data = output.getvalue()
            if not 0 < len(data) <= 1024 * 1024:
                raise ValueError
            thumb = bitmap.copy()
            thumb.thumbnail((240, 240))
            output = io.BytesIO()
            thumb.save(output, "JPEG", quality=70)
            thumbnail = "data:image/jpeg;base64," + base64.b64encode(
                output.getvalue()
            ).decode("ascii")
        except Exception:
            raise OpenError("advertisement_image_invalid") from None
        digest = hashlib.sha256(data).hexdigest()
        with self.store.connect(write=True, guard=guard) as db:
            now = time.time()
            reusable = None
            for row in db.execute(
                "SELECT id,thumbnail,asset,state,width,height,data "
                "FROM business_rule_images WHERE account=? AND group_id=? AND digest=? "
                "AND ((state='ready' AND asset!='') OR "
                "(state='' AND asset='' AND created>=? AND created<=?)) "
                "ORDER BY created,id",
                (account, group, digest, now - 86400, now),
            ):
                if (
                    (row["state"] != "ready" or is_native_asset_id(row["asset"]))
                    and (row["width"], row["height"]) == bitmap.size
                    and row["data"] == data
                ):
                    reusable = {"id": row["id"], "thumbnail": row["thumbnail"]}
                    break
            referenced = set()
            saved = db.execute(
                "SELECT config FROM business_rules WHERE account=? AND group_id=?",
                (account, group),
            ).fetchall()
            drafts = db.execute(
                "SELECT config FROM business_rule_drafts "
                "WHERE account=? AND group_id=? AND expires>?",
                (account, group, now),
            ).fetchall()
            for row in [*saved, *drafts]:
                config = json.loads(row["config"])
                referenced.update(
                    item
                    for rule in [*config.get("keywords", []), *config["ads"]]
                    for item in rule.get("images", [])
                )
            referenced.update(
                row["image_id"]
                for row in db.execute(
                    "SELECT image_id FROM business_rule_image_sends "
                    "WHERE account=? AND group_id=?",
                    (account, group),
                )
            )
            if reusable:
                referenced.add(reusable["id"])
            stale = db.execute(
                "SELECT id FROM business_rule_images WHERE account=? AND group_id=? "
                "AND created<? AND state IN ('','ready')",
                (account, group, now - 86400),
            ).fetchall()
            # Admission and deletion share this lock so queued sends remain pinned
            # even before their durable reference transaction acquires the writer.
            with self._inflight_lock:
                referenced.update(
                    image_id
                    for (path, owner, target, image_id) in self._inflight
                    if (path, owner, target) == (self.store.path, account, group)
                )
                db.executemany(
                    "DELETE FROM business_rule_images WHERE id=?",
                    [(row["id"],) for row in stale if row["id"] not in referenced],
                )
            if reusable:
                return reusable
            count = db.execute(
                "SELECT count(*) FROM business_rule_images WHERE account=? AND group_id=?",
                (account, group),
            ).fetchone()[0]
            if count >= 120:
                raise OpenError("advertisement_image_quota")
            image_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO business_rule_images "
                "(id,account,group_id,digest,width,height,data,thumbnail,created) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    image_id,
                    account,
                    group,
                    digest,
                    bitmap.width,
                    bitmap.height,
                    data,
                    thumbnail,
                    now,
                ),
            )
        return {"id": image_id, "thumbnail": thumbnail}

    def validate(self, account, group, config, db):
        """Reject references that are missing or belong to another scope."""
        for rule in [*config.get("keywords", []), *config["ads"]]:
            for image_id in rule.get("images", []):
                if (
                    db.execute(
                        "SELECT 1 FROM business_rule_images WHERE id=? AND account=? AND group_id=?",
                        (image_id, account, group),
                    ).fetchone()
                    is None
                ):
                    raise OpenError("advertisement_image_scope")

    async def receive(self, adapter, group, metadata, *, guard):
        """Import one explicitly requested native attachment, never a caller URL.

        Args:
            adapter: Existing authenticated receiving adapter.
            group: Administrator-selected business group.
            metadata: Reviewed metadata from the authenticated private envelope.
            guard: Current native authority, selection and pending-request check.

        Returns:
            A normalized local image reference and thumbnail.

        Raises:
            OpenError: If the asset, download or current scope is invalid.
        """
        guard()
        if (
            type(metadata) is not dict
            or set(metadata) != {"asset", "width", "height", "size"}
            or not is_native_asset_id(metadata["asset"])
            or any(
                type(metadata[key]) is not int for key in ("width", "height", "size")
            )
            or not 0 < metadata["size"] <= UPLOAD_LIMIT
            or not 1 <= metadata["width"] <= 16384
            or not 1 <= metadata["height"] <= 16384
            or metadata["width"] * metadata["height"] > 16_000_000
        ):
            raise OpenError("advertisement_image_invalid")
        account, business = str(adapter.account), adapter.business
        deployment = business.deployment
        origin = deployment.origin.rstrip("/")
        endpoints = REVIEWED_MEDIA_ENDPOINTS.get(origin)
        if not endpoints:
            raise OpenError("advertisement_image_scope")
        try:
            async with aiohttp.ClientSession(
                cookie_jar=aiohttp.DummyCookieJar(),
                trust_env=False,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as http:
                guard()
                async with http.get(
                    endpoints[1] + metadata["asset"],
                    headers={},
                    allow_redirects=False,
                    ssl=True,
                ) as response:
                    if response.status != 200:
                        raise OpenError("advertisement_image_download")
                    data = await _read_bounded(response, UPLOAD_LIMIT)
            guard()
            if (
                str(adapter.account) != account
                or adapter.business is not business
                or business.deployment is not deployment
                or deployment.origin.rstrip("/") != origin
                or REVIEWED_MEDIA_ENDPOINTS.get(origin) != endpoints
                or len(data) != metadata["size"]
            ):
                raise OpenError("advertisement_image_scope")
            cancelled = threading.Event()

            def check():
                if cancelled.is_set():
                    raise OpenError("advertisement_image_request_changed")
                guard()

            def import_image():
                # Both header parsing and normalization belong to the worker.
                check()
                with Image.open(io.BytesIO(data)) as bitmap:
                    if bitmap.size != (metadata["width"], metadata["height"]):
                        raise OpenError("advertisement_image_invalid")
                return self.upload(
                    account,
                    group,
                    base64.b64encode(data).decode("ascii"),
                    guard=check,
                )

            try:
                result = await asyncio.to_thread(import_image)
                guard()
                return result
            except BaseException:
                cancelled.set()
                raise
        except (aiohttp.ClientError, TimeoutError):
            raise OpenError("advertisement_image_download") from None
        except OSError:
            raise OpenError("advertisement_image_invalid") from None

    def previews(self, account, group, config):
        """Return only referenced thumbnails from the authorized configuration."""
        ids = {
            image_id
            for rule in [*config.get("keywords", []), *config["ads"]]
            for image_id in rule.get("images", [])
        }
        if not ids:
            return {}
        with self.store.connect() as db:
            return {
                row["id"]: row["thumbnail"]
                for row in db.execute(
                    "SELECT id,thumbnail FROM business_rule_images "
                    f"WHERE account=? AND group_id=? AND id IN ({','.join('?' for _ in ids)})",
                    (account, group, *ids),
                )
            }

    def test_asset(self, receiver, window, message, outer):
        """Verify a scheduled bitmap for the existing evidence-only test witness.

        Args:
            receiver: Original sending adapter in the fixed three-account test.
            window: Live explicitly opened developer window.
            message: Authenticated decoded image, never a web request.
            outer: Authenticated native routing metadata.

        Returns:
            Immutable image metadata only when the reviewed plan and send intent
            exactly match; otherwise None. This grants no receive or send rights.
        """
        account, group = "43506527", "1459282"
        if (
            str(receiver.account) != account
            or window.receiver is not receiver
            or str(window.sender.account) != "23691273"
            or window.groups != [group]
            or window.scopes not in (["group_commands"], ["group_rules"])
            or not window.active()
            or not getattr(window, "_witness_id", "")
            or message.session != 2
            or message.format != 1
            or str(message.sender.id) != account
            or str(message.target.id) != group
            or not message.HasField("image")
            or message.HasField("content")
            or message.HasField("mentions")
            or message.image.is_sticker
        ):
            return None
        row = self.rules.read(account, group)
        try:
            self.rules.send_guard(receiver, row, posting=True)
        except Exception:
            return None
        with self.store.connect() as db:
            image = db.execute(
                "SELECT * FROM business_rule_images WHERE account=? AND group_id=? "
                "AND asset=? AND state='ready'",
                (account, group, message.image.asset.uri),
            ).fetchone()
            sends = db.execute(
                "SELECT operation,rule_id,kind FROM business_rule_sends WHERE account=? "
                "AND group_id=? AND kind IN ('advertisement','keyword') AND created>=? "
                "AND state IN ('dispatched','accepted','verified') ORDER BY created DESC LIMIT 20",
                (account, group, getattr(window, "_witness_since", 0) / 1000),
            ).fetchall()
        if not image or (
            message.image.width != image["width"]
            or message.image.height != image["height"]
            or message.image.size != len(image["data"])
            or hashlib.sha256(image["data"]).hexdigest() != image["digest"]
        ):
            return None
        for send in sends:
            if not any(
                ad["id"] == send["rule_id"]
                and ad["id"].startswith("WSL_TEST_ACTIVITY_")
                and ad["enabled"]
                and image["id"] in ad.get("images", [])
                for ad in row["config"][
                    "ads" if send["kind"] == "advertisement" else "keywords"
                ]
            ):
                continue
            client = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"{receiver.config['id']}/{send['operation']}/image",
                )
            )
            if message.client_id == outer.get(11) == client:
                return {
                    "asset": image["asset"],
                    "digest": image["digest"],
                    "width": image["width"],
                    "height": image["height"],
                    "size": len(image["data"]),
                }
        return None

    async def send(self, adapter, group, image_id, key, authorize, *, proactive=True):
        """Upload once and send one reviewed rule image with durable intent.

        Args:
            adapter: Existing authenticated native adapter.
            group: Exact business group from the saved rule.
            image_id: Immutable local image reference.
            key: Stable slot-specific operation key.
            authorize: Frozen rule, tenant and current send-authority guard.
            proactive: Require proactive grants for scheduled sends (the default).

        Returns:
            Native receipt status; ambiguous uploads or sends are not retried.

        Raises:
            OpenError: If the image or its current authorization is unavailable.
            wire.ProtocolError: If transport identity or a native receipt changes.
        """
        account = str(adapter.account)
        reference = (self.store.path, account, group, image_id)
        with self._inflight_lock:
            self._inflight[reference] += 1
        try:
            result = await self._send(
                adapter, group, image_id, key, authorize, proactive=proactive
            )
            if result in {"accepted", "verified", "rejected", "not_sent"}:
                async with AsyncDatabase(self._connect, write=True) as db:
                    await db.execute(
                        "DELETE FROM business_rule_image_sends "
                        "WHERE account=? AND group_id=? AND operation=? AND image_id=?",
                        (account, group, key, image_id),
                    )
            return result
        finally:
            with self._inflight_lock:
                self._inflight[reference] -= 1
                if not self._inflight[reference]:
                    del self._inflight[reference]

    def _connect(self):
        """Open a worker-owned connection for asynchronous image transactions.

        Returns:
            SQLite connection whose rows support the existing image field names.
        """
        db = sqlite3.connect(self.store.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    async def _send(self, adapter, group, image_id, key, authorize, *, proactive):
        """Perform native delivery while the caller pins the immutable reference.

        Args:
            adapter: Existing authenticated native adapter.
            group: Exact saved business group.
            image_id: Pinned immutable local reference.
            key: Stable native operation key.
            authorize: Frozen rule and current authority guard.
            proactive: Whether scheduled-send grants are required.

        Returns:
            Existing native intent outcome, without retrying unknown work.

        Raises:
            OpenError: If the image or authorization is unavailable.
            wire.ProtocolError: If transport identity or a receipt changes.
        """
        from .feature_image import ImageMessage

        account = str(adapter.account)
        if not all(
            type(value) is str
            and value.isascii()
            and value.isdecimal()
            and not value.startswith("0")
            and len(value) <= 10
            and 0 < int(value) < (1 << 30)
            for value in (account, group)
        ):
            raise OpenError("advertisement_image_scope")
        guard = account_guard(adapter, authorize)
        guard()
        async with AsyncDatabase(self._connect, guard=guard) as db:
            found = await db.fetchone(
                "SELECT * FROM business_rule_images WHERE id=? AND account=? AND group_id=?",
                (image_id, account, group),
            )
            image = dict(found) if found else None
        if not image or (
            await asyncio.to_thread(lambda: hashlib.sha256(image["data"]).hexdigest())
            != image["digest"]
        ):
            raise OpenError("advertisement_image_scope")
        if image["state"] in {"unknown", "uploading"}:
            raise OpenError("advertisement_image_upload_unknown")
        if not image["asset"]:
            async with AsyncDatabase(self._connect, write=True, guard=guard) as db:
                changed = await db.execute(
                    "UPDATE business_rule_images SET state='uploading' "
                    "WHERE id=? AND state='' AND asset=''",
                    (image_id,),
                )
                if changed != 1:
                    raise OpenError("advertisement_image_upload_unknown")
            try:
                receipt = await upload_verified_image(
                    adapter.business,
                    image["data"],
                    width=image["width"],
                    height=image["height"],
                    authorize=guard,
                )
                guard()
                if receipt.account != account or receipt.digest != image["digest"]:
                    raise OpenError("advertisement_image_scope")
                async with AsyncDatabase(self._connect, write=True, guard=guard) as db:
                    await db.execute(
                        "UPDATE business_rule_images SET asset=?,state='ready' WHERE id=?",
                        (receipt.asset_id, image_id),
                    )
                image["asset"] = receipt.asset_id
            except BaseException as exc:
                async with AsyncDatabase(self._connect, write=True) as db:
                    await db.execute(
                        "UPDATE business_rule_images SET state=? WHERE id=? AND state='uploading'",
                        (
                            "unknown" if getattr(exc, "submitted", True) else "",
                            image_id,
                        ),
                    )
                raise
        if not is_native_asset_id(image["asset"]):
            raise OpenError("advertisement_image_scope")
        nim, business, ledger = adapter.nim, adapter.business, adapter.ledger
        team = adapter.groups.get(group)

        def check():
            guard()
            if (
                not team
                or adapter.groups.get(group) != team
                or adapter.nim is not nim
                or adapter.business is not business
                or adapter.ledger is not ledger
                or (
                    proactive
                    and (
                        not adapter.config.get("proactive_send", {}).get(
                            "enabled", False
                        )
                        or group
                        not in adapter.config.get("proactive_send", {}).get(
                            "targets", []
                        )
                    )
                )
            ):
                raise wire.ProtocolError("advertisement_image_scope_changed")

        while True:
            check()
            previous = getattr(adapter, "_onboarding_sent_at", 0)
            await asyncio.sleep(max(0, 3 - (time.monotonic() - previous)))
            async with adapter.send_lock:
                check()
                if getattr(adapter, "_onboarding_sent_at", 0) != previous:
                    continue
                async with AsyncDatabase(self._connect, write=True, guard=guard) as db:
                    existing = await db.fetchone(
                        "SELECT group_id,image_id FROM business_rule_image_sends "
                        "WHERE account=? AND operation=?",
                        (account, key),
                    )
                    if existing and tuple(existing) != (group, image_id):
                        raise wire.ProtocolError("send_key_conflict")
                    await db.execute(
                        "INSERT OR IGNORE INTO business_rule_image_sends "
                        "(operation,account,group_id,image_id) VALUES(?,?,?,?)",
                        (key, account, group, image_id),
                    )
                nonce, state = await ledger.reserve(
                    key,
                    json.dumps(
                        [account, group, image_id, image["digest"], image["asset"]]
                    ),
                )
                if state != "new":
                    return "unknown" if state == "sending" else state
                client_id = str(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"{adapter.config['id']}/{key}")
                )
                try:

                    def encode():
                        message = ImageMessage(
                            sender={"id": int(account)},
                            target={"id": int(group)},
                            created_at=int(time.time() * 1000),
                            device=1,
                            session=2,
                            version=2,
                            format=1,
                            client_id=client_id,
                            image={
                                "asset": {"uri": image["asset"]},
                                "width": image["width"],
                                "height": image["height"],
                                "size": len(image["data"]),
                            },
                        )
                        aad = secrets.randbits(64) or 1
                        sealed = nacl.bindings.crypto_aead_chacha20poly1305_encrypt(
                            lz4.block.compress(
                                message.SerializeToString(), store_size=True
                            ),
                            struct.pack("<Q", aad),
                            struct.pack("<Q", int(nonce)),
                            business.deployment.key("message_key"),
                        )
                        envelope = wire.Envelope(
                            header=int(account) << 34 | int(group) << 4 | 2 << 2 | 1,
                            timestamp=int(time.time()),
                            nonce=int(nonce),
                            aad=aad,
                            body=sealed[:-16],
                            tag=sealed[-16:],
                        )
                        attachment = json.dumps(
                            {"b": wire.b64(envelope.SerializeToString())},
                            separators=(",", ":"),
                        )
                        return wire.properties(
                            [
                                (0, b"1"),
                                (1, str(team).encode()),
                                (8, b"100"),
                                (10, attachment.encode()),
                                (11, client_id.encode()),
                            ]
                        )

                    properties = await asyncio.to_thread(encode)
                    check()
                except BaseException:
                    await ledger.finish(key, "rejected")
                    async with AsyncDatabase(self._connect, write=True) as db:
                        await db.execute(
                            "DELETE FROM business_rule_image_sends "
                            "WHERE account=? AND group_id=? AND operation=? "
                            "AND image_id=?",
                            (account, group, key, image_id),
                        )
                    raise
                adapter._onboarding_sent_at = time.monotonic()
                try:
                    code, body = await nim.request(8, 2, properties)
                    if code != 200:
                        await ledger.finish(key, "rejected")
                        return "rejected"
                    receipt, end = wire.read_properties(body)
                    if (
                        end != len(body)
                        or not receipt.get(12)
                        or not receipt.get(7, b"").isdigit()
                        or receipt.get(11, client_id.encode()) != client_id.encode()
                    ):
                        raise wire.ProtocolError("receipt_shape")
                    await ledger.finish(key, "accepted", receipt[12].decode())
                    return "accepted"
                except BaseException:
                    await ledger.finish(key, "unknown")
                    raise
