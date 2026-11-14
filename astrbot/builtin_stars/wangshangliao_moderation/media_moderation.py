"""Bounded local image inspection, isolated from the incoming message pipeline."""

import asyncio
import copy
import hashlib
import json
import os
import signal
import sys
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from astrbot.core.platform.sources.wangshangliao.database import account_guard
from astrbot.core.platform.sources.wangshangliao.group_scope import group_revision
from astrbot.core.platform.sources.wangshangliao.policy import authorize_action
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from astrbot.core.star.component_runtime import current_execution

from .native_guide_upload import (
    REVIEWED_MEDIA_ENDPOINTS,
    _read_bounded,
    is_native_asset_id,
)

MAX_MEDIA_WORKERS = 2
MAX_PENDING_IMAGES = 64
MAX_ACCOUNT_PENDING_IMAGES = 32
MAX_GROUP_PENDING_IMAGES = 16


@dataclass(frozen=True)
class _MediaEvidence:
    """Keep local recognizer results unforgeable by JSON or model assessments."""

    runtime: object
    account: str
    instance: str
    group: str
    message: str
    payload: dict
    category: str


def verified_assessment(adapter, group, mid, payload, assessment):
    """Accept only a live local worker's exact image-envelope assessment.

    Args:
        adapter: Current scoped native adapter.
        group: Exact business group.
        mid: Original outer message identifier.
        payload: Original envelope, never OCR-produced command text.
        assessment: Internal candidate containing an opaque Python proof.

    Returns:
        Whether the proof and currently enabled rule match this exact event.
    """
    proof = assessment.get("_media_proof")
    category = assessment.get("category")
    field = {"image_qr": "image_qr_recall", "image_digits": "image_digits_recall"}.get(
        category
    )
    return bool(
        type(proof) is _MediaEvidence
        and proof.runtime.active
        and proof.account == str(adapter.account)
        and proof.instance == adapter.config["id"]
        and proof.group == group
        and proof.message == str(mid)
        and proof.payload == payload
        and proof.category == category
        and payload.get("kind") == "image"
        and field
        and adapter.config.get("moderation", {}).get(field) is True
    )


class MediaModeration:
    """Limit waiting images, subprocess CPU and the lifetime of every decision."""

    def __init__(self):
        self.active = True
        self.started_at = time.time()
        self.tasks = set()
        self.pending = OrderedDict()
        self.queued = 0
        self.running = set()
        self.slots = asyncio.Semaphore(MAX_MEDIA_WORKERS)
        self.cache = OrderedDict()
        self.downloads = {}
        self.seen = OrderedDict()
        self.counts = {
            "admitted": 0,
            "busy": 0,
            "matched": 0,
            "failed": 0,
            "skipped": 0,
            "clear": 0,
            "completed": 0,
            "unchecked": 0,
            "metadata_invalid": 0,
            "unsupported_format": 0,
            "unsupported_animation": 0,
            "dimensions_invalid": 0,
            "queue_full": 0,
            "expired": 0,
            "revoked": 0,
            "cancelled": 0,
            "recall_accepted": 0,
            "enforcement_rejected": 0,
            "enforcement_not_applied": 0,
            "enforcement_unknown": 0,
        }

    def enqueue(self, adapter, group, mid, payload, execution_check=None):
        """Queue bounded metadata without waiting for downloads or recognition.

        Args:
            adapter: Authenticated scoped adapter.
            group: Current business group.
            mid: Original native message ID.
            payload: Native image metadata and authenticated identity.
            execution_check: Optional upstream authorization fence.

        Returns:
            Whether an image was admitted or already seen. Rejected coverage is
            observable, never an assertion that an unchecked image is clear.
        """
        from .policy import is_fresh_message

        policy = copy.deepcopy(adapter.config.get("moderation", {}))
        metadata = payload.get("image")
        if (
            not self.active
            or group.startswith("private/")
            or payload.get("kind") != "image"
            or not any(
                policy.get(field) is True
                for field in ("image_qr_recall", "image_digits_recall")
            )
        ):
            return False
        reason = (
            "time_invalid"
            if not is_fresh_message(payload, time.time())
            else "metadata_invalid"
            if (
                not isinstance(metadata, dict)
                or set(metadata) != {"asset", "width", "height", "size"}
                or not is_native_asset_id(metadata["asset"])
                or any(
                    type(metadata[field]) is not int
                    for field in ("width", "height", "size")
                )
                or not 0 <= metadata["size"] <= 5 * 1024 * 1024
                or not 0 <= metadata["width"] <= 16384
                or not 0 <= metadata["height"] <= 16384
                or metadata["width"] * metadata["height"] > 16_000_000
            )
            else ""
        )
        if reason:
            self.counts["skipped"] += 1
            self.counts["unchecked"] += 1
            if reason == "metadata_invalid":
                self.counts["metadata_invalid"] += 1
            adapter.diagnostics.emit(
                "media_rules",
                "unchecked_"
                + (
                    payload.get("image_metadata_error", reason)
                    if reason == "metadata_invalid"
                    else reason
                ),
                str(mid),
                failed=True,
            )
            self._observe_queue(adapter)
            return False
        stamp = float(
            (payload.get("recall_route") or {}).get("time")
            or payload.get("created_at", 0)
        )
        if stamp > 100000000000:
            stamp /= 1000
        if stamp < self.started_at - 2 or time.time() - stamp > 30:
            expired = time.time() - stamp > 30
            self.counts["expired" if expired else "skipped"] += 1
            self.counts["unchecked"] += 1
            adapter.diagnostics.emit(
                "media_rules",
                "unchecked_expired" if expired else "unchecked_historical",
                str(mid),
                failed=True,
            )
            self._observe_queue(adapter)
            return False
        identity = (adapter.config["id"], str(adapter.account), group, str(mid))
        now = time.monotonic()
        while self.seen and next(iter(self.seen.values())) < now - 300:
            self.seen.popitem(last=False)
        if identity in self.seen:
            return True
        account = identity[:2]
        groups = self.pending.get(account, {})
        reason = (
            "queue_full"
            if self.queued >= MAX_PENDING_IMAGES
            else "account_full"
            if sum(len(items) for items in groups.values())
            >= MAX_ACCOUNT_PENDING_IMAGES
            else "group_full"
            if len(groups.get(group, ())) >= MAX_GROUP_PENDING_IMAGES
            else ""
        )
        if reason:
            self.counts["busy"] += 1
            self.counts["queue_full"] += 1
            self.counts["unchecked"] += 1
            adapter.diagnostics.emit(
                "media_rules",
                "unchecked_" + reason,
                str(mid),
                failed=True,
                aggregate=True,
            )
            self._observe_queue(adapter)
            return False
        envelope = copy.deepcopy(payload)
        revision = group_revision(adapter.config, group)
        business = adapter.business
        deployment = business.deployment
        origin = deployment.origin.rstrip("/")
        endpoints = REVIEWED_MEDIA_ENDPOINTS.get(origin)
        if not endpoints:
            self.counts["skipped"] += 1
            self.counts["unchecked"] += 1
            adapter.diagnostics.emit(
                "media_rules", "unchecked_origin_unreviewed", str(mid), failed=True
            )
            self._observe_queue(adapter)
            return False
        # Waiting consumes the original image's lifetime, never a new 30s lease.
        deadline = now + max(0, min(30, stamp + 30 - time.time()))

        def authorize():
            if execution_check:
                execution_check()
            authorize_action(adapter.config, group, "recall")
            if time.monotonic() > deadline or time.time() - stamp > 30:
                raise ProtocolError("media_image_expired")
            if (
                not self.active
                or adapter.config.get("moderation", {}) != policy
                or not policy.get("automation_enabled")
                or group_revision(adapter.config, group) != revision
                or adapter.business is not business
                or business.deployment is not deployment
                or deployment.origin.rstrip("/") != origin
                or REVIEWED_MEDIA_ENDPOINTS.get(origin) != endpoints
            ):
                raise ProtocolError("content_policy_changed")

        guard = account_guard(adapter, authorize)
        self.pending.setdefault(account, OrderedDict()).setdefault(
            group, deque()
        ).append(
            (
                adapter,
                group,
                str(mid),
                envelope,
                guard,
                endpoints,
                now,
                current_execution.get(),
            )
        )
        self.queued += 1
        self.counts["admitted"] += 1
        self.seen[identity] = now
        while len(self.seen) > 2048:
            self.seen.popitem(last=False)
        if len(self.tasks) < MAX_MEDIA_WORKERS:
            task = asyncio.create_task(self._consume(), name="wsl-media-worker")
            self.tasks.add(task)
        self._observe_queue(adapter)
        return True

    def _observe_queue(self, adapter):
        """Publish bounded runtime-wide capacity and unchecked outcome counters.

        Args:
            adapter: Adapter receiving the latest queue observation.
        """
        getattr(adapter.diagnostics, "gauges", {}).update(
            media_pending=self.queued,
            media_workers=len(self.running),
            media_queue_capacity=MAX_PENDING_IMAGES,
            media_queue_full=self.counts["queue_full"],
            media_unchecked=self.counts["unchecked"],
            media_metadata_invalid=self.counts["metadata_invalid"],
            media_unsupported_format=self.counts["unsupported_format"],
            media_unsupported_animation=self.counts["unsupported_animation"],
            media_dimensions_invalid=self.counts["dimensions_invalid"],
            media_expired=self.counts["expired"],
            media_revoked=self.counts["revoked"],
            media_cancelled=self.counts["cancelled"],
            media_completed=self.counts["completed"],
            media_recall_accepted=self.counts["recall_accepted"],
            media_enforcement_unknown=self.counts["enforcement_unknown"],
        )

    async def _consume(self):
        """Rotate accounts and their groups while retaining only active tasks."""
        task = asyncio.current_task()
        try:
            while self.active and self.pending:
                account, groups = next(iter(self.pending.items()))
                group, items = next(iter(groups.items()))
                adapter, group, mid, payload, guard, endpoints, queued, execution = (
                    items.popleft()
                )
                self.queued -= 1
                if items:
                    groups.move_to_end(group)
                else:
                    del groups[group]
                if groups:
                    self.pending.move_to_end(account)
                else:
                    del self.pending[account]
                self.running.add(task)
                # A reused worker must not inherit the first image's authority.
                token = current_execution.set(execution)
                try:
                    adapter.diagnostics.observe(
                        "media_queue", time.monotonic() - queued
                    )
                    self._observe_queue(adapter)
                    await self.inspect(
                        adapter, group, mid, payload, guard, endpoints, queued
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.counts["failed"] += 1
                    self.counts["unchecked"] += 1
                    adapter.diagnostics.emit(
                        "media_rules",
                        "unchecked_worker_failed",
                        mid,
                        failed=True,
                        aggregate=True,
                        error="operation_failed",
                    )
                finally:
                    current_execution.reset(token)
                    self.running.discard(task)
                    self.counts["completed"] += 1
                    self._observe_queue(adapter)
        finally:
            self.tasks.discard(task)

    async def _download(self, key, endpoints, asset):
        """Share only in-flight scoped downloads, never retain original bitmaps.

        Args:
            key: Exact instance/account/group/asset/dimensions and origin binding.
            endpoints: Reviewed native endpoint pair.
            asset: Transport-verified native asset identifier.

        Returns:
            Bounded bitmap bytes, released after the current inspections finish.
        """
        entry = self.downloads.get(key)
        if entry is None:

            async def fetch():
                async with aiohttp.ClientSession(
                    cookie_jar=aiohttp.DummyCookieJar(),
                    trust_env=False,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as http:
                    async with http.get(
                        endpoints[1] + asset, allow_redirects=False, ssl=True
                    ) as response:
                        if response.status != 200:
                            raise ProtocolError("media_download_failed")
                        return await _read_bounded(response, 5 * 1024 * 1024)

            entry = [asyncio.create_task(fetch()), 0]
            self.downloads[key] = entry
        entry[1] += 1
        try:
            return await asyncio.shield(entry[0])
        finally:
            entry[1] -= 1
            if not entry[1]:
                if self.downloads.get(key) is entry:
                    self.downloads.pop(key)
                if not entry[0].done():
                    entry[0].cancel()
                await asyncio.gather(entry[0], return_exceptions=True)

    async def inspect(
        self, adapter, group, mid, payload, guard, endpoints, queued_at=None
    ):
        """Download from one reviewed origin and inspect in a killable process.

        Args:
            adapter: Authenticated scoped adapter.
            group: Frozen group identifier.
            mid: Original outer message ID to recall, not a nested sender.
            payload: Frozen authenticated image envelope.
            guard: Revocable account, group and rule authorization.
            endpoints: Reviewed upload/download endpoint pair.
            queued_at: Original monotonic admission time for end-to-end timing.
        """
        process = None
        phase = "authorization"
        started = time.monotonic() if queued_at is None else queued_at
        try:
            async with self.slots:
                guard()
                metadata = payload["image"]
                phase = "download"
                download_started = time.monotonic()
                try:
                    data = await self._download(
                        (
                            adapter.config["id"],
                            str(adapter.account),
                            group,
                            endpoints[1],
                            metadata["asset"],
                            metadata["width"],
                            metadata["height"],
                        ),
                        endpoints,
                        metadata["asset"],
                    )
                finally:
                    adapter.diagnostics.observe(
                        "media_download", time.monotonic() - download_started
                    )
                phase = "authorization"
                guard()
                # Native size may be a placeholder or describe the original
                # upload; the reviewed CDN recompresses images. Bound the actual
                # download above and validate decoded dimensions in the worker.
                key = (
                    adapter.config["id"],
                    str(adapter.account),
                    group,
                    metadata["width"],
                    metadata["height"],
                    adapter.config["moderation"].get("image_qr_recall") is True,
                    adapter.config["moderation"].get("image_digits_recall") is True,
                    hashlib.sha256(data).hexdigest(),
                )
                cached = self.cache.get(key)
                if cached and cached[0] > time.monotonic():
                    result = cached[1]
                    self.cache.move_to_end(key)
                else:
                    phase = "recognizer"
                    recognizer_started = time.monotonic()
                    process = await asyncio.create_subprocess_exec(
                        sys.executable,
                        str(Path(__file__).with_name("media_worker.py")),
                        str(metadata["width"]),
                        str(metadata["height"]),
                        str(int(key[5])),
                        str(int(key[6])),
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                        start_new_session=os.name != "nt",
                        env={
                            **os.environ,
                            "OMP_THREAD_LIMIT": "1",
                            "OPENBLAS_NUM_THREADS": "1",
                        },
                    )
                    try:
                        output, _ = await asyncio.wait_for(
                            process.communicate(data), 10
                        )
                    finally:
                        adapter.diagnostics.observe(
                            "media_recognizer", time.monotonic() - recognizer_started
                        )
                    if len(output) > 1024:
                        raise ProtocolError("media_recognizer_failed")
                    try:
                        result = json.loads(output)
                    except (ValueError, UnicodeError):
                        raise ProtocolError("media_recognizer_failed") from None
                    if (
                        process.returncode != 0
                        and isinstance(result, dict)
                        and set(result) == {"error"}
                        and isinstance(result["error"], str)
                    ):
                        reason = result["error"]
                        if reason in {
                            "media_image_unsupported_format",
                            "media_image_unsupported_animation",
                        }:
                            self.counts["skipped"] += 1
                            self.counts["unchecked"] += 1
                            self.counts[
                                "unsupported_format"
                                if reason == "media_image_unsupported_format"
                                else "unsupported_animation"
                            ] += 1
                            adapter.diagnostics.emit("media_rules", reason, mid)
                            return
                        if reason in {
                            "media_image_invalid",
                            "media_image_limit",
                            "media_ocr_limit",
                            "media_ocr_timeout",
                            "media_ocr_failed",
                            "media_recognizer_unavailable",
                            "media_recognizer_failed",
                        }:
                            raise ProtocolError(reason)
                    if process.returncode != 0:
                        raise ProtocolError("media_recognizer_failed")
                    if (
                        not isinstance(result, dict)
                        or set(result) != {"qr", "digits"}
                        or any(type(value) is not bool for value in result.values())
                    ):
                        raise ProtocolError("media_recognizer_failed")
                    self.cache[key] = (time.monotonic() + 600, result)
                    while len(self.cache) > 128:
                        self.cache.popitem(last=False)
                phase = "authorization"
                guard()
                policy = adapter.config["moderation"]
                category = (
                    "image_qr"
                    if policy.get("image_qr_recall") and result["qr"]
                    else "image_digits"
                    if policy.get("image_digits_recall") and result["digits"]
                    else ""
                )
                if category:
                    from .content_rules import enforce
                    from .review_store import ReviewStore

                    self.counts["matched"] += 1
                    proof = _MediaEvidence(
                        self,
                        str(adapter.account),
                        adapter.config["id"],
                        group,
                        mid,
                        payload,
                        category,
                    )
                    phase = "enforcement"
                    consumed = await enforce(
                        adapter,
                        group,
                        mid,
                        payload,
                        assessment={
                            "decision": "violation",
                            "category": category,
                            "message_id": mid,
                            "_media_proof": proof,
                        },
                        execution_check=guard,
                    )
                    # Consuming a message is not evidence of an accepted recall.
                    store = getattr(adapter, "_review_store", None)
                    outcome = (
                        await asyncio.to_thread(
                            store.content_outcome, proof.account, group, mid
                        )
                        if isinstance(store, ReviewStore)
                        else "not_applied"
                        if consumed is False
                        else "outcome_unconfirmed"
                    )
                    if outcome == "recalled":
                        self.counts["recall_accepted"] += 1
                    elif outcome == "execution_rejected":
                        self.counts["enforcement_rejected"] += 1
                    elif outcome in {
                        "identity_unverified",
                        "administrator_exempt",
                        "historical_or_unknown_time",
                        "not_applied",
                    }:
                        self.counts["enforcement_not_applied"] += 1
                    else:
                        outcome = (
                            outcome
                            if outcome in {"unknown", "preflight_failed"}
                            else "outcome_unconfirmed"
                        )
                        self.counts["enforcement_unknown"] += 1
                    adapter.diagnostics.emit(
                        "media_enforcement",
                        outcome,
                        mid,
                        failed=outcome
                        in {
                            "unknown",
                            "outcome_unconfirmed",
                            "preflight_failed",
                            "execution_rejected",
                        },
                        aggregate=True,
                    )
                else:
                    self.counts["clear"] += 1
                adapter.diagnostics.emit("media_rules", category or "clear", mid)
        except asyncio.CancelledError:
            self.counts["cancelled"] += 1
            if phase == "enforcement":
                self.counts["enforcement_unknown"] += 1
                outcome = "enforcement_cancelled_or_unknown"
            else:
                self.counts["unchecked"] += 1
                outcome = "unchecked_cancelled"
            adapter.diagnostics.emit(
                "media_rules", outcome, mid, failed=True, aggregate=True
            )
            raise
        except Exception as exc:
            self.counts["failed"] += 1
            reason = (
                "media_recognizer_timeout"
                if phase == "recognizer" and isinstance(exc, TimeoutError)
                else str(exc)
                if isinstance(exc, ProtocolError)
                else "operation_failed"
            )
            if phase == "enforcement":
                self.counts["enforcement_unknown"] += 1
                outcome = "enforcement_failed_or_unknown"
            else:
                self.counts["unchecked"] += 1
                if reason == "media_image_invalid":
                    self.counts["dimensions_invalid"] += 1
                if reason == "media_image_expired":
                    self.counts["expired"] += 1
                    outcome = "unchecked_expired"
                elif phase == "authorization":
                    self.counts["revoked"] += 1
                    outcome = "unchecked_revoked"
                else:
                    outcome = "unchecked_" + phase
            adapter.diagnostics.emit(
                "media_rules",
                outcome,
                mid,
                failed=True,
                aggregate=True,
                error=reason,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        finally:
            if process is not None:
                if process.returncode is None:
                    try:
                        if os.name == "nt":
                            killer = await asyncio.create_subprocess_exec(
                                "taskkill",
                                "/PID",
                                str(process.pid),
                                "/T",
                                "/F",
                                stdout=asyncio.subprocess.DEVNULL,
                                stderr=asyncio.subprocess.DEVNULL,
                            )
                            await killer.wait()
                        else:
                            os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                await process.wait()
            adapter.diagnostics.observe("media_total", time.monotonic() - started)

    async def close(self):
        """Revoke pending results and reap all workers before plugin shutdown."""
        self.active = False
        adapters = {}
        for groups in self.pending.values():
            for items in groups.values():
                for adapter, _, mid, *_ in items:
                    adapters[id(adapter)] = adapter
                    self.counts["cancelled"] += 1
                    self.counts["unchecked"] += 1
                    self.counts["completed"] += 1
                    adapter.diagnostics.emit(
                        "media_rules",
                        "unchecked_cancelled",
                        mid,
                        failed=True,
                        aggregate=True,
                    )
        self.pending.clear()
        self.queued = 0
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        for adapter in adapters.values():
            self._observe_queue(adapter)
        downloads = [entry[0] for entry in self.downloads.values()]
        for task in downloads:
            task.cancel()
        if downloads:
            await asyncio.gather(*downloads, return_exceptions=True)
        self.downloads.clear()
        self.cache.clear()
        self.seen.clear()
