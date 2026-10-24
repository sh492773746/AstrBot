"""Fixed-scope warm-group acceptance with independent persistent test accounting."""

import asyncio
import copy
import hashlib
import inspect
import json
import re
import secrets
import time

from astrbot.core.platform.sources.wangshangliao import wire
from astrbot.core.platform.sources.wangshangliao.adapter import (
    _text_message,
    message_timestamp,
)
from astrbot.core.platform.sources.wangshangliao.database import account_guard
from astrbot.core.platform.sources.wangshangliao.group_scope import GroupAdapter
from astrbot.core.platform.sources.wangshangliao.identity import ActorIdentity
from astrbot.core.platform.sources.wangshangliao.test_window import TestWindow

from .ai_plans import AIPlans
from .conversation_profiles import ConversationProfiles
from .group_ai import DEFAULT, GroupAI
from .knowledge import Knowledge
from .native_conversations import NativeConversations
from .open_store import OpenError, OpenStore

GROUP, RECEIVER, SENDER = "1459282", "43506527", "31846829"
MEMBERS = {RECEIVER, SENDER, "23691273"}
QUESTIONS = (
    "今天下班准备喝柠檬茶，你推荐搭配什么小吃？",
    "我刚才说准备喝什么？只按刚才的对话回答。",
    "查询加拿大28最新开奖和近期走势，过期或接口失败请直说，不要编造。",
)


class _TestAdapter(GroupAdapter):
    """Keep native IO unchanged while binding each test reply to its admitted input."""

    def __init__(self, adapter, fixture):
        super().__init__(adapter, GROUP)
        object.__setattr__(self, "fixture", fixture)

    @property
    def config(self):
        self.fixture.check()
        # This role comes from the complete native test roster, not a saved grant.
        config = self.adapter.config
        return {
            **config,
            "_open_native_roles": {
                **config.get("_open_native_roles", {}),
                GROUP: self.fixture.members[RECEIVER]["groupRole"],
            },
        }

    async def send_text(self, group, key, text, *, authorize=None, **kwargs):
        self.fixture.check()
        mid = self.fixture.sources.get(key)
        if (
            group != GROUP
            or not mid
            or kwargs != {"proactive": True}
            or not self.fixture.window.reply(group, mid, key)
        ):
            raise OpenError("test_group_ai_delivery")
        await self.fixture.roster(self, group)

        async def check():
            self.fixture.check()
            if authorize is not None:
                result = authorize()
                if inspect.isawaitable(result):
                    await result
            self.fixture.check()
            if not self.fixture.window.reply(group, mid, key):
                raise OpenError("test_group_ai_revoked")

        # An admitted test answer is a correlated reply, not a production
        # proactive-send grant. No native connector configuration is changed.
        expected = _text_message(
            self.adapter.config["id"], RECEIVER, GROUP, key, text, None, False, 0
        )
        self.fixture.deliveries[expected.client_id] = (key, text)
        return await self.adapter.send_text(
            group, key, text, test_mid=mid, authorize=check
        )


class GroupAITest:
    """Reuse the real executor without creating production subscriptions or payers."""

    test_only = True
    integrated = True

    def __init__(self, parent, actor, operation, receiver, sender, window, members):
        self.parent, self.actor, self.operation = parent, actor, operation
        self.receiver, self.sender, self.window = receiver, sender, window
        self.team = receiver.groups[GROUP]
        self.context = parent.context
        self.bindings = [
            (a, a.account, a.nim_account, a.nim, a.business, a.ledger)
            for a in (receiver, sender)
        ]
        self.native_guard = account_guard(receiver)
        self.sender_guard = account_guard(sender)
        self.parent_budget = dict(parent.budget)
        self.parent_settings = dict(parent.settings)
        self.parent_tenant = copy.deepcopy(parent.store.tenant(RECEIVER, GROUP))
        self.profile = parent.profiles.resolve("group", RECEIVER, GROUP)
        self.binding = parent.store.model_binding(self.profile["model"])
        with parent.store.connect() as db:
            price = db.execute(
                "SELECT version,config,blocked FROM ai_prices WHERE binding=?",
                (self.binding,),
            ).fetchone()
        self.parent_price = dict(price) if price else None
        self.members = members
        self.sources = {}
        self.deliveries = {}
        self.witnessed = set()
        self.witness_tasks = set()
        self.started = int(time.time() * 1000)
        self.results = {}
        self.send_lock = asyncio.Lock()
        self.closed = False
        self.active = True
        self.capacity_paused = False
        self.suspended_groups = set()
        self.membership_receiving = {}
        self.quota_admins = set()
        self.root = parent.root / "group-ai-tests"
        self.store = OpenStore(self.root)
        self.plans = AIPlans(self.store)
        self.model = dict(parent.model)
        self.resolve_model = parent.resolve_model
        self.profiles = ConversationProfiles(self)
        self.knowledge = Knowledge(self)
        self.conversations = NativeConversations(self.context)
        self.pc28 = parent.pc28
        self.governance = parent.governance
        self.group_ai = GroupAI(self)
        self.adapter = _TestAdapter(receiver, self)
        self.expiry = None

    @property
    def chat_count(self):
        return self.parent.chat_count

    @chat_count.setter
    def chat_count(self, value):
        self.parent.chat_count = value

    def check(self):
        """Revoke all unsent work on any window, identity or source-profile change."""
        self.native_guard()
        self.sender_guard()
        parent = self.parent
        with parent.store.connect() as db:
            price = db.execute(
                "SELECT version,config,blocked FROM ai_prices WHERE binding=?",
                (self.binding,),
            ).fetchone()
        if (
            self.closed
            or not parent.active
            or parent.settings != self.parent_settings
            or parent.settings["paused"]
            or parent.capacity_paused
            or parent.budget != self.parent_budget
            or parent.store.budget() != self.parent_budget
            or parent.store.settings() != self.parent_settings
            or not self.parent_price
            or self.parent_price["blocked"]
            or (dict(price) if price else None) != self.parent_price
            or parent.group_ai.test_fixture is not self
            or self.receiver.test_window is not self.window
            or not self.window.active()
            or self.window.receiver is not self.receiver
            or self.window.sender is not self.sender
            or self.window.groups != [GROUP]
            or self.window.scopes != ["group_ai"]
            or parent.store.tenant(RECEIVER, GROUP) != self.parent_tenant
            or (RECEIVER, GROUP) in parent.suspended_groups
            or parent.membership_receiving.get((RECEIVER, GROUP))
            or parent.group_ai.configs.get((RECEIVER, GROUP), {})
            .get("config", {})
            .get("enabled", False)
            or any(
                a.account != account
                or a.nim_account != peer
                or a.nim is not nim
                or a.business is not business
                or a.ledger is not ledger
                or a.connection_state != "online"
                or a.stopping.is_set()
                or not any(current is a for current in parent.adapters())
                for a, account, peer, nim, business, ledger in self.bindings
            )
            or self.receiver.groups.get(GROUP) != self.team
            or self.sender.groups.get(GROUP, self.team) != self.team
            or GROUP not in self.receiver.enabled_groups
        ):
            raise OpenError("test_group_ai_revoked")
        parent.profiles.assert_current(self.profile, "group", RECEIVER, GROUP)

    async def initialize(self):
        """Copy verified model references and pricing; keep quotas across windows."""
        self.check()
        with self.store.connect() as db:
            if (
                db.execute(
                    "SELECT 1 FROM ai_charges WHERE state IN "
                    "('reserved','dispatched','unknown') LIMIT 1"
                ).fetchone()
                or db.execute(
                    "SELECT 1 FROM group_ai WHERE blocked<>'' LIMIT 1"
                ).fetchone()
            ):
                raise OpenError("test_group_ai_unknown")
        self.store.discover(
            RECEIVER,
            {GROUP: {"name": "隔离热群测试", "team": self.receiver.groups[GROUP]}},
        )
        tenant = self.store.tenant(RECEIVER, GROUP)
        if not tenant["active"]:
            self.store.import_group(
                RECEIVER,
                GROUP,
                {"advertising": False, "flood": False, "keywords": []},
                [SENDER],
                self.actor,
                guard=self.check,
            )
            tenant = self.store.tenant(RECEIVER, GROUP)
        self.store.native_role(RECEIVER, GROUP, tenant["epoch"], "GROUP_ROLE_ADMIN")
        self.tenants = {(RECEIVER, GROUP): self.store.tenant(RECEIVER, GROUP)}
        self.settings = self.store.settings()
        if self.settings["paused"]:
            raise OpenError("test_group_ai_paused")
        billing = self.plans.settings()
        if billing["version"] == 1:
            self.plans.configure(
                1,
                "Server test",
                "No production billing",
                "0.5",
                self.actor,
                trial_monthly_cny="0.5",
            )
        billing = self.plans.settings()
        if (
            billing["monthly_micro"] != 500000
            or billing["trial_monthly_micro"] != 500000
        ):
            raise OpenError("test_group_ai_budget")
        binding = self.binding
        if (
            not self.parent_budget["confirmed"]
            or self.parent_budget["provider"] != self.profile["provider_id"]
            or self.parent_budget["binding"] != binding
        ):
            raise OpenError("test_group_ai_budget")
        with self.store.connect() as db:
            old = db.execute(
                "SELECT version FROM ai_prices WHERE binding=?", (binding,)
            ).fetchone()
        self.plans.configure_price(
            binding,
            self.profile["provider_id"],
            old[0] if old else 0,
            json.loads(self.parent_price["config"]),
            self.actor,
        )
        budget = self.store.budget()
        self.store.confirm_budget(
            budget["version"],
            True,
            self.profile["provider_id"],
            "Existing verified supplier; independently capped test accounting",
            self.actor,
            binding=binding,
        )
        self.budget = self.store.budget()
        with self.store.connect(write=True, guard=self.check) as db:
            prior = db.execute(
                "SELECT version FROM ai_profiles WHERE kind='group' AND account='' AND group_id=''"
            ).fetchone()
            db.execute(
                "INSERT OR REPLACE INTO ai_profiles VALUES('group','','',?,?)",
                ((prior[0] if prior else 0) + 1, json.dumps(self.profile["selection"])),
            )
            payer = db.execute(
                "SELECT version FROM ai_payers WHERE account=? AND group_id=?",
                (RECEIVER, GROUP),
            ).fetchone()
            # This is server test authorization, NOT a fabricated user's consent
            # and never a write to the production payer/subscription tables.
            db.execute(
                "INSERT OR REPLACE INTO ai_payers VALUES(?,?,?,?,?,?,1)",
                (
                    RECEIVER,
                    GROUP,
                    SENDER,
                    "nim/" + str(self.sender.nim_account),
                    (payer[0] if payer else 0) + 1,
                    self.tenants[(RECEIVER, GROUP)]["epoch"],
                ),
            )
            saved = db.execute(
                "SELECT version FROM group_ai WHERE account=? AND group_id=?",
                (RECEIVER, GROUP),
            ).fetchone()
            db.execute(
                "INSERT INTO group_ai VALUES(?,?,?,?,?,0,'') ON CONFLICT(account,group_id) "
                "DO UPDATE SET version=excluded.version,config=excluded.config",
                (
                    RECEIVER,
                    GROUP,
                    (saved[0] if saved else 0) + 1,
                    json.dumps(
                        {
                            **DEFAULT,
                            "enabled": True,
                            "start_hour": 0,
                            "end_hour": 0,
                            "daily_limit": 6,
                        }
                    ),
                    "",
                ),
            )
            self.store.log(
                db,
                self.actor,
                RECEIVER,
                GROUP,
                "test_group_ai_authorized",
                {
                    "operation": self.operation,
                    "test_only": True,
                    "maximum_turns": self.window.remaining,
                },
            )
        await self.group_ai.refresh()
        self.check()

    async def roster(self, adapter, group):
        """Require the real owner/admin/member matrix, including before delivery."""
        self.check()
        if adapter is not self.adapter or group != GROUP:
            raise OpenError("test_group_ai_scope")
        directory = await self.receiver.get_moderation_members(GROUP)
        self.check()
        rows = {str(row["userId"]): row for row in directory.get("groupMemberInfo", [])}
        if (
            not directory.get("complete")
            or len(directory["groupMemberInfo"]) != 3
            or set(rows) != MEMBERS
            or any(
                rows[uid].get("groupRole") != role
                or rows[uid].get("accountState") != "ACCOUNT_STATE_GOOD"
                for uid, role in {
                    SENDER: "GROUP_ROLE_OWNER",
                    RECEIVER: "GROUP_ROLE_ADMIN",
                    "23691273": "GROUP_ROLE_MEMBER",
                }.items()
            )
            or any(
                str(rows[uid].get("nimId")) != str(row.get("nimId"))
                for uid, row in self.members.items()
            )
        ):
            raise OpenError("test_group_ai_roster")
        return rows

    def write_guard(self, adapter, *, group, tenant, members, control):
        self.check()
        if (
            adapter is not self.adapter
            or group != GROUP
            or not control
            or members != self.members
        ):
            raise OpenError("test_group_ai_scope")
        return self.check

    async def notify_quota(self, *args, **kwargs):
        """Keep test quota notifications out of real managers' private messages."""
        return

    async def observe(self, event, command, *, consumed):
        self.check()
        identity = getattr(event, "actor_identity", None)
        mid = str(event.message_obj.message_id)
        payload = event.get_extra("wangshangliao_payload") or {}
        if (
            command
            or consumed
            or event.platform is not self.receiver
            or event.get_group_id() != GROUP
            or event.is_private_chat()
            or not isinstance(identity, ActorIdentity)
            or identity.account != RECEIVER
            or identity.claimed_uid != SENDER
            or identity.verified_uid != SENDER
            or identity.peer != str(self.sender.nim_account)
            or str(payload.get("sender")) != SENDER
            or str(payload.get("recall_route", {}).get("peer")) != identity.peer
            or str(self.receiver.nim_account) not in payload.get("mentions", [])
            or self.window.accepted.get((GROUP, mid)) != "group_ai"
            or payload.get("_audit_incomplete")
        ):
            return
        async with self.receiver.ledger.db.execute(
            "SELECT payload FROM inbox WHERE account=? AND team=? AND message=?",
            (RECEIVER, GROUP, mid),
        ) as cursor:
            row = await cursor.fetchone()
        self.check()
        if not row or json.loads(row[0]) != payload:
            raise OpenError("test_group_ai_evidence")
        row = self.group_ai.configs[(RECEIVER, GROUP)]
        trigger = f"mention/{mid}/{SENDER}/{identity.peer}"
        request = hashlib.sha256(
            f"group-ai/{RECEIVER}/{GROUP}/{row['version']}/{trigger}".encode()
        ).hexdigest()
        key = "group-ai/" + request
        if key in self.sources:
            return
        self.sources[key] = mid
        proxy = copy.copy(event)
        proxy.platform = self.adapter
        await self.group_ai.observe(proxy)

    def observe_reply(self, adapter, group, outer, inner):
        """Persist only this fixture's authenticated replies, without group grants.

        Args:
            adapter: Original sending account's existing native connection.
            group: Decoded business group.
            outer: Authenticated NIM routing envelope.
            inner: Authenticated decoded application message.

        Returns:
            Whether the exact test reply was claimed for evidence-only storage.
        """
        expected = self.deliveries.get(inner.client_id)
        mid = str(outer.get(12, ""))
        if (
            adapter is not self.sender
            or group != GROUP
            or (outer.get(0), outer.get(1), outer.get(2), outer.get(8))
            != ("1", self.team, str(self.receiver.nim_account), "100")
            or inner.session != 2
            or inner.format != 0
            or str(inner.sender.id) != RECEIVER
            or str(inner.target.id) != GROUP
            or not expected
            or not inner.HasField("content")
            or inner.content.data != expected[1]
            or not self.started
            <= message_timestamp(inner, outer)
            <= int(time.time() * 1000) + 5000
            or not mid.isascii()
            or not mid.isdigit()
            or len(mid) > 32
            or int(mid) <= 0
            or len(self.witness_tasks) >= 2
            or len(self.parent.candidate_tasks) >= 2
        ):
            return False
        try:
            self.check()
        except (OpenError, wire.ProtocolError):
            return False
        if mid in self.witnessed:
            return True
        if len(self.witnessed) >= 3:
            return False
        self.witnessed.add(mid)
        payload = {
            "text": expected[1],
            "sender": RECEIVER,
            "name": inner.sender.name,
            "created_at": message_timestamp(inner, outer),
            "mentions": [],
            "_test_group_ai": self.operation,
            "_test_reply_key": expected[0],
        }

        async def persist():
            try:
                await adapter.ledger.ingest(
                    SENDER,
                    GROUP,
                    mid,
                    payload,
                    state="ignored_bot",
                    authorize=self.check,
                )
            except (OpenError, wire.ProtocolError):
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                diagnostic = getattr(adapter, "diagnostics", None)
                if diagnostic is not None:
                    diagnostic.emit(
                        "test_group_ai_witness", "store_failed", mid, failed=True
                    )

        task = asyncio.create_task(persist())
        self.witness_tasks.add(task)
        self.parent.candidate_tasks.add(task)
        task.add_done_callback(self.witness_tasks.discard)
        task.add_done_callback(self.parent.candidate_tasks.discard)
        return True

    async def send(self, step):
        """Send a fixed native mention once; never retry an uncertain receipt."""
        async with self.send_lock:
            self.check()
            if step in self.results:
                return self.results[step]
            if (
                (step != len(self.results) and not (step == 2 and not self.results))
                or 2 in self.results
                or any(r["status"] != "accepted" for r in self.results.values())
                or self.group_ai.tasks
                or self.parent.chat_count
                or self.window.remaining <= 0
                or self.window.outbound_remaining <= 0
                or time.time()
                - self.group_ai.configs[(RECEIVER, GROUP)]["last_attempt"]
                < 60
            ):
                raise OpenError("test_group_ai_send_order")
            await self.roster(self.adapter, GROUP)
            self.check()
            sender = self.sender
            key = f"test-warm/{self.operation}/{step}"
            prefix = "@小满 "
            text = prefix + QUESTIONS[step]
            message = _text_message(
                sender.config["id"],
                SENDER,
                GROUP,
                key,
                text,
                [
                    {
                        "uid": int(self.receiver.nim_account),
                        "nick": "小满",
                        "start": 0,
                        "end": len(prefix.encode("utf-16-le")) // 2,
                    }
                ],
                False,
                int(time.time() * 1000),
            )
            async with sender.send_lock:
                self.check()
                nonce, state = await sender.ledger.reserve(
                    key, json.dumps([GROUP, text])
                )
                if state != "new":
                    raise OpenError("test_group_ai_historical")
                result = self.results[step] = {"step": step, "status": "rejected"}
                try:
                    self.check()
                    attachment = wire.seal_message(
                        sender.business.deployment.key("message_key"),
                        message,
                        int(nonce),
                        secrets.randbits(64) or 1,
                        int(time.time()),
                    )
                    self.check()
                    self.window.outbound_remaining -= 1
                    self.window.outbound_keys.add(key)
                    result["status"] = "unknown"
                    code, body = await sender.nim.request(
                        8,
                        2,
                        wire.properties(
                            [
                                (0, b"1"),
                                (1, self.team.encode()),
                                (8, b"100"),
                                (10, attachment.encode()),
                                (11, message.client_id.encode()),
                            ]
                        ),
                    )
                    if code != 200:
                        result["status"] = "rejected"
                    else:
                        fields, end = wire.read_properties(body)
                        if (
                            end != len(body)
                            or not fields.get(12, b"").isdigit()
                            or fields.get(11, message.client_id.encode())
                            != message.client_id.encode()
                        ):
                            raise OpenError("test_group_ai_receipt")
                        result.update(status="accepted", message_id=fields[12].decode())
                finally:
                    await sender.ledger.finish(
                        key, result["status"], result.get("message_id", "")
                    )
            return dict(result)

    async def expire(self):
        await asyncio.sleep(max(0, self.window.expires - time.monotonic()))
        await self.close()

    async def close(self):
        self.closed, self.active = True, False
        await self.group_ai.close()
        witnesses = list(self.witness_tasks)
        for task in witnesses:
            task.cancel()
        if witnesses:
            await asyncio.gather(*witnesses, return_exceptions=True)
        if self.expiry and self.expiry is not asyncio.current_task():
            self.expiry.cancel()
            await asyncio.gather(self.expiry, return_exceptions=True)
        if getattr(self.parent.group_ai, "test_fixture", None) is self:
            self.parent.group_ai.test_fixture = None


async def control_group_ai(service, actor, payload):
    """Manage the fixed dashboard-only fixture, never production AI enablement.

    Args:
        service: Existing integrated service and its online native adapters.
        actor: Authenticated dashboard principal, not a chat-supplied identity.
        payload: Fixed operation ID and one enumerated action, never message text.

    Returns:
        Test status, bounded accounting metadata or one durable send receipt.

    Raises:
        OpenError: If the actor, native roster, window or authorization changed.
    """
    if (
        not actor.startswith("dashboard/")
        or not actor[10:]
        or set(payload) != {"operation_id", "action"}
        or not isinstance(payload["operation_id"], str)
        or not re.fullmatch(r"WSL_TEST_WARM_[0-9a-f]{32}", payload["operation_id"])
        or not isinstance(payload["action"], str)
        or payload["action"]
        not in {"open", "status", "close", "send_1", "send_2", "send_3"}
    ):
        raise OpenError("test_group_ai_arguments")
    operation, action = payload["operation_id"], payload["action"]
    fixture = getattr(service.group_ai, "test_fixture", None)
    if fixture is not None and (
        fixture.actor != actor or fixture.operation != operation
    ):
        raise OpenError("test_group_ai_busy")
    if action == "close":
        if fixture:
            await fixture.close()
            if fixture.receiver.test_window is fixture.window:
                fixture.receiver.test_window = None
            if getattr(fixture.sender, "test_command_window", None) is fixture.window:
                fixture.sender.test_command_window = None
        return {"test_only": True, "status": "closed"}
    if action.startswith("send_"):
        if not fixture:
            raise OpenError("test_group_ai_window")
        return await fixture.send(int(action[-1]) - 1)
    if action == "status" or fixture is not None:
        if not fixture:
            return {"test_only": True, "status": "closed"}
        fixture.check()
        with fixture.store.connect() as db:
            charges = [
                dict(row)
                for row in db.execute(
                    "SELECT request,state,charged_micro FROM ai_charges ORDER BY created DESC LIMIT 50"
                )
            ]
        return {
            "test_only": True,
            "status": "open",
            "operation_id": operation,
            "tasks": len(fixture.group_ai.tasks),
            "charges": charges,
            "sent": list(fixture.results.values()),
        }
    adapters = {str(a.account): a for a in service.adapters()}
    receiver, sender = adapters.get(RECEIVER), adapters.get(SENDER)
    window = getattr(receiver, "test_window", None)
    if (
        not service.integrated
        or receiver is None
        or sender is None
        or not isinstance(window, TestWindow)
        or window.receiver is not receiver
        or window.sender is not sender
        or window.groups != [GROUP]
        or window.scopes != ["group_ai"]
        or window.accepted
        or not 1 <= window.remaining <= 3
        or not window.active()
        or GROUP not in receiver.enabled_groups
        or (RECEIVER, GROUP) in service.group_ai.tasks
    ):
        raise OpenError("test_group_ai_window")
    directory = await receiver.get_moderation_members(GROUP)
    members = {str(row["userId"]): row for row in directory.get("groupMemberInfo", [])}
    if (
        not directory.get("complete")
        or len(directory.get("groupMemberInfo", [])) != 3
        or set(members) != MEMBERS
        or str(members[RECEIVER].get("nimId")) != str(receiver.nim_account)
        or str(members[SENDER].get("nimId")) != str(sender.nim_account)
    ):
        raise OpenError("test_group_ai_roster")
    # Roster retrieval yields; another dashboard request may have opened a fixture.
    if getattr(service.group_ai, "test_fixture", None) is not None:
        raise OpenError("test_group_ai_busy")
    fixture = GroupAITest(service, actor, operation, receiver, sender, window, members)
    service.group_ai.test_fixture = fixture
    try:
        await fixture.roster(fixture.adapter, GROUP)
        await fixture.initialize()
        fixture.expiry = asyncio.create_task(fixture.expire())
    except BaseException:
        await fixture.close()
        raise
    return {
        "test_only": True,
        "status": "open",
        "operation_id": operation,
        "maximum_turns": window.remaining,
        "monthly_trial_budget_micro": 500000,
    }
