"""Native-wire synthetic load with read-only production ledger comparison."""

import argparse
import asyncio
import json
import os
import re
import resource
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


def percentile(values):
    """Return nearest-rank P95 in milliseconds for a nonempty bounded sample."""
    return sorted(values)[min(len(values) - 1, int(len(values) * 0.95))] * 1000


def cpu_list(value):
    """Parse a bounded Linux CPU list without accepting implicit selections.

    Args:
        value: Explicit CPU numbers or inclusive ranges separated by commas.

    Returns:
        A nonempty set of CPU numbers, checked for availability by the caller.

    Raises:
        argparse.ArgumentTypeError: If syntax or range bounds are invalid.
    """
    cpus = set()
    for item in value.split(","):
        item = item.strip()
        if not re.fullmatch(r"[0-9]{1,5}(?:-[0-9]{1,5})?", item):
            raise argparse.ArgumentTypeError("Use CPU numbers or ranges, e.g. 0,2-3")
        bounds = [int(number) for number in item.split("-")]
        first, last = bounds[0], bounds[-1]
        if not first <= last <= 65535:
            raise argparse.ArgumentTypeError("CPU ranges must be ordered and bounded")
        cpus.update(range(first, last + 1))
    return cpus


def apply_affinity(cpus):
    """Apply and verify only an explicit mask on this benchmark thread.

    Args:
        cpus: Requested CPU set, or None to leave inherited affinity untouched.

    Raises:
        RuntimeError: If affinity is unsupported or the kernel truncates a mask.
        OSError: If the kernel rejects the requested mask.
    """
    if cpus is None:
        return
    if not hasattr(os, "sched_setaffinity") or not hasattr(os, "sched_getaffinity"):
        raise RuntimeError("Explicit CPU affinity requires sched_setaffinity support")
    os.sched_setaffinity(0, cpus)
    if os.sched_getaffinity(0) != cpus:
        raise RuntimeError("Requested CPU affinity was not applied exactly")


def placement():
    """Capture actual benchmark affinity and inherited cgroup CPU constraints.

    Returns:
        Current mask, Linux cgroup membership and v2 ancestor quota/cpuset data.
        Missing files remain absent; no service limits are inferred or modified.
    """
    result = {
        "pid": os.getpid(),
        "actual_cpus": sorted(os.sched_getaffinity(0))
        if hasattr(os, "sched_getaffinity")
        else None,
        "cgroup_membership": [],
        "cgroup_version": None,
        "cgroup_cpu": [],
    }
    try:
        result["cgroup_membership"] = Path("/proc/self/cgroup").read_text().splitlines()
    except OSError:
        return result
    unified = next(
        (line[3:] for line in result["cgroup_membership"] if line.startswith("0::")),
        None,
    )
    if unified is None or ".." in Path(unified).parts:
        return result
    result["cgroup_version"] = 2
    root = Path("/sys/fs/cgroup")
    directory = root / unified.lstrip("/")
    while True:
        limits = {"path": str(directory)}
        for name in ("cpu.max", "cpu.stat", "cpuset.cpus.effective"):
            try:
                limits[name] = (directory / name).read_text().strip()
            except OSError:
                pass
        if len(limits) > 1:
            result["cgroup_cpu"].append(limits)
        if directory == root:
            break
        directory = directory.parent
    return result


async def worker(seconds, *, load_cpus=None, finance=False):
    """Exercise authenticated decoding, durable ingress, actual dispatch and rules.

    Args:
        seconds: Duration at one hundred group messages per second.
        load_cpus: Explicit worker affinity, replacing an inherited reader mask.
        finance: Add four verified fixture principals navigating read-only finance.

    Raises:
        AssertionError: If processing, bounded queues or model isolation fails.
    """
    apply_affinity(load_cpus)
    worker_placement = {"start": placement()}
    from aiohttp import web

    from astrbot.builtin_stars.wangshangliao_moderation.content_rules import (
        classify,
        keyword_category,
    )
    from astrbot.builtin_stars.wangshangliao_moderation.conversation_profiles import (
        DEFAULT as PROFILE_DEFAULT,
    )
    from astrbot.builtin_stars.wangshangliao_moderation.open_service import OpenService
    from astrbot.builtin_stars.wangshangliao_moderation.settings import BusinessSettings
    from astrbot.core.platform.sources.wangshangliao import wire
    from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
    from astrbot.core.platform.sources.wangshangliao.group_scope import group_config
    from astrbot.core.platform.sources.wangshangliao.runtime_policy import (
        PolicySnapshot,
        runtime,
    )
    from astrbot.core.platform.sources.wangshangliao.storage import Ledger

    with tempfile.TemporaryDirectory(prefix="wsl-native-load-") as directory:
        root = Path(directory)
        settings = BusinessSettings(SimpleNamespace(), root=root / "settings")
        conversations = {}

        async def new_conversation(origin, *, title, persona_id):
            """Create an owned synthetic conversation without framework storage.

            Args:
                origin: Original account, sender and profile-scoped origin.
                title: Native conversation display title.
                persona_id: Explicit resolved fixture persona.

            Returns:
                Unique in-memory conversation identifier.
            """
            cid = f"fixture-{len(conversations) + 1}"
            conversations[origin] = SimpleNamespace(
                cid=cid,
                user_id=origin,
                history="[]",
                title=title,
                persona_id=persona_id,
            )
            return cid

        async def update_conversation(origin, cid, *, history, token_usage=None):
            """Update only the original in-memory fixture conversation.

            Args:
                origin: Original account, sender and profile-scoped origin.
                cid: Exact current fixture conversation identifier.
                history: Bounded native dialogue history.
                token_usage: Optional receipt; no framework metrics are written.
            """
            row = conversations[origin]
            assert row.cid == cid
            row.history = json.dumps(history)

        context = SimpleNamespace(
            platform_manager=SimpleNamespace(platform_insts=[]),
            get_config=lambda *args: {"admins_id": []},
            astrbot_config_mgr=SimpleNamespace(
                confs={
                    "local-fixture": {
                        "agent_runner": {
                            "runner_type": "local",
                            "config": {"model": {"provider_id": "local-fixture"}},
                        }
                    }
                }
            ),
            conversation_manager=SimpleNamespace(
                get_curr_conversation_id=AsyncMock(
                    side_effect=lambda origin: getattr(
                        conversations.get(origin), "cid", None
                    )
                ),
                get_conversation=AsyncMock(
                    side_effect=lambda origin, cid: (
                        row
                        if (row := conversations.get(origin)) is not None
                        and row.cid == cid
                        else None
                    )
                ),
                new_conversation=new_conversation,
                update_conversation=update_conversation,
            ),
        )
        public = OpenService(
            SimpleNamespace(context=context), root / "public", integrated=True
        )
        gate = asyncio.Event()
        entered = 0

        async def model(request):
            nonlocal entered
            entered += 1
            await gate.wait()
            return web.json_response(
                {
                    "choices": [{"message": {"content": "菜单"}}],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 30,
                        "total_tokens": 130,
                    },
                }
            )

        app = web.Application()
        app.router.add_post("/v1/chat/completions", model)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        model_config = {
            "base_url": f"http://127.0.0.1:{port}/v1",
            "api_key": "local-fixture-only",
            "model": "slow-fixture",
        }
        public.model = {"provider_id": "local-fixture"}
        public.resolve_model = lambda *_: dict(model_config)
        public.profiles.save(
            "consultation",
            "99",
            "",
            0,
            {
                **PROFILE_DEFAULT,
                "config_id": "local-fixture",
                "provider_id": "local-fixture",
                "persona_id": "__builtin__",
            },
            "dashboard/fixture",
        )
        public.store.confirm_budget(
            1,
            True,
            "local-fixture",
            "Local fake HTTP server, no paid provider",
            "fixture",
            binding=public.store.model_binding(model_config),
        )
        public.budget = public.store.budget()
        public.plans.configure(1, "离线客服", "fixture", "1000", "fixture")
        public.plans.configure_price(
            public.store.model_binding(model_config),
            "local-fixture",
            0,
            {
                "input_per_million": "0.15",
                "output_per_million": "0.6",
                "multiplier": "0.25",
                "credit_cny": "1",
                "input_ceiling": 100000,
                "output_ceiling": 4096,
                "all_output_billed_in_limit": True,
                "evidence": "Local mock only; bounded deterministic token receipt.",
            },
            "fixture",
        )
        groups = [str(1000 + i) for i in range(20)]
        rules = {"advertising": True, "flood": True, "keywords": ["测试引流关键词"]}
        public.tenants = {
            ("99", group): {
                "account": "99",
                "group_id": group,
                "present": 1,
                "active": 1,
                "paused": 0,
                "version": 2,
                "rules": dict(rules),
                "managers": [],
            }
            for group in groups
        }
        config = {
            "id": "load",
            "account_id": "99",
            "enable": True,
            "enabled_groups": groups,
            "reply_private": True,
            "moderation": {"enabled": True, "automation_enabled": True},
        }
        if finance:
            config["type"] = "wangshangliao"
        runtime.service = SimpleNamespace(active=True)
        runtime.open_service = public
        runtime.observer = settings.observe
        runtime.publish(
            "load", PolicySnapshot("99", "load", json.dumps(config), time.time())
        )
        queue = asyncio.Queue(maxsize=64)
        bot = WangshangliaoAdapter(config, {}, queue)
        bot.account, bot.nim_account = "99", "1099"
        bot.connection_state = "online"
        bot.business = SimpleNamespace(
            deployment=SimpleNamespace(key=lambda _: bytes(32))
        )
        bot.nim = SimpleNamespace(acknowledge=AsyncMock())
        bot.groups = {group: "team/" + group for group in groups}
        bot.members = {
            group: {str(10000 + i): str(20000 + i) for i in range(100)}
            for group in groups
        }
        bot.ledger = Ledger(root / "messages.sqlite3")
        await bot.ledger.open()
        context.platform_manager.platform_insts = [bot]
        finance_service = None
        finance_principals, finance_samples, finance_prepared = {}, {}, {}
        finance_latencies, finance_completed = [], {}
        finance_pending, finance_ready_at, finance_replied_at = set(), {}, {}
        if finance:
            from astrbot.builtin_stars.wangshangliao_moderation.cashback import (
                CashbackService,
            )

            finance_service = CashbackService(context, root / "finance", "load")
            public.plugin.cashback = SimpleNamespace(service=finance_service)
            store = finance_service.store
            owner = "fixture@example.invalid"
            store.bootstrap([{"email": owner, "name": "Fixture owner"}])
            members = {
                "10000": {
                    "name": "Fixture inviter",
                    "peer": "20000",
                    "eligible": True,
                }
            }
            now = time.time()
            store.configure(
                owner,
                {
                    "id": groups[0],
                    "name": "Synthetic finance group",
                    "bot_id": "load",
                    "account": "99",
                    "contact": "Fixture only",
                    "enabled": True,
                    "rate": 300,
                    "qualification_seconds": 10800,
                    "version": 0,
                },
                members,
                set(),
                now=now - 100,
            )
            members["10001"] = {
                "name": "Fixture member",
                "peer": "20001",
                "eligible": True,
            }
            store.snapshot(
                groups[0],
                "load",
                "99",
                members,
                [
                    {
                        "member": "10001",
                        "inviter": "10000",
                        "source": "business_apply_log",
                        "event_id": "fixture-join",
                        "event_at": now,
                    }
                ],
                now=now,
            )
            finance_principals = {str(70001 + i): str(170001 + i) for i in range(4)}
            # Seed trusted directory evidence, never a preverified event or handler.
            bot.members[groups[0]].update(finance_principals)
            public.store.remember_roster(
                "99",
                {uid: {"nimId": peer} for uid, peer in finance_principals.items()},
            )
            for uid, peer in finance_principals.items():
                store.finance_grant(
                    "dashboard/fixture",
                    {
                        "account": "99",
                        "group_id": groups[0],
                        "uid": uid,
                        "peer": peer,
                        "name": "Fixture operator " + uid,
                        "role": "finance_admin",
                        "active": True,
                        "version": 0,
                        "request_id": "finance-fixture-" + uid,
                    },
                )
            with store.connect() as db:
                finance_before = tuple(db.iterdump())
        latencies, ingress, tasks, start_times = [], [], [], {}
        ledger_statements = {}

        def count_statement(statement):
            operation = statement.split(None, 1)[0].upper()
            ledger_statements[operation] = ledger_statements.get(operation, 0) + 1

        await bot.ledger.db.set_trace_callback(count_statement)
        replies, peaks = [], {"pending": 0, "private": 0, "workers": 0}
        completed = 0
        consumer_failure = asyncio.get_running_loop().create_future()

        async def consume(event):
            nonlocal completed
            event.send = AsyncMock(side_effect=lambda text: replies.append(str(text)))
            sample = finance_samples.get(event.message_obj.message_id)
            try:
                if event.is_private_chat():
                    if sample is not None:
                        sample["processing_started"] = time.monotonic()
                        await public.on_event(event)
                        sample["command_admitted"] = event.get_extra(
                            "_wsl_command_admission"
                        )
                        assert (
                            sample["command_admitted"] is not None
                            and sample["command_admitted"][1]
                        ), ("Financial command was not admitted", sample)
                        assert event.is_stopped()
                        assert (
                            event.actor_identity.require_verified() == sample["uid"]
                            and event.actor_identity.peer == sample["peer"]
                        )
                        assert event.send.call_count == 1
                        assert sample["heading"] in str(event.send.call_args.args[0])
                        state = public.finance_chat.sessions[
                            public.navigation.key(event)
                        ]
                        assert state["screen"] == sample["screen"]
                        if sample["screen"] == "sections":
                            assert state["data"]["total"] == 1
                        elif sample["screen"] == "members":
                            assert len(state["rows"]) == 1
                            assert state["rows"][0]["member"] == "10001"
                        await event.get_extra("_wsl_invitation_delivery_guard")()
                        finance_latencies.append(
                            time.perf_counter() - sample["started"]
                        )
                        finance_completed[sample["uid"]] = sample["step"]
                        finance_replied_at[sample["uid"]] = time.monotonic()
                        finance_ready_at[sample["uid"]] = (
                            finance_replied_at[sample["uid"]] + 3.05
                        )
                        finance_pending.remove(sample["uid"])
                    elif event.message_str == "__slow_ai__":
                        await public.chat(event)
                    else:
                        await asyncio.sleep(0.02)
                else:
                    policy = group_config(bot.config, event.get_group_id())[
                        "moderation"
                    ]
                    keyword_category(event.message_str, policy) or classify(
                        event.message_str
                    )
                    latencies.append(
                        time.perf_counter()
                        - start_times.pop(event.message_obj.message_id)
                    )
                    completed += 1
            except Exception as exc:
                if not consumer_failure.done():
                    consumer_failure.set_result(exc)
                print(
                    "LOAD_CONSUMER_FAILURE "
                    + json.dumps(
                        {
                            "message_id": event.message_obj.message_id,
                            "sample": sample,
                            "reply_count": event.send.call_count,
                            "last_reply": str(event.send.call_args.args[0])[:500]
                            if event.send.call_count
                            else None,
                            "exception": repr(exc),
                        }
                    ),
                    file=sys.stderr,
                    flush=True,
                )
                traceback.print_exc()
                raise
            finally:
                if not event.processing_completion.done():
                    event.processing_completion.set_result(None)

        async def bus():
            while True:
                event = await queue.get()
                tasks.append(asyncio.create_task(consume(event)))

        def envelope(index, *, private=False, slow=False, financial=None):
            """Build client-side ciphertext before measuring server work.

            Args:
                index: Unique message index within its group/private namespace.
                private: Whether the envelope targets the private session.
                slow: Whether the payload invokes the local slow-model fixture.
                financial: Optional trusted fixture UID, peer and read-only input.

            Returns:
                A sealed native message, without writing or dispatching it.
            """
            if financial is not None:
                private = True
            group, sender = groups[index % 20], str(10000 + index % 100)
            if private:
                sender = str(90000 + index) if not slow else str(80000 + index)
            peer = (
                ("30000" if private and not slow else "transport/" + sender)
                if private
                else str(20000 + index % 100)
            )
            mid = ("p/" if private else "g/") + str(index)
            if financial is not None:
                sender, peer, _ = financial
                mid = "f/" + str(index)
            stamp = int(time.time() * 1000)
            inner = wire.ApplicationMessage(
                sender={"id": int(sender), "name": "fixture"},
                target={"id": 99 if private else int(group)},
                session=1 if private else 2,
                device=1,
                format=0,
                content={
                    "data": financial[2]
                    if financial is not None
                    else "__slow_ai__"
                    if slow
                    else "菜单"
                    if private
                    else "联系我 https://example.com"
                    if index % 10 == 0
                    else f"正常发言 {mid}"
                },
            )
            return {
                0: "0" if private else "1",
                1: bot.nim_account if private else bot.groups[group],
                2: peer,
                7: str(stamp),
                8: "100",
                10: wire.seal_message(bytes(32), inner, index + 1, index + 2, stamp),
                11: mid,
                12: mid,
            }

        prepared = {}

        async def inject(index, *, private=False, slow=False):
            outer = (
                envelope(index, private=private, slow=True)
                if slow
                else prepared.pop((index, private))
            )
            began = time.perf_counter()
            if not private:
                start_times[outer[11]] = began
            await bot.ingest(outer)
            ingress.append(time.perf_counter() - began)

        dispatch = asyncio.create_task(bot.dispatch_pending())
        consumer = asyncio.create_task(bus())
        try:
            for second in range(seconds):
                for i in range(100):
                    index = second * 100 + i
                    prepared[(index, False)] = envelope(index)
                    if i % 10 == 0:
                        for n in range(20):
                            index = 100000 + second * 200 + i * 2 + n
                            prepared[(index, True)] = envelope(index, private=True)
            assert len(prepared) == seconds * 300
            if finance:
                finance_prepared = {uid: [] for uid in finance_principals}
                for step in range(len(range(0, seconds * 100, 320))):
                    if step < 4:
                        text, screen, heading = (
                            ("结账", "groups", "【选择结账群】"),
                            ("1", "root", "【结账】"),
                            ("2", "sections", "【核验中】"),
                            ("1", "members", "【成员明细】"),
                        )[step]
                    else:
                        text, screen, heading = (
                            ("0", "sections", "【核验中】")
                            if step % 2 == 0
                            else ("1", "members", "【成员明细】")
                        )
                    for principal, (uid, peer) in enumerate(finance_principals.items()):
                        outer = envelope(
                            1000000 + step * 4 + principal,
                            financial=(uid, peer, text),
                        )
                        finance_prepared[uid].append(outer)
                        finance_samples[outer[11]] = {
                            "uid": uid,
                            "peer": peer,
                            "step": step,
                            "screen": screen,
                            "heading": heading,
                        }
            await inject(0, private=True, slow=True)
            await inject(1, private=True, slow=True)
            for _ in range(200):
                if consumer_failure.done():
                    raise RuntimeError("Native consumer failed") from (
                        consumer_failure.result()
                    )
                if entered == 2:
                    break
                await asyncio.sleep(0.01)
            assert entered == 2, {"entered": entered, "replies": replies[-4:]}
            start = time.monotonic()
            print("LOAD_READY", flush=True)
            for second in range(seconds):
                for i in range(100):
                    await asyncio.sleep(
                        max(0, start + second + i / 100 - time.monotonic())
                    )
                    if consumer_failure.done():
                        raise RuntimeError("Native consumer failed") from (
                            consumer_failure.result()
                        )
                    await inject(second * 100 + i)
                    # Pace from successful replies, not a fixed ingress schedule.
                    # Never send another command while its principal is in flight.
                    for uid, outers in finance_prepared.items():
                        now = time.monotonic()
                        if (
                            not outers
                            or uid in finance_pending
                            or now < finance_ready_at.get(uid, 0)
                            or now >= start + seconds
                        ):
                            continue
                        outer = outers.pop(0)
                        sample = finance_samples[outer[11]]
                        assert finance_completed.get(uid, -1) == sample["step"] - 1
                        if uid in finance_replied_at:
                            sample["previous_reply_gap_seconds"] = (
                                now - finance_replied_at[uid]
                            )
                            assert sample["previous_reply_gap_seconds"] >= 3.05
                        finance_pending.add(uid)
                        sample["started"] = time.perf_counter()
                        await bot.ingest(outer)
                        ingress.append(time.perf_counter() - sample["started"])
                    if i % 10 == 0:
                        for n in range(20):
                            await inject(
                                100000 + second * 200 + i * 2 + n, private=True
                            )
                async with bot.ledger.db.execute(
                    "SELECT COUNT(*),SUM(team LIKE 'private/%') FROM inbox "
                    "WHERE state IN ('pending','processing')"
                ) as cursor:
                    pending, private = await cursor.fetchone()
                peaks["pending"] = max(peaks["pending"], pending)
                peaks["private"] = max(peaks["private"], private or 0)
                peaks["workers"] = max(peaks["workers"], len(bot.workers))
                assert pending <= 4354 and (private or 0) <= 258
                assert public.chat_count == 2 and entered == 2
            for _ in range(500):
                if consumer_failure.done():
                    raise RuntimeError("Native consumer failed") from (
                        consumer_failure.result()
                    )
                if completed == seconds * 100 and not finance_pending:
                    break
                await asyncio.sleep(0.01)
            assert (
                completed == seconds * 100
                and percentile(latencies) < 1000
                and not prepared
            )
            assert bot.ledger.rejected_count > 0
            gate.set()
            await asyncio.gather(*tasks)
            finance_result = {}
            if finance:
                assert not finance_pending, (
                    "Financial reply timed out",
                    sorted(finance_pending),
                )
                assert len(finance_latencies) == sum(
                    "started" in sample for sample in finance_samples.values()
                )
                assert all(
                    finance_completed.get(uid, -1) >= 3 for uid in finance_principals
                ), ("Financial read chain incomplete", finance_completed)
                assert len(public.finance_chat.sessions) == 4
                with finance_service.store.connect() as db:
                    assert tuple(db.iterdump()) == finance_before
                finance_result = {
                    "finance_interactions": len(finance_latencies),
                    "finance_p95_ms": round(percentile(finance_latencies), 3),
                    "finance_unsent_interactions": sum(
                        len(outers) for outers in finance_prepared.values()
                    ),
                    "finance_min_reply_gap_seconds": round(
                        min(
                            sample["previous_reply_gap_seconds"]
                            for sample in finance_samples.values()
                            if "previous_reply_gap_seconds" in sample
                        ),
                        6,
                    ),
                    "finance_pacing": "One in flight per principal; 3.05 seconds after successful reply and delivery guard; no retries.",
                    "finance_fixture_principals": 4,
                    "finance_native_on_event_consumed": True,
                    "finance_fixture_ledger_unchanged": True,
                    "finance_scope": "Temporary fixtures; read-only navigation, no payments.",
                }
            assert public.chat_count == 0 and entered == 2
            with public.store.connect() as db:
                charges = db.execute(
                    "SELECT state,reserved_micro,charged_micro FROM ai_charges"
                ).fetchall()
            assert len(charges) == 2 and all(
                row["state"] == "settled"
                and 0 < row["charged_micro"] <= row["reserved_micro"]
                for row in charges
            )
            print(
                "LOAD_RESULT "
                + json.dumps(
                    {
                        **finance_result,
                        "groups": 20,
                        "messages_per_second": 100,
                        "messages": completed,
                        "seconds": seconds,
                        "rule_p95_ms": round(percentile(latencies), 3),
                        "load_elapsed_seconds": round(time.monotonic() - start, 3),
                        "durable_ingress_p95_ms": round(percentile(ingress), 3),
                        "model_requests": entered,
                        "atomic_plan_cost_reservations": True,
                        "slow_ai_did_not_block_rules": True,
                        "native_decode_dispatch_storage": True,
                        "message_ledger_statements": ledger_statements,
                        "message_ledger_synchronous": "FULL",
                        "client_ciphertext_prebuilt": True,
                        "queue_peaks": peaks,
                        "private_flood_rejected": bot.ledger.rejected_count,
                        "max_rss_bytes": resource.getrusage(
                            resource.RUSAGE_SELF
                        ).ru_maxrss
                        * 1024,
                        "mode": "integrated",
                        "worker_placement": {
                            **worker_placement,
                            "end": placement(),
                        },
                    }
                ),
                flush=True,
            )
        finally:
            gate.set()
            bot.stopping.set()
            dispatch.cancel()
            consumer.cancel()
            await asyncio.gather(dispatch, consumer, return_exceptions=True)
            for task in bot.workers.values():
                task.cancel()
            await asyncio.gather(*bot.workers.values(), *tasks, return_exceptions=True)
            await bot.ledger.close()
            await runner.cleanup()
            if finance_service is not None:
                finance_service.store.close()


def main():
    """Compare identical read-only production queries before and during synthetic load."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--integrated", action="store_true")
    parser.add_argument(
        "--finance",
        action="store_true",
        help="Add actual read-only private finance navigation on temporary fixtures.",
    )
    parser.add_argument(
        "--absolute-query-report",
        action="store_true",
        help="Report absolute query P95 without enforcing relative growth; default unchanged.",
    )
    parser.add_argument(
        "--database",
        type=Path,
        help="Read an explicitly prepared snapshot before deployment migrations.",
    )
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument(
        "--reader-cpus",
        type=cpu_list,
        help="Pin this read-only benchmark before baseline, e.g. 0; default unchanged.",
    )
    parser.add_argument(
        "--load-cpus",
        type=cpu_list,
        help="Pin only the synthetic worker at entry, e.g. 1-3; default unchanged.",
    )
    args = parser.parse_args()
    assert 10 <= args.seconds <= 35
    if args.worker:
        if args.absolute_query_report:
            parser.error(
                "--absolute-query-report is only valid for the baseline/read process"
            )
        if args.reader_cpus is not None:
            parser.error("--reader-cpus is only valid for the baseline/read process")
        # The parent validates both masks before pinning itself. This child may
        # inherit the reader's narrower mask; apply its explicit mask at entry.
        asyncio.run(
            worker(args.seconds, load_cpus=args.load_cpus, finance=args.finance)
        )
        return
    available = os.sched_getaffinity(0) if hasattr(os, "sched_getaffinity") else None
    if args.reader_cpus is not None or args.load_cpus is not None:
        if not hasattr(os, "sched_getaffinity") or not hasattr(os, "sched_setaffinity"):
            parser.error("Explicit CPU affinity is unsupported on this platform")
        for name, cpus in (
            ("--reader-cpus", args.reader_cpus),
            ("--load-cpus", args.load_cpus),
        ):
            if cpus is not None and not cpus <= available:
                parser.error(
                    f"{name} must be within available CPUs {sorted(available)}"
                )
    apply_affinity(args.reader_cpus)
    reader_placement = {"before_baseline": placement()}
    root = Path(__file__).resolve().parents[1]
    production_path = root / "data/plugin_data/wangshangliao_cashback/cashback.sqlite3"
    path = args.database.resolve() if args.database else production_path
    uri = path.resolve().as_uri() + "?mode=ro"
    from astrbot.builtin_stars.wangshangliao_moderation.cashback_store import (
        CashbackStore,
    )

    ledger = CashbackStore(path, read_only=True, reuse_reads=True)
    with sqlite3.connect(uri, uri=True, timeout=2) as db:
        group = db.execute(
            "SELECT g.id FROM groups g LEFT JOIN entries e ON e.group_id=g.id "
            "GROUP BY g.id ORDER BY COUNT(e.id) DESC LIMIT 1"
        ).fetchone()[0]
        actor = db.execute(
            "SELECT email FROM users WHERE active=1 AND role='admin' LIMIT 1"
        ).fetchone()[0]

    def pressure():
        values = {}
        for kind in ("cpu", "io"):
            source = Path("/proc/pressure") / kind
            if source.exists():
                for line in source.read_text().splitlines():
                    fields = line.split()
                    values[kind + "_" + fields[0] + "_us"] = int(
                        dict(field.split("=") for field in fields[1:])["total"]
                    )
        return values

    def sample():
        began = time.perf_counter()
        cpu_began = time.thread_time()
        usage = resource.getrusage(resource.RUSAGE_SELF)
        per_state = {}
        for state in ("pending", "paid", "verifying", "no_reward"):
            started = time.perf_counter()
            ledger.list_entries(actor, group, state=state, pagination="inviters")
            per_state[state] = time.perf_counter() - started
        after = resource.getrusage(resource.RUSAGE_SELF)
        return (
            time.perf_counter() - began,
            time.thread_time() - cpu_began,
            per_state,
            {
                "voluntary": after.ru_nvcsw - usage.ru_nvcsw,
                "involuntary": after.ru_nivcsw - usage.ru_nivcsw,
            },
        )

    sample()
    before, before_cpu = [], []
    per_state = {"baseline": [], "load": []}
    switches = {"baseline": [], "load": []}
    pressure_before = pressure()
    until = time.monotonic() + args.seconds
    while time.monotonic() < until:
        wall, cpu, states, context = sample()
        before.append(wall)
        before_cpu.append(cpu)
        per_state["baseline"].append(states)
        switches["baseline"].append(context)
        time.sleep(0.1)
    pressure_baseline = pressure()
    reader_placement["after_baseline"] = placement()
    with tempfile.TemporaryDirectory(prefix="wsl-native-benchmark-") as directory:
        command = [
            sys.executable,
            "-m",
            "scripts.check_wsl_open_load",
            "--worker",
            "--seconds",
            str(args.seconds),
        ]
        if args.load_cpus is not None:
            command.extend(["--load-cpus", ",".join(map(str, sorted(args.load_cpus)))])
        if args.finance:
            command.append("--finance")
        process = subprocess.Popen(
            command,
            cwd=root,
            env={
                **{k: v for k, v in os.environ.items() if k != "ASTRBOT_WSL_OPEN"},
                "ASTRBOT_ROOT": directory,
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        measured, measured_cpu = [], []
        try:
            for line in process.stdout:
                if line.strip() == "LOAD_READY":
                    break
            else:
                raise RuntimeError(
                    "Synthetic worker not ready: " + process.stderr.read()[-4000:]
                )
            until = time.monotonic() + args.seconds - 1
            pressure_loaded_before = pressure()
            reader_placement["before_load"] = placement()
            while time.monotonic() < until:
                wall, cpu, states, context = sample()
                measured.append(wall)
                measured_cpu.append(cpu)
                per_state["load"].append(states)
                switches["load"].append(context)
                time.sleep(0.1)
            pressure_loaded_after = pressure()
            reader_placement["after_load"] = placement()
            out, err = process.communicate(timeout=30)
        except BaseException:
            process.terminate()
            process.communicate(timeout=10)
            raise
        if process.returncode:
            failed = Path(
                tempfile.mkdtemp(prefix="wsl-native-load-failed-", dir="/opt/backups")
            )
            evidence = failed / "failure.json"
            evidence.write_text(
                json.dumps(
                    {
                        "command": command,
                        "returncode": process.returncode,
                        "stdout": out,
                        "stderr": err,
                        "baseline_wall_seconds": before,
                        "load_wall_seconds": measured,
                    },
                    indent=2,
                )
            )
            evidence.chmod(0o600)
            raise RuntimeError(
                f"Synthetic worker failed; evidence: {evidence}\n"
                + err[-4000:]
                + out[-4000:]
            )
        result = json.loads(
            next(
                line.removeprefix("LOAD_RESULT ")
                for line in out.splitlines()
                if line.startswith("LOAD_RESULT ")
            )
        )
    ratio = percentile(measured) / percentile(before)
    ledger.close()
    reader_masks_unchanged = all(
        value["actual_cpus"] == reader_placement["before_baseline"]["actual_cpus"]
        for value in reader_placement.values()
    )
    worker_masks_match = (
        all(
            value["actual_cpus"] == sorted(args.load_cpus)
            for value in result["worker_placement"].values()
        )
        if args.load_cpus is not None
        else True
    )
    result["process_placement"] = {
        "initial_available_cpus": sorted(available) if available is not None else None,
        "requested_reader_cpus": sorted(args.reader_cpus)
        if args.reader_cpus is not None
        else None,
        "requested_load_cpus": sorted(args.load_cpus)
        if args.load_cpus is not None
        else None,
        "reader": reader_placement,
        "reader_masks_unchanged": reader_masks_unchanged,
        "worker_masks_match_request": worker_masks_match,
        "production_service_configuration_changed": False,
        "production_service_quota_emulated": False,
        "scope": "Standalone read-only benchmark and synthetic worker only.",
    }
    result["production_queries"] = {
        "read_only": True,
        "acceptance_mode": "absolute_query_report"
        if args.absolute_query_report
        else "relative_growth",
        "relative_growth_enforced": not args.absolute_query_report,
        "live_database": path.resolve() == production_path.resolve(),
        "reader_reuse": True,
        "paid_display_cache": True,
        "authority_rechecked_each_query": True,
        "baseline_p95_ms": round(percentile(before), 3),
        "under_load_p95_ms": round(percentile(measured), 3),
        "p95_growth_percent": round((ratio - 1) * 100, 2),
        "within_ten_percent": ratio <= 1.1,
        "baseline_samples": len(before),
        "load_samples": len(measured),
        "baseline_median_ms": round(statistics.median(before) * 1000, 3),
        "baseline_cpu_p95_ms": round(percentile(before_cpu), 3),
        "under_load_cpu_p95_ms": round(percentile(measured_cpu), 3),
        "baseline_wait_p95_ms": round(
            percentile([max(0, w - c) for w, c in zip(before, before_cpu)]), 3
        ),
        "under_load_wait_p95_ms": round(
            percentile([max(0, w - c) for w, c in zip(measured, measured_cpu)]), 3
        ),
        "state_p95_ms": {
            phase: {
                state: round(percentile([s[state] for s in samples]), 3)
                for state in ("pending", "paid", "verifying", "no_reward")
            }
            for phase, samples in per_state.items()
        },
        "query_context_switches": {
            phase: {
                kind: sum(s[kind] for s in samples)
                for kind in ("voluntary", "involuntary")
            }
            for phase, samples in switches.items()
        },
        "host_pressure_delta": {
            "baseline": {
                key: pressure_baseline[key] - value
                for key, value in pressure_before.items()
            },
            "load": {
                key: pressure_loaded_after[key] - value
                for key, value in pressure_loaded_before.items()
            },
        },
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    output = Path("/opt/backups") / time.strftime(
        "wsl-native-load-%Y%m%dT%H%M%S.json", time.gmtime()
    )
    output.write_text(
        json.dumps(
            {
                **result,
                "query_samples": {
                    "unit": "seconds",
                    "baseline_wall": before,
                    "baseline_cpu": before_cpu,
                    "load_wall": measured,
                    "load_cpu": measured_cpu,
                },
            }
        )
    )
    output.chmod(0o600)
    if not reader_masks_unchanged or not worker_masks_match:
        raise RuntimeError(
            "Benchmark affinity changed; comparison invalid, evidence retained"
        )
    if ratio > 1.1 and not args.absolute_query_report:
        raise RuntimeError("Read-only query P95 target not met; evidence retained")


if __name__ == "__main__":
    main()
