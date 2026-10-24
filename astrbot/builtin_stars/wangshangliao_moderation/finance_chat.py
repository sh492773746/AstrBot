"""Fixed private financial navigation, isolated from all model conversations."""

import asyncio
import json
import re
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from astrbot import logger
from astrbot.core.platform.sources.wangshangliao.database import account_guard
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError

from .cashback_finance import FinanceActor
from .cashback_store import BEIJING, WRITE_GUARD, CashbackError
from .private_invitation import private_query_guard
from .text import plain_text, redact_reply

FOOTER = "\n0 返回上一步；取消 退出。"
STATES = ("pending", "verifying", "paid", "unassigned", "void", "no_reward")
STATE_NAMES = ("待结账", "核验中", "已结账", "未归属", "已作废", "不返现")
OPERATIONS = {
    "1": ("pay", "登记已付款"),
    "2": ("correct", "撤回结账登记"),
    "3": ("ordinary", "改普通成员价"),
    "4": ("agent", "改代理商价"),
    "5": ("assign", "核对邀请归属"),
    "6": ("exception", "异常付款补记"),
}


class FinanceChat:
    """Own short-lived sender-scoped navigation over the shared ledger.

    Args:
        owner: Integrated open service.
        service: Existing cashback domain service.
    """

    def __init__(self, owner, service):
        self.owner, self.service = owner, service
        self.boot = uuid.uuid4().hex
        self.sessions = OrderedDict()
        self.locks = {}
        self.test_service = None
        self.test_until = 0
        self.test_requests = OrderedDict()

    def observe(self, account, session, payload):
        """Invalidate unsubmitted work before a private sender's lane can wait.

        Args:
            account: Authenticated receiving account.
            session: Authenticated private transport session.
            payload: Newly admitted, decoded native envelope.
        """
        if not session.startswith("private/") or payload.get(
            "text", ""
        ).strip() not in {
            "0",
            "取消",
            "结账",
            "/结账",
            "菜单",
            "帮助",
            "私聊菜单",
            "/chat",
            "/exit",
        }:
            return
        for key, state in self.sessions.items():
            if (
                key[0] == account
                and key[1] == "nim/" + str(payload.get("transport_peer", ""))
                and state.get("ingress_session") == account + "/" + session
                and state.get("uid") == payload.get("sender")
            ):
                state["generation"] = state.get("generation", 0) + 1

    async def reply(self, event, text):
        """Send bounded financial text using the existing native sender.

        Args:
            event: Original verified message event.
            text: Server-generated financial response.
        """
        event.set_extra("wsl_plain_command", True)
        event.set_extra("wsl_keep_reply", True)
        event.set_extra("wsl_command_result", True)
        key = self.owner.navigation.key(event)
        state = self.sessions.get(key)
        if state and state["service"] is not self.service:
            text = "【测试账本，不代表真实付款】\n" + text
        if state and event.is_private_chat():
            identity = event.actor_identity
            expected = tuple(
                (r["group_id"], r["version"])
                for r in await asyncio.to_thread(
                    state["service"].store.finance_grants,
                    identity.account,
                    identity.require_verified(),
                    identity.peer,
                )
            )

            async def delivery():
                private_query_guard(event)()
                latest = await asyncio.to_thread(
                    state["service"].store.finance_grants,
                    identity.account,
                    identity.require_verified(),
                    identity.peer,
                )
                if expected != tuple((r["group_id"], r["version"]) for r in latest):
                    raise ProtocolError("finance_reply_scope_changed")
                if state.get("group") and state["group"] not in {
                    r["group_id"] for r in latest
                }:
                    raise ProtocolError("finance_reply_scope_changed")

            event.set_extra("_wsl_invitation_delivery_guard", delivery)
        await event.send(event.plain_result(text))

    def native_guard(self, event, actor, service, *, write=False, cancelled=None):
        """Freeze account identity and cancel unsubmitted paused-group actions.

        Args:
            event: Original native private event.
            actor: Exact persisted financial authorization.
            service: Bound live or test ledger.
            write: Require active group and service for a financial mutation.
            cancelled: Optional signal for a cancelled database worker.

        Returns:
            A synchronous database admission and commit guard.
        """
        private = private_query_guard(event)
        account = str(event.platform.account)
        completion = getattr(event, "processing_completion", None)
        key = self.owner.navigation.key(event)
        state = self.sessions.get(key)
        generation = state.get("generation", 0) if state else None
        account_generation = event.platform.config.get("_business_revision")
        tenant_at_start = self.owner.tenants.get((actor.account, actor.group), {})
        tenant_version = tuple(tenant_at_start.get(k) for k in ("version", "epoch"))

        def check():
            private()
            if (
                event.actor_identity.require_verified() != actor.uid
                or event.actor_identity.peer != actor.peer
                or account != actor.account
                or not self.owner.active
                or event.platform.config.get("_business_revision") != account_generation
                or cancelled is not None
                and cancelled.is_set()
                or completion is not None
                and completion.cancelled()
            ):
                raise CashbackError("finance_forbidden", "身份或服务状态已变化。", 403)
            if (
                write
                and state is not None
                and (
                    self.sessions.get(key) is not state
                    or state.get("generation", 0) != generation
                )
            ):
                raise CashbackError(
                    "finance_cancelled", "未提交的结账操作已取消，没有登记付款。", 409
                )
            if service is not self.service and (
                service is not self.test_service or time.time() >= self.test_until
            ):
                raise CashbackError("test_expired", "测试结账窗口已结束。", 409)
            if write:
                tenant = self.owner.tenants.get((actor.account, actor.group))
                persisted = self.owner.store.tenant(actor.account, actor.group)
                if (
                    self.owner.settings["paused"]
                    or self.owner.store.settings()["paused"]
                    or self.owner.capacity_paused
                    or not tenant
                    or not persisted
                    or not tenant["active"]
                    or not tenant["present"]
                    or tenant["paused"]
                    or not persisted["active"]
                    or not persisted["present"]
                    or persisted["paused"]
                    or tuple(persisted.get(k) for k in ("version", "epoch"))
                    != tenant_version
                    or tuple(tenant.get(k) for k in ("version", "epoch"))
                    != tenant_version
                    or (actor.account, actor.group) in self.owner.suspended_groups
                    or actor.group not in service.selected_groups()
                ):
                    raise CashbackError(
                        "finance_read_only",
                        "本群业务已暂停或机器人已离群，仅可查历史。",
                        409,
                    )

        return account_guard(event.platform, check)

    async def actor(self, event, state):
        """Resolve the selected persisted financial grant without role inference.

        Args:
            event: Native sender.
            state: Sender-bound navigation.

        Returns:
            A typed, exactly scoped financial identity.
        """
        identity = event.actor_identity
        rows = await asyncio.to_thread(
            state["service"].store.finance_grants,
            identity.account,
            identity.require_verified(),
            identity.peer,
        )
        row = next((r for r in rows if r["group_id"] == state.get("group")), None)
        if not row:
            raise CashbackError("finance_forbidden", "没有此群的结账权限。", 403)
        return FinanceActor(
            row["account"], row["uid"], row["peer"], row["group_id"], row["version"]
        )

    async def cancel(self, event, state):
        """Invalidate previews before leaving or navigating away.

        Args:
            event: Original native sender.
            state: Current private state.
        """
        state["generation"] = state.get("generation", 0) + 1
        state.pop("confirmation", None)
        identity = event.actor_identity
        principal = FinanceActor(
            identity.account, identity.claimed_uid, identity.peer, "", 0
        )
        await asyncio.to_thread(
            state["service"].store.finance_cancel, principal, state["session"]
        )

    async def handle(self, event, text):
        """Consume financial input before menu routing and model admission.

        Args:
            event: Actual native event, not a fabricated AI tool request.
            text: Exact original message text.

        Returns:
            Whether this event belongs to the financial workflow.
        """
        entry = text in {"结账", "结账结果"}
        key = self.owner.navigation.key(event)
        state = self.sessions.get(key)
        if not entry and state is None:
            return False
        if not event.is_private_chat():
            if entry and self.owner.admit_command(event):
                await self.reply(
                    event, "请私聊我发送“结账”，群里不展示账目和收款信息。"
                )
            return True
        if key is None:
            return True
        # Exit state changes are immediate even while reply rate limited.
        if text in {"取消", "/exit", "/chat", "菜单", "帮助", "私聊菜单"}:
            if state:
                await self.cancel(event, state)
                self.sessions.pop(key, None)
            if text == "取消":
                if self.owner.admit_command(event):
                    await self.reply(event, "已退出结账，未确认的操作没有执行。")
                return True
            return False
        admitted = self.owner.admit_command(event)
        lock = None
        try:
            if entry:
                await self.owner.end_chat(key[0], key[1], event=event)
                self.owner.navigation.dismiss(event)
            if not admitted:
                return True
            lock = self.locks.get(key)
            if lock is not None and lock.locked():
                return True
            if lock is None:
                if len(self.locks) >= 256:
                    await self.reply(event, "结账服务繁忙，请稍后再试。")
                    return True
                lock = self.locks[key] = asyncio.Lock()
            async with lock:
                await self.owner.verify_actor(event)
                private_query_guard(event)()
                if state and state["expires"] <= time.monotonic() and not entry:
                    await self.cancel(event, state)
                    self.sessions.pop(key, None)
                    await self.reply(event, "结账菜单已过期，请重新发送“结账”。")
                    return True
                if entry:
                    if state and text == "结账结果" and state.get("group"):
                        actor = await self.actor(event, state)
                        self.native_guard(event, actor, state["service"])()
                        result = await asyncio.to_thread(
                            state["service"].store.finance_result, actor
                        )
                        await self.reply(event, self.receipt(result))
                        return True
                    if state:
                        await self.cancel(event, state)
                    if len(self.sessions) >= 256:
                        oldest_key, _ = self.sessions.popitem(last=False)
                        self.locks.pop(oldest_key, None)
                        # Expired database previews are time-bound independently.
                    service = (
                        self.test_service
                        if self.test_service is not None
                        and time.time() < self.test_until
                        and key[0] == "43506527"
                        and event.get_sender_id() == "31846829"
                        else self.service
                    )
                    state = {
                        "service": service,
                        "session": json.dumps(key),
                        "ingress_session": str(event.message_obj.session_id),
                        "uid": event.actor_identity.require_verified(),
                        "screen": "groups",
                        "expires": time.monotonic() + 600,
                        "period": "all",
                        "search": "",
                        "page": 1,
                    }
                    identity = event.actor_identity
                    state["grants"] = await asyncio.to_thread(
                        service.store.finance_grants,
                        identity.account,
                        identity.require_verified(),
                        identity.peer,
                    )
                    self.sessions[key] = state
                    result = await self.groups(state)
                elif text == "0":
                    await self.cancel(event, state)
                    previous = state["screen"]
                    state["screen"] = {
                        "groups": "groups",
                        "root": "groups",
                        "sections": "root",
                        "members": "sections",
                        "action": "members",
                        "reason": "members",
                        "confirm": "members",
                        "assign": "members",
                        "filter": "root",
                        "settings": "root",
                        "setting_value": "settings",
                        "risk": "root",
                        "risk_reason": "risk",
                        "selection": "members",
                    }.get(state["screen"], "root")
                    if previous in {"confirm", "reason"}:
                        state["screen"] = {
                            "configure": "settings",
                            "risk_release": "risk",
                        }.get(state.get("operation"), "members")
                    result = await self.render(event, state)
                else:
                    result = await self.advance(event, state, text)
                state["expires"] = time.monotonic() + 600
                await self.reply(event, result)
        except CashbackError as exc:
            if exc.code in {"finance_forbidden", "forbidden"} and state:
                state["generation"] = state.get("generation", 0) + 1
                self.sessions.pop(key, None)
            if admitted:
                await self.reply(event, exc.text)
        except ProtocolError as exc:
            if state:
                state["generation"] = state.get("generation", 0) + 1
                self.sessions.pop(key, None)
            if admitted:
                await self.reply(
                    event,
                    "旺商聊暂不允许机器人核实成员（1400），没有执行结账操作。"
                    "请让机器人维护者核对官方App中对应账号的二级安全码设置；"
                    "若已设置仍报错，请核对登录状态或联系旺商聊客服。不要把安全码发给机器人。"
                    if str(exc) == "secondary_security_code_required"
                    else "暂时无法核实你的账号身份，没有执行结账操作，请稍后再试。",
                )
        except Exception as exc:
            logger.warning("Cashback chat failed type=%s", type(exc).__name__)
            if admitted:
                await self.reply(
                    event, "结账暂不可用，没有自动重试；请稍后发送“结账结果”核对。"
                )
        finally:
            if lock is not None and not lock.locked():
                self.locks.pop(key, None)
        return True

    async def groups(self, state):
        """Render authorized groups, including paused history.

        Args:
            state: Exact private navigation.

        Returns:
            Named group choices.
        """
        lines = ["【选择结账群】"]
        for index, row in enumerate(state["grants"], 1):
            group = await asyncio.to_thread(
                state["service"].store.get_group, row["group_id"]
            )
            name = (
                group["name"]
                if group
                else self.service.group_name(
                    next(
                        a
                        for a in self.owner.adapters()
                        if str(a.account) == row["account"]
                    ),
                    row["group_id"],
                )
            )
            lines.append(f"{index}. {redact_reply(plain_text(name))}")
        return "\n".join(lines) + (
            FOOTER if len(lines) > 1 else "\n暂无获授权的结账群。"
        )

    async def render(self, event, state):
        """Render a bounded screen after fresh financial authorization.

        Args:
            event: Native caller.
            state: Private selection and filters.

        Returns:
            One complete text screen.
        """
        if state["screen"] == "groups":
            return await self.groups(state)
        actor = await self.actor(event, state)
        self.native_guard(event, actor, state["service"])()
        store = state["service"].store
        screen = state["screen"]
        if screen == "root":
            with store.connect() as db:
                person = actor.authorize(db)
            state["role"] = "finance_admin" if person["role"] == "admin" else "cashier"
            return (
                "【结账】\n"
                + "\n".join(f"{i}. {n}" for i, n in enumerate(STATE_NAMES, 1))
                + "\n7. 姓名和时间筛选\n8. 最近结账结果"
                + (
                    "\n9. 返现设置\n10. 风控复核"
                    if state["role"] == "finance_admin"
                    else ""
                )
                + FOOTER
            )
        if screen in {"sections", "members"}:
            kwargs = {
                "state": state["state"],
                "search": state["search"],
                "period": state["period"],
                "start_at": state.get("start_at"),
                "end_at": state.get("end_at"),
                "pagination": "inviters",
                "page": state["page"],
            }
            if screen == "members":
                kwargs.update(
                    inviter=state["inviter"], member_page=state.get("member_page", 1)
                )
            data = await asyncio.to_thread(
                store.list_entries, actor, actor.group, **kwargs
            )
            state["data"] = data
            if screen == "sections":
                state["sections"] = data["sections"]
                return (
                    f"【{STATE_NAMES[STATES.index(state['state'])]}】"
                    f" 共{data['total']}笔 ¥{data['amount'] / 100:.2f}\n"
                    + "\n".join(
                        f"{i}. {redact_reply(plain_text(r['inviter_name'])) or '未归属'}"
                        f"（账号{r['inviter'] or '待核对'}） {r['count']}人 ¥{r['amount'] / 100:.2f}"
                        for i, r in enumerate(data["sections"], 1)
                    )
                    + f"\n第{data['page']}/{data['page_count']}页；回复 下一页 或 上一页。"
                    + FOOTER
                )
            all_rows = data["items"]
            subpage = state.get("subpage", 0)
            state["rows"] = all_rows[subpage * 10 : (subpage + 1) * 10]
            state["selection"] = []
            return (
                "【成员明细】\n"
                + "\n".join(
                    f"{i}. {redact_reply(plain_text(r['member_name']))}"
                    f"（账号{r['member']}） ¥{r['amount'] / 100:.2f}"
                    f" {'已发言' if r['first_spoke_at'] else '未发言'}"
                    for i, r in enumerate(state["rows"], 1)
                )
                + "\n输入序号选择，如 1 3；或回复“本页”“全部可结账”。"
                + "\n回复 下一页／上一页 翻看成员。"
                + FOOTER
            )
        if screen == "settings":
            with store.connect() as db:
                actor.authorize(db, admin=True)
            row = await asyncio.to_thread(store.get_group, actor.group)
            if row is None:
                adapter = next(
                    a for a in self.owner.adapters() if str(a.account) == actor.account
                )
                row = {
                    "id": actor.group,
                    "bot_id": adapter.config["id"],
                    "account": actor.account,
                    "name": self.service.group_name(adapter, actor.group),
                    "rate": 0,
                    "agent_rate": None,
                    "enabled": False,
                    "version": 0,
                    "require_speech": False,
                    "agent_only": False,
                    "contact": "",
                }
                state["config"] = dict(row)
                state["screen"] = "setup_contact"
                return (
                    "【首次设置返现】\n请填写本群公开的结账联系方式（1至200字）。"
                    + FOOTER
                )
            state["config"] = dict(row)
            agent_price = (
                "未设置"
                if row["agent_rate"] is None
                else f"¥{row['agent_rate'] / 100:.2f}"
            )
            return (
                f"【返现设置】\n返现：{'开启' if row['enabled'] else '暂停'}"
                f"\n普通 ¥{row['rate'] / 100:.2f}；代理 {agent_price}"
                f"\n发言要求：{'开' if row['require_speech'] else '关'}"
                f"；仅代理商：{'开' if row['agent_only'] else '关'}"
                f"\n结账联系：{redact_reply(plain_text(row['contact']))}"
                "\n1. 开启返现\n2. 暂停返现\n3. 普通价格\n4. 代理价格"
                "\n5. 切换发言要求\n6. 切换仅代理商返现\n7. 结账联系方式" + FOOTER
            )
        if screen == "risk":
            cases = await asyncio.to_thread(store.risk_cases, actor, actor.group)
            state["risks"] = list(
                {r["inviter"]: r for r in cases if r["blocked_at"]}.values()
            )
            return (
                "【风控复核】\n"
                + "\n".join(
                    f"{i}. {redact_reply(plain_text(r['inviter_name']))}（账号{r['inviter']}）"
                    for i, r in enumerate(state["risks"], 1)
                )
                + "\n选择邀请人并填写解除依据。"
                + FOOTER
            )
        return "请按当前提示操作。" + FOOTER

    async def advance(self, event, state, text):
        """Apply deterministic navigation without accepting arbitrary financial IDs.

        Args:
            event: Native private caller.
            state: Exact current menu.
            text: Bounded user input.

        Returns:
            The next text screen or committed receipt.
        """
        service = state["service"]
        if state["screen"] == "groups":
            if not text.isdigit() or not 1 <= int(text) <= len(state["grants"]):
                return await self.groups(state)
            await self.cancel(event, state)
            state["group"] = state["grants"][int(text) - 1]["group_id"]
            state["screen"] = "root"
            return await self.render(event, state)
        actor = await self.actor(event, state)
        self.native_guard(event, actor, service)()
        screen = state["screen"]
        previous = state.get("confirmation")
        if screen == "root" and previous:
            command = (
                "确认已付款"
                if previous["payload"]["operation"] in {"pay", "exception"}
                else "确认修改"
            )
            if text == command + " " + previous["code"]:
                result = await asyncio.to_thread(
                    service.store.finance_result, actor, previous["id"]
                )
                if result and result["status"] == "completed":
                    return self.receipt(result)
        if screen.startswith("setup_"):
            if state["role"] != "finance_admin":
                raise CashbackError("forbidden", "首次设置需要本群财务管理员。", 403)
            if screen == "setup_contact":
                if not 1 <= len(text) <= 200 or not text.isprintable():
                    return "联系方式须为1至200字单行文字。" + FOOTER
                state["config"]["contact"] = text
                state["screen"] = "setup_rate"
                return "请输入普通成员每位金额（元，最多两位小数）。" + FOOTER
            if screen in {"setup_rate", "setup_agent"}:
                if screen == "setup_agent" and text == "不设置":
                    state["config"]["agent_rate"] = None
                else:
                    if not re.fullmatch(
                        r"(?:0|[1-9][0-9]{0,5})(?:\.[0-9]{1,2})?", text
                    ):
                        return "请输入有效金额，最多两位小数。" + FOOTER
                    amount = int(Decimal(text) * 100)
                    if not 1 <= amount <= 99999999:
                        return "金额须为0.01至999999.99元。" + FOOTER
                    state["config"][
                        "rate" if screen == "setup_rate" else "agent_rate"
                    ] = amount
                if screen == "setup_rate":
                    state["screen"] = "setup_agent"
                    return "请输入代理商每位金额；暂不定价可回复“不设置”。" + FOOTER
                state["screen"] = "setup_enabled"
                return (
                    "核验期为3小时，默认不要求发言、不限制仅代理商。\n1. 开启返现\n2. 仅保存，保持暂停"
                    + FOOTER
                )
            if text not in {"1", "2"}:
                return "请选择 1 开启，或 2 保持暂停。" + FOOTER
            state["config"]["enabled"] = text == "1"
            state.update(operation="configure", screen="reason")
            return "请填写本次开通或保存的原因。" + FOOTER
        if screen == "confirm":
            confirmation = state.get("confirmation")
            if not confirmation:
                return "确认已失效，请重新发送“结账”。"
            expected = (
                "确认已付款"
                if confirmation["payload"]["operation"] in {"pay", "exception"}
                else "确认修改"
            )
            match = re.fullmatch(re.escape(expected) + r" ([0-9]{6})", text)
            if not match:
                return f"尚未执行，请回复“{expected} 确认码”；0 放弃。" + FOOTER
            result = await self.execute(event, state, actor, match[1])
            state["screen"] = "root"
            return self.receipt(
                {
                    "id": confirmation["id"],
                    "status": "completed",
                    "result": json.dumps(result, ensure_ascii=False),
                }
            )
        if screen == "root":
            if text in {str(i) for i in range(1, 7)}:
                state.update(screen="sections", state=STATES[int(text) - 1], page=1)
            elif text == "7":
                state["screen"] = "filter"
                return (
                    "请输入：姓名 pp；或 时间 全部／本月／今天／本小时"
                    "\n也可输入：时间 2026-10 或 时间 2026-10-17 或 时间 2026-10-17 08"
                    "\n清除姓名：姓名 全部" + FOOTER
                )
            elif text == "8":
                return self.receipt(
                    await asyncio.to_thread(service.store.finance_result, actor)
                )
            elif text in {"9", "10"} and state["role"] == "finance_admin":
                state["screen"] = "settings" if text == "9" else "risk"
            else:
                return await self.render(event, state)
        elif screen == "filter":
            if text.startswith("姓名 ") and len(text) <= 83:
                state["search"] = "" if text[3:] == "全部" else text[3:]
            elif text.startswith("时间 "):
                value = text[3:]
                state["period"] = {
                    "全部": "all",
                    "本月": "month",
                    "今天": "day",
                    "本小时": "hour",
                }.get(value, "custom")
                state.pop("start_at", None)
                state.pop("end_at", None)
                if state["period"] == "custom":
                    fmt = {7: "%Y-%m", 10: "%Y-%m-%d", 13: "%Y-%m-%d %H"}.get(
                        len(value)
                    )
                    try:
                        start = (
                            datetime.strptime(value, fmt).replace(tzinfo=BEIJING)
                            if fmt
                            else None
                        )
                        if start is None:
                            raise ValueError
                    except ValueError as exc:
                        raise CashbackError(
                            "invalid_query", "时间格式不正确，请按示例填写。"
                        ) from exc
                    end = (
                        start.replace(year=start.year + 1, month=1)
                        if len(value) == 7 and start.month == 12
                        else start.replace(month=start.month + 1)
                        if len(value) == 7
                        else start + timedelta(days=1)
                        if len(value) == 10
                        else start + timedelta(hours=1)
                    )
                    state.update(start_at=start.timestamp(), end_at=end.timestamp())
            else:
                return "请填写 姓名 内容 或 时间 内容。" + FOOTER
            state.update(screen="root", page=1)
        elif screen == "sections":
            if text in {"下一页", "上一页"}:
                state["page"] = max(1, state["page"] + (1 if text == "下一页" else -1))
            elif text.isdigit() and 1 <= int(text) <= len(state["sections"]):
                await self.cancel(event, state)
                state.update(
                    screen="members",
                    inviter=state["sections"][int(text) - 1]["inviter"],
                    member_page=1,
                    subpage=0,
                )
        elif screen == "members":
            if text in {"下一页", "上一页"}:
                if text == "下一页":
                    if len(state["rows"]) == 10:
                        state["subpage"] += 1
                        if state["subpage"] == 10:
                            state["subpage"] = 0
                            state["member_page"] += 1
                elif state.get("subpage"):
                    state["subpage"] -= 1
                elif state["member_page"] > 1:
                    state["member_page"] -= 1
                    state["subpage"] = 9
            else:
                await self.cancel(event, state)
                if text == "全部可结账":
                    rows = await self.payable_rows(actor, state)
                    state["limited"] = len(rows) > 100
                    rows = rows[:100]
                elif text == "本页":
                    rows = state["rows"]
                elif re.fullmatch(r"(?:[1-9]|10)(?: (?:[1-9]|10))*", text):
                    indices = sorted({int(i) - 1 for i in text.split()})
                    if any(i >= len(state["rows"]) for i in indices):
                        return "成员序号超出本页范围。" + FOOTER
                    rows = [state["rows"][i] for i in indices]
                else:
                    return "请选择本页成员序号，或回复 本页／全部可结账。" + FOOTER
                if not rows:
                    return "当前没有符合选择条件的记录。" + FOOTER
                state["selection"] = [
                    {"id": r["id"], "version": r["version"]} for r in rows
                ]
                state["screen"] = "action"
                return (
                    f"已选择{len(rows)}笔"
                    + (
                        "（仅本次最早100笔，不会自动处理后续记录）"
                        if state.pop("limited", False)
                        else ""
                    )
                    + "\n"
                    + "\n".join(
                        f"{i}. {label}"
                        for i, (op, label) in OPERATIONS.items()
                        if state["role"] == "finance_admin"
                        or op not in {"assign", "exception"}
                    )
                    + FOOTER
                )
        elif screen == "action":
            if text not in OPERATIONS:
                return "请选择操作序号。" + FOOTER
            operation = OPERATIONS[text][0]
            if (
                operation in {"assign", "exception"}
                and state["role"] != "finance_admin"
            ):
                raise CashbackError("forbidden", "此项需要本群财务管理员。", 403)
            state["operation"] = (
                "reprice" if operation in {"ordinary", "agent"} else operation
            )
            state["category"] = operation if state["operation"] == "reprice" else None
            if operation == "pay":
                return await self.preview(event, state, actor, "")
            if operation == "assign":
                options = await asyncio.to_thread(
                    service.store.inviter_options, actor, actor.group
                )
                state.update(screen="assign", inviters=options)
                return (
                    "【选择已有邀请人】\n"
                    + "\n".join(
                        f"{i}. {redact_reply(plain_text(r['name']))}（账号{r['uid']}）"
                        for i, r in enumerate(options[:100], 1)
                    )
                    + FOOTER
                )
            state["screen"] = "reason"
            return "请填写修改原因或核验依据（2至300字）。" + FOOTER
        elif screen == "assign":
            if not text.isdigit() or not 1 <= int(text) <= min(
                100, len(state["inviters"])
            ):
                return "请选择当前列表中的邀请人。" + FOOTER
            state["assigned_inviter"] = state["inviters"][int(text) - 1]["uid"]
            state["screen"] = "reason"
            return "请填写邀请归属核验依据（2至300字）。" + FOOTER
        elif screen == "reason":
            if not 2 <= len(text) <= 300 or not text.isprintable():
                return "请填写2至300字单行原因。" + FOOTER
            return await self.preview(event, state, actor, text)
        elif screen == "settings":
            if state["role"] != "finance_admin":
                raise CashbackError("forbidden", "没有本群财务设置权限。", 403)
            if text not in {str(i) for i in range(1, 8)}:
                return await self.render(event, state)
            config = state["config"]
            state["operation"] = "configure"
            state["config_field"] = {
                "3": "rate",
                "4": "agent_rate",
                "7": "contact",
            }.get(text)
            if state["config_field"]:
                state["screen"] = "setting_value"
                return (
                    "请输入结账联系方式。" + FOOTER
                    if text == "7"
                    else "请输入每位成员金额（元，最多两位小数）。" + FOOTER
                )
            if text in {"1", "2"}:
                config["enabled"] = text == "1"
            else:
                field = "require_speech" if text == "5" else "agent_only"
                config[field] = not bool(config[field])
            state["screen"] = "reason"
            return "请填写本次返现设置变更原因。" + FOOTER
        elif screen == "setting_value":
            field = state["config_field"]
            if field == "contact":
                if not 1 <= len(text) <= 200 or not text.isprintable():
                    return "联系方式须为1至200字单行文字。" + FOOTER
                state["config"][field] = text
            else:
                try:
                    amount = Decimal(text)
                except InvalidOperation:
                    amount = Decimal(0)
                if (
                    not amount.is_finite()
                    or not Decimal("0.01") <= amount <= Decimal("999999.99")
                    or amount * 100 != int(amount * 100)
                ):
                    return "金额须为0.01至999999.99元，最多两位小数。" + FOOTER
                state["config"][field] = int(amount * 100)
            state["screen"] = "reason"
            return "请填写本次返现设置变更原因。" + FOOTER
        elif screen == "risk":
            if not text.isdigit() or not 1 <= int(text) <= len(state["risks"]):
                return "请选择需要复核的邀请人。" + FOOTER
            state.update(
                operation="risk_release",
                risk_inviter=state["risks"][int(text) - 1]["inviter"],
                screen="reason",
            )
            return "请填写解除风控的复核依据。" + FOOTER
        return await self.render(event, state)

    async def payable_rows(self, actor, state):
        """Select no more than 101 oldest matching payable rows, for truncation.

        Args:
            actor: Scoped financial identity.
            state: Current inviter and filters.

        Returns:
            Bounded eligible records, never a cross-inviter batch.
        """
        result = []
        # Domain reads use bounded pages; stop as soon as the batch bound is known.
        for page in range(1, 102):
            data = await asyncio.to_thread(
                state["service"].store.list_entries,
                actor,
                actor.group,
                state="pending",
                pagination="inviters",
                inviter=state["inviter"],
                member_page=page,
                period=state["period"],
                search=state["search"],
                start_at=state.get("start_at"),
                end_at=state.get("end_at"),
            )
            for row in data["items"]:
                if (
                    row["verification"] == "verified"
                    and row["present"]
                    and not row["ever_left"]
                    and row["pricing_status"] == "priced"
                    and row["hold_until"] <= time.time()
                    and not row["review_reason"]
                    and not row["risk_blocked"]
                    and (not data["group"]["require_speech"] or row["first_spoke_at"])
                ):
                    result.append(row)
            if (
                len(result) >= 101
                or not data["sections"]
                or page >= data["sections"][0]["page_count"]
            ):
                break
        return sorted(result, key=lambda r: (r["first_seen"], r["id"]))[:101]

    async def preview(self, event, state, actor, reason):
        """Generate the exact confirmation through the shared ledger.

        Args:
            event: Native caller.
            state: Selected mutation.
            actor: Scoped grant.
            reason: Explicit change reason, except normal payment.

        Returns:
            Human-readable confirmation with a two-minute code.
        """
        generation = state.get("generation", 0)
        guard = self.native_guard(event, actor, state["service"], write=True)
        guard()
        operation = state["operation"]
        payload = {"operation": operation, "reason": reason}
        if operation == "configure":
            config = state["config"]
            payload["config"] = {
                k: config[k]
                for k in (
                    "id",
                    "bot_id",
                    "account",
                    "name",
                    "contact",
                    "rate",
                    "agent_rate",
                    "enabled",
                    "version",
                    "require_speech",
                    "agent_only",
                )
            }
            payload["config"].update(
                enabled=bool(config["enabled"]),
                require_speech=bool(config["require_speech"]),
                agent_only=bool(config["agent_only"]),
                qualification_seconds=config.get("qualification_seconds", 10800),
            )
        elif operation == "risk_release":
            payload["inviter"] = state["risk_inviter"]
        else:
            payload["items"] = state["selection"]
            if operation == "assign":
                payload["inviter"] = state["assigned_inviter"]
            if operation == "reprice":
                payload["category"] = state["category"]
        token = WRITE_GUARD.set(guard)
        try:
            confirmation = await asyncio.to_thread(
                state["service"].store.finance_preview,
                actor,
                state["session"],
                self.boot,
                payload,
            )
        finally:
            WRITE_GUARD.reset(token)
        confirmation["generation"] = generation
        state.update(screen="confirm", confirmation=confirmation)
        snap = confirmation["payload"]
        group = await asyncio.to_thread(state["service"].store.get_group, actor.group)
        title = redact_reply(plain_text(group["name"])) if group else actor.group
        operation_name = {
            "pay": "登记已付款",
            "correct": "撤回结账登记",
            "reprice": "切换成员价格",
            "assign": "核对邀请归属",
            "exception": "补记实际付款",
            "configure": "修改返现设置",
            "risk_release": "解除返现风控",
        }[operation]
        body = f"【请核对】\n群：{title}\n操作：{operation_name}"
        if snap.get("items"):
            body += (
                f"\n收款邀请人：{redact_reply(plain_text(snap['recipient_name'])) or '未归属'}"
                f"（账号{snap['recipient'] or '待核对'}）\n本次{len(snap['items'])}笔，"
                f"合计¥{snap['amount'] / 100:.2f}\n成员："
                + "、".join(redact_reply(plain_text(n)) for n in snap["members"])
            )
            if "target_amount" in snap:
                body += f"\n切换后合计¥{snap['target_amount'] / 100:.2f}"
        elif operation == "configure":
            c = snap["config"]
            agent_price = (
                "未设置" if c["agent_rate"] is None else f"¥{c['agent_rate'] / 100:.2f}"
            )
            body += (
                f"\n返现{'开启' if c['enabled'] else '暂停'}；核验{c['qualification_seconds'] / 3600:g}小时"
                f"\n普通¥{c['rate'] / 100:.2f}；代理{agent_price}"
                f"\n发言要求{'开' if c['require_speech'] else '关'}；"
                f"仅代理商{'开' if c['agent_only'] else '关'}"
                f"\n联系方式：{redact_reply(plain_text(c['contact']))}"
            )
        else:
            body += f"\n复核邀请人账号：{snap['inviter']}"
        if operation in {"pay", "exception"}:
            body += "\n仅登记，不会转账；必须已经实际付款。"
        if operation == "correct":
            body += "\n仅撤回登记，不会追回外部付款；资格将重新核验。"
        command = "确认已付款" if operation in {"pay", "exception"} else "确认修改"
        return body + f"\n两分钟内回复：{command} {confirmation['code']}" + FOOTER

    async def execute(self, event, state, actor, code):
        """Run upstream checks first, then one atomic confirmation transaction.

        Args:
            event: Exact confirmation message.
            state: Original preview and selected ledger.
            actor: Freshly checked financial grant.
            code: User confirmation code.

        Returns:
            Persisted domain acknowledgement.
        """
        service = state["service"]
        confirmation = state["confirmation"]
        if confirmation.get("generation") != state.get("generation", 0):
            raise CashbackError(
                "finance_cancelled", "未提交的结账操作已取消，没有登记付款。", 409
            )
        cancelled = threading.Event()
        guard = self.native_guard(
            event, actor, service, write=True, cancelled=cancelled
        )
        guard()
        payload = confirmation["payload"]
        async with service.lock(actor.group):
            token = WRITE_GUARD.set(guard)
            try:
                members, history = None, None
                operation = payload["operation"]
                if operation == "configure":
                    adapter = service.adapter(service.collector_id)
                    members = await service.roster(adapter, actor.group)
                    history = await asyncio.to_thread(
                        service.historical_members, actor.group
                    )
                elif operation in {"pay", "correct", "assign", "exception"}:
                    rule = await asyncio.to_thread(service.store.get_group, actor.group)
                    members = await service._scan(
                        actor.group,
                        **({} if rule["enabled"] else {"for_payment": True}),
                    )
                guard()

                def commit(snapshot):
                    if snapshot["operation"] == "configure":
                        service.store.configure(
                            actor, snapshot["config"], members, history
                        )
                        return {"status": "saved"}
                    if snapshot["operation"] == "risk_release":
                        return service.store.release_risk(
                            actor, actor.group, snapshot["inviter"], snapshot["reason"]
                        )
                    return service.store.mutate(
                        actor,
                        actor.group,
                        snapshot["operation"],
                        snapshot["items"],
                        confirmation["id"],
                        snapshot["reason"],
                        category=snapshot.get("category"),
                        inviter=snapshot.get("inviter"),
                        members=members,
                        price_confirmation=snapshot.get("price_confirmation"),
                        actual_payment_confirmed=snapshot["operation"] == "exception",
                    )

                task = asyncio.create_task(
                    asyncio.to_thread(
                        service.store.finance_apply,
                        actor,
                        confirmation["id"],
                        code,
                        state["session"],
                        self.boot,
                        commit,
                    )
                )
                try:
                    return await asyncio.shield(task)
                except asyncio.CancelledError:
                    # Hold the group lock until the database thread finishes.
                    cancelled.set()
                    try:
                        await asyncio.shield(task)
                    except CashbackError:
                        pass
                    raise
            finally:
                WRITE_GUARD.reset(token)

    @staticmethod
    def receipt(result):
        """Format a persisted financial receipt without exposing transport peers.

        Args:
            result: Scoped confirmation row, or None.

        Returns:
            A bounded status and reference.
        """
        if not result:
            return "暂无结账操作记录。"
        labels = {
            "completed": "已完成",
            "pending": "未提交，请勿重复付款",
            "cancelled": "已取消，未执行",
        }
        details = json.loads(result["result"]) if result["result"] else {}
        summary = ""
        if details.get("count") is not None:
            summary += f"\n本次{details['count']}笔"
        if details.get("amount") is not None:
            summary += f"，合计¥{details['amount'] / 100:.2f}"
        return (
            f"【结账结果】\n操作编号：{result['id']}\n"
            f"状态：{labels.get(result['status'], '待核对')}" + summary
        )
