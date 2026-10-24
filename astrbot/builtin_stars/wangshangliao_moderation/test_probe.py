"""Fixed-scope native messages for the explicit three-account acceptance window."""

import asyncio
import json
import re
import secrets
import time
import uuid

from astrbot import logger
from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.adapter import (
    _text_message,
    message_timestamp,
)
from astrbot.core.platform.sources.wangshangliao.directory import member_directory

from .open_store import OpenError


def keyword_probe_guard(service, event, rule):
    """Bind a neutral keyword reply to one admitted fixed-account native probe."""
    from astrbot.core.platform.sources.wangshangliao.test_rules import rule_target
    from astrbot.core.platform.sources.wangshangliao.test_window import TestWindow

    receiver = event.platform
    window = getattr(receiver, "test_window", None)
    if type(window) is not TestWindow or len(window.keywords) != 1:
        raise OpenError("test_keyword_scope")
    marker = window.keywords[0]
    text = event.message_str
    label = text.removeprefix(marker + "_") if isinstance(text, str) else ""
    payload = event.get_extra("wangshangliao_payload") or {}
    mid = str(event.message_obj.message_id)
    adapters = tuple(service.adapters())
    sender = window.sender
    if (
        not re.fullmatch(r"WSL_TEST_ACTIVITY_[0-9a-f]{32}", marker)
        or label not in {"A", "B"}
        or text != marker + "_" + label
        or rule.get("id") != marker + "_keyword_" + label
        or rule.get("match") != "exact"
        or rule.get("terms") != [text]
        or rule.get("text") != marker + "_reply_" + label
        or rule.get("enabled") is not True
        or len(rule.get("images", [])) != 1
        or len(adapters) != 3
        or {a.account for a in adapters} != {"31846829", "43506527", "23691273"}
        or not any(a is receiver for a in adapters)
        or not any(a is sender for a in adapters)
    ):
        raise OpenError("test_keyword_scope")
    key = "test-probe/" + marker + "_trigger_" + label
    client = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{sender.config['id']}/{key}"))
    epoch = receiver.config.get("_business_epoch")
    generation = getattr(window, "_rule_generation", "")

    def guard():
        """Revalidate the exact volatile permit before every reply submission."""
        if (
            not service.active
            or not service.integrated
            or service.settings.get("paused")
            or getattr(service, "capacity_paused", False)
            or getattr(receiver, "test_window", None) is not window
            or window.receiver is not receiver
            or window.sender is not sender
            or window.scopes != ["group_rules"]
            or window.keywords != [marker]
            or receiver.config.get("_business_epoch") != epoch
            or receiver.connection_state != "online"
            or sender.connection_state != "online"
            or tuple(service.adapters()) != adapters
            or not rule_target(
                receiver, event.get_group_id(), event.get_sender_id(), mid
            )
            or event.get_extra("wsl_test_scope") != "group_rules"
            or payload.get("text") != text
            or payload.get("sender") != sender.account
            or payload.get("recall_route", {}).get("client") != client
            or payload.get("recall_route", {}).get("peer") != str(sender.nim_account)
            or key not in window.outbound_keys
            or not generation
            or getattr(window, "_rule_generation", "") != generation
            or payload.get("_test_rule_window") != generation
        ):
            raise OpenError("test_keyword_revoked")

    guard()
    return guard


class QuoteRehearsal:
    """Exercise a fixed nine-message quote matrix without extra native logins."""

    GROUP = "1459282"
    ACCOUNTS = ("31846829", "43506527", "23691273")
    PLAN = (
        (0, None),
        (1, None),
        (2, None),
        (1, 0),
        (2, 0),
        (0, 1),
        (2, 1),
        (0, 2),
        (1, 2),
    )

    def __init__(self, service, actor, operation, window, members):
        """Freeze a dashboard-owned run, exact roster and existing connections.

        Args:
            service: Original integrated service.
            actor: Authenticated dashboard identity.
            operation: Validated fixed-prefix test identifier.
            window: Existing bounded developer window.
            members: Fresh, complete native roster of the three test accounts.
        """
        self.service, self.actor, self.operation, self.window = (
            service,
            actor,
            operation,
            window,
        )
        self.adapters = {str(a.account): a for a in service.adapters()}
        self.members = members
        self.bindings = {
            uid: (
                a,
                a.nim,
                a.business,
                a.ledger,
                a.nim_account,
                a.config.get("_business_epoch"),
            )
            for uid, a in self.adapters.items()
        }
        receiver = self.adapters[self.ACCOUNTS[1]]
        self.team = receiver.groups.get(self.GROUP)
        self.tenant = dict(service.tenants.get((receiver.account, self.GROUP), {}))
        self.expires = time.monotonic() + 180
        self.started = int(time.time() * 1000)
        self.last_sent = 0
        self.closed = False
        self.expected = {}
        self.results = {}
        self.seen = {}
        self.mismatches = {}
        self.lock = asyncio.Lock()

    def guard(self):
        """Reject expired or changed authority before every native or SQLite write.

        Raises:
            OpenError: If any original window, account or group binding changed.
        """
        service, group = self.service, self.GROUP
        receiver = self.adapters[self.ACCOUNTS[1]]
        current = service.tenants.get((receiver.account, group), {})
        if (
            self.closed
            or time.monotonic() >= self.expires
            or getattr(service, "quote_rehearsal", None) is not self
            or not service.integrated
            or not service.active
            or service.settings["paused"]
            or getattr(service, "capacity_paused", False)
            or getattr(receiver, "test_window", None) is not self.window
            or self.window.receiver is not receiver
            or self.window.sender is not self.adapters[self.ACCOUNTS[2]]
            or self.window.groups != [group]
            or self.window.scopes != ["group_commands"]
            or not self.window.active()
            or receiver.groups.get(group) != self.team
            or not self.team
            or not self.tenant.get("active")
            or not self.tenant.get("present")
            or self.tenant.get("paused")
            or current != self.tenant
            or service.store.tenant(receiver.account, group) != self.tenant
            or (receiver.account, group) in service.suspended_groups
            or service.membership_receiving.get((receiver.account, group))
        ):
            raise OpenError("test_quotes_revoked")
        live = {str(a.account): a for a in service.adapters()}
        if set(live) != set(self.ACCOUNTS):
            raise OpenError("test_quotes_accounts")
        for uid, (adapter, nim, business, ledger, peer, epoch) in self.bindings.items():
            if (
                live[uid] is not adapter
                or adapter.nim is not nim
                or adapter.business is not business
                or adapter.ledger is not ledger
                or adapter.nim_account != peer
                or adapter.config.get("_business_epoch") != epoch
                or adapter.connection_state != "online"
                or adapter.stopping.is_set()
            ):
                raise OpenError("test_quotes_accounts")

    async def receive(self, adapter, outer, inner):
        """Capture only predeclared test messages, never unrelated group content.

        Args:
            adapter: Original receiving native connection.
            outer: Authenticated NIM envelope.
            inner: Decrypted application message.

        Returns:
            Whether this exact test message was persisted as evidence only.
        """
        expected = self.expected.get(inner.client_id)
        if not expected or adapter.account == expected["author"]:
            return False
        try:
            self.guard()
        except OpenError:
            return False
        if (
            self.adapters.get(adapter.account) is not adapter
            or outer.get(0) != "1"
            or outer.get(1) != self.team
            or outer.get(2) != str(self.members[expected["author"]]["nimId"])
            or outer.get(8) != "100"
            or outer.get(11) != inner.client_id
            or inner.session != 2
            or str(inner.target.id) != self.GROUP
            or str(inner.sender.id) != expected["author"]
            or inner.format != 0
            or not inner.HasField("content")
            or inner.content.data != expected["text"]
            or not self.started
            <= message_timestamp(inner, outer)
            <= int(time.time() * 1000) + 5000
        ):
            return False
        reference = None
        if expected["quote"] is not None:
            source = expected["quote"]
            reference = {
                "message": inner.reply.server_id,
                "client": inner.reply.client_id,
                "text": inner.reply.content.data,
            }
            # Application quotes are mandatory; NIM may omit thread metadata.
            if (
                inner.appoint != 1
                or reference != {key: source[key] for key in reference}
                or any(
                    base + index in outer and outer[base + index] != value
                    for base in (29, 34)
                    for index, value in enumerate(
                        (
                            source["peer"],
                            self.team,
                            source["time"],
                            source["message"],
                            source["client"],
                        )
                    )
                )
            ):
                self.mismatches[(expected["step"], adapter.account)] = {
                    "appoint": inner.appoint,
                    "reference_matches": reference
                    == {key: source[key] for key in reference},
                    "native_fields_present": [
                        tag for tag in range(29, 39) if tag in outer
                    ],
                    "native_fields_mismatched": [
                        base + index
                        for base in (29, 34)
                        for index, value in enumerate(
                            (
                                source["peer"],
                                self.team,
                                source["time"],
                                source["message"],
                                source["client"],
                            )
                        )
                        if base + index in outer and outer[base + index] != value
                    ],
                }
                return True
        elif inner.HasField("reply") or inner.appoint:
            return False
        mid = str(outer.get(12, ""))
        if (
            not mid.isascii()
            or not mid.isdigit()
            or not 0 < len(mid) <= 20
            or not 0 < int(mid) < 1 << 64
        ):
            return False
        payload = {
            "text": expected["text"],
            "sender": expected["author"],
            "created_at": message_timestamp(inner, outer),
            "recall_route": {
                "client": inner.client_id,
                "peer": outer[2],
                "time": outer[7],
            },
            "_test_quotes": self.operation,
            "reply": reference,
        }
        try:
            await adapter.ledger.ingest(
                adapter.account,
                self.GROUP,
                mid,
                payload,
                state="ignored_bot",
                authorize=self.guard,
            )
            self.guard()
        except asyncio.CancelledError:
            raise
        except Exception:
            adapter.diagnostics.emit(
                "test_quotes", "witness_unavailable", mid, failed=True
            )
            return True
        self.seen[(expected["step"], adapter.account)] = {
            "message": mid,
            "client": inner.client_id,
            "time": outer[7],
            "peer": outer[2],
            "text": expected["text"],
            "reply": reference,
            "native_fields_present": [tag for tag in range(29, 39) if tag in outer],
        }
        return True

    async def send(self, step):
        """Submit one fixed step once; unknown attempts are never replayed.

        Args:
            step: Integer index into the fixed three-original/six-quote matrix.

        Returns:
            Durable send status and independently received evidence.

        Raises:
            OpenError: On a stale window, missing witness or out-of-order request.
        """
        async with self.lock:
            self.guard()
            if step in self.results:
                return self.results[step]
            if step != len(self.results) or any(
                r["status"] != "accepted" for r in self.results.values()
            ):
                raise OpenError("test_quotes_order")
            author_index, source_step = self.PLAN[step]
            author = self.ACCOUNTS[author_index]
            sender = self.adapters[author]
            key = f"test-quotes/{self.operation}/{step}"
            quote = None
            if source_step is not None:
                quote = self.seen.get((source_step, author))
                if not quote:
                    raise OpenError("test_quotes_missing_source")
            await asyncio.sleep(max(0, self.last_sent + 3.1 - time.monotonic()))
            self.guard()
            if self.window.outbound_remaining <= 0:
                raise OpenError("test_quotes_budget")
            text = f"【原生引用测试 {step + 1}/9】{self.operation[-8:]}"
            message = _text_message(
                sender.config["id"],
                author,
                self.GROUP,
                key,
                text,
                None,
                False,
                int(time.time() * 1000),
                quote=quote,
            )
            self.expected[message.client_id] = {
                "author": author,
                "text": text,
                "quote": quote,
                "step": step,
            }
            result = self.results[step] = {
                "step": step,
                "author": author,
                "status": "unknown",
                "witnesses": [],
            }
            async with sender.send_lock:
                self.guard()
                if await sender.ledger.receipt(key) is not None:
                    raise OpenError("test_quotes_historical")
                nonce, state = await sender.ledger.reserve(
                    key, json.dumps([self.GROUP, text, quote], sort_keys=True)
                )
                if state != "new":
                    raise OpenError("test_quotes_historical")
                try:
                    self.guard()
                    attachment = wire.seal_message(
                        sender.business.deployment.key("message_key"),
                        message,
                        int(nonce),
                        secrets.randbits(64) or 1,
                        int(time.time()),
                    )
                    fields = [
                        (0, b"1"),
                        (1, self.team.encode()),
                        (8, b"100"),
                        (10, attachment.encode()),
                        (11, message.client_id.encode()),
                    ]
                    if quote is not None:
                        fields.extend(
                            (base + index, value.encode())
                            for base in (29, 34)
                            for index, value in enumerate(
                                (
                                    quote["peer"],
                                    self.team,
                                    quote["time"],
                                    quote["message"],
                                    quote["client"],
                                )
                            )
                        )
                    self.guard()
                except BaseException:
                    result["status"] = "rejected"
                    await sender.ledger.finish(key, "rejected")
                    raise
                self.window.outbound_remaining -= 1
                self.window.outbound_keys.add(key)
                try:
                    code, body = await sender.nim.request(8, 2, wire.properties(fields))
                    if code != 200:
                        result["status"] = "rejected"
                    else:
                        receipt, end = wire.read_properties(body)
                        if (
                            end != len(body)
                            or not receipt.get(12, b"").isdigit()
                            or receipt.get(11, message.client_id.encode())
                            != message.client_id.encode()
                        ):
                            raise OpenError("test_quotes_receipt")
                        result["status"], result["message"] = (
                            "accepted",
                            receipt[12].decode(),
                        )
                finally:
                    self.last_sent = time.monotonic()
                    await sender.ledger.finish(
                        key, result["status"], result.get("message", "")
                    )
            if result["status"] == "accepted":
                for _ in range(60):
                    self.guard()
                    peers = [uid for uid in self.ACCOUNTS if uid != author]
                    if all(
                        (step, uid) in self.seen
                        and self.seen[(step, uid)]["message"] == result["message"]
                        for uid in peers
                    ):
                        result["witnesses"] = peers
                        if source_step is not None:
                            result["quote_evidence"] = [
                                {
                                    "account": uid,
                                    "application_reference_verified": True,
                                    "native_fields_present": self.seen[(step, uid)][
                                        "native_fields_present"
                                    ],
                                }
                                for uid in peers
                            ]
                        break
                    await asyncio.sleep(0.25)
                if len(result["witnesses"]) != 2:
                    result["status"] = "witness_missing"
                    result["mismatches"] = [
                        {"account": uid, **value}
                        for (observed_step, uid), value in self.mismatches.items()
                        if observed_step == step
                    ]
            logger.info(
                f"WSL controlled quote test finished: step={step} status={result['status']}"
            )
            return dict(result)


async def control_quotes(service, actor, payload):
    """Allow only a fixed test matrix or closing its original dashboard-owned run.

    Args:
        service: Existing integrated service.
        actor: Authenticated dashboard principal.
        payload: Only operation_id and a fixed step or close command.

    Returns:
        Evidence of one fixed test step, without claiming phone rendering.

    Raises:
        OpenError: For arbitrary routes, identities, text or stale test authority.
    """
    from astrbot.core.platform.sources.wangshangliao.test_window import TestWindow

    if (
        type(actor) is not str
        or not actor.startswith("dashboard/")
        or not actor[10:]
        or type(payload) is not dict
        or set(payload) != {"operation_id", "step"}
        or type(payload["operation_id"]) is not str
        or not re.fullmatch(
            r"WSL_TEST_QUOTES_[A-Za-z0-9]{16,64}", payload["operation_id"]
        )
        or payload["step"] != "close"
        and (type(payload["step"]) is not int or not 0 <= payload["step"] < 9)
    ):
        raise OpenError("test_quotes_arguments")
    run = getattr(service, "quote_rehearsal", None)
    if run is not None and (
        run.actor != actor or run.operation != payload["operation_id"]
    ):
        if not run.closed and time.monotonic() < run.expires:
            raise OpenError("test_quotes_busy")
        run = None
    if payload["step"] == "close":
        if run is not None:
            run.closed = True
            receiver = run.adapters[run.ACCOUNTS[1]]
            if getattr(receiver, "test_window", None) is run.window:
                receiver.test_window = None
            sender = run.adapters[run.ACCOUNTS[2]]
            if getattr(sender, "test_command_window", None) is run.window:
                sender.test_command_window = None
            service.quote_rehearsal = None
        return {"status": "closed", "phone_rendering_verified": False}
    if run is None:
        adapters = {str(a.account): a for a in service.adapters()}
        if payload["step"] != 0 or set(adapters) != set(QuoteRehearsal.ACCOUNTS):
            raise OpenError("test_quotes_accounts")
        receiver = adapters["43506527"]
        window = getattr(receiver, "test_window", None)
        if (
            not isinstance(window, TestWindow)
            or not window.active()
            or window.outbound_remaining < 9
        ):
            raise OpenError("test_quotes_window")
        transport = tuple(
            (uid, a, a.nim, a.business, a.ledger, a.nim_account)
            for uid, a in adapters.items()
        )
        directory = await member_directory(receiver.business, QuoteRehearsal.GROUP)
        members = {str(m["userId"]): m for m in directory["groupMemberInfo"]}
        live = {str(a.account): a for a in service.adapters()}
        if (
            not directory["complete"]
            or set(members) != set(QuoteRehearsal.ACCOUNTS)
            or set(live) != set(adapters)
            or members["31846829"]["groupRole"] != "GROUP_ROLE_OWNER"
            or members["23691273"]["groupRole"] != "GROUP_ROLE_MEMBER"
            or any(
                m.get("accountState") != "ACCOUNT_STATE_GOOD" for m in members.values()
            )
            or any(
                str(members[uid]["nimId"]) != str(a.nim_account)
                for uid, a in adapters.items()
            )
            or any(
                live.get(uid) is not original
                or (
                    original.nim,
                    original.business,
                    original.ledger,
                    original.nim_account,
                )
                != (nim, business, ledger, peer)
                for uid, original, nim, business, ledger, peer in transport
            )
            or getattr(receiver, "test_window", None) is not window
        ):
            raise OpenError("test_quotes_roster")
        run = QuoteRehearsal(service, actor, payload["operation_id"], window, members)
        service.quote_rehearsal = run
    return await run.send(payload["step"])


def observe_reply(service, adapter, group, outer, inner):
    """Persist bounded native evidence without enabling the observer's group business.

    Args:
        service: Original integrated test service.
        adapter: Fixed observer's authenticated native connection.
        group: Fixed test group from the decoded envelope.
        outer: Authenticated NIM routing and timestamp.
        inner: Decoded application message.

    Returns:
        Whether the message was accepted for bounded evidence-only persistence.
    """
    from astrbot.core.platform.sources.wangshangliao.test_window import TestWindow

    if (
        not service.integrated
        or not service.active
        or service.settings["paused"]
        or getattr(adapter, "account", "") != "23691273"
        or group != "1459282"
        or inner.session != 2
        or inner.format not in (0, 1)
        or str(inner.target.id) != group
        or str(inner.sender.id) != "43506527"
        or outer.get(0) != "1"
        or outer.get(8) != "100"
        or inner.format == 0
        and not (inner.HasField("content") or inner.mentions.HasField("content"))
        or len(service.candidate_tasks) >= 2
    ):
        return False
    receiver = next((a for a in service.adapters() if a.account == "43506527"), None)
    window = getattr(receiver, "test_window", None)
    if not isinstance(window, TestWindow):
        return False
    team = receiver.groups.get(group)
    ledger = adapter.ledger
    since = getattr(window, "_witness_since", None)
    witness = getattr(window, "_witness_id", "")
    operation = getattr(window, "_witness_operation", "")
    stamp = message_timestamp(inner, outer)
    if (
        type(since) is not int
        or since <= 0
        or not witness
        or not operation
        or not since <= stamp <= int(time.time() * 1000) + 5000
    ):
        return False

    scheduled = None

    def authorized():
        base = (
            service.active
            and not service.settings["paused"]
            and window.sender is adapter
            and window.receiver is receiver
            and receiver.test_window is window
            and window.groups == [group]
            and any(scope.startswith("group_") for scope in window.scopes)
            and window.active()
            and receiver.groups.get(group) == team
            and bool(team)
            and outer.get(1) == team
            and str(outer.get(2)) == str(receiver.nim_account)
            and adapter.ledger is ledger
            and adapter.connection_state == receiver.connection_state == "online"
            and getattr(window, "_witness_since", None) == since
            and getattr(window, "_witness_id", "") == witness
            and getattr(window, "_witness_operation", "") == operation
        )
        if not base:
            return False
        if scheduled is not None:
            rules = getattr(service, "rule_replies", None)
            return bool(
                rules and rules.media.test_asset(receiver, window, scheduled, outer)
            )
        return True

    def guard():
        if not authorized():
            raise wire.ProtocolError("test_witness_revoked")

    if not authorized():
        return False
    seen = getattr(window, "_witness_messages", None)
    if seen is None:
        seen = window._witness_messages = set()
    mid = str(outer.get(12, ""))
    image = None
    if inner.format == 1:
        from .feature_image import REVIEWED_ASSETS, ImageMessage
        from .native_guide_upload import ONBOARDING_GUIDES

        raw = inner.SerializeToString()
        if len(raw) > 4096:
            return False
        decoded = ImageMessage.FromString(raw)
        assets = {
            REVIEWED_ASSETS[digest]: digest
            for _, digest in ONBOARDING_GUIDES.values()
            if digest in REVIEWED_ASSETS
        }
        fixed = (
            decoded.image.asset.uri in assets
            and decoded.image.width == 1024
            and decoded.image.height == 1024
        )
        if (
            not decoded.HasField("image")
            or decoded.HasField("content")
            or decoded.HasField("mentions")
            or not 0 < decoded.image.size <= 1024 * 1024
            or decoded.image.is_sticker
        ):
            return False
        if fixed:
            image = {
                "asset": decoded.image.asset.uri,
                "digest": assets[decoded.image.asset.uri],
                "width": decoded.image.width,
                "height": decoded.image.height,
                "size": decoded.image.size,
            }
        else:
            rules = getattr(service, "rule_replies", None)
            image = (
                rules.media.test_asset(receiver, window, decoded, outer)
                if rules
                else None
            )
            if image is None:
                return False
            scheduled = decoded
        text = ""
    else:
        text = (
            inner.content.data
            if inner.HasField("content")
            else inner.mentions.content.data
        )
    if (
        not mid.isascii()
        or not mid.isdigit()
        or len(mid) > 32
        or int(mid) <= 0
        or len(text.encode()) > 16384
    ):
        return False
    if mid in seen:
        return True
    if len(seen) >= 20:
        return False
    seen.add(mid)
    payload = {
        "text": text,
        "sender": str(inner.sender.id),
        "name": inner.sender.name,
        "created_at": stamp,
        "mentions": [str(person.uid) for person in inner.mentions.people],
        "_test_witness": True,
        "_test_window": witness,
        "_test_probe": operation,
        **({"image": image} if image is not None else {}),
    }

    async def persist():
        try:
            if authorized():
                await ledger.ingest(
                    adapter.account,
                    group,
                    mid,
                    payload,
                    state="ignored_bot",
                    authorize=guard,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            adapter.diagnostics.emit("test_witness", "store_failed", mid, failed=True)

    task = asyncio.create_task(persist())
    service.candidate_tasks.add(task)
    task.add_done_callback(service.candidate_tasks.discard)
    return True


async def send_guides(service, actor, payload):
    """Replay only the two reviewed guides inside the original controlled window.

    Args:
        service: Existing integrated service and its authenticated adapters.
        actor: Dashboard identity, never a chat-supplied actor.
        payload: One fixed-shape test operation ID.

    Returns:
        Native receipts without claiming first-friend or phone-display evidence.

    Raises:
        OpenError: If the original window, identities or exact roster changed.
    """
    from astrbot.core.platform.sources.wangshangliao.database import account_guard
    from astrbot.core.platform.sources.wangshangliao.test_window import TestWindow

    from .feature_image import FeatureGuides

    if (
        not actor.startswith("dashboard/")
        or set(payload) != {"operation_id"}
        or type(payload["operation_id"]) is not str
        or not re.fullmatch(
            r"WSL_TEST_GUIDES_[A-Za-z0-9]{8,64}", payload["operation_id"]
        )
    ):
        raise OpenError("test_guides_arguments")
    adapters = {str(a.account): a for a in service.adapters()}
    observer, receiver = adapters.get("23691273"), adapters.get("43506527")
    window = getattr(receiver, "test_window", None)
    if not isinstance(window, TestWindow) or observer is None:
        raise OpenError("test_guides_window")
    group, team = "1459282", receiver.groups.get("1459282")
    ledger, business, nim = receiver.ledger, receiver.business, receiver.nim
    frozen = account_guard(receiver)
    operation = "test-guides/" + payload["operation_id"]
    tenant = service.tenants.get((receiver.account, group), {})
    epoch, version = tenant.get("epoch"), tenant.get("version")

    def guard():
        frozen()
        current = service.tenants.get((receiver.account, group), {})
        if (
            not service.integrated
            or not service.active
            or service.settings["paused"]
            or getattr(service, "capacity_paused", False)
            or receiver.test_window is not window
            or window.receiver is not receiver
            or window.sender is not observer
            or window.groups != [group]
            or window.scopes != ["group_commands"]
            or not window.active()
            or not getattr(window, "_witness_id", "")
            or not team
            or receiver.groups.get(group) != team
            or receiver.group_catalog.get(group, {}).get("team") != team
            or receiver.ledger is not ledger
            or receiver.business is not business
            or receiver.nim is not nim
            or receiver.connection_state != "online"
            or observer.connection_state != "online"
            or not current.get("active")
            or not current.get("present")
            or current.get("paused")
            or current.get("epoch") != epoch
            or current.get("version") != version
            or service.store.tenant(receiver.account, group) != current
            or (receiver.account, group) in service.suspended_groups
            or getattr(service, "membership_receiving", {}).get(
                (receiver.account, group)
            )
        ):
            raise OpenError("test_guides_window")

    guard()
    directory = await member_directory(business, group)
    guard()
    members = {str(m["userId"]): m for m in directory["groupMemberInfo"]}
    if (
        not directory["complete"]
        or set(members) != {"31846829", "43506527", "23691273"}
        or members["31846829"]["groupRole"] != "GROUP_ROLE_OWNER"
        or members["23691273"]["groupRole"] != "GROUP_ROLE_MEMBER"
        or str(members["23691273"]["nimId"]) != str(observer.nim_account)
        or str(members["43506527"]["nimId"]) != str(receiver.nim_account)
        or any(m.get("accountState") != "ACCOUNT_STATE_GOOD" for m in members.values())
    ):
        raise OpenError("test_guides_roster")
    if operation not in window.outbound_keys:
        if window.outbound_remaining < 2:
            raise OpenError("test_guides_budget")
        for kind in ("instructions", "examples"):
            prior = await ledger.receipt(operation + "/guide-" + kind)
            guard()
            if prior is not None:
                raise OpenError("test_guides_historical")
        if operation not in window.outbound_keys:
            if window.outbound_remaining < 2:
                raise OpenError("test_guides_budget")
            window.outbound_keys.add(operation)
            window.outbound_remaining -= 2
    status = await FeatureGuides().send_group(receiver, group, team, operation, guard)
    return {
        "status": status,
        "receipts": {
            kind: await ledger.receipt(operation + "/guide-" + kind)
            for kind in ("instructions", "examples")
        },
        "first_welcome": False,
        "phone_display_verified": False,
    }


async def send_probe(service, actor, payload):
    """Send one test message using the already online ordinary account.

    Args:
        service: Integrated plugin service, never a separately logged-in worker.
        actor: Authenticated dashboard actor, not a chat-supplied identity.
        payload: Only a stable operation ID and a bounded command or test marker.

    Returns:
        Persisted platform receipt, including an explicit rejected/unknown state.

    Raises:
        OpenError: If the window, exact native roster or fixed scope is invalid.
    """
    if (
        type(actor) is not str
        or not actor.startswith("dashboard/")
        or not actor.removeprefix("dashboard/")
        or type(payload) is not dict
        or set(payload) != {"operation_id", "text"}
        or not isinstance(payload["operation_id"], str)
        or not re.fullmatch(r"WSL_TEST_[A-Za-z0-9_/-]{1,180}", payload["operation_id"])
        or not isinstance(payload["text"], str)
        or not 0 < len(payload["text"].encode()) <= 1024
    ):
        raise OpenError("test_probe_arguments")
    adapters = {str(a.account): a for a in service.adapters()}
    sender, receiver = adapters.get("23691273"), adapters.get("43506527")
    if sender is None or receiver is None:
        raise OpenError("test_probe_accounts")
    window = getattr(receiver, "test_window", None)
    group = "1459282"
    text, operation = payload["text"], payload["operation_id"]
    key = "test-probe/" + operation
    account, nim, ledger, business = (
        sender.account,
        sender.nim,
        sender.ledger,
        sender.business,
    )
    epoch = sender.config.get("_business_epoch")
    team = receiver.groups.get(group)
    original_members = dict(getattr(receiver, "members", {}).get(group, {}))

    def guard():
        if (
            not service.integrated
            or not getattr(service, "active", False)
            or service.settings["paused"]
            or getattr(service, "capacity_paused", False)
            or (receiver.account, group) in getattr(service, "suspended_groups", set())
            or getattr(service, "membership_receiving", {}).get(
                (receiver.account, group)
            )
            or not actor.startswith("dashboard/")
            or not window
            or not window.active()
            or window.sender is not sender
            or window.receiver is not receiver
            or window.groups != [group]
            or getattr(receiver, "test_window", None) is not window
            or sender.connection_state != "online"
            or receiver.connection_state != "online"
            or sender.nim is not nim
            or sender.ledger is not ledger
            or sender.business is not business
            or sender.account != account
            or sender.config.get("_business_epoch") != epoch
            or (sender.groups.get(group) is not None and sender.groups[group] != team)
            or receiver.groups.get(group) != team
            or not str(team or "").isdigit()
            or dict(getattr(receiver, "members", {}).get(group, {})) != original_members
        ):
            raise OpenError("test_probe_window")
        from astrbot.core.platform.sources.wangshangliao.runtime_policy import runtime

        if not (
            "group_commands" in window.scopes
            and runtime.recognize(text)
            or "group_rules" in window.scopes
            and any(word in text for word in window.keywords)
        ):
            raise OpenError("test_probe_text")

    guard()
    async with sender.send_lock:
        guard()
        await asyncio.sleep(
            max(0, getattr(sender, "_test_probe_sent", 0) + 3 - time.monotonic())
        )
        guard()
        directory = await member_directory(receiver.business, group)
        guard()
        members = {str(m["userId"]): m for m in directory["groupMemberInfo"]}
        if (
            not directory["complete"]
            or set(members) != {"31846829", "43506527", "23691273"}
            or members["31846829"]["groupRole"] != "GROUP_ROLE_OWNER"
            or members["23691273"]["groupRole"] != "GROUP_ROLE_MEMBER"
            or str(members["23691273"]["nimId"]) != str(sender.nim_account)
            or str(members["43506527"]["nimId"]) != str(receiver.nim_account)
            or any(
                m.get("accountState")
                in {"ACCOUNT_STATE_BAN", "ACCOUNT_STATUS_CANCELLED"}
                for m in members.values()
            )
        ):
            raise OpenError("test_probe_roster")
        if (
            window.remaining <= 0
            or key not in window.outbound_keys
            and window.outbound_remaining <= 0
        ):
            raise OpenError("test_probe_budget")
        message = _text_message(
            sender.config["id"],
            account,
            group,
            key,
            text,
            None,
            False,
            int(time.time() * 1000),
        )
        nonce, state = await ledger.reserve(key, json.dumps([group, text]))
        if state != "new":
            guard()
            receipt = await ledger.receipt(key)
            guard()
            if key not in window.outbound_keys:
                return {
                    "status": "historical",
                    "prior_status": state,
                    "message_id": receipt.get("server_id", ""),
                }
            return {"status": state, "message_id": receipt.get("server_id", "")}
        try:
            guard()
            attachment = wire.seal_message(
                business.deployment.key("message_key"),
                message,
                int(nonce),
                secrets.randbits(64) or 1,
                int(time.time()),
            )
            guard()
        except BaseException:
            await ledger.finish(key, "rejected")
            raise
        window.outbound_keys.add(key)
        window.outbound_remaining -= 1
        if not getattr(window, "_witness_id", ""):
            window._witness_since = int(time.time() * 1000)
            window._witness_id = secrets.token_hex(16)
            window._witness_operation = operation
        state, mid, code = "unknown", "", None
        try:
            code, body = await nim.request(
                8,
                2,
                wire.properties(
                    [
                        (0, b"1"),
                        (1, team.encode()),
                        (8, b"100"),
                        (10, attachment.encode()),
                        (11, message.client_id.encode()),
                    ]
                ),
            )
            if code != 200:
                state = "rejected"
            else:
                fields, end = wire.read_properties(body)
                if (
                    end != len(body)
                    or not fields.get(12, b"").isdigit()
                    or fields.get(11, message.client_id.encode())
                    != message.client_id.encode()
                ):
                    raise OpenError("test_probe_receipt")
                mid, state = fields[12].decode(), "accepted"
        finally:
            sender._test_probe_sent = time.monotonic()
            await ledger.finish(key, state, mid)
            logger.info(f"WSL controlled test probe finished: status={state}")
        return {"status": state, "message_id": mid, "code": code}
